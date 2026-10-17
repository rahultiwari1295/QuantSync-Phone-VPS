"""FastAPI app mounted at /lab/v1 (nginx forwards /lab/ to 127.0.0.1:8100)."""
from __future__ import annotations

import inspect
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import __version__, db, engine_feed, jobs, pipeline, service, store
from .auth import mirror_strategy_ids, optional_uid, require_uid
from .catalog import catalog
from .config import Config, load_config
from .db import jdump, new_id, tx
from .rules import RuleError, normalise, normalise_exit

router = APIRouter(prefix="/lab/v1")
log = logging.getLogger("qslab.api")
HANDLE = re.compile(r"^[a-z0-9_]{3,20}$")


def get_con(request: Request):
    con = db.connect(request.app.state.cfg)
    try:
        yield con
    finally:
        con.close()


def cfg_of(request: Request) -> Config:
    return request.app.state.cfg


class RuleBody(BaseModel):
    tf: str = Field(pattern="^(1D|1W)$")
    entry: dict
    exit: dict | None = None


class StrategyBody(RuleBody):
    name: str = Field(min_length=2, max_length=60)


class GateBody(BaseModel):
    gate: int | None = Field(default=None, ge=50, le=90)


class ExitChoiceBody(BaseModel):
    choice: str = Field(pattern="^(user|ai)$")


def _owned(con, sid: str, uid: str):
    r = con.execute("SELECT * FROM strategies WHERE id=? AND archived=0", (sid,)).fetchone()
    if not r:
        raise HTTPException(404, "Strategy not found.")
    if r["owner_uid"] != uid:
        raise HTTPException(403, "This strategy belongs to someone else.")
    return r


def _visible(con, sid: str, uid: str | None):
    r = con.execute("SELECT * FROM strategies WHERE id=? AND archived=0", (sid,)).fetchone()
    if not r or (r["visibility"] != "public" and r["owner_uid"] != uid):
        raise HTTPException(404, "Strategy not found.")
    return r


def _bad(e: Exception):
    return HTTPException(400, str(e))


@router.get("/health")
def health(request: Request):
    cfg = cfg_of(request)
    return {"ok": True, "version": __version__, "data": {tf: store.meta(cfg, tf) for tf in ("1D", "1W")},
            "auth": {"mode": cfg.auth_mode, "firebase_project_id": cfg.firebase_project_id or None}}


@router.get("/catalog")
def get_catalog():
    return catalog()


@router.post("/scan")
async def scan(body: RuleBody, request: Request, uid: str | None = Depends(optional_uid)):
    try:
        return await run_in_threadpool(pipeline.scan, cfg_of(request), body.entry, body.tf)
    except RuleError as e:
        raise _bad(e)
    except FileNotFoundError:
        raise HTTPException(503, "Market data is being prepared.")


@router.post("/backtest")
def backtest(body: RuleBody, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    cfg = cfg_of(request)
    try:
        payload = {"tf": body.tf, "entry": normalise(body.entry), "exit": normalise_exit(body.exit)}
        jid = jobs.enqueue(con, "backtest", uid, payload, queue_cap=cfg.queue_cap, bt_hourly=cfg.free_backtests_per_hour)
    except RuleError as e:
        raise _bad(e)
    except jobs.QueueError as e:
        raise HTTPException(e.code, e.msg)
    return {"job_id": jid}


@router.get("/jobs/{jid}")
def job(jid: str, uid: str = Depends(require_uid), con=Depends(get_con)):
    s = jobs.status(con, jid, uid)
    if not s:
        raise HTTPException(404, "Job not found.")
    return s


def _mirror(cfg, con, uid):
    sids = [r[0] for r in con.execute("SELECT id FROM strategies WHERE owner_uid=? AND archived=0 ORDER BY created_at", (uid,))]
    fids = [r[0] for r in con.execute("SELECT strategy_id FROM follows WHERE uid=?", (uid,))]
    h = con.execute("SELECT handle FROM users WHERE uid=?", (uid,)).fetchone()
    mirror_strategy_ids(cfg, uid, sids, fids, h[0] if h else None)


def _ensure_user(con, uid):
    con.execute("INSERT OR IGNORE INTO users(uid,created_at) VALUES(?,?)", (uid, time.time()))


@router.post("/strategies")
def create_strategy(body: StrategyBody, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    cfg = cfg_of(request)
    _ensure_user(con, uid)
    try:
        out = service.create_strategy(con, cfg, uid, body.name, body.tf, body.entry, body.exit)
    except (RuleError, ValueError) as e:
        raise _bad(e)
    _kick_backtest(con, out["id"])
    _subscribe(con, uid, out["id"])
    _mirror(cfg, con, uid)
    return out


def _kick_backtest(con, sid: str) -> None:
    """Every saved strategy gets its past test computed straight away, so its page has results to show."""
    try:
        s = con.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
        v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
        service.ensure_backtest(con, s, v)
    except Exception:  # noqa: BLE001 - the strategy is saved; the page will retry
        log.exception("could not queue past test for %s", sid)


def _engine(request_or_cfg, s) -> str | None:
    cfg = request_or_cfg if isinstance(request_or_cfg, Config) else cfg_of(request_or_cfg)
    eng = engine_feed.engine_of(s)
    return eng if eng and cfg.engine_api and engine_feed.feed(cfg) is not None else None


def _card(con, s, uid=None, cfg: Config | None = None) -> dict:
    v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
    owner = con.execute("SELECT handle FROM users WHERE uid=?", (s["owner_uid"],)).fetchone() if s["owner_uid"] else None
    followers = con.execute("SELECT count(*) FROM follows WHERE strategy_id=? AND uid IS NOT ?", (s["id"], s["owner_uid"])).fetchone()[0]
    enh = con.execute("SELECT id,status FROM ai_enhancements WHERE version_id=? OR id=? ORDER BY created_at DESC LIMIT 1",
                      (v["id"], v["ai_enhancement_id"])).fetchone()
    card = {"id": s["id"], "name": s["name"], "tf": s["tf"], "kind": s["kind"], "visibility": s["visibility"],
            "owner": owner[0] if owner and owner[0] else ("QuantSync" if s["kind"] == "flagship" else None), "mine": bool(uid and uid == s["owner_uid"]),
            "followers": followers, "version": v["n"], "live_from": v["live_from"], "ai_gate": v["ai_gate"],
            "enhanced": bool(v["ai_gate"]), "exit_choice": v["exit_choice"],
            "enhancement": {"id": enh["id"], "status": enh["status"]} if enh else None,
            "stats": service.live_stats(con, v["id"]), "source": "lab"}
    if s["kind"] == "library":
        from .library import meta_for
        card.update(meta_for(s["name"]), owner="QuantSync library")
    eng = _engine(cfg, s) if cfg else None
    if eng:  # flagships show the live engine's own record
        card.update(stats=engine_feed.stats(cfg, eng), source="engine", ai_gate=None, enhanced=False, enhancement=None,
                    live_from=(engine_feed.feed(cfg) or {}).get("live_since"))
        bt = engine_feed.backtest(cfg, eng)
        card["record"] = _record(bt, "Full record") if bt else None
        card["spark"] = ((bt or {}).get("analytics") or {}).get("spark") or []
    else:
        bt = service.cached_backtest(con, s["tf"], json.loads(v["entry_json"]), json.loads(v["exit_json"]),
                                     v["ai_gate"] if v["ai_enhancement_id"] else None, v["ai_enhancement_id"] if v["ai_gate"] else None)
        card["record"] = _record(bt, "Past test 2023–26") if bt else None
        card["spark"] = ((bt or {}).get("analytics") or {}).get("spark") or []
    return card


def _record(bt: dict, label: str) -> dict | None:
    """Headline numbers for tiles: the whole closed record (past test or engine record), all trades counted."""
    a = (bt.get("summary") or {}).get("All")
    if not a:
        return None
    return {"label": label, **{k: a.get(k) for k in ("trades", "wins", "losses", "win_rate", "avg_ret", "profit_factor", "max_dd")}}


@router.get("/strategies")
def list_strategies(request: Request, scope: str = "mine", uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    if scope == "mine":
        if not uid:
            raise HTTPException(401, "Sign in to continue.")
        q, a = "SELECT * FROM strategies WHERE owner_uid=? AND archived=0 ORDER BY created_at DESC", (uid,)
    elif scope == "flagship":
        q, a = "SELECT * FROM strategies WHERE kind='flagship' AND archived=0", ()
    elif scope == "library":
        q, a = "SELECT * FROM strategies WHERE kind='library' AND archived=0 ORDER BY name", ()
    else:  # people's strategies: ranked by followers, never by returns
        q, a = ("SELECT s.* FROM strategies s LEFT JOIN follows f ON f.strategy_id=s.id AND f.uid IS NOT s.owner_uid WHERE s.visibility='public' AND s.kind='user' "
                "AND s.archived=0 GROUP BY s.id ORDER BY count(f.uid) DESC, s.created_at DESC LIMIT 50"), ()
    out = []
    for s in con.execute(q, a).fetchall():
        try:
            out.append(_card(con, s, uid, cfg_of(request)))
            if scope == "library" and out[-1].get("record") is None:  # no past test yet: queue one (once per version)
                try:
                    v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
                    service.ensure_backtest(con, s, v)
                except Exception:  # noqa: BLE001
                    log.exception("could not queue library test for %s", s["id"])
        except Exception:  # noqa: BLE001 - one bad record must not hide the whole list
            log.exception("card failed for %s", s["id"])
            out.append({"id": s["id"], "name": s["name"], "tf": s["tf"], "kind": s["kind"], "visibility": s["visibility"], "owner": None,
                        "mine": bool(uid and uid == s["owner_uid"]), "followers": 0, "version": 0, "live_from": None, "ai_gate": None,
                        "enhanced": False, "exit_choice": "user", "enhancement": None, "broken": True,
                        "stats": {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_ret": None, "open": 0, "shown": False}})
    return out


@router.get("/strategies/{sid}")
def get_strategy(sid: str, request: Request, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    s = _visible(con, sid, uid)
    v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
    out = _card(con, s, uid, cfg_of(request))
    out["info"] = {"entry": json.loads(v["entry_json"]), "exit": json.loads(v["exit_json"])}
    return out


@router.get("/strategies/{sid}/backtest")
def strategy_backtest(sid: str, request: Request, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    s = _visible(con, sid, uid)
    eng = _engine(request, s)
    if eng:
        res = engine_feed.backtest(cfg_of(request), eng)
        if res:
            return {"status": "done", "result": res}
    v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
    try:
        return service.ensure_backtest(con, s, v)
    except jobs.QueueError as e:
        return {"status": "failed", "error": e.msg}


@router.post("/strategies/{sid}/backtest")
def strategy_backtest_run(sid: str, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    cfg = cfg_of(request)
    s = _visible(con, sid, uid)
    v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
    payload = {"tf": s["tf"], "entry": json.loads(v["entry_json"]), "exit": json.loads(v["exit_json"]), "version_id": v["id"]}
    try:
        return {"job_id": jobs.enqueue(con, "backtest", uid, payload, queue_cap=cfg.queue_cap, bt_hourly=cfg.free_backtests_per_hour)}
    except jobs.QueueError as e:
        raise HTTPException(e.code, e.msg)


LATEST_DAYS = 4  # "Latest" on the matches page = matched within the last 4 calendar days


def _subscribe(con, uid: str, sid: str, on: bool = True) -> None:
    con.execute("INSERT INTO follows VALUES(?,?,?,?) ON CONFLICT(uid,strategy_id) DO UPDATE SET alerts=excluded.alerts", (uid, sid, int(on), time.time()))


def _alert_subs(con, uid: str | None) -> list[dict]:
    """Strategies whose matches this person sees, with their colour group.
    QuantSync strategies are always listed (on unless muted). Yours and Followed are listed only while switched on."""
    flags = con.execute("SELECT * FROM strategies WHERE kind='flagship' AND archived=0 ORDER BY name").fetchall()
    mine = {r["strategy_id"]: r["alerts"] for r in con.execute("SELECT strategy_id, alerts FROM follows WHERE uid=?", (uid,)).fetchall()} if uid else {}
    out = [{"s": s, "cat": "q", "on": bool(mine.get(s["id"], 1))} for s in flags]
    if uid:
        rows = con.execute("""SELECT s.* FROM follows f JOIN strategies s ON s.id=f.strategy_id
            WHERE f.uid=? AND f.alerts=1 AND s.kind='user' AND s.archived=0 AND (s.owner_uid=? OR s.visibility='public')
            ORDER BY s.name""", (uid, uid)).fetchall()
        out += [{"s": s, "cat": "y" if s["owner_uid"] == uid else "f", "on": True} for s in rows]
    return out


@router.get("/alerts")
def alerts(uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    items = []
    for x in _alert_subs(con, uid):
        s = x["s"]
        owner = con.execute("SELECT handle FROM users WHERE uid=?", (s["owner_uid"],)).fetchone() if s["owner_uid"] else None
        items.append({"id": s["id"], "name": s["name"], "tf": s["tf"], "cat": x["cat"], "on": x["on"], "owner": owner[0] if owner and owner[0] else None})
    return {"signed_in": bool(uid), "items": items}


@router.get("/matches")
def matches(request: Request, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    """Open matches from every strategy switched on in Your alerts, newest first, split into Latest (last 4 days) and Older."""
    cfg = cfg_of(request)
    today_ = pd.Timestamp.today().normalize()
    rows: list[dict] = []
    for x in _alert_subs(con, uid):
        if not x["on"]:
            continue
        s = x["s"]
        try:
            eng = _engine(cfg, s)
            if eng:
                found = [{"symbol": a["symbol"], "date": a["entry_date"], "price": a["entry_price"], "now": a["price"], "chg": a["ret_pct"]} for a in engine_feed.active(cfg, eng)]
            else:
                found = [{"symbol": r["symbol"], "date": r["signal_date"], "price": r["entry_price"], "now": r["exit_price"], "chg": r["ret_pct"]}
                         for r in con.execute("SELECT * FROM live_trades WHERE version_id=? AND is_open=1 ORDER BY signal_date DESC LIMIT 500", (s["current_version"],))]
        except Exception:  # noqa: BLE001 - one strategy must not hide the others
            log.exception("matches failed for %s", s["id"])
            continue
        for f in found:
            age = (today_ - pd.Timestamp(f["date"])).days if f["date"] else None
            rows.append({**f, "sid": s["id"], "strategy": s["name"], "cat": x["cat"], "age_days": age, "latest": age is not None and age <= LATEST_DAYS})
    rows.sort(key=lambda r: (r["date"] or "", r["symbol"]), reverse=True)
    n_latest = sum(1 for r in rows if r["latest"])
    return {"signed_in": bool(uid), "latest_days": LATEST_DAYS, "rows": rows[:1000], "counts": {"latest": n_latest, "older": len(rows) - n_latest}}


@router.get("/strategies/{sid}/trades")
def trades(sid: str, request: Request, status: str = "active", limit: int = 1000, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    s = _visible(con, sid, uid)
    eng = _engine(request, s)
    if eng:
        return engine_feed.trades(cfg_of(request), eng, status, min(limit, 1000))
    open_ = 1 if status == "active" else 0
    rows = con.execute("SELECT * FROM live_trades WHERE version_id=? AND is_open=? ORDER BY signal_date DESC, symbol LIMIT ?",
                       (s["current_version"], open_, min(limit, 500))).fetchall()
    cfg = None
    out = []
    for r in rows:
        d = dict(r)
        d["age_days"] = (pd.Timestamp.today().normalize() - pd.Timestamp(r["signal_date"])).days
        out.append(d)
    return out


@router.post("/strategies/{sid}/visibility")
def set_visibility(sid: str, public: bool, uid: str = Depends(require_uid), con=Depends(get_con)):
    _owned(con, sid, uid)
    if public and not con.execute("SELECT handle FROM users WHERE uid=?", (uid,)).fetchone()[0]:
        raise HTTPException(400, "Pick a public handle first.")
    con.execute("UPDATE strategies SET visibility=? WHERE id=?", ("public" if public else "private", sid))
    return {"ok": True}


@router.post("/strategies/{sid}/copy")
def copy_strategy(sid: str, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    s = _visible(con, sid, uid)
    v = con.execute("SELECT * FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()
    _ensure_user(con, uid)
    out = service.create_strategy(con, cfg_of(request), uid, f"{s['name']} (copy)", s["tf"], json.loads(v["entry_json"]),
                                  json.loads(v["exit_json"]), copied_from=sid)
    _kick_backtest(con, out["id"])
    _subscribe(con, uid, out["id"])
    _mirror(cfg_of(request), con, uid)
    return out


@router.delete("/strategies/{sid}")
def archive(sid: str, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    _owned(con, sid, uid)
    con.execute("UPDATE strategies SET archived=1 WHERE id=?", (sid,))
    _mirror(cfg_of(request), con, uid)
    return {"ok": True}


@router.post("/strategies/{sid}/enhance")
def enhance(sid: str, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    cfg = cfg_of(request)
    s = _owned(con, sid, uid)
    aid = new_id("ai")
    try:
        jid = jobs.enqueue(con, "enhance", uid, {"strategy_id": sid, "enhancement_id": aid}, queue_cap=cfg.queue_cap, ai_daily=cfg.free_ai_jobs_per_day)
    except jobs.QueueError as e:
        raise HTTPException(e.code, e.msg)
    con.execute("INSERT INTO ai_enhancements(id,version_id,status,created_at) VALUES(?,?,?,?)", (aid, s["current_version"], "queued", time.time()))
    return {"job_id": jid, "enhancement_id": aid}


@router.get("/strategies/{sid}/enhancement")
def enhancement(sid: str, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    s = _visible(con, sid, uid)  # read-only: anyone who can see the strategy (flagships included) can see its AI result
    r = con.execute("SELECT * FROM ai_enhancements WHERE version_id=? OR id=(SELECT ai_enhancement_id FROM strategy_versions WHERE id=?) ORDER BY created_at DESC LIMIT 1",
                    (s["current_version"], s["current_version"])).fetchone()
    if not r:
        return {"status": "none"}
    return {"id": r["id"], "status": r["status"], "reason": r["reason"], "result": json.loads(r["result_json"]) if r["result_json"] else None,
            "exit": json.loads(r["exit_result_json"]) if r["exit_result_json"] else None}


@router.post("/strategies/{sid}/ai-gate")
def set_gate(sid: str, body: GateBody, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    s = _owned(con, sid, uid)
    r = con.execute("SELECT id FROM ai_enhancements WHERE version_id=? AND status='done' ORDER BY created_at DESC LIMIT 1", (s["current_version"],)).fetchone()
    cur = con.execute("SELECT ai_enhancement_id FROM strategy_versions WHERE id=?", (s["current_version"],)).fetchone()[0]
    eid = r[0] if r else cur
    if body.gate and not eid:
        raise HTTPException(409, "Enhance this strategy first.")
    vid = service.new_version(con, sid, ai_enhancement_id=eid if body.gate else None, ai_gate=body.gate, live_from_new=_as_of(request, s["tf"]))
    return {"version_id": vid, "note": "Live record restarts from today for the new setting."}


@router.post("/strategies/{sid}/enhance-exit")
def enhance_exit(sid: str, body: GateBody, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    cfg = cfg_of(request)
    _owned(con, sid, uid)
    if not body.gate:
        raise HTTPException(400, "Choose a score first.")
    try:
        jid = jobs.enqueue(con, "enhance_exit", uid, {"strategy_id": sid, "gate": body.gate}, queue_cap=cfg.queue_cap, ai_daily=cfg.free_ai_jobs_per_day)
    except jobs.QueueError as e:
        raise HTTPException(e.code, e.msg)
    return {"job_id": jid}


@router.post("/strategies/{sid}/exit-choice")
def exit_choice(sid: str, body: ExitChoiceBody, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    s = _owned(con, sid, uid)
    return {"version_id": service.new_version(con, sid, exit_choice=body.choice, live_from_new=_as_of(request, s["tf"]))}


def _as_of(request: Request, tf: str) -> str | None:
    """A new version's live record starts at the latest data date, like a new strategy (entries follow at the next open)."""
    return (store.meta(cfg_of(request), tf) or {}).get("as_of")


@router.post("/strategies/{sid}/follow")
def follow(sid: str, on: bool = True, alerts: bool = False, request: Request = None, uid: str = Depends(require_uid), con=Depends(get_con)):
    s = _visible(con, sid, uid)
    _ensure_user(con, uid)
    if s["kind"] == "flagship":  # QuantSync strategies are never removed from the list: switching off keeps a muted row
        _subscribe(con, uid, sid, alerts if on else False)
        _mirror(cfg_of(request), con, uid)
        return {"ok": True}
    if on:
        n = con.execute("SELECT count(*) FROM follows WHERE uid=?", (uid,)).fetchone()[0]
        if n >= 200:
            raise HTTPException(400, "Follow limit reached.")
        con.execute("INSERT INTO follows VALUES(?,?,?,?) ON CONFLICT(uid,strategy_id) DO UPDATE SET alerts=excluded.alerts", (uid, sid, int(alerts), time.time()))
    else:
        con.execute("DELETE FROM follows WHERE uid=? AND strategy_id=?", (uid, sid))
    _mirror(cfg_of(request), con, uid)
    return {"ok": True}


@router.post("/strategies/{sid}/report")
def report(sid: str, reason: str = "", uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    _visible(con, sid, uid)
    con.execute("INSERT INTO reports(strategy_id,uid,reason,created_at) VALUES(?,?,?,?)", (sid, uid, reason[:300], time.time()))
    return {"ok": True}


@router.get("/me")
def me(uid: str = Depends(require_uid), con=Depends(get_con)):
    _ensure_user(con, uid)
    u = con.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    return {"handle": u["handle"], "plan": u["plan"], "pricing_enabled": con.execute("SELECT v FROM app_config WHERE k='pricing_enabled'").fetchone()[0] == "true"}


@router.put("/me/handle")
def set_handle(handle: str, request: Request, uid: str = Depends(require_uid), con=Depends(get_con)):
    h = handle.strip().lower()
    if not HANDLE.match(h):
        raise HTTPException(400, "3-20 letters, numbers or underscores.")
    _ensure_user(con, uid)
    try:
        con.execute("UPDATE users SET handle=? WHERE uid=?", (h, uid))
    except Exception:
        raise HTTPException(409, "That handle is taken.")
    _mirror(cfg_of(request), con, uid)
    return {"handle": h}


@router.get("/me/portfolio")
def portfolio(status: str = "active", uid: str = Depends(require_uid), con=Depends(get_con)):
    """Trades from the strategies the user owns or follows (current versions)."""
    rows = con.execute("""SELECT l.*, s.id AS strategy_id, s.name AS strategy_name, (v.ai_gate IS NOT NULL) AS enhanced
        FROM live_trades l JOIN strategy_versions v ON v.id=l.version_id JOIN strategies s ON s.current_version=v.id
        WHERE s.archived=0 AND (s.owner_uid=? OR s.id IN (SELECT strategy_id FROM follows WHERE uid=?))
        ORDER BY l.signal_date DESC, l.symbol LIMIT 1000""", (uid, uid)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return {"summary": {"active": 0, "closed": 0, "win_rate": None, "avg_ret": None, "wins": 0, "losses": 0}, "trades": []}
    closed = df[df.is_open == 0]
    wins = int((closed.ret_pct > 0).sum())
    summary = {"active": int((df.is_open == 1).sum()), "closed": len(closed), "wins": wins, "losses": len(closed) - wins,
               "win_rate": round(wins / len(closed) * 100, 1) if len(closed) >= 30 else None,
               "avg_ret": round(float(closed.ret_pct.mean()), 2) if len(closed) else None,
               "active_avg": round(float(df[df.is_open == 1].ret_pct.mean()), 2) if (df.is_open == 1).any() else None}
    sel = df[df.is_open == (1 if status == "active" else 0)].head(200)
    return {"summary": summary, "trades": json.loads(sel.to_json(orient="records"))}


@router.get("/patterns")
def patterns(request: Request, tf: str = "1D"):
    """Chart-pattern scan results. Reads the pattern feed written by the live engine's export (see README)."""
    f = cfg_of(request).data_dir / "patterns" / f"latest_{tf}.parquet"
    if not f.exists():
        return {"configured": False, "rows": []}
    df = pd.read_parquet(f).head(300)
    return {"configured": True, "rows": json.loads(df.to_json(orient="records", date_format="iso"))}


@router.get("/chart/{symbol}")
def chart(symbol: str, request: Request, tf: str = "1D", n: int = 180):
    cfg = cfg_of(request)
    if not re.fullmatch(r"[A-Za-z0-9&-]{1,20}", symbol) or tf not in ("1D", "1W"):
        raise HTTPException(400, "Bad request.")
    b = store.bars(cfg, tf, [symbol.upper()], columns=["open", "high", "low", "close", "volume", "sma_50", "sma_200", "rsi_14"])
    if symbol.upper() not in b:
        raise HTTPException(404, "Symbol not found.")
    df = b[symbol.upper()].tail(min(n, 600)).copy()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return {"symbol": symbol.upper(), "tf": tf, "bars": json.loads(df.to_json(orient="records"))}


@router.get("/today")
def today(request: Request, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    cfg = cfg_of(request)
    rows = con.execute("SELECT * FROM strategies WHERE kind='flagship' AND archived=0").fetchall()
    flag = [_card(con, s, uid, cfg) for s in rows]
    fresh = []
    for s in rows:
        eng = _engine(cfg, s)
        if eng:
            fresh += [{"symbol": a["symbol"], "signal_date": a["entry_date"], "sid": s["id"], "name": s["name"]} for a in engine_feed.active(cfg, eng)]
        else:
            fresh += [dict(r) for r in con.execute("""SELECT l.symbol, l.signal_date, ? sid, ? name FROM live_trades l
                WHERE l.version_id=? AND l.is_open=1""", (s["id"], s["name"], s["current_version"]))]
    fresh.sort(key=lambda r: r["signal_date"] or "", reverse=True)
    return {"data": {tf: store.meta(cfg, tf) for tf in ("1D", "1W")}, "flagship": flag, "fresh": fresh[:20]}


# ---- data for the redesigned app (charts, X-Ray links, holdings) ------------------------------------------------
SYM = re.compile(r"^[A-Za-z0-9&-]{1,20}$")
CHART_COLS = ["open", "high", "low", "close", "volume", "sma_50", "sma_200", "rsi_14"]


def _syms(symbols: str, cap: int) -> list[str]:
    out = []
    for s in (symbols or "").split(","):
        s = s.strip().upper()
        if s and SYM.match(s) and s not in out:
            out.append(s)
    if not out:
        raise HTTPException(400, "Pass at least one symbol.")
    return out[:cap]


def _bars_json(df: pd.DataFrame, n: int) -> list[dict]:
    df = df.tail(n).copy()
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return json.loads(df.to_json(orient="records"))


@router.get("/chart-batch")
def chart_batch(request: Request, symbols: str, tf: str = "1D", n: int = 60):
    """Small candle series for many symbols in one call (pattern cards, match previews, sparklines)."""
    if tf not in ("1D", "1W"):
        raise HTTPException(400, "Bad timeframe.")
    want = _syms(symbols, 40)
    b = store.bars(cfg_of(request), tf, want, columns=CHART_COLS)
    return {"tf": tf, "bars": {s: _bars_json(b[s], min(max(n, 10), 260)) for s in want if s in b}}


BENCH = ("NIFTY 50", "NIFTY50", "NIFTY", "^NSEI", "NIFTYBEES")


@router.get("/benchmark")
def benchmark(request: Request, tf: str = "1D", start: str | None = None):
    """Nifty 50 closes for drawing a benchmark line under an equity curve. Uses the first index-like symbol in the data."""
    if tf not in ("1D", "1W"):
        raise HTTPException(400, "Bad timeframe.")
    cfg = cfg_of(request)
    for s in BENCH:
        b = store.bars(cfg, tf, [s], start=start, columns=["close"])
        if s in b and len(b[s]):
            df = b[s].copy()
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
            return {"symbol": s, "tf": tf, "points": [{"d": d, "v": round(float(v), 2)} for d, v in zip(df["date"], df["close"])]}
    return {"symbol": None, "tf": tf, "points": []}


@router.get("/symbol/{symbol}/strategies")
def symbol_strategies(symbol: str, request: Request, uid: str | None = Depends(optional_uid), con=Depends(get_con)):
    """Open matches of one stock across QuantSync, your own and public strategies (for the X-Ray page)."""
    sym = symbol.strip().upper()
    if not SYM.match(sym):
        raise HTTPException(400, "Bad symbol.")
    cfg = cfg_of(request)
    out = []
    for s in con.execute("SELECT * FROM strategies WHERE kind='flagship' AND archived=0").fetchall():
        eng = _engine(cfg, s)
        if eng:
            out += [{"sid": s["id"], "strategy": s["name"], "cat": "q", "date": a["entry_date"], "price": a["entry_price"], "now": a["price"],
                     "chg": a["ret_pct"]} for a in engine_feed.active(cfg, eng) if str(a["symbol"]).upper() == sym]
    rows = con.execute("""SELECT s.id, s.name, s.owner_uid, l.signal_date, l.entry_price, l.exit_price, l.ret_pct FROM live_trades l
        JOIN strategies s ON s.current_version=l.version_id
        WHERE l.symbol=? AND l.is_open=1 AND s.archived=0 AND s.kind='user' AND (s.visibility='public' OR s.owner_uid=?)
        ORDER BY l.signal_date DESC LIMIT 20""", (sym, uid or "")).fetchall()
    out += [{"sid": r["id"], "strategy": r["name"], "cat": "y" if uid and r["owner_uid"] == uid else "f", "date": r["signal_date"],
             "price": r["entry_price"], "now": r["exit_price"], "chg": r["ret_pct"]} for r in rows]
    return {"symbol": sym, "rows": out}


class Holding(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9&-]{1,20}$")
    qty: float = Field(gt=0, le=1e9)
    avg_price: float = Field(gt=0, le=1e9)


class HoldingsBody(BaseModel):
    items: list[Holding] = Field(max_length=300)


@router.get("/me/holdings")
def get_holdings(uid: str = Depends(require_uid), con=Depends(get_con)):
    rows = con.execute("SELECT symbol, qty, avg_price, updated_at FROM holdings WHERE uid=? ORDER BY symbol", (uid,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.put("/me/holdings")
def put_holdings(body: HoldingsBody, uid: str = Depends(require_uid), con=Depends(get_con)):
    """Replaces the whole list (the page always sends everything it shows)."""
    _ensure_user(con, uid)
    now = time.time()
    with tx(con):
        con.execute("DELETE FROM holdings WHERE uid=?", (uid,))
        for h in body.items:
            con.execute("INSERT OR REPLACE INTO holdings VALUES(?,?,?,?,?)", (uid, h.symbol.upper(), h.qty, h.avg_price, now))
    return {"ok": True, "count": len(body.items)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.migrate(app.state.cfg)
    yield


def create_app(cfg: Config | None = None) -> FastAPI:
    app = FastAPI(title="QuantSync Lab", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/lab/openapi.json")
    app.state.cfg = cfg or load_config()
    app.include_router(router)

    # The website (Vercel, localhost) calls this API directly when Settings points at a phone or laptop URL.
    # Bearer tokens, no cookies, so credentials stay off. Extra origins: cors_origins in config.yaml (extra:).
    extra = [o for o in (app.state.cfg.extra or {}).get("cors_origins", []) if isinstance(o, str)]
    origin_re = r"https://([a-z0-9-]+\.)*vercel\.app|https://([a-z0-9-]+\.)*duckdns\.org(:\d+)?|http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"
    cors = dict(allow_origins=extra, allow_origin_regex=origin_re, allow_credentials=False,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "Accept", "X-Dev-Uid"], max_age=86400)
    native_pna = "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters
    if native_pna:  # Chrome asks before a public page calls localhost / LAN (Starlette >= 0.38)
        cors["allow_private_network"] = True
    app.add_middleware(CORSMiddleware, **cors)

    if not native_pna:
        @app.middleware("http")
        async def _private_network(request: Request, call_next):
            resp = await call_next(request)
            if request.method == "OPTIONS" and request.headers.get("access-control-request-private-network"):
                resp.headers["Access-Control-Allow-Private-Network"] = "true"
            return resp

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # JSON, so the UI can show what happened
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": f"Server error ({type(exc).__name__}). Please try again."})
    return app



