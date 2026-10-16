"""
collectors/admin/api.py — /api/admin/* for the private ops page, plus /api/relay/* for the home-network relay.
Every /api/admin route requires a verified admin Firebase token (see auth.py); every action is audited.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import registry
from ..core import db, pipeline
from ..core import fetcher as F
from .auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])
relay_router = APIRouter(prefix="/api/relay", tags=["relay"])

SYMBOL_RE = re.compile(r"^[A-Z0-9&_\-]{1,20}$")
DATASETS = ("listing", "results", "shareholding", "ratios", "news")
STATES = ("FRESH", "STALE", "MISSING", "DEAD")
REFRESH_JOB = {"results": ("results_daily", {"mode": "daily"}), "listing": ("listing", {}), "news": ("news", {}),
               "shareholding": ("shareholding", {}), "ratios": ("ratios", {})}


def _q(sql: str, args=()) -> List[Dict[str, Any]]:
    with db.session() as c:
        return db.rows(c, sql, args)


def _one(sql: str, args=()) -> Optional[Dict[str, Any]]:
    r = _q(sql, args)
    return r[0] if r else None


def _last_started(job: str) -> Optional[datetime]:
    r = _one("SELECT started_at FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 1", (job,))
    return datetime.fromisoformat(r["started_at"]) if r else None


@router.get("/me")
def me(adm=Depends(require_admin)):
    return {"email": adm["email"], "ok": True}


@router.get("/overview")
def overview(adm=Depends(require_admin)):
    cov = coverage(adm)
    runs = _q("SELECT job, status, started_at, finished_at, summary FROM job_runs WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job) ORDER BY started_at DESC")
    alerts: List[Dict[str, str]] = []
    for r in runs:
        if r["status"] == "FAILED":
            alerts.append({"level": "bad", "text": f"{r['job']} failed at {r['started_at']}"})
    for s in _q("SELECT route, paused_until, forced_off, last_error FROM source_health"):
        if s["forced_off"]:
            alerts.append({"level": "warn", "text": f"route {s['route']} switched off by you"})
        elif (s["paused_until"] or 0) > time.time():
            alerts.append({"level": "warn", "text": f"route {s['route']} paused after repeated failures ({s['last_error'] or ''})"})
    for ds, v in cov["datasets"].items():
        bad = v.get("STALE", 0) + v.get("MISSING", 0) + v.get("DEAD", 0)
        if v["total"] and bad / v["total"] > 0.25:
            alerts.append({"level": "warn", "text": f"{ds}: {bad} of {v['total']} stocks stale or missing"})
    pend = _one("SELECT COUNT(*) n FROM review_queue WHERE status='PENDING'")["n"]
    if pend:
        alerts.append({"level": "info", "text": f"{pend} items waiting for your review"})
    ds_path = os.path.join(db.DATA_DIR, "data_status.json")
    quality = None
    try:
        quality = json.load(open(ds_path))
    except (OSError, ValueError):
        pass
    return {"coverage": cov["datasets"], "lastRuns": runs, "alerts": alerts, "dataQuality": quality, "pendingReview": pend,
            "rejectedToday": _one("SELECT COUNT(*) n FROM validation_log WHERE ts >= ?", (date.today().isoformat(),))["n"],
            "now": db.now_iso()}


@router.get("/coverage")
def coverage(adm=Depends(require_admin)):
    out: Dict[str, Dict[str, int]] = {}
    for r in _q("SELECT dataset, state, COUNT(*) n FROM dataset_status GROUP BY dataset, state"):
        out.setdefault(r["dataset"], {s: 0 for s in STATES})[r["state"]] = r["n"]
    for v in out.values():
        v["total"] = sum(v.get(s, 0) for s in STATES)
    return {"datasets": out}


@router.get("/coverage/{dataset}")
def coverage_detail(dataset: str, state: str = "", q: str = "", limit: int = 300, adm=Depends(require_admin)):
    if dataset not in DATASETS:
        raise HTTPException(404, "unknown dataset")
    sql, args = "SELECT symbol, state, as_of, checked_at, note FROM dataset_status WHERE dataset=?", [dataset]
    if state:
        sql += " AND state=?"; args.append(state.upper())
    if q:
        sql += " AND symbol LIKE ?"; args.append(q.upper() + "%")
    sql += " ORDER BY symbol LIMIT ?"; args.append(max(1, min(limit, 1000)))
    return {"dataset": dataset, "rows": _q(sql, args)}


@router.get("/sources")
def sources(adm=Depends(require_admin)):
    health = _q("SELECT * FROM source_health ORDER BY route")
    today = {r["route"]: r for r in _q("SELECT route, ok, fail FROM source_daily WHERE day=?", (date.today().isoformat(),))}
    now = time.time()
    for h in health:
        t = today.get(h["route"], {"ok": 0, "fail": 0})
        h["today_ok"], h["today_fail"] = t["ok"], t["fail"]
        h["state"] = "off" if h["forced_off"] else ("paused" if (h["paused_until"] or 0) > now else "active")
    return {"routes": health}


@router.get("/jobs")
def jobs(adm=Depends(require_admin)):
    now = datetime.now()
    out = []
    for name, spec in registry.JOBS.items():
        last = _one("SELECT * FROM job_runs WHERE job=? ORDER BY id DESC LIMIT 1", (name,))
        if last:
            last.pop("log_tail", None)
        out.append({"name": name, "label": spec["label"], "about": spec["about"], "schedule": spec["schedule"],
                    "next": registry.next_run(spec["schedule"], now, _last_started(name)), "last": last})
    queued = _q("SELECT * FROM job_queue WHERE status IN ('QUEUED','STARTED') ORDER BY id DESC LIMIT 20")
    return {"jobs": out, "queue": queued}


@router.get("/jobs/{name}/runs")
def job_runs(name: str, limit: int = 10, adm=Depends(require_admin)):
    if name not in registry.JOBS:
        raise HTTPException(404, "unknown job")
    return {"runs": _q("SELECT * FROM job_runs WHERE job=? ORDER BY id DESC LIMIT ?", (name, max(1, min(limit, 50))))}


@router.post("/jobs/{name}/run")
def run_job(name: str, request: Request, args: Dict[str, Any] = Body(default={}), adm=Depends(require_admin)):
    if name not in registry.JOBS:
        raise HTTPException(404, "unknown job")
    clean = {k: v for k, v in (args or {}).items() if k in ("symbols", "limit", "mode", "days_back", "from_date") and isinstance(v, (str, int))}
    with db.session() as c:
        dup = c.execute("SELECT 1 FROM job_queue WHERE job=? AND status IN ('QUEUED','STARTED')", (name,)).fetchone()
        if dup:
            return {"ok": True, "queued": False, "note": "already queued or running"}
        c.execute("INSERT INTO job_queue(job,args,requested_at,requested_by) VALUES (?,?,?,?)", (name, json.dumps(clean), db.now_iso(), adm["email"]))
        db.audit(c, adm["email"], "run_job", {"job": name, "args": clean}, adm.get("_ip", ""))
    return {"ok": True, "queued": True}


@router.post("/refresh/{dataset}/{symbol}")
def refresh(dataset: str, symbol: str, adm=Depends(require_admin)):
    symbol = symbol.upper()
    if dataset not in REFRESH_JOB or not SYMBOL_RE.match(symbol):
        raise HTTPException(400, "bad dataset or symbol")
    job, base = REFRESH_JOB[dataset]
    args = dict(base, symbols=symbol) if dataset == "results" else dict(base)
    with db.session() as c:
        c.execute("INSERT INTO job_queue(job,args,requested_at,requested_by) VALUES (?,?,?,?)", (job, json.dumps(args), db.now_iso(), adm["email"]))
        db.audit(c, adm["email"], "refresh", {"dataset": dataset, "symbol": symbol}, adm.get("_ip", ""))
    return {"ok": True, "job": job, "args": args}


@router.post("/sources/{route}/{action}")
def source_action(route: str, action: str, adm=Depends(require_admin)):
    if action not in ("pause", "resume", "off", "on"):
        raise HTTPException(400, "bad action")
    with db.session() as c:
        c.execute("INSERT OR IGNORE INTO source_health(route) VALUES (?)", (route,))
        if action == "pause":
            c.execute("UPDATE source_health SET paused_until=? WHERE route=?", (time.time() + 3600, route))
        elif action == "resume":
            c.execute("UPDATE source_health SET paused_until=0, consecutive_fail=0 WHERE route=?", (route,))
        else:
            c.execute("UPDATE source_health SET forced_off=? WHERE route=?", (1 if action == "off" else 0, route))
        db.audit(c, adm["email"], f"source_{action}", {"route": route}, adm.get("_ip", ""))
    return {"ok": True}


@router.get("/review")
def review(adm=Depends(require_admin)):
    def parse(rows):
        for r in rows:
            for k in ("old_json", "new_json", "alt_json"):
                try:
                    r[k.replace("_json", "")] = json.loads(r.pop(k)) if r.get(k) else None
                except ValueError:
                    r[k.replace("_json", "")] = r.pop(k)
        return rows
    return {"pending": parse(_q("SELECT * FROM review_queue WHERE status='PENDING' ORDER BY id DESC LIMIT 100")),
            "recent": parse(_q("SELECT * FROM review_queue WHERE status!='PENDING' ORDER BY id DESC LIMIT 30")),
            "rejections": _q("SELECT ts, job, dataset, symbol, period, rule, reason, severity FROM validation_log WHERE severity!='INFO' ORDER BY id DESC LIMIT 60")}


@router.post("/review/{rid}/{action}")
def review_action(rid: int, action: str, adm=Depends(require_admin)):
    if action == "approve":
        return pipeline.approve_review(rid, adm["email"])
    if action == "reject":
        return pipeline.reject_review(rid, adm["email"])
    raise HTTPException(400, "bad action")


@router.get("/audit")
def audit_log(limit: int = 100, adm=Depends(require_admin)):
    return {"rows": _q("SELECT ts, who, ip, action, detail FROM admin_audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))}


def _master_db() -> str:
    return os.environ.get("QS_MASTER_DB", os.path.join(db.DATA_DIR, "quantsync_master.db"))


def _dna_db() -> str:
    return os.environ.get("QS_DNA_DB", os.path.join(db.DATA_DIR, "stock_dna_cache.db"))


@router.get("/performance")
def performance(adm=Depends(require_admin)):
    out: Dict[str, Any] = {"trackRecord": None, "radar": None, "dna": None, "openTrades": []}
    try:
        from common import ledger_truth
        c = sqlite3.connect(_master_db()); c.row_factory = sqlite3.Row
        closed = [dict(r) for r in c.execute("SELECT * FROM closed_trades")]
        tr = ledger_truth.track_record(closed)
        tr.pop("byStrategy", None)
        out["trackRecord"] = {k: tr[k] for k in ("liveSince", "live", "simulated", "byMonth")}
        out["openTrades"] = [dict(r) for r in c.execute("SELECT symbol, strategy_id, entry_date, entry_price, current_ltp, unrealized_pnl FROM active_trades")]
        c.close()
    except Exception as e:
        out["trackRecordError"] = str(e)[:200]
    try:
        c = sqlite3.connect(_dna_db()); c.row_factory = sqlite3.Row
        sd = c.execute("SELECT MAX(scan_date) FROM stock_screener_rows").fetchone()[0]
        cols = {r[1] for r in c.execute("PRAGMA table_info(stock_screener_rows)")}
        q = "SELECT breakout_status, COUNT(*) n FROM stock_screener_rows WHERE scan_date=? GROUP BY breakout_status"
        out["radar"] = {"scanDate": sd, "byStatus": {r[0] or "NONE": r[1] for r in c.execute(q, (sd,))},
                        "quarantined": c.execute("SELECT COUNT(*) FROM stock_screener_rows WHERE scan_date=? AND COALESCE(data_quarantine,0)=1", (sd,)).fetchone()[0] if "data_quarantine" in cols else None}
        out["dna"] = {"reports": c.execute("SELECT COUNT(*) FROM stock_dna_reports").fetchone()[0],
                      "updatedToday": c.execute("SELECT COUNT(*) FROM stock_dna_reports WHERE report_date=?", (date.today().isoformat(),)).fetchone()[0],
                      "byStance": {r[0] or "NONE": r[1] for r in c.execute("SELECT stance, COUNT(*) FROM stock_dna_reports GROUP BY stance")}}
        c.close()
    except Exception as e:
        out["radarError"] = str(e)[:200]
    return out


# ── relay (home-network collector pushes raw responses) ─────────────────────
def _relay_ok(request: Request) -> None:
    secret = os.environ.get("QS_RELAY_TOKEN", "")
    got = request.headers.get("x-relay-token", "")
    if not secret or len(secret) < 24 or not hmac.compare_digest(secret.encode(), got.encode()):
        raise HTTPException(403, "relay token invalid")


@relay_router.get("/tasks")
def relay_tasks(request: Request):
    """URLs the VPS could not fetch itself in the last day — the relay fetches them from a home IP."""
    _relay_ok(request)
    since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = _q("SELECT DISTINCT source, key, url FROM raw_fetch WHERE ok=0 AND fetched_at>=? AND route IN ('direct:nse','chromium:nse') "
              "AND (source, key) NOT IN (SELECT source, key FROM raw_fetch WHERE ok=1 AND fetched_at>=?) LIMIT 200", (since, since))
    return {"tasks": rows}


@relay_router.post("/ingest")
def relay_ingest(request: Request, item: Dict[str, Any] = Body(...)):
    _relay_ok(request)
    try:
        body = base64.b64decode(item["body_b64"])
        source, key = str(item["source"])[:60], str(item["key"])[:120]
    except Exception:
        raise HTTPException(400, "bad payload")
    if len(body) > 8_000_000:
        raise HTTPException(413, "too large")
    with db.session() as c:
        c.execute("INSERT INTO relay_inbox(ts,source,key,url,status,body,content_hash) VALUES (?,?,?,?,?,?,?)",
                  (db.now_iso(), source, key, str(item.get("url", ""))[:500], int(item.get("status", 200)), body, hashlib.sha256(body).hexdigest()))
    return {"ok": True}
