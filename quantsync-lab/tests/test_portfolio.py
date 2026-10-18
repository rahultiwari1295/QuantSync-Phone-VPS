import numpy as np
import pandas as pd
import pytest

from lab import metrics


def test_portfolio_two_trade_hand_example():
    """Two-trade hand example (one slot) matches exact numbers.
    Trade 1: +10% -> 100,000 * 1.10 = 110,000.
    Trade 2: -5%  -> 110,000 * 0.95 = 104,500.
    Max drawdown = (104,500 - 110,000) / 110,000 = -5.0%.
    """
    trades = [
        {"symbol": "AAA", "entry_date": "2024-01-01", "exit_date": "2024-01-05", "ret_pct": 10.0},
        {"symbol": "BBB", "entry_date": "2024-01-06", "exit_date": "2024-01-10", "ret_pct": -5.0},
    ]
    res = metrics.portfolio(trades, slots=1, capital=100_000.0, seed=7)
    assert res["final"] == 104500.0
    assert res["max_dd"] == -5.0
    assert res["taken"] == 2
    assert res["skipped"] == 0
    assert res["slots"] == 1
    assert res["capital"] == 100_000.0
    assert len(res["equity"]) >= 2
    assert res["equity"][0]["v"] == 100_000.0
    assert res["equity"][-1]["v"] == 104500.0


def test_portfolio_1000_overlapping_trades():
    """1,000 overlapping trades all +4%, 10 slots -> final equity is bounded by the
    hand-computed value (each trade moves equity by ~0.4%), and far below the old compounding.
    """
    # 1000 trades overlapping on the same day:
    trades_same_day = [
        {"symbol": f"SYM_{i}", "entry_date": "2024-01-01", "exit_date": "2024-01-15", "ret_pct": 4.0}
        for i in range(1000)
    ]
    res_same_day = metrics.portfolio(trades_same_day, slots=10, capital=100_000.0, seed=7)
    assert res_same_day["taken"] == 10
    assert res_same_day["skipped"] == 990
    assert res_same_day["final"] == 104000.0

    # 1000 sequential trades all +4% with 10 slots:
    dates = pd.date_range("2020-01-01", periods=1001, freq="D")
    trades_seq = [
        {"symbol": f"S_{i}", "entry_date": dates[i], "exit_date": dates[i + 1], "ret_pct": 4.0}
        for i in range(1000)
    ]
    res_seq = metrics.portfolio(trades_seq, slots=10, capital=100_000.0, seed=7)
    # Each trade moves equity by ~0.4% (equity / 10 * 0.04)
    hand_computed = 100_000.0 * ((1.0 + 0.04 / 10.0) ** 1000)
    old_compounding = 100_000.0 * ((1.0 + 0.04) ** 1000)  # ~1.08e22

    assert abs(res_seq["final"] - hand_computed) / hand_computed < 0.01
    assert res_seq["final"] < 10_000_000  # under 1 crore, far below 1.08e22
    assert res_seq["final"] < old_compounding / 1e12


def test_portfolio_max_dd_bounds():
    """max_dd is always between -100 and 0."""
    # Profitable trades only: max_dd should be 0.0
    profitable = [
        {"symbol": "A", "entry_date": "2024-01-01", "exit_date": "2024-01-05", "ret_pct": 5.0},
        {"symbol": "B", "entry_date": "2024-01-06", "exit_date": "2024-01-10", "ret_pct": 8.0},
    ]
    res_prof = metrics.portfolio(profitable, slots=5, capital=100_000.0)
    assert res_prof["max_dd"] == 0.0

    # Severe losing trades
    losing = [
        {"symbol": "A", "entry_date": "2024-01-01", "exit_date": "2024-01-05", "ret_pct": -50.0},
        {"symbol": "B", "entry_date": "2024-01-06", "exit_date": "2024-01-10", "ret_pct": -80.0},
        {"symbol": "C", "entry_date": "2024-01-11", "exit_date": "2024-01-15", "ret_pct": -90.0},
    ]
    res_loss = metrics.portfolio(losing, slots=1, capital=100_000.0)
    assert res_loss["max_dd"] is not None
    assert -100.0 <= res_loss["max_dd"] <= 0.0


def test_portfolio_determinism():
    """Same input twice -> identical output (seed)."""
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    rng = np.random.default_rng(42)
    trades = []
    for i in range(100):
        e_idx = rng.integers(0, 40)
        x_idx = e_idx + rng.integers(1, 10)
        trades.append({
            "symbol": f"S_{i % 15}",
            "entry_date": dates[e_idx],
            "exit_date": dates[x_idx],
            "ret_pct": float(rng.uniform(-10.0, 15.0)),
        })
    df = pd.DataFrame(trades)

    res1 = metrics.portfolio(df, slots=5, capital=100_000.0, seed=123)
    res2 = metrics.portfolio(df, slots=5, capital=100_000.0, seed=123)

    assert res1["final"] == res2["final"]
    assert res1["max_dd"] == res2["max_dd"]
    assert res1["cagr"] == res2["cagr"]
    assert res1["taken"] == res2["taken"]
    assert res1["skipped"] == res2["skipped"]
    assert res1["equity"] == res2["equity"]


def test_summarize_and_analytics_include_portfolio():
    """summarize and analytics return portfolio fields and portfolio max_dd."""
    df = pd.DataFrame([
        {"symbol": "A", "entry_date": pd.Timestamp("2024-01-01"), "exit_date": pd.Timestamp("2024-01-05"), "ret_pct": 5.0, "is_open": False, "bars": 5},
        {"symbol": "B", "entry_date": pd.Timestamp("2024-01-06"), "exit_date": pd.Timestamp("2024-01-10"), "ret_pct": -3.0, "is_open": False, "bars": 5},
    ])
    s = metrics.summarize(df, slots=10)
    assert "portfolio" in s
    assert s["max_dd"] == s["portfolio"]["max_dd"]
    assert s["portfolio"]["taken"] == 2

    an = metrics.analytics(df, slots=10)
    assert "portfolio" in an
    assert "equity" in an
    assert len(an["equity"]) >= 2
    assert an["equity"][0]["v"] == 100_000.0
