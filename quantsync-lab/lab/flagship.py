"""QuantSync's two flagship strategies, built from the engine's signals (see lab/signals.py).

AI Alpha   : momentum breakouts: RS leader, Stage-2 base breakout, power gap.
AI Pattern : chart-structure breakouts: apex base, high tight flag.
Both are plain rule JSON, so anyone can copy and edit them. 'AI' means the score gate chosen by the walk-forward
enhancement run (only kept when it shows a real edge on 2023+ data)."""
from __future__ import annotations

import json
import time

from . import service
from .db import jdump, new_id, tx
from .rules import normalise, normalise_exit, rule_hash

_sig = lambda k: {"l": {"f": k}, "op": "is_true"}
FLAGSHIPS = {
    "AI Alpha": {"tf": "1D",
                 "entry": {"v": 1, "any": [_sig("sig_rs_leader"), _sig("sig_stage2_base"), _sig("sig_power_gap")]},
                 "exit": {"v": 1, "any": [{"type": "stop_pct", "v": 6}, {"type": "trail_atr", "v": 3}, {"type": "time", "v": 40}]}},
    "AI Pattern": {"tf": "1D",
                   "entry": {"v": 1, "any": [_sig("sig_apex_base"), _sig("sig_high_tight_flag")]},
                   "exit": {"v": 1, "any": [{"type": "stop_pct", "v": 5.5}, {"type": "target_pct", "v": 12}, {"type": "time", "v": 40}]}},
}


def ensure(cfg, con, name: str) -> str:
    """Create the flagship if missing; start a new version if its rules changed. Returns the strategy id."""
    spec = FLAGSHIPS[name]
    entry, exit_ = normalise(spec["entry"]), normalise_exit(spec["exit"])
    row = con.execute("SELECT id, current_version FROM strategies WHERE kind='flagship' AND name=? AND archived=0", (name,)).fetchone()
    if not row:
        return service.create_strategy(con, cfg, None, name, spec["tf"], entry, exit_, kind="flagship", visibility="public")["id"]
    cur = con.execute("SELECT entry_json, exit_json FROM strategy_versions WHERE id=?", (row["current_version"],)).fetchone()
    if json.loads(cur["entry_json"]) != entry or json.loads(cur["exit_json"]) != exit_:
        service.new_version(con, row["id"], entry_json=jdump(entry), exit_json=jdump(exit_), rule_hash=rule_hash(entry, spec["tf"]),
                            ai_enhancement_id=None, ai_gate=None)
    return row["id"]


def pick_gate(res: dict, min_trades: int = 40) -> int | None:
    """A fixed, conservative rule (not tuned on the reported years): score 60, else 50, and only when the
    walk-forward run found a real edge and the gate keeps enough trades with a positive lift."""
    if not res.get("edge_found"):
        return None
    for g in (60, 50):
        st = (res.get("gates") or {}).get(str(g)) or {}
        if (st.get("All") or {}).get("trades", 0) >= min_trades and (st.get("lift") or 0) > 0:
            return g
    return None


def build(cfg, con, name: str, ai: bool = True, log=print) -> dict:
    """Seed -> past test -> (optional) AI enhancement and gate. Safe to re-run."""
    spec = FLAGSHIPS[name]
    sid = ensure(cfg, con, name)
    v = con.execute("SELECT v.* FROM strategies s JOIN strategy_versions v ON v.id=s.current_version WHERE s.id=?", (sid,)).fetchone()
    entry, exit_ = json.loads(v["entry_json"]), json.loads(v["exit_json"])
    bt = service.run_backtest(cfg, con, entry, exit_, spec["tf"], v["id"])
    out = {"name": name, "id": sid, "past_test": bt["summary"].get("All"), "gate": v["ai_gate"], "ai": None}
    log(f"{name}: past test {json.dumps(out['past_test'])}")
    if ai and not v["ai_gate"]:
        aid = new_id("ai")
        with tx(con):
            con.execute("INSERT INTO ai_enhancements(id,version_id,status,created_at) VALUES(?,?,?,?)", (aid, v["id"], "running", time.time()))
        service._enhance(cfg, con, {"strategy_id": sid, "enhancement_id": aid}, lambda p, s: log(f"  {name}: {p}% {s}"))
        e = con.execute("SELECT status, candidate, reason, result_json FROM ai_enhancements WHERE id=?", (aid,)).fetchone()
        res = json.loads(e["result_json"]) if e["result_json"] else {}
        out["ai"] = {"status": e["status"], "candidate": e["candidate"], "reason": e["reason"], "edge_found": res.get("edge_found")}
        gate = pick_gate(res)
        out["ai"]["gate_chosen"] = gate
        if e["status"] == "done" and gate:
            service.new_version(con, sid, ai_enhancement_id=aid, ai_gate=gate)
            out["gate"] = gate
        log(f"  {name}: AI {out['ai']}")
    return out
