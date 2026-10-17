"""Read-only HTTP API over intel.db, mounted on the main server at /api/intel2.

Fast by construction: indexed queries, a read-only connection per request, small JSON, and a short in-memory cache
for the heavy screens (briefs, feed). Writes only happen in the background jobs (intel.run)."""
from __future__ import annotations

import json
import os
import threading
import time

from fastapi import APIRouter, HTTPException, Query, Response

from . import db
from .classify import LABEL

router = APIRouter(prefix="/api/intel2", tags=["intel"])
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()


def _cached(key: str, ttl: float, fn):
    t = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and t - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (t, val)
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
    return val


def _ago(hours: float) -> str:
    """Cut-off in the same clock the events use (IST, naive ISO)."""
    from datetime import timedelta
    return (db.now_ist().replace(tzinfo=None) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _con():
    if not os.path.exists(db.INTEL_DB):
        db.migrate()
    return db.connect(ro=True)


def _ev(r) -> dict:
    d = dict(r)
    for k in ("numbers_json", "ai_json"):
        v = d.pop(k, None)
        if v:
            try:
                d[k.replace("_json", "")] = json.loads(v)
            except ValueError:
                pass
    d.pop("created_at", None)
    d["label"] = LABEL.get(d.get("type"), (d.get("type") or "").replace("_", " ").title())
    return d


def _q(sql: str, args=()) -> list[dict]:
    con = _con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


@router.get("/events")
def events(response: Response, symbol: str | None = None, type: str | None = None, category: str | None = None,
           min_impact: int = 0, days: int = Query(3, ge=1, le=365), limit: int = Query(60, ge=1, le=300),
           before: str | None = None, q: str | None = None):
    """The live feed. Filter by stock, type, category, impact; page with `before` (an event ts)."""
    response.headers["Cache-Control"] = "public, max-age=30"
    w, a = ["ts >= ?", "impact >= ?"], [_ago(hours=24 * days), min_impact]
    if symbol:
        w.append("symbol = ?"); a.append(symbol.upper())
    if type:
        types = type.split(","); w.append(f"type IN ({','.join('?' * len(types))})"); a += types
    if category:
        cats = category.split(","); w.append(f"category IN ({','.join('?' * len(cats))})"); a += cats
    if before:
        w.append("ts < ?"); a.append(before)
    if q:
        w.append("(headline LIKE ? OR symbol LIKE ? OR company LIKE ?)"); a += [f"%{q}%"] * 3
    key = f"ev:{w}:{a}:{limit}"
    con_rows = _cached(key, 20, lambda: _rows_ev(f"SELECT * FROM events WHERE {' AND '.join(w)} ORDER BY ts DESC LIMIT ?", (*a, limit)))
    return {"items": con_rows, "next": con_rows[-1]["ts"] if len(con_rows) == limit else None}


def _rows_ev(sql, args):
    con = _con()
    try:
        return [_ev(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


@router.get("/events/{eid}")
def event(eid: str):
    r = _rows_ev("SELECT * FROM events WHERE id=?", (eid,))
    if not r:
        raise HTTPException(404, "Not found")
    return r[0]


@router.get("/top")
def top(response: Response, hours: int = Query(24, ge=1, le=240), limit: int = Query(20, ge=1, le=100)):
    response.headers["Cache-Control"] = "public, max-age=60"
    return _cached(f"top:{hours}:{limit}", 60, lambda: _rows_ev(
        "SELECT * FROM events WHERE ts >= ? AND symbol IS NOT NULL ORDER BY impact DESC, ts DESC LIMIT ?", (_ago(hours=hours), limit)))


@router.get("/stock/{symbol}")
def stock(symbol: str, response: Response):
    """Everything about one company in one call: timeline, insiders, deals, meetings, pledge, ownership, facts."""
    response.headers["Cache-Control"] = "public, max-age=60"
    s = symbol.upper()

    def build():
        con = _con()
        try:
            one = lambda sql: (dict(r) if (r := con.execute(sql, (s,)).fetchone()) else None)
            many = lambda sql, n=50: [dict(r) for r in con.execute(sql + f" LIMIT {n}", (s,)).fetchall()]
            return {"symbol": s,
                    "timeline": [_ev(r) for r in con.execute("SELECT * FROM events WHERE symbol=? ORDER BY ts DESC LIMIT 150", (s,))],
                    "insiders": many("SELECT * FROM insider_trades WHERE symbol=? ORDER BY known_at DESC"),
                    "deals": many("SELECT * FROM deals WHERE symbol=? ORDER BY deal_date DESC"),
                    "meetings": many("SELECT * FROM meetings WHERE symbol=? ORDER BY meeting_date DESC", 20),
                    "pledge": many("SELECT * FROM pledges WHERE symbol=? ORDER BY known_at DESC", 12),
                    "owners": many("SELECT * FROM facts_owner WHERE symbol=? ORDER BY quarter_end DESC", 12),
                    "facts": one("SELECT * FROM facts_daily WHERE symbol=? ORDER BY known_at DESC LIMIT 1")}
        finally:
            con.close()
    return _cached(f"stock:{s}", 60, build)


@router.get("/brief")
def brief(response: Response, kind: str = "auto"):
    """Latest brief. kind=auto picks evening after 17:00 IST, else morning."""
    response.headers["Cache-Control"] = "public, max-age=120"
    if kind == "auto":
        kind = "evening" if db.now_ist().hour >= 17 else "morning"
    rows = _q("SELECT kind, day, created_at, body_json FROM briefs WHERE kind=? ORDER BY day DESC LIMIT 1", (kind,)) or \
        _q("SELECT kind, day, created_at, body_json FROM briefs ORDER BY created_at DESC LIMIT 1")
    if not rows:
        return {"kind": kind, "empty": True}
    b = json.loads(rows[0]["body_json"])
    b["created_at"] = rows[0]["created_at"]
    return b


@router.get("/deals")
def deals(response: Response, days: int = Query(5, ge=1, le=90), side: str | None = None, limit: int = Query(100, le=500)):
    response.headers["Cache-Control"] = "public, max-age=300"
    w, a = ["deal_date >= date('now', ?)"], [f"-{days} day"]
    if side in ("Buy", "Sell"):
        w.append("side=?"); a.append(side)
    return {"items": _q(f"SELECT * FROM deals WHERE {' AND '.join(w)} ORDER BY deal_date DESC, value_cr DESC LIMIT ?", (*a, limit))}


@router.get("/insiders")
def insiders(response: Response, days: int = Query(30, ge=1, le=365), side: str | None = None, min_cr: float = 0, limit: int = Query(100, le=500)):
    response.headers["Cache-Control"] = "public, max-age=300"
    w, a = ["known_at >= date('now', ?)", "COALESCE(value_cr,0) >= ?"], [f"-{days} day", min_cr]
    if side in ("Buy", "Sell"):
        w.append("side=?"); a.append(side)
    return {"items": _q(f"SELECT * FROM insider_trades WHERE {' AND '.join(w)} ORDER BY known_at DESC, value_cr DESC LIMIT ?", (*a, limit))}


@router.get("/calendar")
def calendar(response: Response, days: int = Query(14, ge=1, le=90)):
    response.headers["Cache-Control"] = "public, max-age=600"
    return {"items": _q("SELECT * FROM meetings WHERE meeting_date BETWEEN date('now') AND date('now', ?) ORDER BY meeting_date, is_results DESC, symbol",
                        (f"+{days} day",))}


@router.get("/markets")
def markets(response: Response):
    response.headers["Cache-Control"] = "public, max-age=120"
    return {"items": _q("SELECT * FROM markets ORDER BY region, name"), "flows": _q("SELECT * FROM flows ORDER BY day DESC LIMIT 10")}


@router.get("/status")
def status():
    """Health of every source and job: what ran, when, and what failed."""
    return {"sources": _q("SELECT * FROM source_state ORDER BY source"),
            "runs": _q("SELECT job, started, finished, ok, added, note FROM runs ORDER BY id DESC LIMIT 60"),
            "counts": _q("SELECT category, count(*) n, max(ts) last FROM events GROUP BY category"),
            "reader": _q("SELECT ai_status, count(*) n FROM events GROUP BY ai_status")}


@router.get("/types")
def types():
    return {"types": [{"type": k, "label": v} for k, v in LABEL.items()]}
