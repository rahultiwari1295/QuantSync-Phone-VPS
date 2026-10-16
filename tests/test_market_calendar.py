import datetime as dt, os, pickle, sys, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from common import market_calendar as mc
IST = mc.IST
def test_basics():
    assert not mc.is_trading_day("2026-10-02")          # Gandhi Jayanti
    assert not mc.is_trading_day("2026-10-10")          # Saturday
    assert mc.is_trading_day("2026-10-07")
    assert mc.previous_trading_day("2026-10-05") == dt.date(2026, 10, 1)
    assert mc.next_trading_day("2026-10-01") == dt.date(2026, 10, 5)
    assert mc.last_completed_session(dt.datetime(2026, 10, 7, 15, 29, tzinfo=IST)) == dt.date(2026, 10, 6)
    assert mc.last_completed_session(dt.datetime(2026, 10, 7, 15, 30, tzinfo=IST)) == dt.date(2026, 10, 7)
    assert mc.last_completed_session(dt.datetime(2026, 10, 10, 12, 0, tzinfo=IST)) == dt.date(2026, 10, 9)
    # UTC input late evening = next IST day
    assert mc.last_completed_session(dt.datetime(2026, 10, 6, 20, 0, tzinfo=dt.timezone.utc)) == dt.date(2026, 10, 6)
    assert mc.trading_days_between("2026-09-30", "2026-10-06") == [dt.date(2026,9,30), dt.date(2026,10,1), dt.date(2026,10,5), dt.date(2026,10,6)]
    assert mc.recent_trading_days(3, "2026-10-04") == [dt.date(2026,9,30), dt.date(2026,10,1), dt.date(2026,10,5)][:0] + [dt.date(2026,9,29), dt.date(2026,9,30), dt.date(2026,10,1)]
    assert mc.is_market_open(dt.datetime(2026, 10, 7, 9, 15, tzinfo=IST))
    assert not mc.is_market_open(dt.datetime(2026, 10, 2, 11, 0, tzinfo=IST))

def test_matches_candle_history():
    p = os.environ.get("CANDLES_PKL")
    if not p: return
    s = pickle.load(open(p, "rb"))
    have = collections.Counter()
    for k in ["RELIANCE","TCS","HDFCBANK","INFY","ICICIBANK","SBIN","ITC","LT"]:
        for x in s[k]: have[x["t"][:10]] += 1
    first = min(min(x["t"][:10] for x in s[k]) for k in ["RELIANCE","TCS","HDFCBANK"])
    d, end = dt.date.fromisoformat(first), dt.date(2026, 9, 23)   # before the corrupted window
    bad = []
    while d <= end:
        if (have[d.isoformat()] == 0) == mc.is_trading_day(d):
            bad.append(d.isoformat())
        d += dt.timedelta(1)
    assert not bad, bad
