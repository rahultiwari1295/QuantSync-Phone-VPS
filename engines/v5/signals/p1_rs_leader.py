"""
vps_data/engines/v11/signals/p1_rs_leader.py
=============================================
Strategy P1: RELATIVE STRENGTH LEADER REFINED (1D Breakout)
Point-in-time implementation for V11 live scanning (Zero Lookahead).
"""

from typing import Dict, Any, Optional
import numpy as np
import pandas as pd


def detect_rs_leader(
    df: pd.DataFrame,
    i: int,
    nifty_df: Optional[pd.DataFrame] = None,
) -> Optional[Dict[str, Any]]:
    """
    Evaluates point-in-time RS Leader setup on bar i.
    Zero-lookahead: only bars <= i are accessed.
    """
    if i < 60:
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
    prev_c = float(c_arr[i - 1])

    # Minimum liquidity and price
    if c < 20.0:
        return None
    turnover_cr = (c * v) / 10_000_000.0
    if turnover_cr < 0.50:
        return None

    # Must be bullish bar
    if c <= o:
        return None

    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    if cir < 0.70:
        return None

    upper_wick = (h - max(c, o)) / rng
    if upper_wick > 0.12:
        return None

    lower_wick = (min(c, o) - l) / rng
    body_pct = abs(c - o) / rng
    bar_ret = (c - o) / o * 100.0 if o > 0 else 0.0
    gap_pct = (o - prev_c) / prev_c * 100.0 if prev_c > 0 else 0.0

    # 20-bar breakout check
    h_20_prior = float(np.max(h_arr[max(0, i - 20): i]))
    if c < h_20_prior:
        return None

    chase_pct = (c - h_20_prior) / h_20_prior * 100.0
    if chase_pct > 3.5:
        return None

    # Volume ratios
    v_slice20 = v_arr[max(0, i - 20): i]
    vol20 = float(np.mean(v_slice20)) if len(v_slice20) > 0 else v
    vol_ratio20 = round(v / max(1.0, vol20), 2)
    if vol_ratio20 < 2.0:
        return None

    v_slice50 = v_arr[max(0, i - 50): i]
    vol50 = float(np.mean(v_slice50)) if len(v_slice50) > 0 else v
    vol_ratio50 = round(v / max(1.0, vol50), 2)

    # Up/Down Volume Ratio over past 20 bars
    up_v, dn_v = 0.0, 0.0
    for k in range(max(1, i - 20), i + 1):
        if c_arr[k] >= c_arr[k - 1]:
            up_v += float(v_arr[k])
        else:
            dn_v += float(v_arr[k])
    ud_ratio = round(up_v / max(1.0, dn_v), 2)

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

    # Short term trend
    p_above_vma7 = 1 if (i >= 7 and c > float(np.mean(c_arr[i - 6: i + 1]))) else 0
    vma7_curr = float(np.mean(c_arr[max(0, i - 6): i + 1])) if i >= 6 else c
    vma7_prev = float(np.mean(c_arr[max(0, i - 9): i - 2])) if i >= 9 else vma7_curr
    vma7_rising = 1 if vma7_curr >= vma7_prev else 0

    p_above_vma20 = 1 if c > s20 else 0
    p_above_vma48 = 1 if c > s50 else 0
    if p_above_vma48 == 0:
        return None

    # Performance
    c_60 = float(c_arr[max(0, i - 60)])
    ret_60d = (c - c_60) / c_60 * 100.0 if c_60 > 0 else 0.0

    c_20 = float(c_arr[max(0, i - 20)])
    ret_20d = (c - c_20) / c_20 * 100.0 if c_20 > 0 else 0.0

    # Relative strength vs NIFTY
    rs_vs_nifty = ret_60d
    if nifty_df is not None and not nifty_df.empty:
        try:
            curr_date = str(df.iloc[i].get("date", ""))[:10]
            n_idx = nifty_df[nifty_df["date"] <= curr_date].index
            if len(n_idx) >= 60:
                n_i = n_idx[-1]
                n_c = float(nifty_df.iloc[n_i]["close"])
                n_c60 = float(nifty_df.iloc[max(0, n_i - 60)]["close"])
                n_ret = (n_c - n_c60) / n_c60 * 100.0 if n_c60 > 0 else 0.0
                rs_vs_nifty = round(ret_60d - n_ret, 2)
        except Exception:
            pass

    # 52w High
    high_252 = float(np.max(h_arr[max(0, i - 252): i + 1]))
    dist_52w_high = (high_252 - c) / high_252 * 100.0 if high_252 > 0 else 0.0

    # Base depth over past 40 bars
    base_high = float(np.max(h_arr[max(0, i - 40): i]))
    base_low = float(np.min(l_arr[max(0, i - 40): i]))
    base_depth_pct = (base_high - base_low) / base_high * 100.0 if base_high > 0 else 0.0
    if base_depth_pct > 28.0:
        return None

    # ATR14
    tr_arr = [max(h_arr[k] - l_arr[k], abs(h_arr[k] - c_arr[k - 1])) for k in range(max(1, i - 13), i + 1)]
    atr_val = float(np.mean(tr_arr)) if tr_arr else rng
    atr_pct = round(atr_val / c * 100.0, 2)

    # Structural Stop: Max of candle low * 0.99 and 6% stop
    stop_p = round(max(l * 0.99, c * 0.94), 2)
    risk_pct = round((c - stop_p) / c * 100.0, 2)
    if risk_pct > 7.0:
        return None

    features = {
        "cir": round(cir, 3),
        "upper_wick_pct": round(upper_wick, 3),
        "lower_wick_pct": round(lower_wick, 3),
        "body_pct": round(body_pct, 3),
        "bar_ret": round(bar_ret, 2),
        "gap_pct": round(gap_pct, 2),
        "vol_ratio20": vol_ratio20,
        "vol_ratio50": vol_ratio50,
        "turnover_cr": round(turnover_cr, 2),
        "ud_ratio": ud_ratio,
        "dist_sma20": round(dist_sma20, 2),
        "dist_sma50": round(dist_sma50, 2),
        "dist_sma200": round(dist_sma200, 2),
        "ma_stacked": ma_stacked,
        "p_above_vma7": p_above_vma7,
        "vma7_rising": vma7_rising,
        "p_above_vma20": p_above_vma20,
        "p_above_vma48": p_above_vma48,
        "ret_60d": round(ret_60d, 2),
        "ret_20d": round(ret_20d, 2),
        "rs_vs_nifty": rs_vs_nifty,
        "dist_52w_high": round(dist_52w_high, 2),
        "chase_pct": round(chase_pct, 2),
        "base_depth_pct": round(base_depth_pct, 2),
        "atr_pct": atr_pct,
        "risk_pct": risk_pct,
    }

    # 2R target price
    target_price = round(c + 2.0 * (c - stop_p), 2)

    return {
        "entry_price": round(c, 2),
        "stop_price": stop_p,
        "target_price": target_price,
        "risk_pct": risk_pct,
        "features": features,
    }
