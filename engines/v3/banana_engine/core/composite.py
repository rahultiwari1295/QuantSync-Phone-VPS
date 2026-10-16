"""
banana_engine/core/composite.py — Expected Value & Lifecycle State Machine (Layer 4)
===================================================================================
Implements Expected Value in risk units (EV_R), backtest-calibrated tiers (Elite, Strong, Watch, Skip),
and the 4-stage lifecycle state machine matching Section 7 of BananaPatterns v4.
"""

from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import pandas as pd


def composite_ev_r(p_win: float, entry: float, stop: float, target: float) -> Tuple[float, float]:
    """
    Computes expected value in R-multiples:
      r = (target - entry) / (entry - stop)
      EV_R = p_win * r - (1 - p_win) * 1.0
    Returns: (ev_r, r_multiple)
    """
    one_r = entry - stop
    if one_r <= 0:
        return -1.0, 0.0

    r = (target - entry) / one_r
    ev = p_win * r - (1.0 - p_win) * 1.0
    return round(float(ev), 2), round(float(r), 2)


def assign_tier(
    ev_r: float,
    p_win: float,
    r_ratio: float,
    stop_dist_pct: float,
    days_to_earnings: int,
    regime_state: str,
    cutoffs: Optional[Dict[str, float]] = None
) -> str:
    """
    Assigns quality tier: ELITE, STRONG, WATCH, or SKIP.
    """
    # 1. Hard rejection filters
    if p_win < 0.35:
        return "SKIP"
    if r_ratio < 1.3:
        return "SKIP"
    if stop_dist_pct > 8.0:
        return "SKIP"
    if days_to_earnings <= 3:
        return "SKIP"
    if regime_state == "HALT":
        return "SKIP"

    # Default calibrated cutoffs (or custom from backtest)
    if cutoffs is None:
        cutoffs = {"ELITE": 0.45, "STRONG": 0.25, "WATCH": 0.05}

    if ev_r >= cutoffs.get("ELITE", 0.45):
        return "ELITE"
    if ev_r >= cutoffs.get("STRONG", 0.25):
        return "STRONG"
    if ev_r >= cutoffs.get("WATCH", 0.05):
        return "WATCH"
    return "SKIP"


def lifecycle_stage(
    close: float,
    pivot: float,
    breakout_confirmed: bool = False
) -> Tuple[str, str]:
    """
    Orthogonal lifecycle state machine:
      'watch' -> Forming a base
      'trig'  -> Fresh breakout (in buy range [0%, +6%])
      'hold'  -> Powering up / Climbing (+6% to +25%)
      'exit'  -> Extended (> +25%) or Cooling off (< -2% below pivot)
    Returns: (stage_code, display_text)
    """
    if pivot <= 0:
        return "watch", "Forming a base"

    ratio = close / pivot

    if ratio < 0.98 and breakout_confirmed:
        return "exit", "Cooling off"
    if ratio < 1.0 and not breakout_confirmed:
        return "watch", "Forming a base"
    if 0.99 <= ratio <= 1.06:
        return "trig", "Fresh breakout"
    if 1.06 < ratio <= 1.25:
        return "hold", "Powering up"
    return "exit", "Extended"
