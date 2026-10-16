import json, os, sqlite3, sys, shutil, types
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
# stub broker so tests never hit the network
stub = types.ModuleType("common.broker"); stub.get_session=lambda force=False:"t"; stub._CANDLE_URL="x"; stub._post=lambda *a,**k:{}
sys.modules["common.broker"] = stub
import common.candle_sync as cs
from common import data_quality as dq

def test_merge_overwrites_provisional_and_drops_holiday():
    existing = [
        {"t":"2026-10-01","o":1,"h":2,"l":1,"c":1.5,"v":100},
        {"t":"2026-10-02","o":1,"h":2,"l":1,"c":1.6,"v":0,"p":1},   # holiday ghost
        {"t":"2026-10-05","o":1,"h":2,"l":1,"c":1.7,"v":0,"p":1},   # sweep snapshot
    ]
    new = [["2026-10-05T00:00:00+05:30",1.1,2.2,0.9,1.9,5000],["2026-10-06T00:00:00+05:30",1.9,2.0,1.8,1.95,4000],
           ["2026-10-07T00:00:00+05:30",2,2,2,2,1]]  # beyond cutoff
    out = cs.merge_official_candles(existing, new, "2026-10-06")
    assert [b["t"] for b in out] == ["2026-10-01","2026-10-05","2026-10-06"]
    assert out[1]["c"] == 1.9 and out[1]["v"] == 5000 and "p" not in out[1]

def test_needs_refresh():
    good=[{"t":d,"o":1,"h":1,"l":1,"c":1,"v":5} for d in ["2026-10-01","2026-10-05","2026-10-06"]]
    assert not cs._needs_refresh(good, ["2026-10-01","2026-10-05","2026-10-06"])
    bad=[dict(b) for b in good]; bad[-1]["v"]=0
    assert cs._needs_refresh(bad, ["2026-10-01","2026-10-05","2026-10-06"])

def test_sync_end_to_end(tmp_path, monkeypatch):
    db = tmp_path/"h.db"; con = sqlite3.connect(db)
    con.execute("create table StockHistory(token,symbol,rawCandles,candleCount,isV3Eligible,isAlgoEligible)")
    rows = {"AAA":[{"t":"2026-10-05","o":10,"h":11,"l":9,"c":10.5,"v":0,"p":1}],
            "BBB":[{"t":"2026-10-01","o":5,"h":5,"l":5,"c":5,"v":9},{"t":"2026-10-05","o":5,"h":5,"l":5,"c":5,"v":9},{"t":"2026-10-06","o":5,"h":5,"l":5,"c":5,"v":9}]}
    for i,(k,v) in enumerate(rows.items()):
        con.execute("insert into StockHistory values(?,?,?,?,1,1)",(str(i),k,json.dumps(v),len(v)))
    con.commit(); con.close()
    monkeypatch.setattr(cs,"DB_PATH",str(db)); calls=[]
    def fake(tok, exchange, from_date, to_date, max_retries=3):
        calls.append((tok,from_date)); return [["2026-10-05",10,12,9,11.5,700],["2026-10-06",11.5,12,11,11.8,800]]
    monkeypatch.setattr(cs,"fetch_symbol_candles",fake); monkeypatch.setattr(cs.time,"sleep",lambda s:None)
    r = cs.sync_candles_incremental(cutoff_date="2026-10-06", rebuild_bundles_after=False)
    assert r["updated_symbols"]==1 and r["skipped_symbols"]==1, r      # BBB already official
    got = json.loads(sqlite3.connect(db).execute("select rawCandles from StockHistory where symbol='AAA'").fetchone()[0])
    assert got[-1]=={"t":"2026-10-06","o":11.5,"h":12.0,"l":11.0,"c":11.8,"v":800} and got[0]["v"]==700
    q = dq.assess_session("2026-10-06", str(db)); assert q["status"]=="OK", q

def test_gate_catches_real_incident():
    p = os.environ.get("HIST_DB")
    if not p: return
    q = dq.assess_session("2026-10-07", p)
    print(q)
    assert q["status"] == "FAILED"          # the real 7 Oct data: zero-volume sweep bars
    q2 = dq.assess_session("2026-09-23", p)
    assert q2["status"] in ("OK","DEGRADED"), q2  # the last session before the incident (bars may be older than last 5)
