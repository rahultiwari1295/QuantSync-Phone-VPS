import numpy as np
import pandas as pd
import pytest

from lab import features, metrics, rules, simulator, store
from lab.synth import make_candles


def test_no_lookahead_features():
    d = make_candles(1)
    full = features.compute(d)
    cut = features.compute(d.iloc[:1500])
    a, b = full.iloc[:1500].drop(columns="date"), cut.drop(columns="date")
    np.testing.assert_allclose(a.to_numpy(), b.to_numpy(), equal_nan=True, rtol=1e-5)


def test_weekly_bars_and_rsi_range():
    d = make_candles(2)
    w = features.to_weekly(d)
    assert len(w) < len(d) / 4 and w["high"].ge(w["low"]).all()
    r = features.compute(d)["rsi_14"].dropna()
    assert r.between(0, 100).all()


def test_cook_nulls_not_nan(cfg):
    df = store.bars(cfg, "1D")["SYM00"]
    assert df["rsi_14"].isna().sum() >= 13
    meta = store.meta(cfg, "1D")
    assert meta["symbols"] == 20


def test_rule_validation_and_hash():
    r = {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 50}}]}
    r2 = {"v": 1, "all": [{"r": {"c": 50.0}, "op": ">", "l": {"f": "rsi_14"}}]}
    assert rules.rule_hash(r, "1D") == rules.rule_hash(r2, "1D") != rules.rule_hash(r, "1W")
    bad = [
        {"v": 2, "all": []},
        {"v": 1, "all": [{"l": {"f": "x; DROP TABLE"}, "op": ">", "r": {"c": 1}}]},
        {"v": 1, "all": [{"l": {"f": "close"}, "op": ">", "r": {"c": "1; --"}}]},
        {"v": 1, "all": [{"l": {"f": "close"}, "op": "~", "r": {"c": 1}}]},
        {"v": 1, "all": [{"l": {"f": "close", "lag": 99}, "op": ">", "r": {"c": 1}}]},
        {"v": 1, "all": [{"l": {"f": "close"}, "op": ">", "r": {"c": 1}}] * 13},
        {"v": 1, "all": [{"all": [{"all": [{"all": [{"l": {"f": "close"}, "op": ">", "r": {"c": 1}}]}]}]}]},
        {"v": 1, "all": [{"l": {"f": "close"}, "op": "is_true"}]},
    ]
    for b in bad:
        with pytest.raises(rules.RuleError):
            rules.normalise(b)


def test_scan_matches_pandas(cfg):
    rule = {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 60}},
                            {"l": {"f": "close"}, "op": ">", "r": {"f": "sma_50"}}]}
    got = store.signals(cfg, rule, "1D", start="2024-01-01")
    exp = 0
    for df in store.bars(cfg, "1D").values():
        exp += int(((df.rsi_14 > 60) & (df.close > df.sma_50) & (df.date >= "2024-01-01")).sum())
    assert len(got) == exp > 0


def test_cross_above_uses_prior_bar(cfg):
    rule = {"v": 1, "all": [{"l": {"f": "macd"}, "op": "crosses_above", "r": {"f": "macd_signal"}}]}
    got = store.signals(cfg, rule, "1D", start="2023-01-01")
    df = store.bars(cfg, "1D")["SYM03"]
    exp = ((df.macd > df.macd_signal) & (df.macd.shift(1) <= df.macd_signal.shift(1)) & (df.date >= "2023-01-01")).sum()
    assert (got.symbol == "SYM03").sum() == exp


def test_rule_fuzz(cfg):
    rng = np.random.default_rng(0)
    keys = ["close", "rsi_14", "sma_20", "macd", "vol_ratio", "atr_pct", "is_green"]
    ops = [">", "<", ">=", "<=", "crosses_above", "crosses_below", "between"]
    for _ in range(40):
        conds = []
        for _ in range(rng.integers(1, 5)):
            op = ops[rng.integers(len(ops))]
            l = {"f": keys[rng.integers(len(keys) - 1)], "lag": int(rng.integers(0, 3))}
            r = {"list": [10, 90]} if op == "between" else ({"c": float(rng.uniform(0, 100))} if rng.random() < .5 else {"f": keys[rng.integers(5)]})
            conds.append({"l": l, "op": op, "r": r})
        out = store.signals(cfg, {"v": 1, "any": conds[:1] + [{"all": conds[1:]}] if len(conds) > 1 else conds}, "1D", start="2025-06-01")
        assert {"symbol", "date", "close"} <= set(out.columns)


def _bars(o, h, l, c):
    n = len(o)
    return pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=n), "open": o, "high": h, "low": l, "close": c, "atr_14": [2.0] * n})


def test_golden_stop_target_gap():
    # signal on bar0 -> entry bar1 open 100
    b = _bars([100, 100, 101, 99, 90], [101, 102, 111, 100, 91], [99, 99, 100, 95, 89], [100, 101, 110, 98, 90])
    sig = np.array([1, 0, 0, 0, 0], bool)
    t = simulator.simulate_symbol("X", b, sig, {"v": 1, "any": [{"type": "target_pct", "v": 10}, {"type": "stop_pct", "v": 5}]}, "1D")[0]
    assert t["exit_reason"] == "target" and t["exit_price"] == pytest.approx(110)
    assert t["ret_pct"] == pytest.approx((110 * .999) / (100 * 1.001) * 100 - 100)
    # both inside one bar -> stop first
    b2 = _bars([100, 100, 100], [101, 112, 100], [99, 94, 99], [100, 100, 100])
    t2 = simulator.simulate_symbol("X", b2, sig[:3], {"v": 1, "any": [{"type": "target_pct", "v": 10}, {"type": "stop_pct", "v": 5}]}, "1D")[0]
    assert t2["exit_reason"] == "stop" and t2["exit_price"] == pytest.approx(95)
    # gap through the stop fills at the open
    b3 = _bars([100, 100, 90], [101, 101, 91], [99, 99, 89], [100, 100, 90])
    t3 = simulator.simulate_symbol("X", b3, sig[:3], {"v": 1, "any": [{"type": "stop_pct", "v": 5}]}, "1D")[0]
    assert t3["exit_price"] == 90
    # big gap-up entry is skipped
    b4 = _bars([100, 120, 121], [101, 121, 122], [99, 119, 120], [100, 120, 121])
    assert simulator.simulate_symbol("X", b4, sig[:3], None, "1D") == []


def test_one_trade_at_a_time_and_open_trade():
    n = 12
    b = _bars([100] * n, [101] * n, [99] * n, [100] * n)
    sig = np.ones(n, bool)
    tr = simulator.simulate_symbol("X", b, sig, {"v": 1, "any": [{"type": "time", "v": 3}]}, "1D")
    assert [t["bars"] for t in tr[:3]] == [3, 3, 3]
    assert all(tr[i + 1]["entry_date"] > tr[i]["exit_date"] for i in range(len(tr) - 1))
    assert tr[-1]["is_open"] in (True, False)


def test_parity_live_vs_history(cfg):
    """Trades already closed at cut-off T are identical whether or not later data exists."""
    rule = {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 55}}]}
    ex = {"v": 1, "any": [{"type": "stop_pct", "v": 4}, {"type": "target_pct", "v": 8}, {"type": "time", "v": 10}]}
    allb = store.bars(cfg, "1D", ["SYM01", "SYM02"])
    sg = store.signals(cfg, rule, "1D")
    full = simulator.simulate(allb, simulator.sig_arrays(sg, allb), ex, "1D")
    T = pd.Timestamp("2024-06-28")
    cutb = {k: v[v.date <= T].reset_index(drop=True) for k, v in allb.items()}
    part = simulator.simulate(cutb, simulator.sig_arrays(sg[sg.date <= T], cutb), ex, "1D")
    closed = part[~part.is_open]
    key = ["symbol", "entry_date", "exit_date", "exit_price"]
    merged = closed[key].merge(full[key], how="left", indicator=True)
    assert (merged["_merge"] == "both").all() and len(closed) > 5


def test_metrics(cfg):
    t = pd.DataFrame({"entry_date": pd.to_datetime(["2023-02-01", "2024-03-01", "2024-04-01"]),
                      "exit_date": pd.to_datetime(["2023-02-10", "2024-03-10", "2024-04-10"]),
                      "ret_pct": [5.0, -2.0, 3.0], "bars": [5, 5, 5], "is_open": [False] * 3})
    m = metrics.by_year(t, [2023, 2024])
    assert m["2024"]["trades"] == 2 and m["All"]["win_rate"] == pytest.approx(66.7)
    assert m["2023"]["profit_factor"] is None


def test_cook_from_live_stockhistory_db(tmp_path):
    import json, sqlite3
    from lab.config import Config
    db = tmp_path / "stock_history.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE StockHistory (token TEXT, symbol TEXT PRIMARY KEY, rawCandles TEXT, candleCount INTEGER)")
    d = make_candles(4, start="2024-01-01")
    raw = [{"t": str(r.date.date()), "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume} for r in d.itertuples()]
    c.execute("INSERT INTO StockHistory VALUES('1','ABC',?,?)", (json.dumps(raw), len(raw)))
    c.execute("INSERT INTO StockHistory VALUES('2','BAD','not json',0)")
    c.commit(); c.close()
    cfg = Config(data_dir=tmp_path / "data", candles_db=db)
    m = store.cook(cfg, "1D")
    assert m["symbols"] == 1 and any(s.startswith("BAD") for s in m["skipped"])


def test_demo_catalog_in_sync():
    import json, pathlib
    from lab.catalog import catalog
    p = pathlib.Path(__file__).resolve().parents[2] / "src" / "lab" / "catalog.json"
    if p.exists():  # the web demo ships a copy of the catalog; regenerate it when features change
        assert json.loads(p.read_text()) == json.loads(json.dumps(catalog()))
