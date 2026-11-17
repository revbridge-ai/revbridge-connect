"""Scheduler for RevBridge Connect's deliverability jobs.

Reads [deliverability.jobs.*] from connect.toml and runs each script in
services/infra/scripts on its schedule, with the same guarantees as a
Kubernetes CronJob:
  - concurrencyPolicy: Forbid  -> never two runs of the same job at the same time
  - activeDeadlineSeconds      -> the job's `timeout`; when it is exceeded, the process is terminated
5-field cron (minute hour day month day-of-week) with *, */n, a-b, a,b,c.

Usage: python scripts/scheduler.py [--once <job>] [--list]
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import time
import tomllib

ROOT = os.environ.get("CONNECT_ROOT", "/workspace")
MANIFEST = os.environ.get("CONNECT_MANIFEST", os.path.join(ROOT, "connect.toml"))
SCRIPTS = os.path.join(ROOT, "services", "infra", "scripts")
RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]


def parse_field(field: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        out.update(range(a, b + 1, step))
    return out


def parse_cron(expr: str) -> list[set[int]]:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"invalid cron: {expr!r}")
    return [parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, RANGES)]


def matches(spec: list[set[int]], t: dt.datetime) -> bool:
    dow = (t.weekday() + 1) % 7  # cron: 0 = Sunday
    return (t.minute in spec[0] and t.hour in spec[1] and t.day in spec[2]
            and t.month in spec[3] and dow in spec[4])


def load_jobs() -> dict[str, dict]:
    with open(MANIFEST, "rb") as fh:
        manifest = tomllib.load(fh)
    jobs = manifest.get("deliverability", {}).get("jobs", {})
    for name, job in jobs.items():
        job["spec"] = parse_cron(job["schedule"])
        job.setdefault("timeout", 300)
    return jobs


def log(msg: str) -> None:
    print(f"{dt.datetime.now(dt.UTC).isoformat(timespec='seconds')} [scheduler] {msg}", flush=True)


LAST_RUNS: dict[str, dict] = {}


def publish_job_metrics() -> None:
    """Metrics of each job: last run, duration and exit code.

    They go to Redis (mail_infra.metrics_sink) and the API exposes them on GET /metrics (Prometheus).
    A failure here never takes the scheduler down.
    """
    try:
        from mail_infra.metrics_sink import publish, sink_is_redis

        if not sink_is_redis():
            return
        series = []
        for job, r in LAST_RUNS.items():
            tags = [f"job_name:{job}"]
            series.append({"metric": "job.last_run_timestamp_seconds", "points": [[0, r["end"]]], "type": "gauge", "tags": tags})
            series.append({"metric": "job.duration_seconds", "points": [[0, r["duration"]]], "type": "gauge", "tags": tags})
            series.append({"metric": "job.exit_code", "points": [[0, r["rc"]]], "type": "gauge", "tags": tags})
            series.append({"metric": "job.runs_total", "points": [[0, r["runs"]]], "type": "gauge", "tags": tags})
            series.append({"metric": "job.failures_total", "points": [[0, r["failures"]]], "type": "gauge", "tags": tags})
        publish("scheduler", series, ttl_sec=7 * 24 * 3600)
    except Exception as e:  # noqa: BLE001
        log(f"scheduler metrics not published: {e}")


def record_run(name: str, rc: int, duration: float) -> None:
    r = LAST_RUNS.setdefault(name, {"runs": 0, "failures": 0})
    r.update(end=time.time(), duration=round(duration, 1), rc=rc, runs=r["runs"] + 1,
             failures=r["failures"] + (1 if rc != 0 else 0))
    publish_job_metrics()


def start(name: str, job: dict) -> subprocess.Popen:
    path = os.path.join(SCRIPTS, job["script"])
    log(f"start job={name} script={job['script']}")
    return subprocess.Popen([sys.executable, path], cwd=SCRIPTS, start_new_session=True)


def main() -> int:
    jobs = load_jobs()
    if "--list" in sys.argv:
        for name, job in sorted(jobs.items()):
            state = "on " if job.get("enabled") else "off"
            print(f"{state} {name:22s} {job['schedule']:16s} {job['script']}  {job.get('needs', '')}")
        return 0
    if "--once" in sys.argv:
        name = sys.argv[sys.argv.index("--once") + 1]
        proc = start(name, jobs[name])
        return proc.wait(timeout=jobs[name]["timeout"])

    active = {n: j for n, j in jobs.items() if j.get("enabled")}
    log(f"active jobs: {', '.join(sorted(active)) or 'none'}")
    running: dict[str, tuple[subprocess.Popen, float]] = {}
    stop = False

    def _term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    last_minute = None
    while not stop:
        now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        # reap finished processes and enforce the timeout (activeDeadlineSeconds)
        for name, (proc, started) in list(running.items()):
            rc = proc.poll()
            if rc is not None:
                dur = time.monotonic() - started
                log(f"end job={name} rc={rc} dur={dur:.0f}s")
                del running[name]
                record_run(name, rc, dur)
            elif time.monotonic() - started > active[name]["timeout"]:
                log(f"timeout job={name} after {active[name]['timeout']}s; terminating")
                os.killpg(proc.pid, signal.SIGTERM)
        if now != last_minute:
            last_minute = now
            for name, job in active.items():
                if matches(job["spec"], now):
                    if name in running:
                        log(f"skip job={name}: previous run still running (Forbid)")
                        continue
                    running[name] = (start(name, job), time.monotonic())
        time.sleep(5)
    for name, (proc, _) in running.items():
        os.killpg(proc.pid, signal.SIGTERM)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
