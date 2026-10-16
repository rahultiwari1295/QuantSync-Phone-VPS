"""SQLite-backed job queue: lanes (fast/ai), atomic claim, progress, timeouts, retry, per-user limits."""
from __future__ import annotations

import json
import sqlite3
import time
from statistics import median

from .db import jdump, new_id, tx

LANE = {"scan": "fast", "backtest": "fast", "gated_backtest": "ai", "pattern": "fast", "enhance": "ai", "enhance_exit": "ai"}
TIMEOUT = {"fast": 180, "ai": 600}
MAX_ATTEMPTS = 2


class QueueError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg); self.code, self.msg = code, msg


def enqueue(con: sqlite3.Connection, kind: str, uid: str | None, payload: dict, *, queue_cap=50,
            ai_daily=5, bt_hourly=10) -> str:
    lane = LANE[kind]
    with tx(con):
        queued = con.execute("SELECT count(*) FROM jobs WHERE status IN('queued','running')").fetchone()[0]
        if queued >= queue_cap:
            raise QueueError(503, "The queue is full. Try again in a few minutes.")
        if uid and lane == "ai":
            active = con.execute("SELECT count(*) FROM jobs WHERE uid=? AND lane='ai' AND status IN('queued','running')", (uid,)).fetchone()[0]
            if active:
                raise QueueError(409, "You already have an enhancement running.")
            today = con.execute("SELECT count(*) FROM jobs WHERE uid=? AND lane='ai' AND created_at>?", (uid, time.time() - 86400)).fetchone()[0]
            if today >= ai_daily:
                raise QueueError(429, "Daily enhancement limit reached.")
        if uid and kind == "backtest":
            h = con.execute("SELECT count(*) FROM jobs WHERE uid=? AND kind='backtest' AND created_at>?", (uid, time.time() - 3600)).fetchone()[0]
            if h >= bt_hourly:
                raise QueueError(429, "Backtest limit reached for this hour.")
        jid = new_id("job")
        con.execute("INSERT INTO jobs(id,kind,lane,uid,payload_json,timeout_s,created_at) VALUES(?,?,?,?,?,?,?)",
                    (jid, kind, lane, uid, jdump(payload), TIMEOUT[lane], time.time()))
    return jid


def claim(con: sqlite3.Connection, lane: str) -> sqlite3.Row | None:
    return con.execute(
        """UPDATE jobs SET status='running', started_at=?, attempts=attempts+1, stage='starting'
           WHERE id=(SELECT id FROM jobs WHERE lane=? AND status='queued' ORDER BY created_at LIMIT 1)
           RETURNING *""", (time.time(), lane)).fetchone()


def progress(con, jid: str, pct: int, stage: str) -> None:
    con.execute("UPDATE jobs SET progress=?, stage=? WHERE id=? AND status='running'", (pct, stage, jid))


def finish(con, jid: str, result: dict) -> None:
    con.execute("UPDATE jobs SET status='done', progress=100, stage='done', result_json=?, finished_at=? WHERE id=?",
                (jdump(result), time.time(), jid))


def fail(con, jid: str, err: str) -> None:
    row = con.execute("SELECT attempts FROM jobs WHERE id=?", (jid,)).fetchone()
    if row and row["attempts"] < MAX_ATTEMPTS:
        con.execute("UPDATE jobs SET status='queued', error=?, stage='retrying' WHERE id=?", (err[:500], jid))
    else:
        con.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?", (err[:500], time.time(), jid))


def recover_stuck(con) -> int:
    """Running jobs past their timeout (worker died) go back to the queue or fail."""
    rows = con.execute("SELECT id FROM jobs WHERE status='running' AND started_at + timeout_s < ?", (time.time(),)).fetchall()
    for r in rows:
        fail(con, r["id"], "timed out")
    return len(rows)


def status(con, jid: str, uid: str | None = None) -> dict | None:
    r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r or (uid and r["uid"] and r["uid"] != uid):
        return None
    out = {"id": r["id"], "kind": r["kind"], "status": r["status"], "progress": r["progress"], "stage": r["stage"],
           "error": r["error"], "result": json.loads(r["result_json"]) if r["result_json"] else None}
    if r["status"] == "queued":
        ahead = con.execute("SELECT count(*) FROM jobs WHERE lane=? AND status IN('queued','running') AND created_at<?", (r["lane"], r["created_at"])).fetchone()[0]
        d = [x[0] for x in con.execute("SELECT finished_at-started_at FROM jobs WHERE lane=? AND status='done' ORDER BY finished_at DESC LIMIT 50", (r["lane"],))]
        out["position"] = ahead + 1
        out["eta_s"] = int((median(d) if d else 45) * (ahead + 1))
    return out
