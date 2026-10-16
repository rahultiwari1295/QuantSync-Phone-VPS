"""
quantsync_v11/proven_strategies/signals/p5_high_tight_flag.py
============================================================
Strategy P5: HIGH TIGHT FLAG BREAKOUT
Pedigree: Thomas Bulkowski (Encyclopedia of Chart Patterns)

Edge: Extreme Institutional Momentum. A stock that doubles (+70% to +100%) in 6-8 weeks
and then refuses to give back more than 15-20% demonstrates an absolute absence of sellers.
Bulkowski ranks this as having the highest success rate and lowest failure rate of any pattern.
"""

from typing import Dict, Any, Optional
import numpy as np


def detect_high_tight_flag(
    c_arr: np.ndarray,
    o_arr: np.ndarray,
    h_arr: np.ndarray,
    l_arr: np.ndarray,
    v_arr: np.ndarray,
    v_s20: np.ndarray,
    atr14: np.ndarray,
    i: int,
) -> Optional[Dict[str, Any]]:
    if i < 70:
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

    # Step 1: Search for the flagpole peak between 10 and 25 bars ago
    flag_start_idx = max(0, i - 25)
    flag_peak_idx = int(flag_start_idx + np.argmax(h_arr[flag_start_idx: i]))
    flag_peak_h = float(h_arr[flag_peak_idx])

    # Peak must be at least 8 bars ago and at most 25 bars ago
    bars_in_flag = i - flag_peak_idx
    if bars_in_flag < 8 or bars_in_flag > 25:
        return None

    # Step 2: Check Flagpole Surge (from 45 bars prior to flag peak up to flag peak)
    pole_start_idx = max(0, flag_peak_idx - 40)
    pole_start_low = float(np.min(l_arr[pole_start_idx: flag_peak_idx]))
    if pole_start_low <= 0:
        return None

    pole_gain_pct = (flag_peak_h - pole_start_low) / pole_start_low * 100.0
    if pole_gain_pct < 70.0:  # Must have surged >= 70% in the pole
        return None

    # Step 3: Flag Consolidation Depth
    flag_low = float(np.min(l_arr[flag_peak_idx: i]))
    flag_pullback_pct = (flag_peak_h - flag_low) / flag_peak_h * 100.0
    if flag_pullback_pct > 20.0:  # Pulled back too much; not tight
        return None

    # Step 4: Breakout Trigger
    # Today's close must break above the flag high (or within 1% of peak)
    if c < flag_peak_h * 0.99 or c <= o:
        return None

    cir = (c - l) / rng
    if cir < 0.65:
        return None

    upper_wick = (h - max(c, o)) / rng
    if upper_wick > 0.20:
        return None

    # Volume confirmation on breakout
    vol20 = float(v_s20[i]) if not np.isnan(v_s20[i]) and v_s20[i] > 0 else v
    vol_ratio = v / max(1.0, vol20)
    if vol_ratio < 1.6:
        return None

    turnover = c * v
    if turnover < 20_000_000:
        return None

    # Dynamic stop: Below flag low or 6.5% max risk
    struct_stop = flag_low * 0.99
    stop_p = max(struct_stop, c * 0.935)
    stop_p = round(stop_p, 2)

    risk_pct = (c - stop_p) / c * 100.0
    if risk_pct < 2.0 or risk_pct > 7.5:
        return None

    atr_val = float(atr14[i]) if not np.isnan(atr14[i]) else rng
    body_pct = abs(c - o) / rng
    lower_wick = (min(c, o) - l) / rng
    bar_ret = (c - o) / o * 100.0 if o > 0 else 0.0

    return {
        "entry_price": round(c, 2),
        "stop_price": stop_p,
        "risk_pct": round(risk_pct, 2),
        "features": {
            "pole_gain_pct": round(pole_gain_pct, 2),
            "flag_pullback_pct": round(flag_pullback_pct, 2),
            "bars_in_flag": bars_in_flag,
            "vol_ratio": round(vol_ratio, 2),
            "turnover_cr": round(turnover / 1e7, 2),
            "cir": round(cir, 3),
            "upper_wick": round(upper_wick, 3),
            "lower_wick": round(lower_wick, 3),
            "body_pct": round(body_pct, 3),
            "bar_ret": round(bar_ret, 2),
            "atr_pct": round(atr_val / c * 100, 2),
        }
    }
