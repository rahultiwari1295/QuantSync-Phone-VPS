import json, os, sqlite3, sys, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from common import corporate_actions as ca

def test_parser():
    P = ca.parse_price_factor
    assert P("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == 0.2
    assert P("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share") == 0.1
    assert P("Face Value Split (Sub-Division) - From Rs.5/- Per Share To Rs.1/- Per Share") == 0.2
    assert P("Bonus 1:1") == 0.5
    assert P("Bonus 3:2") == 0.4
    assert abs(P("Bonus 1:2") - 2/3) < 1e-6
    assert P("Bonus 1:1 And Dividend - Rs 2 Per Share") == 0.5
    assert P("Consolidation Of Shares From Rs 1/- Per Share To Rs 10/- Per Share") == 10.0
    for s in ["Dividend - Rs 5 Per Share", "Rights 1:5 @ Premium Rs 90/-", "Annual General Meeting", "Buy Back", ""]:
        assert P(s) is None, s

def test_apply_on_real_store(tmp_path):
    src = os.environ.get("HIST_DB")
    if not src: return
    db = tmp_path/"h.db"; shutil.copy(src, db)
    con = sqlite3.connect(db)
    before = json.loads(con.execute("select rawCandles from StockHistory where symbol='INDIAGLYCO'").fetchone()[0])
    con.close()
    ca.record_actions([
        {"symbol":"INDIAGLYCO","ex_date":"2026-09-02","subject":"Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"},
        {"symbol":"TCS","ex_date":"2026-09-02","subject":"Bonus 1:1"},   # no such gap -> must NOT be applied
    ], db_path=str(db))
    r = ca.apply_pending(str(db))
    assert [a["symbol"] for a in r["applied"]] == ["INDIAGLYCO"]
    assert [u["symbol"] for u in r["unverified"]] == ["TCS"]
    con = sqlite3.connect(db)
    after = json.loads(con.execute("select rawCandles from StockHistory where symbol='INDIAGLYCO'").fetchone()[0])
    i = next(k for k,b in enumerate(after) if b["t"]>="2026-09-02")
    ratio = after[i]["o"]/after[i-1]["c"]
    assert 0.85 < ratio < 1.15, ratio            # gap gone
    assert after[i:] == before[i:]               # post-ex bars untouched
    assert ca.apply_pending(str(db))["applied"] == []   # idempotent
    assert con.execute("select count(*) from StockHistory_backup where reason like 'corp_action%'").fetchone()[0] == 1
