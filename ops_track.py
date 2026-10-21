"""
ops_track.py — records every scheduler job run (start, end, ok, error) so the private /rahul page can show what ran.

Used by scheduler.py:  schedule.every().day.at("15:15").do(track("job_decision_1515", sched.job_decision_1515))
The wrapper never changes what a job does: the job's return value and exceptions pass straight through, and any
problem while recording is swallowed (a broken ops database must never stop a trading job).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Callable, Optional

_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("QS_DATA_DIR", os.path.join(_ROOT, "Data"))
RUNS_DB = os.environ.get("QS_OPS_RUNS_DB", os.path.join(DATA_DIR, "ops_runs.db"))
KEEP_DAYS = 14

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'scheduler',   -- scheduler | manual
  started REAL NOT NULL, finished REAL, ok INTEGER, note TEXT, pid INTEGER, who TEXT);
CREATE INDEX IF NOT EXISTS ix_runs_job ON runs(job, started);
"""
_state = {"pruned": False}


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or RUNS_DB
    os.makedirs(os.path.dirname(p), exist_ok=True)
    con = sqlite3.connect(p, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    con.executescript(_SCHEMA)
    return con


def start(job: str, source: str = "scheduler", pid: Optional[int] = None, who: Optional[str] = None,
          path: Optional[str] = None) -> Optional[int]:
    try:
        con = connect(path)
        try:
            if not _state["pruned"]:
                _state["pruned"] = True
                con.execute("DELETE FROM runs WHERE started < ?", (time.time() - KEEP_DAYS * 86400,))
            cur = con.execute("INSERT INTO runs(job,source,started,pid,who) VALUES (?,?,?,?,?)", (job, source, time.time(), pid, who))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - recording must never break a job
        return None


def finish(run_id: Optional[int], ok: bool, note: str = "", path: Optional[str] = None) -> None:
    if run_id is None:
        return
    try:
        con = connect(path)
        try:
            con.execute("UPDATE runs SET finished=?, ok=?, note=? WHERE id=?", (time.time(), 1 if ok else 0, (note or "")[:400], run_id))
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass


def _summ(result: Any) -> tuple[bool, str]:
    """A job that returns {"status": "error"/"failed"} counts as failed; anything else that returned normally is ok."""
    if isinstance(result, dict):
        st = str(result.get("status", "")).lower()
        bad = st in ("error", "failed", "fail")
        try:
            note = json.dumps(result, default=str)[:300]
        except Exception:  # noqa: BLE001
            note = str(result)[:300]
        return (not bad), note
    return True, ""


def track(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(*a: Any, **k: Any) -> Any:
        rid = start(name)
        try:
            out = fn(*a, **k)
        except BaseException as e:  # noqa: BLE001 - re-raised below, unchanged
            finish(rid, False, f"{type(e).__name__}: {e}")
            raise
        ok, note = _summ(out)
        finish(rid, ok, note)
        return out
    wrapper.__name__ = getattr(fn, "__name__", name)
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper
