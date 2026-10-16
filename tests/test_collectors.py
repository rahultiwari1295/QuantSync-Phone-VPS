import json, os, sqlite3, sys, time
from datetime import date, datetime, timedelta
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.core import db, parsers as P, validators as V, pipeline as PL, ratios as R, fetcher as F
from collectors import registry
from collectors.jobs import listing as J_listing, corp_actions as J_ca, results as J_res, news as J_news

HIST_DDL = """
CREATE TABLE StockHistory(token TEXT, symbol TEXT PRIMARY KEY, rawCandles TEXT, candleCount INT);
CREATE TABLE CompanyMaster(symbol TEXT PRIMARY KEY, next_results_date TEXT, market_cap_cr REAL);
CREATE TABLE StockTechnicalsDaily(symbol TEXT PRIMARY KEY, cmp REAL);
CREATE TABLE CorporateEvents(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, event_date TEXT NOT NULL, event_type TEXT NOT NULL,
  ex_date TEXT, record_date TEXT, amount REAL, quantity INTEGER, deal_value_cr REAL, counterparty TEXT, buy_sell TEXT,
  source_event_id TEXT, source TEXT, details_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(source, source_event_id));
CREATE TABLE CompanyFundamentalsHistory(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, quarter TEXT, period_type TEXT,
  period_end_date TEXT, reported_date TEXT, available_date TEXT, snapshot_date TEXT, source TEXT, source_url TEXT, source_fetched_at TEXT,
  basis TEXT, basis_changed INTEGER DEFAULT 0,
  revenue_cr REAL, gross_profit_cr REAL, op_profit_cr REAL, opm_pct REAL, net_profit_cr REAL, eps REAL, interest_expense_cr REAL,
  equity_capital_cr REAL, reserves_cr REAL, borrowings_cr REAL, cash_cr REAL, UNIQUE(symbol, period_end_date, period_type));
"""

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "COLLECTOR_DB", str(tmp_path / "c.db"))
    monkeypatch.setattr(db, "HIST_DB", str(tmp_path / "h.db"))
    monkeypatch.setattr(db, "FUND_CACHE_DB", str(tmp_path / "f.db"))
    h = sqlite3.connect(db.HIST_DB); h.executescript(HIST_DDL)
    for s in ("AAA", "BBB", "NEWCO"):
        h.execute("INSERT INTO StockHistory(symbol) VALUES (?)", (s,)); h.execute("INSERT INTO CompanyMaster(symbol) VALUES (?)", (s,))
    h.commit(); h.close()
    db.init().close()
    return tmp_path

def hist():
    c = sqlite3.connect(db.HIST_DB); c.row_factory = sqlite3.Row; return c

# ── parsers ────────────────────────────────────────────────────────────────
def test_to_num():
    assert P.to_num("1,234.5") == 1234.5 and P.to_num("(12.5)") == -12.5 and P.to_num("52%") == 52.0
    assert P.to_num("-") is None and P.to_num("") is None and P.to_num(None) is None and P.to_num("abc") is None

def test_dividend_and_actions():
    assert P.parse_dividend_amount("Dividend - Rs 3.75 Per Share") == 3.75
    assert P.parse_dividend_amount("Final Dividend - Rs.2/- Per Share") == 2.0
    assert P.parse_dividend_amount("Dividend - Rs 2 Per Share And Special Dividend - Rs 1 Per Share") == 3.0
    assert P.parse_dividend_amount("Bonus 1:1") is None
    assert P.classify_action("Face Value Split (Sub-Division) - From Rs 10/- To Rs 2/-") == "SPLIT"
    assert P.classify_action("Bonus 1:1") == "BONUS" and P.classify_action("Annual General Meeting/Dividend - Rs 2") == "DIVIDEND"
    assert P.classify_action(None) == "OTHER"

def test_equity_list():
    csv = "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\nAAA,Aaa Ltd,EQ,03-FEB-2017,10,1,INE000A01010,10\n,x,EQ,,,,,\nBBB,Bbb,EQ,01-JAN-2020,2,1,INE000B01010,2\n"
    r = P.parse_equity_list_csv(csv)
    assert [x["symbol"] for x in r] == ["AAA", "BBB"] and r[0]["listing_date"] == date(2017, 2, 3) and r[1]["face_value"] == 2.0

def test_announcements_and_categories():
    rows = [{"symbol": "AAA", "desc": "Outcome of Board Meeting - Financial Results", "an_dt": "10-Oct-2026 18:33:12", "attchmntText": "", "seq_id": 9, "sm_isin": "I"},
            {"symbol": "AAA", "desc": "", "an_dt": "10-Oct-2026 18:33:12"}, {"symbol": "", "desc": "x", "an_dt": "10-Oct-2026 18:33:12"}]
    out = P.parse_nse_announcements(rows)
    assert len(out) == 1 and out[0]["category"] == "RESULTS" and out[0]["ts"] == "2026-10-10T18:33:12" and len(out[0]["hash"]) == 40
    assert P.categorize_announcement("Dividend declared", "") == "DIVIDEND"
    assert P.categorize_announcement("Receipt of order worth Rs 100 cr", "") == "ORDER_WIN"
    assert P.categorize_announcement("SEBI order against promoter", "") == "LEGAL"
    assert P.categorize_announcement("Board meeting intimation", "") == "BOARD_MEETING"

def test_nse_results_lakhs_to_crore():
    payload = {"resCmpData": [{"re_to_dt": "30-JUN-2026", "re_from_dt": "01-APR-2026", "re_con_type": "Consolidated", "re_res_type": "Audited",
        "re_net_sale": "100000", "re_tot_exp": "80000", "re_int_new": "2000", "re_depr_and_amort": "3000", "re_oth_inc_new": "1000",
        "re_pro_loss_bef_tax": "21000", "re_net_profit": "15000", "re_basic_eps_for_cont_dic_opr": "7.5"}, {"re_to_dt": None}]}
    r = P.parse_nse_results(payload)
    assert len(r) == 1 and r[0]["revenue_cr"] == 1000.0 and r[0]["op_profit_cr"] == 250.0 and r[0]["opm_pct"] == 25.0
    assert r[0]["consolidated"] and r[0]["audited"] and r[0]["eps"] == 7.5 and r[0]["net_profit_cr"] == 150.0

def test_screener_parsers():
    blob = {"source_url": "https://www.screener.in/company/AAA/consolidated/", "quarterly_performance": [
        {"quarter": "Sep 2023", "sales_cr": "362", "expenses_cr": "173", "op_profit_cr": "189", "opm_pct": "52%", "other_income_cr": "24",
         "interest_cr": "9", "depreciation_cr": "23", "pbt_cr": "182", "net_profit_cr": "118", "eps": "2.97"}, {"quarter": "bad"}],
        "shareholding_pattern": {"Promoters": {"holding_pct": 50, "quarter": "Jun 2026"}, "FIIs": {"holding_pct": 20, "quarter": "Jun 2026"},
                                 "DIIs": {"holding_pct": 10, "quarter": "Jun 2026"}, "Public": {"holding_pct": 20, "quarter": "Jun 2026"}}}
    q = P.parse_screener_quarters(blob)
    assert len(q) == 1 and q[0]["period_end"] == date(2023, 9, 30) and q[0]["consolidated"] and q[0]["opm_pct"] == 52.0
    sh = P.parse_screener_shareholding(blob)
    assert sh["quarter_end"] == date(2026, 6, 30) and sh["promoter_pct"] == 50.0

# ── validators ─────────────────────────────────────────────────────────────
def test_validators():
    assert V.after_listing(date(2015, 3, 31), date(2017, 2, 3)) and not V.after_listing(date(2017, 3, 31), date(2017, 2, 3))
    assert V.after_listing(date(2017, 1, 1), date(2017, 2, 3), grace_days=60) is None
    assert V.not_future(date(2030, 1, 1), date(2026, 10, 10))
    assert V.pl_arithmetic(362, 173, 189) is None and V.pl_arithmetic(362, 173, 50)
    assert V.holding_sum({"a": 50, "b": 50}) is None and V.holding_sum({"a": 50, "b": 30})
    assert V.pledge_ok(50, 60) and V.pledge_ok(50, 10) is None
    assert V.sudden_jump(100, 5000) and V.sudden_jump(100, 120) is None and V.sudden_jump(500, -300)
    assert V.cross_source(100, 101) is None and V.cross_source(100, 130)

def test_ratios():
    qs = [{"period_end": date(2025, 9, 30) + timedelta(days=91 * i), "revenue_cr": 100 + i, "op_profit_cr": 20, "net_profit_cr": 10, "eps": 2.0, "pbt_cr": 14, "interest_cr": 1} for i in range(5)]
    r = R.compute_ratios(qs, 200.0, 10.0, 500.0, 100.0, 40.0)
    assert r["ttm_eps"] == 8.0 and r["pe"] == 25.0 and r["market_cap_cr"] == 2000.0 and r["pb"] == 4.0 and r["quarters_used"] == 4
    assert r["debt_to_equity"] == 0.2 and r["net_debt_cr"] == 60.0 and r["revenue_yoy_pct"] is not None
    gap = [qs[0], qs[1], qs[3], qs[4]]
    assert R.compute_ratios(gap, 100, 1, 1, 0, 0)["pe"] is None and R.compute_ratios([], None, None, None, None, None)["pe"] is None

# ── fetcher ────────────────────────────────────────────────────────────────
def spec(job="t"):
    return F.FetchSpec(job, "src", "k", [F.Attempt("direct", "nse", "u1"), F.Attempt("chromium", "nse", "u1"), F.Attempt("relay", "generic")])

def test_fallback_chain_and_block_detection(env):
    calls = []
    def direct(a, s): calls.append("direct"); return 403, "text/html", b"Access Denied"
    def chrom(a, s): calls.append("chromium"); return 200, "application/json", b'[{"a":1}]'
    r = F.Fetcher({"direct": direct, "chromium": chrom}, sleep=False).fetch(spec())
    assert r.ok and r.route == "chromium:nse" and r.data == [{"a": 1}] and calls == ["direct", "chromium"]
    assert r.tried[0].startswith("direct:nse:fail")

def test_html_instead_of_json_is_failure(env):
    r = F.Fetcher({"direct": lambda a, s: (200, "text/html", b"<html>hi</html>"), "chromium": lambda a, s: (500, "", b"x"),
                   "relay": lambda a, s: (404, "", b"")}, sleep=False).fetch(spec())
    assert not r.ok

def test_route_pauses_after_three_failures(env):
    f = F.Fetcher({"direct": lambda a, s: (403, "", b"no"), "chromium": lambda a, s: (200, "", b"[1]")}, sleep=False)
    for _ in range(3):
        f.fetch(spec())
    r = f.fetch(spec())
    assert "direct:nse:paused" in r.tried and r.ok
    with db.session() as c:
        assert c.execute("SELECT paused_until FROM source_health WHERE route='direct:nse'").fetchone()[0] > time.time()

def test_relay_route_uses_pushed_body(env):
    with db.session() as c:
        c.execute("INSERT INTO relay_inbox(ts,source,key,status,body) VALUES (?,?,?,?,?)", (db.now_iso(), "src", "k", 200, b'{"ok":true}'))
    r = F.Fetcher({"direct": lambda a, s: (403, "", b""), "chromium": lambda a, s: (403, "", b"")}, sleep=False).fetch(spec())
    assert r.ok and r.route == "relay" and r.data == {"ok": True}

# ── pipeline ───────────────────────────────────────────────────────────────
def q(end, rev=1000.0, np_=100.0, **kw):
    return {"period_end": end, "revenue_cr": rev, "expenses_cr": rev * 0.8, "op_profit_cr": rev * 0.2, "net_profit_cr": np_, "eps": 1.0,
            "other_income_cr": None, "interest_cr": None, "depreciation_cr": None, "pbt_cr": None, **kw}

def test_listing_gate_blocks_pre_ipo_quarters(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "NEWCO", [q(date(2024, 3, 31)), q(date(2025, 12, 31)), q(date(2026, 6, 30))], "SCREENER_QUARTERS", "u",
                            date(2025, 6, 1), today=date(2026, 10, 10))
        h.commit()
        got = [r[0] for r in h.execute("SELECT period_end_date FROM CompanyFundamentalsHistory WHERE symbol='NEWCO' ORDER BY 1")]
        assert got == ["2025-12-31", "2026-06-30"] and run.rejected == 1
        assert c.execute("SELECT COUNT(*) FROM validation_log WHERE rule='after_listing'").fetchone()[0] == 1

def test_future_and_arithmetic_rejected(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        bad = q(date(2026, 6, 30), op_profit_cr=999.0)
        PL.promote_quarters(c, h, run, "AAA", [q(date(2027, 3, 31)), bad], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        assert h.execute("SELECT COUNT(*) FROM CompanyFundamentalsHistory").fetchone()[0] == 0 and run.rejected == 2

def test_big_jump_is_held_for_review_then_approved(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "AAA", [q(date(2025, 12, 31), rev=980, np_=95), q(date(2026, 3, 31), rev=1000, np_=100)], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        PL.promote_quarters(c, h, run, "AAA", [q(date(2026, 6, 30), rev=50000, np_=9000)], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        h.commit(); c.commit()
        assert h.execute("SELECT COUNT(*) FROM CompanyFundamentalsHistory WHERE symbol='AAA'").fetchone()[0] == 2
        rv = c.execute("SELECT * FROM review_queue WHERE status='PENDING'").fetchone()
        assert rv and "jump" in rv["reason"]
    assert PL.approve_review(rv["id"], "me")["ok"]
    with hist() as h:
        assert h.execute("SELECT revenue_cr FROM CompanyFundamentalsHistory WHERE period_end_date='2026-06-30'").fetchone()[0] == 50000

def test_screener_never_overwrites_official(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "AAA", [q(date(2026, 6, 30), rev=1000)], "NSE_RESULTS", "u", None, today=date(2026, 10, 10), source_kind="nse")
        PL.promote_quarters(c, h, run, "AAA", [q(date(2026, 6, 30), rev=1100)], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        h.commit()
        r = h.execute("SELECT revenue_cr, source FROM CompanyFundamentalsHistory").fetchone()
        assert r["revenue_cr"] == 1000 and r["source"] == "NSE_RESULTS"

def test_official_overwrites_screener_and_logs_disagreement(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "AAA", [q(date(2026, 6, 30), rev=1000)], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        PL.promote_quarters(c, h, run, "AAA", [q(date(2026, 6, 30), rev=1200)], "NSE_RESULTS", "u", None, today=date(2026, 10, 10), source_kind="nse")
        h.commit(); c.commit()
        assert h.execute("SELECT revenue_cr, source FROM CompanyFundamentalsHistory").fetchone()[:] == (1200, "NSE_RESULTS")
        assert c.execute("SELECT COUNT(*) FROM validation_log WHERE rule LIKE 'screener_vs_official%'").fetchone()[0] >= 1

def test_actions_respect_listing_and_sanity(env):
    rows = [{"symbol": "AAA", "kind": "DIVIDEND", "ex_date": date(2018, 6, 1), "amount": 5.0, "record_date": None, "face_value": 10, "subject": "Dividend - Rs 5"},
            {"symbol": "AAA", "kind": "DIVIDEND", "ex_date": date(2026, 6, 1), "amount": 5.0, "record_date": None, "face_value": 10, "subject": "Dividend - Rs 5"},
            {"symbol": "AAA", "kind": "DIVIDEND", "ex_date": date(2026, 7, 1), "amount": 99999.0, "record_date": None, "face_value": 10, "subject": "x"},
            {"symbol": "AAA", "kind": "SPLIT", "ex_date": date(2026, 8, 1), "amount": None, "record_date": None, "face_value": 2, "subject": "Split"}]
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_actions(c, h, run, rows, {"AAA": date(2020, 1, 1)}, "NSE_CORP_ACTION")
        h.commit(); c.commit()
        assert h.execute("SELECT COUNT(*) FROM CorporateEvents").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM dividends_clean").fetchone()[0] == 1 and run.rejected == 2

def test_announcement_dedupe(env):
    rows = P.parse_nse_announcements([{"symbol": "AAA", "desc": "Dividend", "an_dt": "10-Oct-2026 10:00:00", "seq_id": 1}] * 2)
    with PL.JobRun("t") as run, db.session() as c:
        PL.promote_announcements(c, run, rows, "NSE", {"AAA"})
        c.commit()
        assert c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0] == 1

# ── jobs end to end (fake network) ─────────────────────────────────────────
def test_listing_job_refuses_truncated_file(env):
    csv = "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\nAAA,A,EQ,03-FEB-2017,10,1,INE1,10\n"
    f = F.Fetcher({"direct": lambda a, s: (200, "text/csv", csv.encode())}, sleep=False)
    with PL.JobRun("listing") as run:
        J_listing.run(run, f)
    assert run.status == "FAILED"
    with db.session() as c:
        assert c.execute("SELECT COUNT(*) FROM listing").fetchone()[0] == 0

def test_listing_job_saves_and_sets_master(env):
    lines = ["SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE"]
    lines += [f"S{i},N,EQ,01-JAN-2010,10,1,INE{i:05d},10" for i in range(1600)]
    lines += ["NEWCO,New Co,EQ,05-JUN-2025,10,1,INE9,10", "AAA,A,EQ,03-FEB-2017,10,1,INE1,10"]
    f = F.Fetcher({"direct": lambda a, s: (200, "text/csv", "\n".join(lines).encode())}, sleep=False)
    with PL.JobRun("listing") as run:
        J_listing.run(run, f)
    with hist() as h:
        assert h.execute("SELECT listing_date FROM CompanyMaster WHERE symbol='NEWCO'").fetchone()[0] == "2025-06-05"
    with db.session() as c:
        assert PL.listing_dates(c)["AAA"] == date(2017, 2, 3)

def test_results_from_cache_applies_listing_gate(env):
    with db.session() as c:
        c.execute("INSERT INTO listing(symbol,listing_date) VALUES ('NEWCO','2025-06-05')")
    blob = {"source_url": "https://www.screener.in/company/NEWCO/consolidated/", "quarterly_performance": [
        {"quarter": "Mar 2024", "sales_cr": "100", "expenses_cr": "80", "op_profit_cr": "20", "net_profit_cr": "10", "eps": "1"},
        {"quarter": "Jun 2026", "sales_cr": "120", "expenses_cr": "96", "op_profit_cr": "24", "net_profit_cr": "12", "eps": "1.2"}]}
    fc = sqlite3.connect(db.FUND_CACHE_DB); fc.execute("CREATE TABLE stock_fundamentals(symbol TEXT PRIMARY KEY, data_json TEXT, updated_at TEXT)")
    fc.execute("INSERT INTO stock_fundamentals VALUES ('NEWCO', ?, '')", (json.dumps(blob),)); fc.commit(); fc.close()
    with PL.JobRun("results") as run:
        J_res.run(run, F.Fetcher(sleep=False), mode="from_cache", symbols="NEWCO")
    with hist() as h:
        assert [r[0] for r in h.execute("SELECT period_end_date FROM CompanyFundamentalsHistory")] == ["2026-06-30"]

def test_news_job(env):
    ann = [{"symbol": "AAA", "desc": "Financial Results for Q1", "an_dt": "10-Oct-2026 10:00:00", "seq_id": 5}]
    mt = [{"bm_symbol": "BBB", "bm_date": (date.today() + timedelta(days=5)).strftime("%d-%b-%Y"), "bm_purpose": "Financial Results", "bm_desc": ""}]
    def direct(a, s):
        return 200, "application/json", json.dumps(ann if "announcements" in a.url else mt).encode()
    with PL.JobRun("news") as run:
        J_news.run(run, F.Fetcher({"direct": direct}, sleep=False))
    with db.session() as c:
        assert c.execute("SELECT category FROM announcements").fetchone()[0] == "RESULTS"
        assert c.execute("SELECT is_results FROM board_meetings").fetchone()[0] == 1
    with hist() as h:
        assert h.execute("SELECT next_results_date FROM CompanyMaster WHERE symbol='BBB'").fetchone()[0]

# ── registry ───────────────────────────────────────────────────────────────
def test_registry_schedule():
    s = {"at": "19:00", "days": [1, 2, 3, 4, 5]}
    sat = datetime(2026, 10, 10, 20, 0)
    mon = datetime(2026, 10, 12, 19, 30)
    assert not registry.is_due(s, sat, None) and registry.is_due(s, mon, None)
    assert not registry.is_due(s, mon, datetime(2026, 10, 12, 19, 5))
    n = {"every_min": 15, "window": ["09:00", "18:00"], "days": [1, 2, 3, 4, 5]}
    assert registry.is_due(n, datetime(2026, 10, 12, 10, 0), datetime(2026, 10, 12, 9, 40)) and not registry.is_due(n, datetime(2026, 10, 12, 19, 0), None)
    assert registry.in_season(date(2026, 10, 20)) and not registry.in_season(date(2026, 9, 1))

def test_spike_does_not_flag_following_normal_quarters(env):
    rows = [q(date(2025, 6, 30), rev=1000), q(date(2025, 9, 30), rev=1010), q(date(2025, 12, 31), rev=60000),
            q(date(2026, 3, 31), rev=1020), q(date(2026, 6, 30), rev=1030)]
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "AAA", rows, "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        h.commit()
        assert [r[0] for r in h.execute("SELECT period_end_date FROM CompanyFundamentalsHistory ORDER BY 1")] == ["2025-06-30", "2025-09-30", "2026-03-31", "2026-06-30"]
        assert c.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 1

def test_negative_revenue_is_accepted_with_note(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "AAA", [q(date(2026, 6, 30), rev=-7.0, np_=-8.0, expenses_cr=-5.6, op_profit_cr=-1.4)], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        assert h.execute("SELECT COUNT(*) FROM CompanyFundamentalsHistory").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM validation_log WHERE rule='negative_revenue' AND severity='INFO'").fetchone()[0] == 1


def test_future_period_rejected(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        future_q = q(date(2026, 9, 30), rev=1000.0, np_=100.0)
        PL.promote_quarters(c, h, run, "AAA", [future_q], "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 11))
        assert h.execute("SELECT COUNT(*) FROM CompanyFundamentalsHistory").fetchone()[0] == 0 and run.rejected == 1
        assert c.execute("SELECT COUNT(*) FROM validation_log WHERE rule='future_period' AND severity='REJECT'").fetchone()[0] == 1


def test_bank_screener_fixture(env):
    import fundamental_fetcher
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "screener_aubank.html")
    assert os.path.exists(fixture_path)
    with open(fixture_path, "r", encoding="utf-8") as f:
        html = f.read()
    data = fundamental_fetcher.parse_screener_html(html, symbol="AUBANK", source_url="https://www.screener.in/company/AUBANK/")
    qp = data.get("quarterly_performance", [])
    assert len(qp) >= 12
    latest = qp[-1]
    assert latest["quarter"] == "Jun 2026"
    assert float(latest["sales_cr"].replace(",", "")) > 3000.0
    assert float(latest["net_profit_cr"].replace(",", "")) > 500.0
    assert latest["expenses_cr"] is None
    assert latest["opm_pct"] is None

    rows = P.parse_screener_quarters(data)
    assert len(rows) >= 12
    with PL.JobRun("bank_test") as run, db.session() as c, hist() as h:
        PL.promote_quarters(c, h, run, "AUBANK", rows, "SCREENER_QUARTERS", data["source_url"], None, today=date(2026, 10, 11))
        h.commit()
        assert run.saved > 0
        assert run.rejected == 0
        newest = h.execute("SELECT MAX(period_end_date) FROM CompanyFundamentalsHistory WHERE symbol='AUBANK'").fetchone()[0]
        assert newest == "2026-06-30"


def test_holding_company_exemption(env):
    with PL.JobRun("t") as run, db.session() as c, hist() as h:
        rows = [q(date(2025, 3, 31), rev=5.0, np_=4.0),
                q(date(2025, 6, 30), rev=6.0, np_=5.0),
                q(date(2025, 9, 30), rev=120.0, np_=115.0)]
        PL.promote_quarters(c, h, run, "MAHSCOOTER", rows, "SCREENER_QUARTERS", "u", None, today=date(2026, 10, 10))
        h.commit()
        assert run.saved == 3
        assert run.rejected == 0
        assert c.execute("SELECT COUNT(*) FROM validation_log WHERE rule='holding_co_seasonal_swing' AND severity='INFO'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0] == 0


def test_results_job_live_screener_fetch(env):
    from collectors.jobs import results as R

    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "screener_aubank.html")
    with open(fixture_path, "rb") as f:
        aubank_html = f.read()

    # Create symbol in StockHistory
    with hist() as h:
        h.execute("INSERT OR REPLACE INTO StockHistory(symbol) VALUES ('LIVECO')")
        h.execute("INSERT OR REPLACE INTO CompanyMaster(symbol) VALUES ('LIVECO')")
        h.commit()

    # Mock fetcher: NSE fails, Screener succeeds with fixture
    def mock_transport(a, s):
        if a.site == "nse":
            return (404, "application/json", b"{}")
        if a.site == "screener":
            return (200, "text/html", aubank_html)
        return (404, "", b"")

    f = F.Fetcher({"direct": mock_transport, "chromium": mock_transport}, sleep=False)

    with PL.JobRun("results_test") as run:
        R.run(run, f, mode="weekly", symbols="LIVECO")

    assert run.saved >= 12
    assert run.rejected == 0

    with hist() as h:
        rows = h.execute("SELECT period_end_date, revenue_cr, net_profit_cr, source FROM CompanyFundamentalsHistory WHERE symbol='LIVECO' ORDER BY period_end_date").fetchall()
        assert len(rows) >= 12
        assert rows[-1][0] == "2026-06-30"
        assert rows[-1][3] == "SCREENER_QUARTERS"

    # Verify cached into fundamental_cache.db
    cached = R._cache_blob("LIVECO")
    assert cached is not None
    assert cached.get("symbol") == "LIVECO"
    assert len(cached.get("quarterly_performance", [])) >= 12


