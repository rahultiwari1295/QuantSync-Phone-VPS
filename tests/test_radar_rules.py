"""Radar lists must come from the tested trade states, not from price-distance guesses."""
import json
import sqlite3

from common import radar_feed as rf


def make_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE stock_screener_rows (symbol TEXT, scan_date TEXT, curr_price REAL, breakout_level REAL,
        breakout_status TEXT, days_since_breakout INTEGER, dna_score REAL, target_1 REAL, stop_loss REAL, data_quarantine INTEGER,
        stance TEXT, action TEXT, rsi REAL, ema_alignment TEXT, trade_state TEXT, trade_action TEXT, trade_json TEXT)""")
    for r in rows:
        c.execute("INSERT INTO stock_screener_rows VALUES (:symbol,'2026-10-09',:curr_price,:breakout_level,:breakout_status,:days,:dna,"
                  ":target_1,:stop_loss,:q,:stance,:action,60,'BULL_STACK',:trade_state,:trade_action,:trade_json)", r)
    c.commit(); c.close()


def row(sym, state, t_action, **kw):
    d = dict(symbol=sym, curr_price=110.0, breakout_level=100.0, breakout_status="SUCCESS", days=1, dna=70.0, target_1=130.0,
             stop_loss=90.0, q=0, stance="BULLISH", action="BUY", trade_state=state, trade_action=t_action,
             trade_json=json.dumps({"state": state, "action": t_action, "grade": "A", "confidence": {"label": "Validated edge"}}))
    d.update(kw)
    return d


def test_lists_follow_trade_states(tmp_path):
    db = str(tmp_path / "c.db")
    make_db(db, [
        row("BUYME", "BREAKOUT", "BUY"),
        row("UNPROVEN", "BREAKOUT", "WATCH"),                    # valid breakout without a validated edge -> not in success
        row("WEAK", "WEAK_BREAKOUT", "AVOID"),
        row("NEARME", "NEAR", "WATCH", curr_price=98.0),
        row("RTWATCH", "RETEST_WATCH", "WATCH"),
        row("DEAD", "FAILED", "AVOID", breakout_status="FAILED"),
        row("OLD", "EXTENDED", "AVOID"),
    ])
    out = rf.build_breakouts(scan_date="2026-10-09", cache_db=db, master_db=str(tmp_path / "none.db"))
    names = lambda k: [r["symbol"] for r in out[k]]
    assert names("success") == ["BUYME"]
    assert names("near_breakout") == ["NEARME"]
    assert names("retest") == ["RTWATCH"]
    assert names("failed") == ["DEAD"]
    assert out["success"][0]["trade_call"]["grade"] == "A" and "trade_json" not in out["success"][0]


def test_dna_does_not_gate_radar_when_rules_exist(tmp_path):
    db = str(tmp_path / "c.db")
    make_db(db, [row("BEARDNA", "BREAKOUT", "BUY", stance="BEARISH", action="AVOID_X")])
    out = rf.build_breakouts(scan_date="2026-10-09", cache_db=db, master_db=str(tmp_path / "none.db"))
    assert [r["symbol"] for r in out["success"]] == ["BEARDNA"]          # DNA is an info chip, not a gate


def test_price_data_quarantine_still_blocks(tmp_path):
    db = str(tmp_path / "c.db")
    make_db(db, [row("BADDATA", "BREAKOUT", "BUY", q=1)])
    out = rf.build_breakouts(scan_date="2026-10-09", cache_db=db, master_db=str(tmp_path / "none.db"))
    assert out["success"] == []


def test_v3_candidate_filter_ignores_dna_but_keeps_data_gate():
    ok = dict(curr_price=100, trade_state="BREAKOUT", stance="BEARISH", action="AVOID", diff_from_pivot_pct=9.0, target_1=130, stop_loss=90)
    assert rf.tradeable_candidate(ok) is True
    assert rf.tradeable_candidate({**ok, "data_quarantine": 1}) is False
    assert rf.tradeable_candidate({**ok, "stop_loss": 105}) is False
