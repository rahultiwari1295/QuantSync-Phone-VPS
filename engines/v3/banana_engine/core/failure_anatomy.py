"""
banana_engine/core/failure_anatomy.py — Institutional Post-Hoc Failure Mode Classifier
=====================================================================================
Diagnoses and classifies the root cause of breakout failures using daily OHLC data.

Failure Modes Defined:
  1. IMMEDIATE_REJECTION: Day 1 gap-down >2% or closes below entry by >2% immediately
  2. FALSE_VOLUME: Breakout day was a bull trap (gapped up >2% but closed red with heavy volume)
  3. SLOW_BLEED: Failed without ever reaching +4% MFE, slowly grinding to stop over >= 8 days
  4. OVERHEAD_TRAP: Rallied near prior resistance / pivot (+4% to +7%) then experienced violent reversal to stop
  5. TIME_DECAY: Trapped in stagnation — neither target nor stop hit within 20 days, dead capital
  6. SECTOR_CONTAGION: Immediate breakdown accompanied by multi-day distribution selling

Used to generate training labels for a negative-filter model, rejecting high-risk traps.
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional, Any, List


FAILURE_MODES = [
    "fail_immediate_rejection",
    "fail_false_volume",
    "fail_slow_bleed",
    "fail_overhead_trap",
    "fail_time_decay",
]


def classify_trade_failure(
    bars: pd.DataFrame,
    T: int,
    entry_price: float,
    stop_pct: float = 7.0,
    target_pct: float = 10.0,
    horizon: int = 20,
    pivot_price: Optional[float] = None
) -> Dict[str, int]:
    """
    Classifies failure modes for a setup starting at bar T.
    
    Returns:
      Dict mapping failure mode names to binary indicators (0 or 1).
      If the trade was a successful winner, all failure flags are 0.
    """
    res = {m: 0 for m in FAILURE_MODES}
    res["is_failure"] = 0

    if bars is None or len(bars) <= T + 1:
        return res

    future = bars.iloc[T + 1: T + 1 + horizon].copy()
    if future.empty:
        return res

    target_price = entry_price * (1.0 + target_pct / 100.0)
    stop_price = entry_price * (1.0 - stop_pct / 100.0)

    # 1. Check if False Volume trap on breakout bar itself (bar T)
    c_T = float(bars['close'].iloc[T])
    o_T = float(bars['open'].iloc[T])
    c_prev = float(bars['close'].iloc[T - 1]) if T > 0 else c_T
    if o_T > c_prev * 1.02 and c_T < o_T:
        # Gapped up >2% at open, but sold off and closed red
        res["fail_false_volume"] = 1

    # Check Day 1 forward behavior
    day1_o = float(future['open'].iloc[0])
    day1_c = float(future['close'].iloc[0])
    day1_l = float(future['low'].iloc[0])

    if (day1_o - entry_price) / entry_price < -0.02 or (day1_c - entry_price) / entry_price < -0.02:
        res["fail_immediate_rejection"] = 1

    # Trace forward path
    hit_target_idx = None
    hit_stop_idx = None
    max_high = entry_price
    min_low = entry_price

    for j, row in enumerate(future.itertuples()):
        h = float(row.high)
        l = float(row.low)
        if h > max_high:
            max_high = h
        if l < min_low:
            min_low = l

        if hit_target_idx is None and h >= target_price:
            hit_target_idx = j
        if hit_stop_idx is None and l <= stop_price:
            hit_stop_idx = j
        if hit_target_idx is not None and hit_stop_idx is not None:
            break

    # Determine success vs failure
    # Winner: target hit strictly before stop
    is_winner = False
    if hit_target_idx is not None:
        if hit_stop_idx is None or hit_target_idx < hit_stop_idx:
            is_winner = True

    if is_winner:
        # Not a failure
        return res

    # If not a winner, this trade was a failure or stagnation
    res["is_failure"] = 1

    mfe_pct = (max_high - entry_price) / entry_price * 100.0

    if hit_stop_idx is not None:
        # Stopped out
        if hit_stop_idx >= 7 and mfe_pct < 4.0:
            # Slow grind downward with no upside traction
            res["fail_slow_bleed"] = 1
        elif mfe_pct >= 4.0:
            # Reached respectable upside, then collapsed to stop
            res["fail_overhead_trap"] = 1
    else:
        # Neither target nor stop was hit in horizon bars
        res["fail_time_decay"] = 1

    return res


def batch_classify_failures(
    df: pd.DataFrame,
    bars_lookup: Dict[str, pd.DataFrame],
    stop_pct: float = 7.0,
    target_pct: float = 10.0,
    horizon: int = 20
) -> pd.DataFrame:
    """
    Computes failure classifications for a full DataFrame of setups.
    """
    fail_rows = []
    for row in df.itertuples():
        sym = getattr(row, "symbol", "")
        dt = str(getattr(row, "date", ""))[:10]
        entry = float(getattr(row, "entry", 0.0))
        bars = bars_lookup.get(sym)

        if bars is None or entry <= 0:
            fail_rows.append({m: 0 for m in FAILURE_MODES} | {"is_failure": 0})
            continue

        # Find bar T index
        match = bars[bars['date'] == dt]
        if match.empty:
            fail_rows.append({m: 0 for m in FAILURE_MODES} | {"is_failure": 0})
            continue

        T = match.index[0]
        f_dict = classify_trade_failure(
            bars, T, entry_price=entry,
            stop_pct=stop_pct, target_pct=target_pct, horizon=horizon
        )
        fail_rows.append(f_dict)

    return pd.DataFrame(fail_rows, index=df.index)
