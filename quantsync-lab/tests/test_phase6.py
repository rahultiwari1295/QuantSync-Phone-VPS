import numpy as np
import pandas as pd
import pytest

from lab import pipeline, rules, simulator


def _make_bars(rows):
    df = pd.DataFrame(rows)
    for c in ["open", "high", "low", "close", "atr_14"]:
        if c not in df.columns:
            df[c] = df["close"]
    return df


def test_breakeven_exit():
    # Signal at bar 0, entry at bar 1 open (100)
    # breakeven: trigger 5%, lock 1%
    b = _make_bars([
        {"date": pd.Timestamp("2024-01-01"), "open": 100, "high": 102, "low": 98, "close": 100},
        {"date": pd.Timestamp("2024-01-02"), "open": 100, "high": 106, "low": 99, "close": 104},  # high hits 106 >= 105 -> stop moved to 101
        {"date": pd.Timestamp("2024-01-03"), "open": 103, "high": 104, "low": 100.5, "close": 102},  # low 100.5 hits 101 -> stop exit
    ])
    sig = np.array([True, False, False])
    ex = {"v": 1, "any": [{"type": "stop_pct", "v": 5.0}, {"type": "breakeven", "v": [5.0, 1.0]}, {"type": "time", "v": 10}]}
    trades = simulator.simulate_symbol("TEST", b, sig, rules.normalise_exit(ex), "1D")
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == pytest.approx(101.0)
    assert t["entry_price"] == 100.0


def test_scale_out_exit():
    # Signal at bar 0, entry at bar 1 open (100)
    # scale_out: at +10%, sell 50%, lock remaining at +2%
    b = _make_bars([
        {"date": pd.Timestamp("2024-01-01"), "open": 100, "high": 101, "low": 99, "close": 100},
        {"date": pd.Timestamp("2024-01-02"), "open": 100, "high": 112, "low": 99, "close": 111},  # hits 110 scale target
        {"date": pd.Timestamp("2024-01-03"), "open": 105, "high": 106, "low": 101.5, "close": 103},  # hits 102 locked stop
    ])
    sig = np.array([True, False, False])
    ex = {"v": 1, "any": [{"type": "stop_pct", "v": 5.0}, {"type": "scale_out", "v": [10.0, 0.5, 2.0]}, {"type": "time", "v": 10}]}
    trades = simulator.simulate_symbol("TEST", b, sig, rules.normalise_exit(ex), "1D")
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "stop"
    # Weighted avg exit price: 0.5 * 110 + 0.5 * 102 = 106.0
    assert t["exit_price"] == pytest.approx(106.0)


def test_stop_field_and_target_r():
    # Signal at bar 0, entry at bar 1 open (100)
    # stop_field: v3_risk_pct = 4.0% -> stop at 96
    # target_r: 2.0 -> target at 100 + 2 * 4 = 108
    b = _make_bars([
        {"date": pd.Timestamp("2024-01-01"), "open": 100, "high": 101, "low": 99, "close": 100, "v3_risk_pct": 4.0},
        {"date": pd.Timestamp("2024-01-02"), "open": 100, "high": 109, "low": 99, "close": 108},  # hits target 108
    ])
    sig = np.array([True, False])
    ex = {"v": 1, "any": [{"type": "stop_field", "field": "v3_risk_pct"}, {"type": "target_r", "v": 2.0}, {"type": "time", "v": 10}]}
    trades = simulator.simulate_symbol("TEST", b, sig, rules.normalise_exit(ex), "1D")
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "target"
    assert t["exit_price"] == pytest.approx(108.0)


def test_trail_close_below():
    # Signal at bar 0, entry at bar 1 open (100)
    # trail_close_below: field sma_7, min_bars 2
    b = _make_bars([
        {"date": pd.Timestamp("2024-01-01"), "open": 100, "high": 101, "low": 99, "close": 100, "sma_7": 98},
        {"date": pd.Timestamp("2024-01-02"), "open": 100, "high": 105, "low": 99, "close": 103, "sma_7": 99},  # bar e
        {"date": pd.Timestamp("2024-01-03"), "open": 103, "high": 104, "low": 97, "close": 98, "sma_7": 100},   # bar e+1: close 98 < sma_7 100
        {"date": pd.Timestamp("2024-01-04"), "open": 97, "high": 99, "low": 96, "close": 98, "sma_7": 99},     # bar e+2: fills at open 97
    ])
    sig = np.array([True, False, False, False])
    ex = {"v": 1, "any": [{"type": "trail_close_below", "field": "sma_7", "min_bars": 2}, {"type": "time", "v": 10}]}
    trades = simulator.simulate_symbol("TEST", b, sig, rules.normalise_exit(ex), "1D")
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "trail_close"
    assert t["exit_price"] == pytest.approx(97.0)


def test_pick_ranking():
    # Pick top 2 by conviction
    pick = {"max_per_day": 2, "rank_by": "v3_conviction", "desc": True}
    df = pd.DataFrame([
        {"date": "2024-01-01", "symbol": "A", "v3_conviction": 60},
        {"date": "2024-01-01", "symbol": "B", "v3_conviction": 90},
        {"date": "2024-01-01", "symbol": "C", "v3_conviction": 85},
        {"date": "2024-01-01", "symbol": "D", "v3_conviction": 70},
    ])
    res = df.sort_values(["date", "v3_conviction"], ascending=[True, False]).groupby("date", as_index=False).head(pick["max_per_day"])
    assert list(res["symbol"]) == ["B", "C"]
