"""Strategy lifecycle, job handlers and the live tracker."""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from pathlib import Path

import numpy as np
import pandas as pd

from . import FSV, ai, jobs, metrics, pipeline, simulator, store
from .config import Config
from .db import jdump, new_id, tx
from .rules import normalise, normalise_exit, rule_hash

log = logging.getLogger("qslab")


def create_strategy(con, cfg, uid: str | None, name: str, tf: str, entry: dict, exit_: dict,
                    kind="user", visibility="private", copied_from: str | None = None, pick: dict | None = None) -> dict:
    if tf not in ("1D", "1W"):
        raise ValueError("tf must be 1D or 1W")
    entry, exit_ = normalise(entry), normalise_exit(exit_)
    name = " ".join(name.split())[:60]
    if len(name) < 2:
        raise ValueError("name too short")
    sid, vid = new_id("stg"), new_id("ver")
    live_from = (store.meta(cfg, tf) or {}).get("as_of") or time.strftime("%Y-%m-%d")
    pick_json = jdump(pick) if pick else None
    with tx(con):
        if uid:
            n = con.execute("SELECT count(*) FROM strategies WHERE owner_uid=? AND archived=0", (uid,)).fetchone()[0]
            if n >= 100:
                raise ValueError("strategy limit (100) reached")
        con.execute("INSERT INTO strategies(id,owner_uid,kind,name,tf,visibility,current_version,copied_from,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, uid, kind, name, tf, visibility, vid, copied_from, time.time(), time.time()))
        con.execute("INSERT INTO strategy_versions(id,strategy_id,n,entry_json,exit_json,rule_hash,fsv,live_from,pick_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (vid, sid, 1, jdump(entry), jdump(exit_), rule_hash(entry, tf), FSV, live_from, pick_json, time.time()))
    return {"id": sid, "version_id": vid}


def new_version(con, sid: str, **changes) -> str:
    """Any change to rules, gate or exit choice starts a new version, which restarts the live record."""
    cur = con.execute("SELECT v.*, s.tf FROM strategy_versions v JOIN strategies s ON s.current_version=v.id WHERE s.id=?", (sid,)).fetchone()
    d = dict(cur)
    d.update(changes)
    vid = new_id("ver")
    with tx(con):
        con.execute("""INSERT INTO strategy_versions(id,strategy_id,n,entry_json,exit_json,rule_hash,fsv,ai_enhancement_id,ai_gate,exit_choice,live_from,pick_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (vid, sid, cur["n"] + 1, d["entry_json"], d["exit_json"], d["rule_hash"], FSV, d["ai_enhancement_id"],
                     d["ai_gate"], d["exit_choice"], d.get("live_from_new") or time.strftime("%Y-%m-%d"), d.get("pick_json"), time.time()))
        con.execute("UPDATE strategies SET current_version=?, updated_at=? WHERE id=?", (vid, time.time(), sid))
    return vid


def _ctx(con, sid):
    r = con.execute("SELECT s.tf, v.* FROM strategies s JOIN strategy_versions v ON v.id=s.current_version WHERE s.id=?", (sid,)).fetchone()
    return r, json.loads(r["entry_json"]), json.loads(r["exit_json"])


def _gate_key(x, gate, enh_id, pick=None) -> str:
    base = pipeline.exit_hash(x)
    if pick:
        base += f"|pk_{pipeline.canonical(pick)}"
    if gate and enh_id:
        base += f"|g{gate}|{enh_id}"
    return base


def run_backtest(cfg, con, e, x, tf, version_id="", prog=lambda pct, st: None, pick: dict | None = None) -> dict:
    """Backtest with a cache keyed on (rule, exit, data as-of)."""
    key = (rule_hash(e, tf), _gate_key(x, None, None, pick), f'{(store.meta(cfg, tf) or {}).get("as_of", "")}|m{metrics.METRICS_V}')
    hit = con.execute("SELECT summary_json FROM backtest_runs WHERE rule_hash=? AND exit_hash=? AND as_of=?", key).fetchone()
    if hit:
        return json.loads(hit[0])
    res = pipeline.backtest(cfg, e, x, tf, prog, pick=pick)
    con.execute("INSERT INTO backtest_runs(id,version_id,rule_hash,exit_hash,as_of,summary_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("bt"), version_id, *key, jdump(res), time.time()))
    return res


def cached_backtest(con, tf, e, x, gate=None, enh_id=None, pick: dict | None = None) -> dict | None:
    """Newest past test computed with the current metric definitions (older ones are recomputed, never shown)."""
    r = con.execute("SELECT summary_json FROM backtest_runs WHERE rule_hash=? AND exit_hash=? AND as_of LIKE ? ORDER BY created_at DESC LIMIT 1",
                    (rule_hash(e, tf), _gate_key(x, gate, enh_id, pick), f"%|m{metrics.METRICS_V}")).fetchone()
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


def ensure_backtest(con, s, v, variant: str | None = None) -> dict:
    """Cached past test, or a queued system job computing it (one per version). Never needs a signed-in user."""
    e, x = json.loads(v["entry_json"]), json.loads(v["exit_json"])
    pick = json.loads(v["pick_json"]) if ("pick_json" in v.keys() and v["pick_json"]) else None
    if variant == "plain":
        gate, enh = None, None
    elif variant == "ai":
        enh_row = con.execute("SELECT * FROM ai_enhancements WHERE version_id=? OR id=? ORDER BY created_at DESC LIMIT 1",
                              (v["id"], v["ai_enhancement_id"])).fetchone()
        if not enh_row:
            return {"status": "none", "error": "No AI enhancement found."}
        if enh_row["status"] in ("queued", "running"):
            return {"status": "running", "job_id": enh_row["id"]}
        if enh_row["status"] == "failed":
            return {"status": "failed", "error": enh_row["reason"] or "AI check failed."}
        res_json = json.loads(enh_row["result_json"]) if enh_row["result_json"] else {}
        gate = res_json.get("gate", v["ai_gate"] or 60)
        enh = enh_row["id"]
    else:
        gate, enh = (v["ai_gate"], v["ai_enhancement_id"]) if v["ai_gate"] and v["ai_enhancement_id"] else (None, None)
    res = cached_backtest(con, s["tf"], e, x, gate, enh, pick=pick)
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
    if pick:
        p["pick"] = pick
    if gate:
        p.update(gate=gate, enhancement_id=enh)
    jid = jobs.enqueue(con, kind, None, p, queue_cap=10_000)
    return {"status": "running", "job_id": jid}


# ---- job handlers -----------------------------------------------------------------------------
def handle(cfg: Config, con: sqlite3.Connection, job: sqlite3.Row) -> dict:
    p = json.loads(job["payload_json"])
    prog = lambda pct, st: jobs.progress(con, job["id"], pct, st)
    if job["kind"] == "backtest":
        return run_backtest(cfg, con, normalise(p["entry"]), normalise_exit(p.get("exit")), p["tf"], p.get("version_id") or "", prog, pick=p.get("pick"))
    if job["kind"] == "gated_backtest":
        return run_gated_backtest(cfg, con, normalise(p["entry"]), normalise_exit(p.get("exit")), p["tf"], int(p["gate"]), p["enhancement_id"],
                                  p.get("version_id") or "", prog)
    if job["kind"] == "enhance":
        return _enhance(cfg, con, p, prog)
    if job["kind"] == "enhance_exit":
        return _enhance_exit(cfg, con, p, prog)
    if job["kind"] == "ai_check":
        return _ai_check(cfg, con, p, prog)
    raise ValueError(f"unknown job kind {job['kind']}")


def _enhance(cfg, con, p, prog):
    r, entry, exit_ = _ctx(con, p["strategy_id"])
    pick = json.loads(r["pick_json"]) if ("pick_json" in r.keys() and r["pick_json"]) else None
    aid = p["enhancement_id"]
    as_of = (store.meta(cfg, r["tf"]) or {}).get("as_of", "")
    res = pipeline.enhance(cfg, entry, exit_, r["tf"], prog, pick=pick)
    public = {k: v for k, v in res.items() if not k.startswith("_")}
    if res["status"] == "done":
        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        res["_scorer"].save(cfg.models_dir / aid)
    con.execute("""UPDATE ai_enhancements 
                   SET status=?, candidate=?, model_path=?, result_json=?, reason=?, 
                       kind='user', protocol='v2', verdict=?, as_of=?, finished_at=? 
                   WHERE id=?""",
                (res["status"], res.get("candidate"), str(cfg.models_dir / aid) if res["status"] == "done" else None,
                 jdump(public), res.get("reason"), res.get("verdict"), as_of, time.time(), aid))
    return {"enhancement_id": aid, "status": res["status"]}


def _ai_check(cfg: Config, con: sqlite3.Connection, p: dict, prog) -> dict:
    r, entry, exit_ = _ctx(con, p["strategy_id"])
    as_of = (store.meta(cfg, r["tf"]) or {}).get("as_of", "")

    if p.get("rescore_only"):
        row = con.execute("""SELECT id, model_path, result_json FROM ai_enhancements 
                             WHERE version_id=? AND kind='system' AND status='done' 
                             ORDER BY created_at DESC LIMIT 1""", (r["id"],)).fetchone()
        if not row or not row["model_path"]:
            return {"status": "none", "reason": "No existing system model to rescore."}
        try:
            scorer = ai.Scorer.load(row["model_path"])
        except Exception as e:
            return {"status": "failed", "reason": f"Failed to load model: {e}"}

        trades, sg = pipeline.run_trades(cfg, entry, exit_, r["tf"])
        ds = pipeline._dataset(trades, sg)
        ds = ai.prepare_dataset(ds, cfg)
        blind_yr = getattr(cfg, "ai_blind_year", 2026)
        blind_ds = ds[ds.entry_date >= pd.Timestamp(f"{blind_yr}-01-01")]

        res_data = json.loads(row["result_json"]) if row["result_json"] else {}
        gate = res_data.get("gate", 60)

        if len(blind_ds) and scorer:
            sc_blind = pd.Series(scorer.score(blind_ds), index=blind_ds.index)
            res_data["blind"] = {
                "year": blind_yr,
                "plain": ai.plain_stats(blind_ds),
                "ai": ai.gate_stats(blind_ds, sc_blind, gate),
                "z": ai._z(blind_ds, sc_blind, gate),
                "note": "Too few 2026 trades to confirm" if len(blind_ds) < 20 else None,
            }
            res_data["checked_at"] = int(time.time())
            con.execute("UPDATE ai_enhancements SET result_json=?, as_of=?, finished_at=? WHERE id=?",
                        (jdump(res_data), as_of, time.time(), row["id"]))
        return {"status": "done", "rescored": True, "enhancement_id": row["id"]}

    # Full AI check
    prog(10, "Running trade simulation")
    pick = json.loads(r["pick_json"]) if ("pick_json" in r.keys() and r["pick_json"]) else None
    res = pipeline.enhance(cfg, entry, exit_, r["tf"], prog, pick=pick)
    aid = p.get("enhancement_id") or new_id("enh")
    public = {k: v for k, v in res.items() if not k.startswith("_")}
    model_path = str(cfg.models_dir / aid) if res["status"] == "done" and res.get("_scorer") else None
    if model_path:
        cfg.models_dir.mkdir(parents=True, exist_ok=True)
        res["_scorer"].save(model_path)

    verdict = res.get("verdict", "no_edge" if res["status"] == "done" else "failed")
    reason = res.get("reason", "")

    # Live re-check (Phase 4.7)
    six_months_ago = str((pd.Timestamp.now() - pd.DateOffset(months=6)).date())
    lt = con.execute("""SELECT variant, ret_pct FROM live_trades 
                        WHERE version_id=? AND is_open=0 AND exit_date >= ?""",
                     (r["id"], six_months_ago)).fetchall()
    ai_rets = [row["ret_pct"] for row in lt if row["variant"] == "ai" and row["ret_pct"] is not None]
    plain_rets = [row["ret_pct"] for row in lt if row["variant"] == "plain" and row["ret_pct"] is not None]
    if len(ai_rets) >= 20:
        if np.mean(ai_rets) < np.mean(plain_rets if plain_rets else [0.0]):
            verdict = "paused_live"
            reason = "AI filter paused: it stopped beating the plain rules in live tracking."
            public["verdict"] = verdict
            public["reason"] = reason

    existing = con.execute("SELECT id FROM ai_enhancements WHERE id=?", (aid,)).fetchone()
    if existing:
        con.execute("""UPDATE ai_enhancements SET status=?, kind='system', protocol='v2', verdict=?,
                       as_of=?, candidate=?, model_path=?, result_json=?, reason=?, finished_at=? WHERE id=?""",
                    (res["status"], verdict, as_of, res.get("candidate"), model_path,
                     jdump(public), reason, time.time(), aid))
    else:
        con.execute("""INSERT INTO ai_enhancements(id, version_id, status, kind, protocol, verdict, as_of,
                       candidate, model_path, result_json, reason, created_at, finished_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, r["id"], res["status"], "system", "v2", verdict, as_of,
                     res.get("candidate"), model_path, jdump(public), reason, time.time(), time.time()))

    # If verdict is improved, run and cache gated backtest
    if verdict == "improved" and res.get("gate"):
        prog(85, "Caching gated backtest")
        try:
            run_gated_backtest(cfg, con, entry, exit_, r["tf"], int(res["gate"]), aid, r["id"], prog)
        except Exception:
            log.exception("failed to cache gated backtest for %s", r["id"])

    return {"enhancement_id": aid, "status": res["status"], "verdict": verdict}


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
    for v in con.execute("SELECT v.*, s.tf, s.kind FROM strategy_versions v JOIN strategies s ON s.current_version=v.id WHERE s.archived=0"):
        tf, entry, exit_ = v["tf"], json.loads(v["entry_json"]), json.loads(v["exit_json"])
        if v["exit_choice"] == "ai" and v["ai_enhancement_id"]:
            e = con.execute("SELECT exit_result_json FROM ai_enhancements WHERE id=?", (v["ai_enhancement_id"],)).fetchone()
            ex = json.loads(e[0]).get("best_exit") if e and e[0] else None
            exit_ = ex or exit_
        pick = json.loads(v["pick_json"]) if ("pick_json" in v.keys() and v["pick_json"]) else None
        try:
            start = str((pd.Timestamp(v["live_from"]) - pd.Timedelta(days=200 if tf == "1D" else 900)).date()) if v["live_from"] else None
            trades, sg = pipeline.run_trades(cfg, entry, exit_, tf, start=start, pick=pick)
        except Exception:
            log.exception("live update failed for %s", v["id"]); continue

        # 1. Plain trades
        plain_trades = trades[trades["signal_date"] >= pd.Timestamp(v["live_from"])] if len(trades) else trades
        with tx(con):
            for t in plain_trades.itertuples():
                con.execute("""INSERT INTO live_trades(version_id,variant,symbol,signal_date,entry_date,entry_price,exit_date,exit_price,exit_reason,ret_pct,is_open,score)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(version_id,variant,symbol,signal_date) DO UPDATE SET
                    exit_date=excluded.exit_date, exit_price=excluded.exit_price, exit_reason=excluded.exit_reason,
                    ret_pct=excluded.ret_pct, is_open=excluded.is_open, score=excluded.score""",
                    (v["id"], "plain", t.symbol, str(t.signal_date.date()), str(t.entry_date.date()), t.entry_price,
                     None if t.is_open else str(t.exit_date.date()), t.exit_price, t.exit_reason, t.ret_pct, int(t.is_open),
                     getattr(t, "score", None)))
                n += 1

        # 2. Check for AI variant:
        ai_path = None
        ai_gate = None
        if v["ai_gate"] is not None and v["ai_enhancement_id"]:
            row = con.execute("SELECT model_path FROM ai_enhancements WHERE id=?", (v["ai_enhancement_id"],)).fetchone()
            if row and row[0]:
                ai_path, ai_gate = row[0], v["ai_gate"]
        elif v["kind"] in ("flagship", "library", "system"):
            sys_enh = con.execute("""SELECT id, model_path, verdict, result_json FROM ai_enhancements 
                                     WHERE version_id=? AND kind='system' AND status='done' 
                                     ORDER BY created_at DESC LIMIT 1""", (v["id"],)).fetchone()
            if sys_enh and sys_enh["verdict"] == "improved" and sys_enh["model_path"]:
                res_obj = json.loads(sys_enh["result_json"]) if sys_enh["result_json"] else {}
                ai_path = sys_enh["model_path"]
                ai_gate = res_obj.get("gate", 60)

        if ai_path and ai_gate is not None and len(trades):
            ai_trades = _apply_gate_path(cfg, ai_path, ai_gate, trades, sg)
            ai_trades = ai_trades[ai_trades["signal_date"] >= pd.Timestamp(v["live_from"])] if len(ai_trades) else ai_trades
            with tx(con):
                for t in ai_trades.itertuples():
                    con.execute("""INSERT INTO live_trades(version_id,variant,symbol,signal_date,entry_date,entry_price,exit_date,exit_price,exit_reason,ret_pct,is_open,score)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(version_id,variant,symbol,signal_date) DO UPDATE SET
                        exit_date=excluded.exit_date, exit_price=excluded.exit_price, exit_reason=excluded.exit_reason,
                        ret_pct=excluded.ret_pct, is_open=excluded.is_open, score=excluded.score""",
                        (v["id"], "ai", t.symbol, str(t.signal_date.date()), str(t.entry_date.date()), t.entry_price,
                         None if t.is_open else str(t.exit_date.date()), t.exit_price, t.exit_reason, t.ret_pct, int(t.is_open),
                         getattr(t, "score", None)))
                    n += 1
    return n


def _apply_gate_path(cfg, model_path: str, gate: int, trades: pd.DataFrame, sg: pd.DataFrame) -> pd.DataFrame:
    p = Path(model_path)
    if not p.exists() or trades.empty:
        return trades.iloc[0:0]
    sc = ai.Scorer.load(model_path)
    f = sg.rename(columns={"date": "signal_date"}).drop(columns=["close"])
    m = trades.merge(f, on=["symbol", "signal_date"], how="left")
    m = ai.prepare_dataset(m, cfg)
    m["score"] = sc.score(m)
    return m[m["score"] >= gate].reset_index(drop=True)


def _apply_gate(cfg, con, v, trades, sg):
    row = con.execute("SELECT model_path FROM ai_enhancements WHERE id=?", (v["ai_enhancement_id"],)).fetchone()
    if not row or not row[0]:
        return trades.iloc[0:0]
    return _apply_gate_path(cfg, row[0], v["ai_gate"], trades, sg)


def live_stats(con, version_id: str, variant: str = "plain") -> dict:
    rows = pd.read_sql_query("SELECT * FROM live_trades WHERE version_id=? AND variant=?", con, params=(version_id, variant))
    if rows.empty and variant == "plain":
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
