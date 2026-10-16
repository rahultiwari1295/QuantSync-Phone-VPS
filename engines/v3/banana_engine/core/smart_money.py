"""
banana_engine/core/smart_money.py — Institutional Smart Money Accumulation Engine
================================================================================
Extracts institutional accumulation/distribution signatures within consolidation bases
using ONLY daily OHLCV candlestick data.

Features generated:
  1. obv_divergence_score: OBV trend slope vs Price trend slope inside base (stealth accumulation)
  2. ad_line_curvature: 2nd derivative (acceleration) of Chaikin A/D line inside base
  3. close_position_drift: Linear trend of intraday close position (close - low)/(high - low)
  4. large_bar_bias: Win rate (% green) of the top-5 highest volume days in base
  5. price_volume_divergence: Supply exhaustion ratio (down-day volume contraction)
  6. accumulation_score: Composite multi-factor institutional footprint score (0 - 100)

All features are strictly point-in-time and require only OHLCV data.
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional, Any


def linregress_slope(y: np.ndarray) -> float:
    """Computes simple linear regression slope over 1D numpy array."""
    n = len(y)
    if n < 2:
        return 0.0
    x = np.arange(n, dtype=float)
    x_mean = (n - 1) / 2.0
    y_mean = float(np.mean(y))
    denom = float(np.sum((x - x_mean) ** 2))
    if denom == 0.0:
        return 0.0
    numer = float(np.sum((x - x_mean) * (y - y_mean)))
    return float(numer / denom)


def compute_smart_money_features(
    bars: pd.DataFrame,
    T: int,
    base_peak_idx: Optional[int] = None
) -> Dict[str, float]:
    """
    Computes 6 institutional accumulation metrics for the base ending at bar T.
    
    Parameters:
      bars: DataFrame with ['open', 'high', 'low', 'close', 'volume']
      T: Index of the current bar (breakout day)
      base_peak_idx: Index where base started (peak_idx). If None, defaults to max(0, T - 30).
    
    Returns:
      Dict with keys:
        'obv_divergence_score'
        'ad_line_curvature'
        'close_position_drift'
        'large_bar_bias'
        'price_volume_divergence'
        'accumulation_score'
    """
    default_res = {
        "obv_divergence_score": 0.0,
        "ad_line_curvature": 0.0,
        "close_position_drift": 0.0,
        "large_bar_bias": 0.5,
        "price_volume_divergence": 0.0,
        "accumulation_score": 50.0,
    }

    if bars is None or len(bars) == 0 or T < 3:
        return default_res

    start_idx = base_peak_idx if (base_peak_idx is not None and base_peak_idx >= 0) else max(0, T - 30)
    if start_idx >= T:
        start_idx = max(0, T - 10)

    body = bars.iloc[start_idx:T + 1].copy()
    n = len(body)
    if n < 4:
        return default_res

    closes = body['close'].values.astype(float)
    highs = body['high'].values.astype(float)
    lows = body['low'].values.astype(float)
    opens = body['open'].values.astype(float)
    volumes = body['volume'].values.astype(float)

    mean_vol = max(float(np.mean(volumes)), 1.0)
    mean_close = max(float(np.mean(closes)), 0.01)

    # 1. OBV Divergence Score
    # Direction: +1 if close > prev_close, -1 if close < prev_close, 0 if equal
    diff = np.diff(closes, prepend=closes[0])
    direction = np.zeros(n, dtype=float)
    direction[diff > 0] = 1.0
    direction[diff < 0] = -1.0
    obv = np.cumsum(direction * volumes)

    # Normalize OBV and Price to unit scale for fair slope comparison
    norm_obv = (obv - np.mean(obv)) / (np.std(obv) + 1e-6)
    norm_close = (closes - np.mean(closes)) / (np.std(closes) + 1e-6)

    obv_slope = linregress_slope(norm_obv)
    price_slope = linregress_slope(norm_close)
    # Divergence: OBV rising faster than price indicates accumulation
    obv_div = float(np.clip((obv_slope - price_slope) * 10.0, -10.0, 10.0))

    # 2. A/D Line Curvature (2nd derivative / acceleration)
    # CLV (Close Location Value) in [-1, +1]
    ranges = highs - lows
    ranges[ranges == 0] = 1e-6
    clv = ((closes - lows) - (highs - closes)) / ranges
    ad_line = np.cumsum(clv * volumes)

    # Fit 2nd degree polynomial: ad_line = a * t^2 + b * t + c
    t = np.arange(n, dtype=float)
    try:
        poly = np.polyfit(t, ad_line, deg=2)
        # poly[0] is quadratic coefficient 'a'; 2nd derivative is 2*a
        # Normalize by mean volume to make scale invariant across symbols
        curvature = float(np.clip((2.0 * poly[0]) / (mean_vol + 1e-6) * 100.0, -10.0, 10.0))
    except Exception:
        curvature = 0.0

    # 3. Close Position Drift (Intraday accumulation trend)
    # (close - low) / (high - low) is in [0, 1]
    close_pos = (closes - lows) / ranges
    drift = float(np.clip(linregress_slope(close_pos) * 100.0, -10.0, 10.0))

    # 4. Large Bar Bias (Direction on highest-volume days)
    k = min(5, n)
    top_vol_indices = np.argsort(volumes)[-k:]
    green_count = int(np.sum(closes[top_vol_indices] >= opens[top_vol_indices]))
    large_bar_bias = float(round(green_count / float(k), 3))

    # 5. Price Volume Divergence (Supply Exhaustion)
    # Compare down-day volume in 1st half of base vs 2nd half
    mid = n // 2
    down_mask_1 = (diff[:mid] < 0)
    down_mask_2 = (diff[mid:] < 0)

    vol_down_1 = float(np.mean(volumes[:mid][down_mask_1])) if np.any(down_mask_1) else mean_vol
    vol_down_2 = float(np.mean(volumes[mid:][down_mask_2])) if np.any(down_mask_2) else (mean_vol * 0.5)

    # Exhaustion ratio: > 0 means down-volume shrank in second half
    if vol_down_1 > 0:
        pv_div = float(np.clip(1.0 - (vol_down_2 / vol_down_1), -1.0, 1.0))
    else:
        pv_div = 0.0

    # 6. Composite Accumulation Score (0 - 100)
    score = 50.0
    # Add OBV divergence contribution (-15 to +15)
    score += np.clip(obv_div * 1.5, -15.0, 15.0)
    # Add A/D curvature contribution (-15 to +15)
    score += np.clip(curvature * 1.5, -15.0, 15.0)
    # Add Close position drift contribution (-10 to +10)
    score += np.clip(drift * 1.0, -10.0, 10.0)
    # Add Large bar bias contribution (bias 0.5 is 0; 1.0 is +15; 0.0 is -15)
    score += (large_bar_bias - 0.5) * 30.0
    # Add Supply exhaustion contribution (-10 to +10)
    score += np.clip(pv_div * 10.0, -10.0, 10.0)

    accumulation_score = float(round(np.clip(score, 0.0, 100.0), 2))

    return {
        "obv_divergence_score": round(obv_div, 4),
        "ad_line_curvature": round(curvature, 4),
        "close_position_drift": round(drift, 4),
        "large_bar_bias": round(large_bar_bias, 3),
        "price_volume_divergence": round(pv_div, 4),
        "accumulation_score": accumulation_score,
    }
