"""
common/radar_feed.py — the ONE place that decides what appears on Radar.

Before this, the same lists were built twice (server.py endpoint and the
snapshot_manager RAM snapshot) with different thresholds, and the snapshot
path — which is what the app actually received — skipped every gate.
Both now call build_breakouts().

Gates applied to every list:
  * price data passed the quality gate (data_quarantine = 0)
  * DNA stance is BULLISH/ACCUMULATION and the DNA action is not an AVOID
  * room to target (target_1 > price) and a valid stop (stop_loss < price)
  * "near" means within 3% of the trigger — same as the NEAR_BREAKOUT stage
  * pattern candidates (V3) must be fresh (≤ 6% past the pivot, not EXTENDED)
Each row carries `pattern_edge`: the pattern's fair-test record vs a random day.
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DB = os.path.join(_ROOT, "Data", "stock_dna_cache.db")
MASTER_DB = os.path.join(_ROOT, "Data", "quantsync_master.db")

NEAR_PCT = 3.0
MAX_PAST_PIVOT_PCT = 6.0
# Radar and DNA are separate products. DNA (fundamentals / quality) is shown as an info chip on a Radar row but does
# not decide whether a chart setup appears. Price-data quality is still a hard gate.
REQUIRE_DNA = False


def pattern_key(name: Optional[str]) -> str:
    """'Inverse Head & Shoulders · Reversal Trough' -> 'INVERSE_HEAD_AND_SHOULDERS'."""
    base = (name or "").split("·")[0].strip().upper().replace("&", "AND")
    return re.sub(r"[^A-Z0-9]+", "_", base).strip("_")


def load_pattern_edge(master_db: str = MASTER_DB) -> Dict[Any, Dict[str, Any]]:
    out: Dict[Any, Dict[str, Any]] = {}
    try:
        c = sqlite3.connect(master_db)
        c.row_factory = sqlite3.Row
        for r in c.execute("SELECT * FROM pattern_edge"):
            out[(r["pattern_type"], r["timeframe"])] = dict(r)
        c.close()
    except sqlite3.Error:
        pass
    return out


def best_scan_date(cur, min_stocks: int = 800) -> Optional[str]:
    row = cur.execute("SELECT scan_date, COUNT(*) c FROM stock_screener_rows GROUP BY scan_date HAVING c >= ? "
                      "ORDER BY scan_date DESC LIMIT 1", (min_stocks,)).fetchone()
    if row and row[0]:
        return row[0]
    row = cur.execute("SELECT MAX(scan_date) FROM stock_screener_rows").fetchone()
    return row[0] if row else None


def tradeable_candidate(c: Dict[str, Any]) -> bool:
    p = float(c.get("curr_price") or 0)
    if p <= 0 or c.get("data_quarantine"):
        return False
    if str(c.get("execution_state") or "").upper() == "EXTENDED":
        return False
    ruled = bool(c.get("trade_state"))            # state already judged by common.trade_rules (age, grade, edge)
    dfp = c.get("diff_from_pivot_pct")
    if not ruled and dfp is not None and float(dfp) > MAX_PAST_PIVOT_PCT:
        return False
    t1, sl = c.get("target_1"), c.get("stop_loss")
    if t1 is not None and float(t1) <= p:
        return False
    if sl is not None and float(sl) >= p:
        return False
    if REQUIRE_DNA:
        stance = str(c.get("stance") or "").upper()
        if stance and stance not in ("BULLISH", "ACCUMULATION"):
            return False
        if "AVOID" in str(c.get("action") or "").upper():
            return False
    return True


def build_breakouts(scan_date: Optional[str] = None, limit: int = 50, cache_db: str = CACHE_DB,
                    master_db: str = MASTER_DB, v3_candidates=None) -> Dict[str, Any]:
    if not os.path.exists(cache_db):
        return {"scan_date": None, "success": [], "near_breakout": [], "retest": [], "failed": []}
    conn = sqlite3.connect(cache_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        scan_date = scan_date or best_scan_date(cur)
        cols = {r[1] for r in cur.execute("PRAGMA table_info(stock_screener_rows)")}
        clean = "COALESCE(data_quarantine, 0) = 0" if "data_quarantine" in cols else "1=1"
        dna_ok = ("stance IN ('BULLISH', 'ACCUMULATION') "
                  "AND (action IS NULL OR (action NOT LIKE '%AVOID%' AND action NOT IN ('NO_TRADE_WAIT', 'TAKE_PROFIT_TIGHTEN_STOP')))") if REQUIRE_DNA else "1=1"
        has_rules = "trade_state" in cols
        room = "(target_1 IS NULL OR target_1 > curr_price) AND (stop_loss IS NULL OR stop_loss < curr_price)"

        def q(cond: str, order: str) -> List[Dict[str, Any]]:
            sql = f"SELECT * FROM stock_screener_rows WHERE scan_date = ? AND {clean} AND ({cond}) ORDER BY {order} LIMIT ?"
            return [dict(r) for r in cur.execute(sql, (scan_date, limit)).fetchall()]

        if has_rules:
            # states come from common.trade_rules (tested definitions); no SQL guesses
            success = q(f"""trade_action = 'BUY' AND trade_state IN ('BREAKOUT', 'RETEST_HOLD') AND {room}""",
                        "COALESCE(days_since_breakout, 99) ASC, dna_score DESC")
            near = q(f"""trade_state IN ('NEAR', 'ATTEMPT') AND breakout_level > 0""",
                     "((breakout_level - curr_price) / breakout_level) ASC, dna_score DESC")
            retest = q(f"""trade_state IN ('RETEST_WATCH', 'RETEST_HOLD')""",
                       "COALESCE(days_since_breakout, 999) ASC, dna_score DESC")
            failed = q("trade_state = 'FAILED'", "COALESCE(days_since_breakout, 999) ASC, dna_score DESC")
        else:
            success = q(f"""breakout_status IN ('SUCCESS', 'POST_BREAKOUT') AND breakout_level > 0
                            AND curr_price >= breakout_level * 0.98
                            AND curr_price <= breakout_level * {1 + MAX_PAST_PIVOT_PCT / 100}
                            AND {dna_ok} AND {room}""",
                        "COALESCE(days_since_breakout, 99) ASC, dna_score DESC")
            near = q(f"""breakout_level > 0 AND curr_price < breakout_level
                         AND ((breakout_level - curr_price) / breakout_level * 100.0) <= {NEAR_PCT}
                         AND {dna_ok} AND (dna_score IS NULL OR dna_score >= 50)""",
                     "((breakout_level - curr_price) / breakout_level) ASC, dna_score DESC")
            retest = q(f"""breakout_status IN ('SUCCESS', 'POST_BREAKOUT') AND days_since_breakout BETWEEN 1 AND 30
                           AND curr_price BETWEEN breakout_level * 0.985 AND breakout_level * 1.025
                           AND rsi BETWEEN 50.0 AND 75.0 AND ema_alignment = 'BULL_STACK'
                           AND {dna_ok} AND {room}""",
                       "COALESCE(days_since_breakout, 999) ASC, dna_score DESC")
            failed = q("breakout_status = 'FAILED'", "COALESCE(days_since_breakout, 999) ASC, dna_score DESC")
    finally:
        conn.close()

    import json as _json
    for lst in (success, near, retest, failed):
        for r in lst:
            r["timeframe"] = r.get("timeframe") or "DAILY"
            tj = r.pop("trade_json", None)
            if tj:
                try:
                    r["trade_call"] = _json.loads(tj)
                except ValueError:
                    pass

    if v3_candidates is not None:
        have = {r["symbol"] for r in success + near + retest}
        for target, stage in ((success, "SUCCESS"), (near, "NEAR_BREAKOUT"), (retest, "RETEST")):
            extra = [c for c in v3_candidates(target_stage=stage) if tradeable_candidate(c) and c.get("symbol") not in have]
            target.extend(extra[:max(0, limit - len(target))])

    edge = load_pattern_edge(master_db)
    for lst in (success, near, retest):
        for r in lst:
            r["pattern_edge"] = edge.get((pattern_key(r.get("pattern_type")), (r.get("timeframe") or "DAILY").upper()))

    return {"scan_date": scan_date, "success": success, "near_breakout": near, "retest": retest, "failed": failed}
