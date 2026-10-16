"""
collectors/run.py — CLI + daemon.

  python -m collectors.run list
  python -m collectors.run <job> [key=value ...]        run one job now
  python -m collectors.run daemon                       the scheduler (one systemd service replaces many cron lines)

The daemon polls job_queue (ops page "Run now") every few seconds and starts due scheduled jobs.
Each job runs in its own subprocess so a crash or a stuck browser can't take the scheduler down.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

from .core import db
from .core.fetcher import Fetcher
from .core.pipeline import JobRun
from . import registry

PY = sys.executable


def run_job(name: str, args: Dict[str, Any], trigger: str = "manual", fetcher: Optional[Fetcher] = None) -> int:
    spec = registry.JOBS[name]
    mod = importlib.import_module(f"collectors.jobs.{spec['module']}")
    merged = {**spec["args"], **args}
    try:
        with JobRun(name, trigger=trigger, args=merged) as jr:
            mod.run(jr, fetcher or Fetcher(), **merged)
        return 0 if jr.status in ("OK", "PARTIAL") else 1
    except Exception:
        return 1
    finally:
        try:
            from .core.fetcher import _pool
            _pool.close()
        except Exception:
            pass


def _parse_kv(items) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for it in items:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k] = int(v) if v.isdigit() else v
    return out


def _last_started(conn, job: str) -> Optional[datetime]:
    r = conn.execute("SELECT started_at FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 1", (job,)).fetchone()
    return datetime.fromisoformat(r["started_at"]) if r else None


def _running(conn, job: str) -> bool:
    r = conn.execute("SELECT id, started_at FROM job_runs WHERE job=? AND status='RUNNING' ORDER BY id DESC LIMIT 1", (job,)).fetchone()
    if not r:
        return False
    age_min = (datetime.now() - datetime.fromisoformat(r["started_at"])).total_seconds() / 60
    if age_min > registry.JOBS[job]["timeout_min"] + 5:
        conn.execute("UPDATE job_runs SET status='FAILED', finished_at=?, summary='orphaned: no finish recorded' WHERE id=?", (db.now_iso(), r["id"]))
        conn.commit()
        return False
    return True


def _spawn(job: str, args: Dict[str, Any], trigger: str) -> subprocess.Popen:
    cmd = [PY, "-m", "collectors.run", job, *[f"{k}={v}" for k, v in args.items()], f"__trigger={trigger}"]
    return subprocess.Popen(cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def daemon(poll: float = 5.0) -> None:
    print("collectors daemon started", flush=True)
    procs: Dict[str, subprocess.Popen] = {}
    while True:
        try:
            now = datetime.now()
            with db.session() as c:
                for j in [j for j, p in procs.items() if p.poll() is not None]:
                    procs.pop(j)
                for q in db.rows(c, "SELECT * FROM job_queue WHERE status='QUEUED' ORDER BY id"):
                    if q["job"] not in registry.JOBS:
                        c.execute("UPDATE job_queue SET status='REJECTED' WHERE id=?", (q["id"],))
                        continue
                    if q["job"] in procs or _running(c, q["job"]):
                        continue
                    procs[q["job"]] = _spawn(q["job"], json.loads(q["args"] or "{}"), f"manual:{q['requested_by']}")
                    c.execute("UPDATE job_queue SET status='STARTED', picked_at=? WHERE id=?", (db.now_iso(), q["id"]))
                for name, spec in registry.JOBS.items():
                    if name in procs or not registry.is_due(spec["schedule"], now, _last_started(c, name)) or _running(c, name):
                        continue
                    procs[name] = _spawn(name, {}, "schedule")
                c.commit()
        except Exception as e:
            print("daemon loop error:", e, flush=True)
        time.sleep(poll)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "list":
        for n, s in registry.JOBS.items():
            print(f"{n:22s} {json.dumps(s['schedule']):70s} {s['about']}")
        return 0
    if cmd == "daemon":
        db.init().close()
        daemon()
        return 0
    if cmd not in registry.JOBS:
        print("unknown job", cmd)
        return 2
    kv = _parse_kv(argv[1:])
    trigger = str(kv.pop("__trigger", "manual"))
    db.init().close()
    return run_job(cmd, kv, trigger)


if __name__ == "__main__":
    sys.exit(main())
