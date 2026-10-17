"""Offline tests for intel/: collectors on fixture payloads shaped like the NSE/BSE responses, classifier, scoring,
reader number checks, fields, briefs and the HTTP API. Run: python -m pytest tests/test_intel.py -q"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("QS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QS_INTEL_DB", str(tmp_path / "intel.db"))
    monkeypatch.setenv("QS_HIST_DB", str(tmp_path / "hist.db"))
    monkeypatch.setenv("QS_COLLECTOR_DB", str(tmp_path / "collectors.db"))
    monkeypatch.setenv("QS_LOCK_DIR", str(tmp_path))
    c = sqlite3.connect(tmp_path / "collectors.db")
    c.executescript("""
      CREATE TABLE listing(symbol TEXT PRIMARY KEY, isin TEXT, name TEXT);
      INSERT INTO listing VALUES ('TCS','INE467B01029','Tata Consultancy Services Limited'),('HAL','INE066F01020','Hindustan Aeronautics Limited'),
                                 ('DIXON','INE935N01020','Dixon Technologies (India) Limited');
      CREATE TABLE ratios_daily(symbol TEXT PRIMARY KEY, as_of TEXT, computed_at TEXT, price REAL, ttm_revenue_cr REAL, ttm_net_profit_cr REAL,
        ttm_eps REAL, market_cap_cr REAL, pe REAL, pb REAL, roe_pct REAL, roce_pct REAL, debt_to_equity REAL, net_debt_cr REAL,
        revenue_yoy_pct REAL, profit_yoy_pct REAL, opm_ttm_pct REAL, quarters_used INTEGER);
      INSERT INTO ratios_daily(symbol, market_cap_cr, pe, pb, roe_pct, roce_pct, debt_to_equity, revenue_yoy_pct, profit_yoy_pct, opm_ttm_pct)
        VALUES ('HAL', 300000, 35, 9, 26, 32, 0.0, 12, 22, 31), ('TCS', 1300000, 28, 12, 50, 62, 0.1, 6, 8, 26);
      CREATE TABLE shareholding(symbol TEXT, quarter_end TEXT, promoter_pct REAL, pledged_pct REAL, fii_pct REAL, dii_pct REAL, public_pct REAL, source TEXT, updated_at TEXT);
      INSERT INTO shareholding VALUES ('HAL','2026-03-31',71.6,0,12.0,10.0,6.4,'x',''),('HAL','2026-06-30',71.6,0,13.1,10.4,4.9,'x','');
    """)
    c.commit(); c.close()
    import intel.db as d
    importlib.reload(d)
    for m in ("intel.classify", "intel.score", "intel.net", "intel.collect", "intel.fields", "intel.brief", "intel.reader", "intel.api"):
        importlib.reload(importlib.import_module(m))
    d.migrate()
    return tmp_path


NOW = datetime.now()
D = lambda days=0: (NOW - timedelta(days=days)).strftime("%d-%b-%Y")


class FakeNet:
    def __init__(self):
        self.calls = []

    def json(self, url, site="web", **kw):
        self.calls.append(url)
        if "corporate-announcements" in url:
            return [{"symbol": "HAL", "desc": "Bagging/Receiving of orders/contracts",
                     "attchmntText": "HAL has received an order worth Rs 2,500 crore from the Ministry of Defence",
                     "attchmntFile": "https://nsearchives.nseindia.com/corporate/HAL_1.pdf", "sm_name": "Hindustan Aeronautics Limited",
                     "sm_isin": "INE066F01020", "sort_date": NOW.strftime("%Y-%m-%d %H:%M:%S")},
                    {"symbol": "TCS", "desc": "Resignation of Statutory Auditor", "attchmntText": "Auditor resigned citing pre-occupation",
                     "attchmntFile": "https://nsearchives.nseindia.com/corporate/TCS_2.pdf", "sm_name": "Tata Consultancy Services Limited",
                     "sort_date": NOW.strftime("%Y-%m-%d %H:%M:%S")}]
        if "AnnSubCategoryGetData" in url:
            return {"Table": [{"SLONGNAME": "Dixon Technologies (India) Ltd", "NEWSSUB": "Dixon Technologies - Board Meeting Intimation for Financial Results",
                               "HEADLINE": "Board meeting to consider quarterly results", "DT_TM": NOW.strftime("%Y-%m-%dT%H:%M:%S"),
                               "ATTACHMENTNAME": "abc.pdf"}]} if "pageno=1&" in url else {"Table": []}
        if "corporate-board-meetings" in url:
            return [{"bm_symbol": "DIXON", "bm_date": (NOW + timedelta(days=5)).strftime("%d-%b-%Y"), "bm_purpose": "Financial Results",
                     "bm_desc": "To consider and approve the financial results", "bm_timestamp": D(1) + " 10:00:00"}]
        if "corporates-pit" in url:
            return {"data": [{"symbol": "HAL", "company": "Hindustan Aeronautics", "acqName": "Mr A Director", "personCategory": "Promoter Group",
                              "secAcq": "100000", "secVal": "450000000", "tdpTransactionType": "Buy", "acqMode": "Market Purchase",
                              "befAcqSharesPer": "71.50", "afterAcqSharesPer": "71.60", "acqfromDt": D(3), "acqtoDt": D(3), "date": D(1) + " 18:00"}]}
        if "corporate-sast-reg29" in url:
            return {"data": []}
        if "corporate-pledgedata" in url:
            return {"data": [{"symbol": "TCS", "comName": "TCS", "percSharesPledged": "0.00", "broadcastDt": D(0)}]}
        if "fiidiiTradeReact" in url:
            return [{"category": "FII/FPI *", "date": D(1), "buyValue": "12000", "sellValue": "13500", "netValue": "-1500"},
                    {"category": "DII **", "date": D(1), "buyValue": "11000", "sellValue": "9000", "netValue": "2000"}]
        if "finance/chart" in url:
            return {"chart": {"result": [{"meta": {"regularMarketPrice": 101.0, "chartPreviousClose": 100.0},
                                          "indicators": {"quote": [{"close": [99.0, 100.0, 101.0]}]}}]}}
        raise AssertionError(url)

    def csv(self, url, site="web"):
        if "bulk.csv" in url:
            return [{"Date": D(1), "Symbol": "DIXON", "Security Name": "Dixon Tech", "Client Name": "BIG FUND LTD", "Buy/Sell": "BUY",
                     "Quantity Traded": "250000", "Trade Price / Wght. Avg. Price": "15388.00"}]
        return [{"Date": D(1), "Symbol": "TCS", "Security Name": "TCS", "Client Name": "SELLER LLP", "Buy/Sell": "SELL",
                 "Quantity Traded": "500000", "Trade Price / Wght. Avg. Price": "4100.5"}]

    def rss(self, url):
        return [{"title": "HAL shares rise after defence order", "link": "https://example.com/a", "summary": "Hindustan Aeronautics order", "published": ""},
                {"title": "US stocks close higher as yields ease", "link": "https://example.com/b", "summary": "", "published": ""}]


def test_classify_and_money():
    from intel import classify as C
    assert C.classify("Resignation of Statutory Auditor")[0] == "auditor_exit"
    assert C.classify("Bagging/Receiving of orders", "order worth Rs 500 crore")[0] == "order_win"
    assert C.classify("Outcome of Board Meeting - Financial Results")[0] == "results"
    assert C.classify("Qualified Institutions Placement")[0] == "fund_raise"
    assert C.money_cr("order worth Rs 2,500 crore and Rs 40 lakh") == 2500
    assert C.money_cr("USD 10 million") is None
    assert abs(C.money_cr("Rs. 75 lakh") - 0.75) < 1e-9


def test_score_bounds():
    from intel.score import impact
    s, p = impact(50, 2500, 300000, True, False)
    assert 50 < s <= 100 and "size" in p
    assert impact(90, 1e6, 10, True, True, None, True)[0] == 100
    assert impact(20, prior_move_pct=30)[0] == 5


def test_collect_all(env, monkeypatch):
    from intel import collect, db
    net = FakeNet()
    monkeypatch.setattr(collect, "Net", lambda: net)
    out = collect.run()
    assert out["nse_announcements"] == 2 and out["deals"] == 2 and out["insider"] == 1
    assert out["board_meetings"] == 1 and out["fii_dii"] == 2 and out["global_markets"] == len(collect.MARKETS)
    assert collect.run(["filings"])["nse_announcements"] == 0  # idempotent
    con = db.connect()
    ev = {r["type"]: dict(r) for r in con.execute("SELECT * FROM events")}
    assert ev["order_win"]["symbol"] == "HAL" and ev["order_win"]["ai_status"] == "pending" and ev["order_win"]["impact"] > 50
    assert ev["auditor_exit"]["sentiment"] == "negative"
    assert ev["insider_buy"]["impact"] >= 45 and "bought" in ev["insider_buy"]["summary"]
    assert {"bulk_buy", "bulk_sell", "results_date"} <= set(ev)
    bse = con.execute("SELECT symbol FROM events WHERE source='BSE'").fetchone()
    assert bse["symbol"] == "DIXON"  # name matched to symbol
    assert con.execute("SELECT value_cr FROM insider_trades").fetchone()[0] == pytest.approx(45.0)
    assert con.execute("SELECT count(*) FROM meetings WHERE is_results=1").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM events WHERE category='news' AND symbol='HAL'").fetchone()[0] == 1


def test_source_failure_is_isolated(env, monkeypatch):
    from intel import collect
    from intel.net import Blocked
    net = FakeNet()
    real = net.json
    net.json = lambda url, site="web", **k: (_ for _ in ()).throw(Blocked("403")) if "announcements" in url else real(url, site)
    monkeypatch.setattr(collect, "Net", lambda: net)
    out = collect.run(["filings", "meetings"])
    assert str(out["nse_announcements"]).startswith("failed") and out["board_meetings"] == 1


def test_fields_brief_api(env, monkeypatch):
    from intel import api, brief, collect, db, fields
    monkeypatch.setattr(collect, "Net", FakeNet)
    collect.run()
    r = fields.run()
    assert r["facts_daily"] == 2 and r["facts_owner"] == 2
    con = db.connect()
    hal = dict(con.execute("SELECT * FROM facts_owner WHERE symbol='HAL' ORDER BY quarter_end DESC").fetchone())
    assert hal["fii_change_qoq"] == pytest.approx(1.1) and hal["known_at"] == "2026-07-21"
    assert con.execute("SELECT earnings_yield FROM facts_daily WHERE symbol='TCS'").fetchone()[0] == pytest.approx(100 / 28)
    monkeypatch.setattr(brief, "send", lambda b: False)
    b = brief.build("morning")
    assert b["top"] and b["markets"] and b["calendar"] and b["insiders"]
    assert "Morning brief" in brief.telegram_text(b)
    brief.run("evening")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(api.router)
    c = TestClient(app)
    feed = c.get("/api/intel2/events?days=30").json()
    assert len(feed["items"]) >= 8 and all("label" in x for x in feed["items"])
    assert c.get("/api/intel2/events?symbol=HAL&type=order_win").json()["items"][0]["numbers"]["_score"]["base"] == 50
    st = c.get("/api/intel2/stock/HAL").json()
    assert st["timeline"] and st["insiders"] and st["owners"] and st["facts"]["roce"] == 32
    assert c.get("/api/intel2/brief?kind=evening").json()["kind"] == "evening"
    assert c.get("/api/intel2/deals?days=10").json()["items"]
    assert c.get("/api/intel2/insiders").json()["items"]
    assert c.get("/api/intel2/calendar").json()["items"][0]["symbol"] == "DIXON"
    assert len(c.get("/api/intel2/markets").json()["items"]) == len(collect.MARKETS)
    assert c.get("/api/intel2/top?hours=48").json()[0]["impact"] >= 50
    s = c.get("/api/intel2/status").json()
    assert s["sources"] and s["runs"]


def test_reader_verify_and_run(env, monkeypatch):
    from intel import collect, db, reader
    monkeypatch.setattr(collect, "Net", FakeNet)
    collect.run(["filings"])
    out = reader._verify({"summary": "x", "sentiment": "great", "key_numbers": [{"label": "Order", "value": "Rs 2,500 crore"},
                                                                                 {"label": "Fake", "value": "9,999"}]},
                         "received an order worth Rs 2,500 crore")
    assert [k["label"] for k in out["key_numbers"]] == ["Order"] and out["sentiment"] == "neutral"
    monkeypatch.setattr(reader, "available", lambda: True)
    monkeypatch.setattr(reader, "pdf_text", lambda b: "HAL has received an order worth Rs 2,500 crore from MoD. " * 20)

    class N:
        def __init__(self, timeout=40): pass
        def bytes(self, url, site="web"): return b"%PDF"
    monkeypatch.setattr(reader, "Net", N)
    monkeypatch.setattr(reader, "ask", lambda prompt, heavy=False, timeout=900: {
        "summary": "HAL won a ₹2,500 crore defence order.", "what_changed": "Order book grows", "sentiment": "positive",
        "key_numbers": [{"label": "Order value", "value": "2,500 crore"}], "confidence": 0.8})
    r = reader.run(budget_s=60)
    assert r["read"] == 2
    con = db.connect()
    row = con.execute("SELECT ai_status, summary, ai_json FROM events WHERE type='order_win'").fetchone()
    assert row["ai_status"] == "done" and "2,500" in row["summary"] and json.loads(row["ai_json"])["key_numbers"]


def test_run_cli(env, monkeypatch, capsys):
    from intel import collect, run
    monkeypatch.setattr(collect, "Net", FakeNet)
    import intel.brief as b
    monkeypatch.setattr(b, "send", lambda x: False)
    assert run.main(["morning"]) == 0
    assert '"brief"' in capsys.readouterr().out
    assert run.main(["nope"]) == 2
