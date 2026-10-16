"""
quantsync_v11/proven_strategies/signals/p4_power_earnings_gap.py
==============================================================
Strategy P4: POWER EARNINGS GAP (PEG / PEAD Catalyst Surge)
Pedigree: Ball & Brown (1968), Bernard & Thomas (1989), Dan Zanger & Gil Morales

Edge: Post-Earnings Announcement Drift. An explosive gap on massive institutional
turnover signals a fundamental regime shift / earnings surprise. Large institutions
must scale in over weeks, causing a 30-to-60 day structural drift upward.
"""

from typing import Dict, Any, Optional
import numpy as np


def detect_power_earnings_gap(
    c_arr: np.ndarray,
    o_arr: np.ndarray,
    h_arr: np.ndarray,
    l_arr: np.ndarray,
    v_arr: np.ndarray,
    v_s20: np.ndarray,
    atr14: np.ndarray,
    i: int,
) -> Optional[Dict[str, Any]]:
    if i < 40:
        return None

    c = float(c_arr[i])
    o = float(o_arr[i])
    h = float(h_arr[i])
    l = float(l_arr[i])
    v = float(v_arr[i])
    prev_c = float(c_arr[i - 1])

    if c < 20.0:
        return None

    # Gap percentage: Open must be at least 3.5% above previous close
    gap_pct = (o - prev_c) / prev_c * 100.0
    if gap_pct < 3.5:
        return None

    rng = h - l
    if rng <= 0:
        return None

    # Must close in top 30% of range (institutions held into the close)
    cir = (c - l) / rng
    if cir < 0.70:
        return None

    # Upper wick <= 15%
    upper_wick = (h - max(c, o)) / rng
    if upper_wick > 0.15:
        return None

    # Low must not fill more than 40% of the opening gap
    gap_size = o - prev_c
    gap_fill = o - l
    gap_fill_pct = (gap_fill / gap_size * 100.0) if gap_size > 0 else 100.0
    if gap_fill_pct > 40.0:
        return None

    # Massive volume confirmation: >= 3.0x 20-day average
    vol20 = float(v_s20[i]) if not np.isnan(v_s20[i]) and v_s20[i] > 0 else v
    vol_ratio = v / max(1.0, vol20)
    if vol_ratio < 3.0:
        return None

    # Heavy turnover: >= 5 Cr
    turnover = c * v
    if turnover < 50_000_000:
        return None

    # Dynamic structural stop: Below gap cushion (prev_c + 0.35 * gap_size) or Low * 0.99
    gap_cushion = prev_c + 0.35 * gap_size
    stop_p = max(l * 0.99, gap_cushion)
    stop_p = round(stop_p, 2)

    risk_pct = (c - stop_p) / c * 100.0
    if risk_pct < 2.0 or risk_pct > 8.0:
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
            "gap_pct": round(gap_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "turnover_cr": round(turnover / 1e7, 2),
            "cir": round(cir, 3),
            "gap_fill_pct": round(gap_fill_pct, 2),
            "upper_wick": round(upper_wick, 3),
            "lower_wick": round(lower_wick, 3),
            "body_pct": round(body_pct, 3),
            "bar_ret": round(bar_ret, 2),
            "atr_pct": round(atr_val / c * 100, 2),
        }
    }
