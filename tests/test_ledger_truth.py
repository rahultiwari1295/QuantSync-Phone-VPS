import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from common import ledger_truth as lt
def T(ed, xd, ep, xp, pnl, sid="A"): return {"entry_date":ed,"exit_date":xd,"entry_price":ep,"exit_price":xp,"net_pnl":pnl,"strategy_id":sid,"holding_days":3}
def test_summary_no_placeholders():
    s = lt.summarize([T("2026-01-01","2026-01-05",100,110,100)])
    assert s["profitFactor"] is None and s["winRate"] == 100.0
    assert lt.summarize([])["winRate"] is None
def test_origin_and_months():
    tr = lt.track_record([T("2026-09-01","2026-09-10",100,90,-10), T("2026-09-09","2026-09-20",100,120,20), T("2026-10-01","2026-10-05",100,95,-5)])
    assert tr["live"]["trades"] == 2 and tr["simulated"]["trades"] == 1
    m = {x["month"]: x for x in tr["byMonth"]}
    assert m["2026-09"]["origin"] == "MIXED" and m["2026-09"]["liveTrades"] == 1 and m["2026-10"]["origin"] == "LIVE"
def test_check_fill():
    bar = {"l": 100, "h": 110}
    assert lt.check_fill(105, bar) == "OK" and lt.check_fill(120, bar) == "OUT_OF_RANGE" and lt.check_fill(1, None) == "NO_CANDLE"
