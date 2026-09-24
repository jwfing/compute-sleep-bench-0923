"""Bounded, network-free CPU and direct disk I/O pulses for idle experiments."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import mmap
import os
from pathlib import Path
import time


def emit(kind, **fields):
    print(json.dumps(dict(event=kind, utc=datetime.now(timezone.utc).isoformat(),
                          monotonic=time.monotonic(), run_id=os.getenv('BENCH_RUN_ID'),
                          **fields)), flush=True)


def io_counts():
    return {k: int(v) for k, v in (line.split(':') for line in
            Path('/proc/self/io').read_text().splitlines())}


def cpu_pulse(seconds):
    deadline = time.monotonic() + seconds
    iterations = 0
    value = b'compute-activity-benchmark'
    while time.monotonic() < deadline:
        value = hashlib.pbkdf2_hmac('sha256', value, b'fixed-benchmark-salt', 10000)
        iterations += 10000
    return dict(pbkdf2_iterations=iterations, checksum=value.hex())


def disk_pulse(path, size_mib):
    # Fail rather than silently measuring page-cache traffic on unsupported filesystems.
    chunk = 4 * 1024 * 1024
    total = size_mib * 1024 * 1024
    before = io_counts()
    with mmap.mmap(-1, chunk) as buffer:
        buffer[:] = os.urandom(chunk)
        view = memoryview(buffer)
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_RDWR | os.O_DIRECT, 0o600)
        try:
            written = read = 0
            while written < total:
                amount = min(chunk, total - written)
                n = os.write(fd, view[:amount])
                if n != amount:
                    raise RuntimeError('Partial direct write')
                written += n
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            while read < total:
                amount = min(chunk, total - read)
                n = os.readv(fd, [view[:amount]])
                if n != amount:
                    raise RuntimeError('Partial direct read')
                read += n
        finally:
            os.close(fd)
            view.release()
    after = io_counts()
    return dict(mode='O_DIRECT+fsync', path=str(path), file_bytes=total,
                bytes_written=written, bytes_read=read,
                proc_io_delta={k: after[k] - before[k] for k in before})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('case', choices=('cpu', 'disk'))
    parser.add_argument('--seconds', type=int, default=1500)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--pulse-seconds', type=float, default=20)
    parser.add_argument('--size-mib', type=int, default=256)
    args = parser.parse_args()
    started = time.monotonic()
    deadline = started + args.seconds
    path = Path('/app') / ('bench-io-' + str(os.getenv('BENCH_RUN_ID', 'local-test')) + '.bin')
    sequence = 0
    try:
        emit('load_config', case=args.case, duration_seconds=args.seconds,
             interval_seconds=args.interval, cpu_pulse_wall_seconds=args.pulse_seconds,
             disk_size_mib=args.size_mib, cpu_workers=1)
        while time.monotonic() < deadline:
            sequence += 1
            begin = time.monotonic()
            cpu = time.process_time()
            emit('pulse_start', case=args.case, sequence=sequence)
            fields = (cpu_pulse(min(args.pulse_seconds, deadline - begin)) if args.case == 'cpu'
                      else disk_pulse(path, args.size_mib))
            emit('pulse_end', case=args.case, sequence=sequence,
                 wall_seconds=time.monotonic() - begin,
                 cpu_seconds=time.process_time() - cpu, **fields)
            # Fixed one-minute schedule; skip missed slots rather than burst to catch up.
            next_slot = int((time.monotonic() - started) // args.interval) + 1
            time.sleep(max(0, min(deadline, started + next_slot * args.interval) - time.monotonic()))
        emit('load_complete', case=args.case, pulses=sequence, elapsed_seconds=time.monotonic()-started)
    except Exception as exc:
        emit('load_error', case=args.case, error_type=type(exc).__name__, detail=str(exc))
        raise
    finally:
        if args.case == 'disk':
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
