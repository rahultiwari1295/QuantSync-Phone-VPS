"""Strategy lifecycle, job handlers and the live tracker."""
from __future__ import annotations

import json
import logging
import sqlite3
import time

import pandas as pd

from . import FSV, ai, jobs, metrics, pipeline, simulator, store
from .config import Config
from .db import jdump, new_id, tx
from .rules import normalise, normalise_exit, rule_hash

log = logging.getLogger("qslab")


def create_strategy(con, cfg: Config, uid: str | None, name: str, tf: str, entry: dict, exit_: dict | None,
                    kind: str = "user", visibility: str = "private", copied_from: str | None = None) -> dict:
    if tf not in ("1D", "1W"):
        raise ValueError("tf must be 1D or 1W")
    entry, exit_ = normalise(entry), normalise_exit(exit_)
    name = " ".join(name.split())[:60]
    if len(name) < 2:
        raise ValueError("name too short")
    sid, vid = new_id("stg"), new_id("ver")
    live_from = (store.meta(cfg, tf) or {}).get("as_of") or time.strftime("%Y-%m-%d")
    with tx(con):
        if uid:
            n = con.execute("SELECT count(*) FROM strategies WHERE owner_uid=? AND archived=0", (uid,)).fetchone()[0]
            if n >= 100:
                raise ValueError("strategy limit (100) reached")
        con.execute("INSERT INTO strategies(id,owner_uid,kind,name,tf,visibility,current_version,copied_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, uid, kind, name, tf, visibility, vid, copied_from, time.time(), time.time()))
        con.execute("INSERT INTO strategy_versions(id,strategy_id,n,entry_json,exit_json,rule_hash,fsv,live_from,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (vid, sid, 1, jdump(entry), jdump(exit_), rule_hash(entry, tf), FSV, live_from, time.time()))
    return {"id": sid, "version_id": vid}


def new_version(con, sid: str, **changes) -> str:
    """Any change to rules, gate or exit choice starts a new version, which restarts the live record."""
    cur = con.execute("SELECT v.*, s.tf FROM strategy_versions v JOIN strategies s ON s.current_version=v.id WHERE s.id=?", (sid,)).fetchone()
    d = dict(cur)
    d.update(changes)
    vid = new_id("ver")
    with tx(con):
        con.execute("""INSERT INTO strategy_versions(id,strategy_id,n,entry_json,exit_json,rule_hash,fsv,ai_enhancement_id,ai_gate,exit_choice,live_from,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (vid, sid, cur["n"] + 1, d["entry_json"], d["exit_json"], d["rule_hash"], FSV, d["ai_enhancement_id"],
                     d["ai_gate"], d["exit_choice"], d.get("live_from_new") or time.strftime("%Y-%m-%d"), time.time()))
        con.execute("UPDATE strategies SET current_version=?, updated_at=? WHERE id=?", (vid, time.time(), sid))
    return vid


def _ctx(con, sid):
    r = con.execute("SELECT s.tf, v.* FROM strategies s JOIN strategy_versions v ON v.id=s.current_version WHERE s.id=?", (sid,)).fetchone()
    return r, json.loads(r["entry_json"]), json.loads(r["exit_json"])


def run_backtest(cfg, con, e, x, tf, version_id="", prog=lambda pct, st: None) -> dict:
    """Backtest with a cache keyed on (rule, exit, data as-of)."""
    key = (rule_hash(e, tf), pipeline.exit_hash(x), f'{(store.meta(cfg, tf) or {}).get("as_of", "")}|m{metrics.METRICS_V}')
    hit = con.execute("SELECT summary_json FROM backtest_runs WHERE rule_hash=? AND exit_hash=? AND as_of=?", key).fetchone()
    if hit:
        return json.loads(hit[0])
    res = pipeline.backtest(cfg, e, x, tf, prog)
    con.execute("INSERT INTO backtest_runs(id,version_id,rule_hash,exit_hash,as_of,summary_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("bt"), version_id, *key, jdump(res), time.time()))
    return res


def _gate_key(x, gate, enh_id) -> str:
    return pipeline.exit_hash(x) + (f"|g{gate}|{enh_id}" if gate and enh_id else "")


def cached_backtest(con, tf, e, x, gate=None, enh_id=None) -> dict | None:
    """Newest past test computed with the current metric definitions (older ones are recomputed, never shown)."""
    r = con.execute("SELECT summary_json FROM backtest_runs WHERE rule_hash=? AND exit_hash=? AND as_of LIKE ? ORDER BY created_at DESC LIMIT 1",
                    (rule_hash(e, tf), _gate_key(x, gate, enh_id), f"%|m{metrics.METRICS_V}")).fetchone()
    return json.loads(r[0]) if r else None


def run_gated_backtest(cfg, con, e, x, tf, gate: int, enh_id: str, version_id="", prog=lambda pct, st: None) -> dict:
    """Past test of only the results the AI score would have let through. Scores are walk-forward (each year scored by a
    model trained on earlier years), the same ones the 'Pick a score' table uses, so nothing here sees the future."""
    key = (rule_hash(e, tf), _gate_key(x, gate, enh_id), f'{(store.meta(cfg, tf) or {}).get("as_of", "")}|m{metrics.METRICS_V}')
    hit = con.execute("SELECT summary_json FROM backtest_runs WHERE rule_hash=? AND exit_hash=? AND as_of=?", key).fetchone()
    if hit:
        return json.loads(hit[0])
    res = pipeline.enhance(cfg, e, x, tf, prog)
    if res["status"] != "done":
        raise ValueError(res.get("reason") or "Not enough past results to score.")
    ds, sc = res["_ds"], res["_scores"]
    keep = ds[(sc >= gate).to_numpy()].copy()
    keep["is_open"] = False
    out = pipeline.summarise_trades(cfg, keep, tf)
    out.update(ai_gate=gate, note=f"Only results with AI score {gate} or higher (walk-forward scores).")
    con.execute("INSERT INTO backtest_runs(id,version_id,rule_hash,exit_hash,as_of,summary_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("bt"), version_id, *key, jdump(out), time.time()))
    return out


def ensure_backtest(con, s, v) -> dict:
    """Cached past test, or a queued system job computing it (one per version). Never needs a signed-in user."""
    e, x = json.loads(v["entry_json"]), json.loads(v["exit_json"])
    gate, enh = (v["ai_gate"], v["ai_enhancement_id"]) if v["ai_gate"] and v["ai_enhancement_id"] else (None, None)
    res = cached_backtest(con, s["tf"], e, x, gate, enh)
    if res:
        return {"status": "done", "result": res}
    kind = "gated_backtest" if gate else "backtest"
    j = con.execute("""SELECT id, status, error, finished_at FROM jobs WHERE kind=? AND json_extract(payload_json,'$.version_id')=?
                       ORDER BY created_at DESC LIMIT 1""", (kind, v["id"])).fetchone()
    if j and j["status"] in ("queued", "running"):
        return {"status": "running", "job_id": j["id"]}
    if j and j["status"] == "failed" and time.time() - (j["finished_at"] or 0) < 600:  # don't hammer a failing rule
        return {"status": "failed", "error": j["error"]}
    p = {"tf": s["tf"], "entry": e, "exit": x, "version_id": v["id"]}
    if gate:
        p.update(gate=gate, enhancement_id=enh)
    jid = jobs.enqueue(con, kind, None, p, queue_cap=10_000)
    return {"status": "running", "job_id": jid}


# ---- job handlers -----------------------------------------------------------------------------
def handle(cfg: Config, con: sqlite3.Connection, job: sqlite3.Row) -> dict:
    p = json.loads(job["payload_json"])
    prog = lambda pct, st: jobs.progress(con, job["id"], pct, st)
    if job["kind"] == "backtest":
        return run_backtest(cfg, con, normalise(p["entry"]), normalise_exit(p.get("exit")), p["tf"], p.get("version_id") or "", prog)
    if job["kind"] == "gated_backtest":
        return run_gated_backtest(cfg, con, normalise(p["entry"]), normalise_exit(p.get("exit")), p["tf"], int(p["gate"]), p["enhancement_id"],
                                  p.get("version_id") or "", prog)
    if job["kind"] == "enhance":
        return _enhance(cfg, con, p, prog)
    if job["kind"] == "enhance_exit":
        return _enhance_exit(cfg, con, p, prog)
    raise ValueError(f"unknown job kind {job['kind']}")


def _enhance(cfg, con, p, prog):
    r, entry, exit_ = _ctx(con, p["strategy_id"])
    aid = p["enhancement_id"]
    res = pipeline.enhance(cfg, entry, exit_, r["tf"], prog)
    public = {k: v for k, v in res.items() if not k.startswith("_")}
    if res["status"] == "done":
        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        res["_scorer"].save(cfg.models_dir / aid)
    con.execute("UPDATE ai_enhancements SET status=?, candidate=?, model_path=?, result_json=?, reason=?, finished_at=? WHERE id=?",
                (res["status"], res.get("candidate"), str(cfg.models_dir / aid) if res["status"] == "done" else None,
                 jdump(public), res.get("reason"), time.time(), aid))
    return {"enhancement_id": aid, "status": res["status"]}


def _enhance_exit(cfg, con, p, prog):
    r, entry, exit_ = _ctx(con, p["strategy_id"])
    row = con.execute("SELECT id FROM ai_enhancements WHERE status='done' AND (version_id=? OR id=?) ORDER BY created_at DESC LIMIT 1",
                      (r["id"], r["ai_enhancement_id"])).fetchone()
    if not row:
        raise ValueError("Run the entry enhancement first.")
    res = pipeline.enhance(cfg, entry, exit_, r["tf"], prog)  # deterministic: same scores as the stored run
    out = pipeline.enhance_exit(cfg, entry, exit_, r["tf"], res["_scores"], res["_ds"], int(p["gate"]), prog)
    con.execute("UPDATE ai_enhancements SET exit_result_json=? WHERE id=?", (jdump({"gate": p["gate"], **out}), row["id"]))
    return out


# ---- live tracker -----------------------------------------------------------------------------
def update_live(cfg: Config, con: sqlite3.Connection) -> int:
    """Run after each cook. Same simulator as history; upserts open/closed trades since each version's live_from."""
    n = 0
    for v in con.execute("SELECT v.*, s.tf FROM strategy_versions v JOIN strategies s ON s.current_version=v.id WHERE s.archived=0"):
        tf, entry, exit_ = v["tf"], json.loads(v["entry_json"]), json.loads(v["exit_json"])
        if v["exit_choice"] == "ai" and v["ai_enhancement_id"]:
            e = con.execute("SELECT exit_result_json FROM ai_enhancements WHERE id=?", (v["ai_enhancement_id"],)).fetchone()
            ex = json.loads(e[0]).get("best_exit") if e and e[0] else None
            exit_ = ex or exit_
        try:
            start = str((pd.Timestamp(v["live_from"]) - pd.Timedelta(days=200 if tf == "1D" else 900)).date()) if v["live_from"] else None
            trades, sg = pipeline.run_trades(cfg, entry, exit_, tf, start=start)
        except Exception:
            log.exception("live update failed for %s", v["id"]); continue
        if v["ai_gate"] and v["ai_enhancement_id"]:
            trades = _apply_gate(cfg, con, v, trades, sg)
        trades = trades[trades["signal_date"] >= pd.Timestamp(v["live_from"])] if len(trades) else trades
        with tx(con):
            for t in trades.itertuples():
                con.execute("""INSERT INTO live_trades(version_id,symbol,signal_date,entry_date,entry_price,exit_date,exit_price,exit_reason,ret_pct,is_open,score)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(version_id,symbol,signal_date) DO UPDATE SET
                    exit_date=excluded.exit_date, exit_price=excluded.exit_price, exit_reason=excluded.exit_reason,
                    ret_pct=excluded.ret_pct, is_open=excluded.is_open""",
                    (v["id"], t.symbol, str(t.signal_date.date()), str(t.entry_date.date()), t.entry_price,
                     None if t.is_open else str(t.exit_date.date()), t.exit_price, t.exit_reason, t.ret_pct, int(t.is_open),
                     getattr(t, "score", None)))
                n += 1
    return n


def _apply_gate(cfg, con, v, trades, sg):
    row = con.execute("SELECT model_path FROM ai_enhancements WHERE id=?", (v["ai_enhancement_id"],)).fetchone()
    if not row or not row[0] or trades.empty:
        return trades.iloc[0:0]
    sc = ai.Scorer.load(row[0])
    f = sg.rename(columns={"date": "signal_date"}).drop(columns=["close"])
    m = trades.merge(f, on=["symbol", "signal_date"], how="left")
    m["score"] = sc.score(m)
    return m[m["score"] >= v["ai_gate"]].reset_index(drop=True)


def live_stats(con, version_id: str) -> dict:
    rows = pd.read_sql_query("SELECT * FROM live_trades WHERE version_id=?", con, params=(version_id,))
    if rows.empty:
        return {**metrics.summarize(pd.DataFrame({"is_open": [], "ret_pct": [], "exit_date": [], "bars": []})), "open": 0, "shown": False}
    rows["is_open"] = rows["is_open"].astype(bool)
    rows["exit_date"] = pd.to_datetime(rows["exit_date"]); rows["entry_date"] = pd.to_datetime(rows["entry_date"])
    rows["bars"] = (rows["exit_date"].fillna(pd.Timestamp.today()) - rows["entry_date"]).dt.days.clip(lower=1)
    s = metrics.summarize(rows)
    shown = s["trades"] >= metrics.MIN_TRADES_SHOWN
    if not shown:
        s["win_rate"] = None
    return {**s, "open": int(rows["is_open"].sum()), "shown": shown}
