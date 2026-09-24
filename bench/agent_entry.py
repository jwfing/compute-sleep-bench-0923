"""Run Hermes with observable, optionally paced, real Tavily requests."""
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone


def emit(kind, **fields):
    print(json.dumps(dict(event=kind, utc=datetime.now(timezone.utc).isoformat(),
        monotonic=time.monotonic(), run_id=os.environ.get('BENCH_RUN_ID'), **fields)), flush=True)


class ObservedTavily:
    def __init__(self, request, interval=0, clock=time.monotonic, sleep=time.sleep, report=emit):
        self.request, self.interval = request, interval
        self.clock, self.sleep, self.report = clock, sleep, report
        self.last_search = None
        self.count = 0
        self.lock = threading.Lock()

    def __call__(self, endpoint, payload):
        # Serialize all Tavily requests; search start times are >= interval apart.
        with self.lock:
            self.count += 1
            sequence = self.count
            if sequence > 80:
                raise RuntimeError('Experiment Tavily request cap reached')
            if endpoint == 'search' and self.last_search is not None:
                delay = max(0, self.last_search + self.interval - self.clock())
                if delay:
                    self.report('search_pacing_wait', seconds=delay, sequence=sequence)
                    self.sleep(delay)
            if endpoint == 'search':
                self.last_search = self.clock()
            # Prevent accidental advanced-search auto-selection and paid fallback.
            if endpoint == 'search':
                payload = dict(payload, search_depth='basic', auto_parameters=False)
            start = self.clock()
            self.report('tavily_start', endpoint=endpoint, sequence=sequence,
                query_sha256=hashlib.sha256(payload.get('query', '').encode()).hexdigest())
            try:
                result = self.request(endpoint, payload)
                self.report('tavily_end', endpoint=endpoint, sequence=sequence,
                            seconds=self.clock()-start, result_count=len(result.get('results', [])))
                return result
            except Exception as error:
                self.report('tavily_error', endpoint=endpoint, sequence=sequence,
                            error_type=type(error).__name__)
                raise


if __name__ == '__main__':
    from tools import web_tools
    web_tools._tavily_request = ObservedTavily(web_tools._tavily_request,
        interval=float(os.environ.get('BENCH_SEARCH_INTERVAL', '0')))
    from hermes_cli.main import main
    main()
