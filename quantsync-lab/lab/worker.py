"""Queue workers. `python -m lab.worker fast|ai`. The AI lane yields to cooks and to the live engine."""
from __future__ import annotations

import fcntl
import os
import subprocess
from datetime import datetime, timedelta
import logging
import signal
import sys
import time

from . import db, jobs, service
from .config import Config, load_config

log = logging.getLogger("qslab.worker")


def _is_market_hours_ist() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 0)) and now.hour < 16


def _eod_pipeline_running() -> bool:
    lock_file = "/tmp/quantsync_eod_pipeline.lock"
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
        except (BlockingIOError, OSError):
            return True
    try:
        res = subprocess.run(["pgrep", "-f", "eod_pipeline_orchestrator"], capture_output=True)
        if res.returncode == 0:
            return True
    except Exception:
        pass
    return False


def _cook_running(cfg: Config) -> bool:
    if (cfg.run_dir / "cook.lock").exists():
        return True
    try:
        res = subprocess.run(["pgrep", "-f", "lab.cli cook"], capture_output=True)
        if res.returncode == 0:
            return True
    except Exception:
        pass
    return False


def busy(cfg: Config, lane: str) -> bool:
    if lane != "ai":
        return False
    if (cfg.run_dir / "live-busy").exists():
        return True
    if _is_market_hours_ist():
        return True
    if _cook_running(cfg) or _eod_pipeline_running():
        return True
    return False
def run_one(cfg: Config, con, lane: str) -> bool:
    jobs.recover_stuck(con)
    if busy(cfg, lane):
        return False
    job = jobs.claim(con, lane)
    if not job:
        return False
    try:
        result = service.handle(cfg, con, job)
        jobs.finish(con, job["id"], result)
    except Exception as e:  # noqa: BLE001 - a job must never kill the worker
        log.exception("job %s failed", job["id"])
        jobs.fail(con, job["id"], f"{type(e).__name__}: {e}")
        if job["kind"] == "enhance":
            _mark_enhancement(con, job, str(e))
    return True


def _mark_enhancement(con, job, err):
    import json
    p = json.loads(job["payload_json"])
    after = con.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
    if after == "failed" and p.get("enhancement_id"):
        con.execute("UPDATE ai_enhancements SET status='failed', reason=?, finished_at=? WHERE id=?", (err[:200], time.time(), p["enhancement_id"]))


def main(lane: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config(); db.migrate(cfg)
    con = db.connect(cfg)
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(v=True))
    while not stop["v"]:
        if not run_one(cfg, con, lane):
            time.sleep(2)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fast")
