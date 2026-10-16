"""
banana_engine/core/features.py — Feature Engineering & Parity Pipeline
======================================================================
Implements the unified extract_features() function producing the exact 38 domain
features specified in Section 4 of BananaPatterns v4.
Strict point-in-time parity guaranteed between backfill training and live inference.
"""
import os
import sys

# Ensure banana_engine root is on sys.path
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from datetime import date, datetime
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

from core.structure import (
    detect_base_at,
    classify_context,
    is_breakout_day,
    find_swing_points,
    count_squats_in_base,
    count_failed_pokes_in_base,
    last_squat_idx,
    regime_state,
    mean_atr_pct,
    CONTEXT_GATES,
    DRY_DEPTH_LOOKBACK
)
from core.data_layer import (
    load_adjusted,
    days_to_next_earnings,
    sector_at,
    listing_date_at
)

# -----------------------------------------------------------------------------
# 4.1 Master 38-Feature Registry
# -----------------------------------------------------------------------------
FEATURES: List[str] = [
    # A. Base geometry (10)
    "base_depth_pct",
    "duration_weeks",
    "coil_ratio",
    "dry_ratio",
    "num_contractions",
    "depth_contraction_ratio",
    "tightness_5d",
    "tightness_10d",
    "days_since_peak",
    "position_in_base",

    # B. Volume behavior (6)
    "dry_depth",
    "ud_ratio",
    "breakout_volume_ratio",
    "volume_slope_base",
    "max_volume_day_ratio",
    "volume_contraction_ratio",

    # C. Trend context (8)
    "rs_rating",
    "dist_from_52w_high_pct",
    "dist_from_52w_low_pct",
    "pct_above_50dma",
    "pct_above_150dma",
    "pct_above_200dma",
    "dma_150_above_200",
    "dma_200_slope_22d",

    # D. Market regime at signal (4)
    "breadth_above_20dma",
    "breadth_above_50dma",
    "breadth_above_200dma",
    "regime_state_ord",

    # E. Sector context (5)
    "sector_return_5d",
    "sector_return_20d",
    "sector_rs_rank",
    "sector_breadth",
    "stock_vs_sector_rs",

    # F. Micro-events (3)
    "squat_count_in_base",
    "failed_poke_count_in_base",
    "days_since_last_squat",

    # G. Calendar (2)
    "days_to_next_earnings",
    "weeks_since_ipo",
]


def linregress_slope(y: np.ndarray) -> float:
    """
    Computes simple linear regression slope over array y.
    """
    n = len(y)
    if n < 2:
        return 0.0
    x = np.arange(n)
    x_mean = (n - 1) / 2.0
    y_mean = float(np.mean(y))
    denom = float(np.sum((x - x_mean) ** 2))
    if denom == 0:
        return 0.0
    numer = float(np.sum((x - x_mean) * (y - y_mean)))
    return float(numer / denom)


def compute_sector_features(
    sector: str,
    as_of: str,
    sector_summary_lookup: Optional[Dict[str, Any]] = None
) -> Dict[str, float]:
    """
    Point-in-time sector performance metrics.
    """
    if sector_summary_lookup and sector in sector_summary_lookup:
        data = sector_summary_lookup[sector]
        return {
            "ret_5d": float(data.get("ret_5d", 0.0)),
            "ret_20d": float(data.get("ret_20d", 0.0)),
            "rank": float(data.get("rank", 0.5)),
            "breadth": float(data.get("breadth", 50.0)),
        }
    return {
        "ret_5d": 0.0,
        "ret_20d": 0.0,
        "rank": 0.5,
        "breadth": 50.0,
    }


# FIXED: Fix 1.1 (BLOCKER-01) — rs_rating must be supplied; no default fallback to prevent degeneracy
def extract_features(
    symbol: str,
    as_of: str,
    bars_all: Optional[pd.DataFrame] = None,
    rs_rating: Optional[float] = None,
    breadth: Optional[Dict[str, float]] = None,
    sector_lookup: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Extracts the complete 38-feature vector for symbol as of query date.
    Strict PIT: All rolling statistics end at or before bar T.
    """
    if rs_rating is None:
        raise ValueError("rs_rating must be supplied; no default")

    as_of_str = str(as_of)[:10]

    # Load adjusted OHLCV up to as_of
    if bars_all is None:
        bars = load_adjusted(symbol, end_date=as_of_str)
    else:
        bars = bars_all[bars_all['date'] <= as_of_str].copy().reset_index(drop=True)

    if bars.empty:
        return None

    T = len(bars) - 1
    if T < 25:
        return None

    # 1. Base Detection
    base = detect_base_at(bars, T)
    if base is None:
        return None

    # Context classification
    ath_pit = float(bars['high'].iloc[:T + 1].max())
    list_date = listing_date_at(symbol, as_of_str)
    ctx = classify_context(base, bars, T, listing_date=list_date, ath_pit=ath_pit)

    gate = CONTEXT_GATES[ctx]
    if base.duration_weeks < gate["min_weeks"]:
        return None
    if base.depth_pct > gate["max_depth"]:
        return None
    # DEPRECATED: Gating in feature extraction prevents training on low-RS negatives and violates test_02 min < 30.
    # RS gating belongs in passes_trend_template (Fix 3.1).
    if False and rs_rating < gate["min_rs"]:
        return None

    # Volume confirmation
    vol_sma20_prior = float(bars['volume'].iloc[max(0, T - 20):T].mean()) if T >= 5 else float(bars['volume'].iloc[T])
    if not is_breakout_day(bars, T, base.pivot, vol_sma20_prior):
        return None

    # 2. Base Geometry Features
    body = bars.iloc[base.peak_idx + 1:T + 1]
    mid = len(body) // 2
    first, second = body.iloc[:mid], body.iloc[mid:]

    atr_first = mean_atr_pct(first)
    atr_second = mean_atr_pct(second)
    vol_first = float(first['volume'].mean()) if len(first) > 0 else 1.0
    vol_second = float(second['volume'].mean()) if len(second) > 0 else 1.0

    c = float(bars['close'].iloc[T])
    high_52w = float(bars['high'].iloc[max(0, T - 252):T + 1].max())
    low_52w = float(bars['low'].iloc[max(0, T - 252):T + 1].min())

    sma50 = float(bars['close'].iloc[max(0, T - 49):T + 1].mean()) if T >= 10 else c
    sma150 = float(bars['close'].iloc[max(0, T - 149):T + 1].mean()) if T >= 50 else np.nan
    sma200 = float(bars['close'].iloc[max(0, T - 199):T + 1].mean()) if T >= 100 else np.nan

    # 3. Swing-Based Contraction Features
    sh, sl = find_swing_points(body, left=3, right=3)
    num_contractions = len(sl)
    if len(sl) >= 2:
        pb_first_h = float(body['high'].iloc[:sl[0] + 1].max())
        pb_first_l = float(body['low'].iloc[sl[0]])
        depth_first_pb = (pb_first_h - pb_first_l) / max(pb_first_h, 0.01)

        pb_last_h = float(body['high'].iloc[:sl[-1] + 1].max())
        pb_last_l = float(body['low'].iloc[sl[-1]])
        depth_last_pb = (pb_last_h - pb_last_l) / max(pb_last_h, 0.01)

        depth_contraction_ratio = round(depth_last_pb / max(depth_first_pb, 0.001), 3) if depth_first_pb > 0 else np.nan
    else:
        depth_contraction_ratio = np.nan

    t5 = bars.iloc[max(0, T - 4):T + 1]
    t10 = bars.iloc[max(0, T - 9):T + 1]
    tightness_5d = round(float((t5['high'].max() - t5['low'].min()) / max(c, 0.01)), 4)
    tightness_10d = round(float((t10['high'].max() - t10['low'].min()) / max(c, 0.01)), 4)

    # 4. Volume Behavior Features
    volume_slope_base = round(linregress_slope(body['volume'].values) / max(float(body['volume'].mean()), 1.0), 5)
    vol_first10 = float(body['volume'].iloc[:10].mean()) if len(body) >= 10 else vol_first
    vol_last10 = float(body['volume'].iloc[-10:].mean()) if len(body) >= 10 else vol_second
    volume_contraction_ratio = round(vol_last10 / max(vol_first10, 1.0), 3) if vol_first10 > 0 else np.nan

    dry_window = bars['volume'].iloc[max(0, base.peak_idx - DRY_DEPTH_LOOKBACK):base.peak_idx]
    dry_median = float(dry_window.median()) if len(dry_window) >= 15 else 1.0
    dry_depth = round(float(body['volume'].min()) / max(dry_median, 1.0), 3) if len(dry_window) >= 15 else np.nan
    max_volume_day_ratio = round(float(body['volume'].max()) / max(dry_median, 1.0), 3) if len(dry_window) >= 15 else np.nan

    up_v = float(body[body['close'] >= body['open']]['volume'].sum())
    down_v = float(body[body['close'] < body['open']]['volume'].sum())
    ud_ratio = round(up_v / max(down_v, 1.0), 3) if down_v > 0 else np.nan

    # 5. Market Regime at Signal
    if breadth is None:
        breadth = {"pct_above_20dma": 50.0, "pct_above_50dma": 50.0, "pct_above_200dma": 50.0}
    regime_ord = {"RISK_OFF": 0, "NEUTRAL": 1, "RISK_ON": 2}[regime_state(breadth)]

    # 6. Sector Context at Signal
    sec_name = sector_at(symbol, as_of_str)
    sec_feats = compute_sector_features(sec_name, as_of_str, sector_lookup)
    ret_20d_stock = float((c / max(float(bars['close'].iloc[max(0, T - 20)]), 0.01) - 1.0) * 100.0)
    stock_vs_sector_rs = round(ret_20d_stock - sec_feats["ret_20d"], 2)

    # 7. Micro-Events
    squats = count_squats_in_base(bars, base, vol_sma20_prior)
    failed_pokes = count_failed_pokes_in_base(bars, base)
    days_since_squat = (T - last_squat_idx(bars, base)) if squats > 0 else 999

    # 8. Calendar
    dte = days_to_next_earnings(symbol, as_of_str)
    cur_d = pd.to_datetime(as_of_str).date()
    weeks_ipo = round(max(0.0, (cur_d - list_date).days / 7.0), 1)

    # 9. Moving average slope 22d
    if T >= 221 and not np.isnan(sma200):
        prev_sma200 = float(bars['close'].iloc[T - 221:T - 199].mean())
        dma_200_slope_22d = round((sma200 / max(prev_sma200, 0.01) - 1.0) * 100.0, 3)
    else:
        dma_200_slope_22d = np.nan

    dma_150_above_200 = int(sma150 > sma200) if (not np.isnan(sma150) and not np.isnan(sma200)) else np.nan

    return {
        "base_depth_pct": round(base.depth_pct, 2),
        "duration_weeks": round(base.duration_weeks, 1),
        "coil_ratio": round(atr_second / max(atr_first, 0.001), 3) if atr_first > 0 else 1.0,
        "dry_ratio": round(vol_second / max(vol_first, 1.0), 3) if vol_first > 0 else 1.0,
        "num_contractions": num_contractions,
        "depth_contraction_ratio": depth_contraction_ratio,
        "tightness_5d": tightness_5d,
        "tightness_10d": tightness_10d,
        "days_since_peak": T - base.peak_idx,
        "position_in_base": round((c - base.floor) / max(base.pivot - base.floor, 0.01), 3),

        "dry_depth": dry_depth,
        "ud_ratio": ud_ratio,
        "breakout_volume_ratio": round(float(bars['volume'].iloc[T]) / max(vol_sma20_prior, 1.0), 2),
        "volume_slope_base": volume_slope_base,
        "max_volume_day_ratio": max_volume_day_ratio,
        "volume_contraction_ratio": volume_contraction_ratio,

        "rs_rating": int(rs_rating),
        "dist_from_52w_high_pct": round((high_52w - c) / max(high_52w, 0.01) * 100.0, 2),
        "dist_from_52w_low_pct": round((c - low_52w) / max(low_52w, 0.01) * 100.0, 2),
        "pct_above_50dma": round((c / max(sma50, 0.01) - 1.0) * 100.0, 2),
        "pct_above_150dma": round((c / max(sma150, 0.01) - 1.0) * 100.0, 2) if not np.isnan(sma150) else np.nan,
        "pct_above_200dma": round((c / max(sma200, 0.01) - 1.0) * 100.0, 2) if not np.isnan(sma200) else np.nan,
        "dma_150_above_200": dma_150_above_200,
        "dma_200_slope_22d": dma_200_slope_22d,

        "breadth_above_20dma": float(breadth.get("pct_above_20dma", 50.0)),
        "breadth_above_50dma": float(breadth.get("pct_above_50dma", 50.0)),
        "breadth_above_200dma": float(breadth.get("pct_above_200dma", 50.0)),
        "regime_state_ord": regime_ord,

        "sector_return_5d": sec_feats["ret_5d"],
        "sector_return_20d": sec_feats["ret_20d"],
        "sector_rs_rank": sec_feats["rank"],
        "sector_breadth": sec_feats["breadth"],
        "stock_vs_sector_rs": stock_vs_sector_rs,

        "squat_count_in_base": squats,
        "failed_poke_count_in_base": failed_pokes,
        "days_since_last_squat": days_since_squat,

        "days_to_next_earnings": min(dte, 60),
        "weeks_since_ipo": weeks_ipo,

        "_meta": {
            "symbol": symbol,
            "as_of": as_of_str,
            "pivot": base.pivot,
            "floor": base.floor,
            "context": ctx,
            "entry": c,
        }
    }
