"""
vps_data/engines/v11/signals/p2_apex_base.py
=============================================
Strategy P2: APEX BASE BREAKOUT (Institutional Consolidation Compression Breakout)
Point-in-time implementation for V11 live scanning (Zero Lookahead).
Completely self-contained: zero external dependencies.
"""

from typing import Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd


def _find_base_start(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    T: int,
    min_depth: float = 0.05,
    max_depth: float = 0.35,
    min_bars: int = 15,
    max_lookback: int = 250,
) -> Optional[int]:
    """Scans backward from T to locate the consolidation peak that started the base."""
    for peak_idx in range(T - min_bars, max(0, T - max_lookback), -1):
        pivot = highs[peak_idx]

        # Local maximum check over +/- 10 bars
        w_lo = max(0, peak_idx - 10)
        w_hi = min(T, peak_idx + 11)
        if pivot < np.max(highs[w_lo:w_hi]):
            continue

        floor = float(np.min(lows[peak_idx:T]))
        depth = (pivot - floor) / max(pivot, 0.01)
        if not (min_depth <= depth <= max_depth):
            continue

        # Prior closes should not have exceeded pivot by more than 1.5%
        if np.any(closes[peak_idx + 1:T] > pivot * 1.015):
            continue

        return peak_idx
    return None


def detect_apex_base(
    df: pd.DataFrame,
    i: int,
) -> Optional[Dict[str, Any]]:
    """
    Evaluates point-in-time APEX Base breakout on bar i.
    Zero-lookahead: strictly bars <= i are evaluated.
    """
    if i < 40:
        return None

    c_arr = df["close"].values
    o_arr = df["open"].values
    h_arr = df["high"].values
    l_arr = df["low"].values
    v_arr = df["volume"].values

    c = float(c_arr[i])
    o = float(o_arr[i])
    h = float(h_arr[i])
    l = float(l_arr[i])
    v = float(v_arr[i])

    # Minimum liquidity and price
    if c < 20.0 or c <= o:
        return None
    turnover_cr = (c * v) / 10_000_000.0
    if turnover_cr < 1.0:
        return None

    # Base detection
    peak_idx = _find_base_start(h_arr, l_arr, c_arr, i)
    if peak_idx is None:
        return None

    pivot = float(h_arr[peak_idx])
    floor = float(np.min(l_arr[peak_idx:i + 1]))
    base_depth_pct = round((pivot - floor) / max(pivot, 0.01) * 100.0, 2)
    base_weeks = round((i - peak_idx) / 5.0, 1)

    if base_weeks < 4.0 or base_weeks > 80.0:
        return None
    if base_depth_pct < 5.0 or base_depth_pct > 35.0:
        return None

    # Breakout condition: close clears or equals pivot
    if c < pivot * 0.995:
        return None

    chase_pct = (c - pivot) / pivot * 100.0
    high_chase_pct = (h - pivot) / pivot * 100.0
    if chase_pct > 3.0:
        return None

    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    if cir < 0.85:  # High closing range
        return None

    upper_wick = (h - max(c, o)) / rng
    if upper_wick > 0.10:
        return None

    lower_wick = (min(c, o) - l) / rng
    body_pct = abs(c - o) / rng
    bar_ret = (c - o) / o * 100.0 if o > 0 else 0.0

    # Volume ratios
    v_slice20 = v_arr[max(0, i - 20): i]
    vol20 = float(np.mean(v_slice20)) if len(v_slice20) > 0 else v
    vol_ratio20 = round(v / max(1.0, vol20), 2)
    if vol_ratio20 < 2.2:
        return None

    v_slice50 = v_arr[max(0, i - 50): i]
    vol50 = float(np.mean(v_slice50)) if len(v_slice50) > 0 else v
    vol_ratio50 = round(v / max(1.0, vol50), 2)

    # Base internal dynamics: coil ratio and dry ratio
    base_slice = slice(peak_idx + 1, i + 1)
    base_len = i - peak_idx
    mid = peak_idx + 1 + (base_len // 2)

    tr_arr = [max(h_arr[k] - l_arr[k], abs(h_arr[k] - c_arr[k - 1])) for k in range(peak_idx + 1, i + 1)]
    first_tr = tr_arr[:len(tr_arr) // 2]
    sec_tr = tr_arr[len(tr_arr) // 2:]

    atr_first = float(np.mean(first_tr)) if first_tr else rng
    atr_sec = float(np.mean(sec_tr)) if sec_tr else rng
    coil_ratio = round(atr_sec / max(atr_first, 0.01), 3)

    first_vol = v_arr[peak_idx + 1: mid]
    sec_vol = v_arr[mid: i + 1]
    vol_first = float(np.mean(first_vol)) if len(first_vol) > 0 else v
    vol_sec = float(np.mean(sec_vol)) if len(sec_vol) > 0 else v
    dry_ratio = round(vol_sec / max(vol_first, 1.0), 3)

    dry_window = v_arr[max(0, peak_idx - 50): peak_idx]
    dry_median = float(np.median(dry_window)) if len(dry_window) > 10 else vol20
    min_base_v = float(np.min(v_arr[base_slice])) if base_len > 0 else v
    dry_depth = round(min_base_v / max(dry_median, 1.0), 3)

    # Up/Down ratio inside base
    up_v, dn_v = 0.0, 0.0
    for k in range(peak_idx + 1, i + 1):
        if c_arr[k] >= o_arr[k]:
            up_v += float(v_arr[k])
        else:
            dn_v += float(v_arr[k])
    ud_ratio = round(up_v / max(dn_v, 1.0), 3)

    # Moving averages
    s20 = float(np.mean(c_arr[max(0, i - 19): i + 1])) if i >= 19 else c
    s50 = float(np.mean(c_arr[max(0, i - 49): i + 1])) if i >= 49 else c
    s200 = float(np.mean(c_arr[max(0, i - 199): i + 1])) if i >= 199 else s50

    dist_sma20 = (c - s20) / s20 * 100.0 if s20 > 0 else 0.0
    dist_sma50 = (c - s50) / s50 * 100.0 if s50 > 0 else 0.0
    dist_sma200 = (c - s200) / s200 * 100.0 if s200 > 0 else 0.0
    if dist_sma200 > 55.0:
        return None

    ma_stacked = 1 if (s20 > s50 > s200) else 0

    c_above_vma7 = 1 if (i >= 7 and c > float(np.mean(c_arr[i - 6: i + 1]))) else 0
    c_above_vma20 = 1 if c > s20 else 0
    c_above_vma48 = 1 if c > s50 else 0

    vma7_curr = float(np.mean(c_arr[max(0, i - 6): i + 1])) if i >= 6 else c
    vma7_prev = float(np.mean(c_arr[max(0, i - 9): i - 2])) if i >= 9 else vma7_curr
    vma7_slope = round((vma7_curr - vma7_prev) / vma7_prev * 100.0, 2) if vma7_prev > 0 else 0.0

    ath = float(np.max(h_arr[:i + 1]))
    ath_dist_pct = round((ath - c) / ath * 100.0, 2) if ath > 0 else 0.0
    if ath_dist_pct > 35.0:
        return None

    # Structural Stop: Max of candle low, floor, or 4.5% stop
    min_stop = round(c * 0.955, 2)
    stop_p = round(max(l, floor, min_stop), 2)
    if stop_p >= c * 0.985:
        stop_p = min_stop

    risk_pct = round((c - stop_p) / c * 100.0, 2)
    if risk_pct > 5.5:
        return None

    features = {
        "base_weeks": base_weeks,
        "base_depth_pct": base_depth_pct,
        "coil_ratio": coil_ratio,
        "dry_ratio": dry_ratio,
        "dry_depth": dry_depth,
        "ud_ratio": ud_ratio,
        "chase_pct": round(chase_pct, 2),
        "high_chase_pct": round(high_chase_pct, 2),
        "cir": round(cir, 3),
        "upper_wick_pct": round(upper_wick, 3),
        "lower_wick_pct": round(lower_wick, 3),
        "body_pct": round(body_pct, 3),
        "bar_ret": round(bar_ret, 2),
        "marubozu_score": round(body_pct * (1.0 - upper_wick), 3),
        "vol_ratio20": vol_ratio20,
        "vol_ratio50": vol_ratio50,
        "turnover_cr": round(turnover_cr, 2),
        "dist_sma20": round(dist_sma20, 2),
        "dist_sma50": round(dist_sma50, 2),
        "dist_sma200": round(dist_sma200, 2),
        "ma_stacked": ma_stacked,
        "c_above_vma7": c_above_vma7,
        "c_above_vma20": c_above_vma20,
        "c_above_vma48": c_above_vma48,
        "vma7_slope": vma7_slope,
        "ath_dist_pct": ath_dist_pct,
        "risk_pct": risk_pct,
    }

    # APEX Base Target: Tranche 1 target is +12%
    target_price = round(c * 1.12, 2)

    return {
        "entry_price": round(c, 2),
        "stop_price": stop_p,
        "target_price": target_price,
        "risk_pct": risk_pct,
        "features": features,
    }
