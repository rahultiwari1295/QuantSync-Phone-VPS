"""
quantsync_v11/proven_strategies/signals/p9_stage2_base_breakout.py
=================================================================
Strategy P9: STAN WEINSTEIN STAGE-2 BASE BREAKOUT
Pedigree: Stan Weinstein (Secrets for Profiting in Bull and Bear Markets, 1988)

Edge: Structural Accumulation-to-Markup Transition. Stocks spend most of their time
in dormant Stage 1 bases. Catching the precise structural transition where a 60-120 bar
base breaks out into Stage 2 with a flattening/rising 30-week (150-day) MA captures
the entire institutional markup phase with low drawdown.
"""

from typing import Dict, Any, Optional
import numpy as np


def detect_stage2_base_breakout(
    c_arr: np.ndarray,
    o_arr: np.ndarray,
    h_arr: np.ndarray,
    l_arr: np.ndarray,
    v_arr: np.ndarray,
    v_s20: np.ndarray,
    sma150: np.ndarray,
    atr14: np.ndarray,
    i: int,
) -> Optional[Dict[str, Any]]:
    if i < 150:
        return None

    c = float(c_arr[i])
    o = float(o_arr[i])
    h = float(h_arr[i])
    l = float(l_arr[i])
    v = float(v_arr[i])

    if c < 20.0:
        return None

    rng = h - l
    if rng <= 0:
        return None

    # Weinstein 30-Week (150-day) MA inflection:
    # Must not be falling steeply; should be flat or turning up
    s150_curr = float(sma150[i])
    s150_prev20 = float(sma150[max(0, i - 20)])
    if c < s150_curr:
        return None
    if s150_curr < s150_prev20 * 0.99:  # Still declining
        return None

    # Base structure over 60 bars (approx 3 months):
    base_high = float(np.max(h_arr[max(0, i - 60): i]))
    base_low = float(np.min(l_arr[max(0, i - 60): i]))
    base_depth_pct = (base_high - base_low) / base_high * 100.0

    # Base depth must be healthy consolidation: between 10% and 35%
    if base_depth_pct < 10.0 or base_depth_pct > 35.0:
        return None

    # Breakout condition: Today's Close breaks above the 60-bar base high
    if c < base_high or c <= o:
        return None

    cir = (c - l) / rng
    if cir < 0.65:
        return None

    upper_wick = (h - max(c, o)) / rng
    if upper_wick > 0.20:
        return None

    # Volume confirmation: >= 1.8x 20d average
    vol20 = float(v_s20[i]) if not np.isnan(v_s20[i]) and v_s20[i] > 0 else v
    vol_ratio = v / max(1.0, vol20)
    if vol_ratio < 1.8:
        return None

    turnover = c * v
    if turnover < 20_000_000:
        return None

    # Dynamic stop: Below base breakout level or 1.5 ATR below entry
    atr_val = float(atr14[i]) if not np.isnan(atr14[i]) else rng
    pivot_stop = base_high * 0.97
    atr_stop = c - 1.5 * atr_val
    stop_p = max(pivot_stop, atr_stop)
    stop_p = max(stop_p, c * 0.93)  # Max 7% risk
    stop_p = round(stop_p, 2)

    risk_pct = (c - stop_p) / c * 100.0
    if risk_pct < 1.5 or risk_pct > 7.0:
        return None

    body_pct = abs(c - o) / rng
    lower_wick = (min(c, o) - l) / rng
    bar_ret = (c - o) / o * 100.0 if o > 0 else 0.0

    return {
        "entry_price": round(c, 2),
        "stop_price": stop_p,
        "risk_pct": round(risk_pct, 2),
        "features": {
            "base_depth_pct": round(base_depth_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "turnover_cr": round(turnover / 1e7, 2),
            "cir": round(cir, 3),
            "upper_wick": round(upper_wick, 3),
            "lower_wick": round(lower_wick, 3),
            "body_pct": round(body_pct, 3),
            "bar_ret": round(bar_ret, 2),
            "atr_pct": round(atr_val / c * 100, 2),
            "dist_sma150_pct": round((c - s150_curr) / s150_curr * 100, 2),
        }
    }
