"""One submission per boot; no background networking except the chosen workload."""
import hmac
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOOT = str(uuid.uuid4())
LOCK = threading.Lock()
OUTPUT_LOCK = threading.Lock()
RUN = None
CHILD = None
ROOT = Path(__file__).resolve().parents[1]
EVENTS = Path(os.environ.get("EVENTS_PATH", "/tmp/bench-events.jsonl"))
TOKEN = os.environ.get("BENCH_TOKEN", "")


def event(kind, **fields):
    row = dict(utc=datetime.now(timezone.utc).isoformat(), monotonic=time.monotonic(),
               boot_id=BOOT, run_id=RUN, event=kind, **fields)
    line = json.dumps(row, ensure_ascii=False)
    with OUTPUT_LOCK:
        print(line, flush=True)
        with EVENTS.open("a") as f:
            f.write(line + "\n")


def terminate_child():
    if CHILD and CHILD.poll() is None:
        try:
            os.killpg(CHILD.pid, signal.SIGTERM)
            CHILD.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(CHILD.pid, signal.SIGKILL)
            CHILD.wait()
        except ProcessLookupError:
            pass


def workload(case, seconds, interval):
    global CHILD
    started = time.monotonic()
    event("workload_start", case=case, duration_limit_seconds=seconds)
    try:
        if case == "idle":
            time.sleep(seconds)
        elif case == "outbound":
            deadline = started + seconds
            seq = 0
            while time.monotonic() < deadline:
                seq += 1
                event("outbound_start", sequence=seq)
                request = urllib.request.Request(os.environ["OUTBOUND_URL"],
                    headers={"X-Bench-Run": RUN, "X-Bench-Sequence": str(seq)})
                try:
                    with urllib.request.urlopen(request, timeout=15) as response:
                        response.read(4096)
                        event("outbound_end", sequence=seq, status=response.status)
                except Exception as exc:
                    event("outbound_error", sequence=seq, error_type=type(exc).__name__)
                time.sleep(max(0, min(interval, deadline - time.monotonic())))
        else:
            prompt_name = "smoke.md" if case == "smoke" else ("research-paced.md" if case == "hermes-paced" else "research.md")
            args = ["python", str(ROOT / "bench/agent_entry.py"), "chat", "--provider", os.environ["HERMES_PROVIDER"],
                    "--model", os.environ["HERMES_MODEL"], "--toolsets", "web,file",
                    "--max-turns", "3" if case == "smoke" else "90", "--quiet", "-q",
                    (ROOT / "prompts" / prompt_name).read_text()]
            child_env = dict(os.environ, BENCH_RUN_ID=RUN,
                             BENCH_SEARCH_INTERVAL="75" if case == "hermes-paced" else "0")
            CHILD = subprocess.Popen(args, stdin=subprocess.DEVNULL, start_new_session=True, env=child_env)
            event("agent_spawned", pid=CHILD.pid)
            try:
                code = CHILD.wait(timeout=seconds)
                event("agent_exit", returncode=code,
                      duration_seconds=time.monotonic() - started,
                      qualification="requires_tool_trace_review")
            except subprocess.TimeoutExpired:
                event("agent_timeout", classification="controller_limit_not_platform_sleep")
                terminate_child()
    except Exception as exc:
        event("workload_error", error_type=type(exc).__name__)
    finally:
        event("workload_end", elapsed_seconds=time.monotonic() - started)
        # Keep listener alive, without heartbeat, for post-work idle observation.


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, code, data):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        event("inbound", method="GET")
        self.reply(200 if self.path == "/health" else 404, {"boot_id": BOOT})

    def do_POST(self):
        global RUN
        event("inbound", method="POST")
        if self.path != "/run":
            return self.reply(404, {})
        if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN):
            return self.reply(401, {})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            case = data["case"]
            seconds = int(data.get("seconds", 2700 if case == "hermes" else 1500))
            interval = int(data.get("interval", 30))
            if case not in {"idle", "outbound", "hermes", "hermes-paced", "smoke"} or not 1 <= seconds <= 3600 or not 1 <= interval <= 300:
                raise ValueError()
            if case == "outbound" and not os.environ.get("OUTBOUND_URL", "").startswith("https://"):
                return self.reply(422, {"error": "Set OUTBOUND_URL to your HTTPS receiver"})
            if case in {"hermes", "hermes-paced", "smoke"} and not all(os.environ.get(k) for k in ("HERMES_PROVIDER", "HERMES_MODEL")):
                return self.reply(422, {"error": "Set HERMES_PROVIDER and HERMES_MODEL"})
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": "Invalid request"})
        with LOCK:
            if RUN is not None:
                return self.reply(409, {"error": "One run per boot", "run_id": RUN})
            RUN = str(uuid.uuid4())
        self.reply(202, {"run_id": RUN, "boot_id": BOOT, "case": case})
        event("submission_response_sent", case=case)
        threading.Thread(target=workload, args=(case, seconds, interval), daemon=True).start()


def shutdown(signum, frame):
    event("signal", signal=signum)
    terminate_child()
    raise SystemExit(128 + signum)


def network_samples():
    """Read local kernel counters only; never send network heartbeats."""
    while True:
        try:
            tx = rx = 0
            for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
                interface, values = line.split(':', 1)
                if interface.strip() == 'lo':
                    continue
                values = values.split()
                rx += int(values[0]); tx += int(values[8])
            event('network_sample', tx_bytes=tx, rx_bytes=rx,
                  agent_running=CHILD is not None and CHILD.poll() is None)
        except OSError:
            return
        time.sleep(15)


if __name__ == "__main__":
    if len(TOKEN) < 24:
        raise SystemExit("BENCH_TOKEN must contain at least 24 characters")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)
    event("boot", hermes_commit=os.environ.get("HERMES_COMMIT", "unavailable"))
    threading.Thread(target=network_samples, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
