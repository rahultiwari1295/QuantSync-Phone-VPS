"""
banana_engine/core/backtest.py — Walk-Forward Backtest & Cost Engine
====================================================================
Simulates trade execution from walk-forward predictions, deducts realistic costs
(5 bps slippage + 2 bps commission), and computes institutional R-multiple statistics
matching Section 8 of BananaPatterns v4.
"""

from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd

SLIPPAGE_BPS   = 5.0     # 0.05% slippage per side
COMMISSION_BPS = 2.0     # 0.02% commission per side


def compute_cost_r(entry: float, stop: float) -> float:
    """
    Computes round-trip friction cost in units of initial risk R.
    """
    one_r = entry - stop
    if one_r <= 0:
        return 0.02
    round_trip_pct = (2.0 * (SLIPPAGE_BPS + COMMISSION_BPS)) / 10000.0
    cost_rupees = entry * round_trip_pct
    return round(float(cost_rupees / one_r), 4)


def backtest_stats(trades_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Computes performance statistics over a dataframe of simulated trades with 'r' column.
    """
    if trades_df.empty:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "expectancy_r": 0.0,
            "profit_factor": 0.0,
            "sharpe_of_r": 0.0,
            "max_drawdown_r": 0.0
        }

    df = trades_df.copy()
    r = df['r'].values

    wins = r[r > 0]
    losses = r[r <= 0]
    win_rate = round(float(len(wins) / len(r)), 4) if len(r) > 0 else 0.0

    sum_win = float(np.sum(wins)) if len(wins) > 0 else 0.0
    sum_loss = abs(float(np.sum(losses))) if len(losses) > 0 else 1.0
    profit_factor = round(sum_win / max(sum_loss, 0.001), 2)

    exp_r = round(float(np.mean(r)), 3)
    std_r = float(np.std(r)) if len(r) > 1 else 1.0
    # Annualized Sharpe for 20-day holding horizon: sqrt(252 / 20)
    sharpe = round(float((exp_r / max(std_r, 0.001)) * np.sqrt(252.0 / 20.0)), 2)

    # Max Drawdown in R-multiples
    equity_curve = np.cumsum(r)
    peak = np.maximum.accumulate(equity_curve)
    dd = peak - equity_curve
    max_dd = round(float(np.max(dd)), 2) if len(dd) > 0 else 0.0

    return {
        "n_trades": len(df),
        "win_rate": win_rate,
        "avg_r": exp_r,
        "median_r": round(float(np.median(r)), 3),
        "expectancy_r": exp_r,
        "profit_factor": profit_factor,
        "sharpe_of_r": sharpe,
        "max_drawdown_r": max_dd
    }
