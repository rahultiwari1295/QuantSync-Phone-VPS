"""
banana_engine/core/structure.py — Deterministic Structure Engine (Layer 1)
===========================================================================
Implements point-in-time trend template filtering, swing extrema detection,
reverse base finding, context classification, micro-events (Squat, Failed Poke),
RS ratings, and market regime states matching Section 3 of BananaPatterns v4.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import numpy as np


# -----------------------------------------------------------------------------
# 3.1 Global Constants & Defaults
# -----------------------------------------------------------------------------
MIN_BASE_WEEKS              = 3
MAX_BASE_WEEKS              = 400     # >7y is not a base; it's a hold
MIN_BASE_DEPTH_PCT          = 5.0     # below this, not a real consolidation
MAX_BASE_DEPTH_PCT          = 35.0    # above this, distribution
MAX_BASE_DEPTH_PCT_IPO      = 50.0    # IPOs are structurally wilder

SWING_LEFT                  = 5       # bars left of extrema (daily)
SWING_RIGHT                 = 5

BREAKOUT_VOLUME_RATIO_MIN   = 1.5     # breakout day vs prior 20d SMA volume
BREAKOUT_PROXIMITY_PCT      = 1.0     # close within 1% of pivot
BREAKOUT_MAX_EXTENSION_PCT  = 6.0     # don't call a +10% close a "fresh breakout"

SQUAT_POKE_PROXIMITY        = 0.99
SQUAT_CIR_MAX               = 0.40
SQUAT_VOLUME_RATIO_MIN      = 1.25

FAILED_POKE_LOOKAHEAD_DAYS  = 5
FAILED_POKE_RECLAIM_PCT     = 0.98

ATR_PERIOD                  = 14
DRY_DEPTH_LOOKBACK          = 50      # median volume window ending at base start
RS_WEIGHTS                  = (2.0, 1.0, 1.0)   # 3m, 6m, 12m
RS_LOOKBACKS                = (63, 125, 250)

CONTEXT_GATES = {
    "standard_vcp": dict(min_weeks=3,  max_depth=35, min_vol_ratio=1.5, min_rs=70),
    "blue_sky":     dict(min_weeks=3,  max_depth=35, min_vol_ratio=1.5, min_rs=70),
    "multi_year":   dict(min_weeks=45, max_depth=35, min_vol_ratio=1.5, min_rs=70),
    "ipo_base":     dict(min_weeks=3,  max_depth=50, min_vol_ratio=1.3, min_rs=50),
}


@dataclass
class MicroEvent:
    event_type: str  # "squat", "failed_poke"
    date: str
    price: float
    volume_ratio: float = 1.0
    close_in_range: float = 0.5


@dataclass
class BaseObject:
    symbol: str
    peak_idx: int
    pivot: float
    floor: float
    depth_pct: float
    duration_weeks: float
    coil_ratio: float
    dry_ratio: float
    dry_depth: float
    ud_ratio: float


def mean_atr_pct(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """
    Computes average ATR percentage (True Range / Close * 100) across a dataframe.
    """
    if len(df) < 2:
        return 1.0
    highs = df['high'].values
    lows = df['low'].values
    closes = df['close'].values
    prev_closes = np.roll(closes, 1)
    prev_closes[0] = closes[0]

    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)))
    atr_pct = (tr / np.maximum(closes, 0.01)) * 100.0
    return float(np.nanmean(atr_pct))


# -----------------------------------------------------------------------------
# 3.2 Trend Template Gate (Minervini Stage 2 Filter)
# -----------------------------------------------------------------------------
def passes_trend_template(
    bars: pd.DataFrame,
    as_of_idx: int,
    rs_rating: int,
    min_rs: int = 70,
    min_dist_from_low_pct: float = 30.0,
    max_dist_from_high_pct: float = 25.0,
    require_200dma_rising: bool = True
) -> bool:
    """
    Strict point-in-time Minervini Trend Template verification.
    """
    n = as_of_idx + 1
    if n < 150:
        return False

    c = float(bars['close'].iloc[as_of_idx])
    close_series = bars['close'].iloc[:n]

    sma50 = close_series.iloc[-50:].mean() if n >= 50 else c
    sma150 = close_series.iloc[-150:].mean() if n >= 150 else c
    sma200 = close_series.iloc[-200:].mean() if n >= 200 else sma150

    high_52w = bars['high'].iloc[max(0, n - 252):n].max()
    low_52w = bars['low'].iloc[max(0, n - 252):n].min()

    checks = {
        "price_above_150_200": c > sma150 and (c > sma200 if n >= 200 else True),
        "150_above_200": (sma150 > sma200) if n >= 200 else True,
        "50_above_150_200": (sma50 > sma150 and (sma50 > sma200 if n >= 200 else True)),
        "price_above_50": c > sma50,
        "rs_strong": rs_rating >= min_rs,
        "near_52w_high": (high_52w - c) / max(high_52w, 0.01) * 100 <= max_dist_from_high_pct,
        "above_52w_low": (c - low_52w) / max(low_52w, 0.01) * 100 >= min_dist_from_low_pct,
    }

    if require_200dma_rising and n >= 222:
        sma200_prev = close_series.iloc[-222:-22].mean()
        checks["200_rising"] = sma200 > sma200_prev

    return all(checks.values())


# -----------------------------------------------------------------------------
# 3.3 Swing Detector
# -----------------------------------------------------------------------------
def find_swing_points(bars: pd.DataFrame, left: int = SWING_LEFT, right: int = SWING_RIGHT) -> Tuple[np.ndarray, np.ndarray]:
    """
    Returns (swing_high_idxs, swing_low_idxs) as numpy integer arrays.
    """
    highs = bars['high'].values
    lows = bars['low'].values
    n = len(bars)
    sh, sl = [], []
    for i in range(left, n - right):
        wh = highs[i - left: i + right + 1].max()
        wl = lows[i - left: i + right + 1].min()
        if highs[i] == wh:
            sh.append(i)
        if lows[i] == wl:
            sl.append(i)
    return np.array(sh, dtype=int), np.array(sl, dtype=int)


# -----------------------------------------------------------------------------
# 3.4 Point-in-Time Base Detector
# -----------------------------------------------------------------------------
def find_base_start(
    bars: pd.DataFrame,
    T: int,
    min_depth: float = 0.05,
    max_depth: float = 0.35,
    min_bars: int = 15,
    max_lookback: int = 250
) -> Optional[int]:
    """
    Returns the index of the local peak that started the base ending at T,
    or None if no valid consolidation base exists.
    """
    highs = bars['high'].values
    lows = bars['low'].values
    closes = bars['close'].values

    # Scan backward from T - min_bars
    for peak_idx in range(T - min_bars, max(0, T - max_lookback), -1):
        pivot = highs[peak_idx]

        # Local max check over +/- 15 bars
        w_lo = max(0, peak_idx - 15)
        w_hi = min(len(bars), peak_idx + 16)
        if pivot < highs[w_lo:w_hi].max():
            continue

        floor = lows[peak_idx:T].min()
        depth = (pivot - floor) / max(pivot, 0.01)
        if not (min_depth <= depth <= max_depth):
            continue

        # Base must not have closed above pivot * 1.01 prior to T
        if any(closes[peak_idx + 1:T] > pivot * 1.01):
            continue

        return peak_idx
    return None


def detect_base_at(bars: pd.DataFrame, T: int, rs_ratings: Any = None) -> Optional[BaseObject]:
    """
    Evaluates point-in-time consolidation base ending at bar T.
    """
    if T < 15:
        return None

    peak_idx = find_base_start(bars, T)
    if peak_idx is None:
        return None

    pivot = float(bars['high'].iloc[peak_idx])
    floor = float(bars['low'].iloc[peak_idx:T + 1].min())
    depth_pct = round((pivot - floor) / max(pivot, 0.01) * 100.0, 2)
    duration_weeks = round((T - peak_idx) / 5.0, 1)

    if duration_weeks > MAX_BASE_WEEKS:
        return None

    # Base body (excluding peak bar itself)
    body = bars.iloc[peak_idx + 1:T + 1]
    mid = len(body) // 2
    if mid < 5:
        return None

    first, second = body.iloc[:mid], body.iloc[mid:]

    atr_first = mean_atr_pct(first)
    atr_second = mean_atr_pct(second)
    coil_ratio = round(atr_second / atr_first if atr_first > 0 else 1.0, 3)

    vol_first = first['volume'].mean()
    vol_second = second['volume'].mean()
    dry_ratio = round(vol_second / vol_first if vol_first > 0 else 1.0, 3)

    # Dry depth — PIT: median volume of the 50 bars ENDING at peak_idx
    dry_window_lo = max(0, peak_idx - DRY_DEPTH_LOOKBACK)
    dry_window = bars['volume'].iloc[dry_window_lo:peak_idx]
    if len(dry_window) < 20:
        dry_depth = np.nan
    else:
        dry_depth = round(float(body['volume'].min()) / max(float(dry_window.median()), 1.0), 3)

    up_days = body[body['close'] >= body['open']]
    down_days = body[body['close'] < body['open']]
    up_v = float(up_days['volume'].sum())
    dn_v = float(down_days['volume'].sum())
    ud_ratio = round(up_v / max(dn_v, 1.0), 3) if dn_v > 0 else np.nan

    sym = str(bars['symbol'].iloc[0]) if 'symbol' in bars.columns else "EQUITY"

    return BaseObject(
        symbol=sym,
        peak_idx=peak_idx,
        pivot=round(pivot, 2),
        floor=round(floor, 2),
        depth_pct=depth_pct,
        duration_weeks=duration_weeks,
        coil_ratio=coil_ratio,
        dry_ratio=dry_ratio,
        dry_depth=dry_depth,
        ud_ratio=ud_ratio
    )


# -----------------------------------------------------------------------------
# 3.5 Breakout Confirmation
# -----------------------------------------------------------------------------
def is_breakout_day(bars: pd.DataFrame, T: int, pivot: float, vol_sma20_prior: float) -> bool:
    """
    Confirms price breakout and volume surge on bar T.
    """
    c = float(bars['close'].iloc[T])
    v = float(bars['volume'].iloc[T])
    if c < pivot * (1.0 - BREAKOUT_PROXIMITY_PCT / 100.0):
        return False
    if v < max(vol_sma20_prior, 1.0) * BREAKOUT_VOLUME_RATIO_MIN:
        return False
    return True


# -----------------------------------------------------------------------------
# 3.6 Context Classifier
# -----------------------------------------------------------------------------
def classify_context(base: BaseObject, bars: pd.DataFrame, T: int, listing_date: date, ath_pit: float) -> str:
    """
    One unified detector mapped into context tags:
    - ipo_base
    - blue_sky
    - multi_year
    - standard_vcp
    """
    c = float(bars['close'].iloc[T])
    cur_date = pd.to_datetime(bars['date'].iloc[T]).date()
    weeks_since_listing = (cur_date - listing_date).days / 7.0

    if weeks_since_listing <= 65:
        return "ipo_base"
    if (ath_pit - c) / max(ath_pit, 0.01) <= 0.08:
        return "blue_sky"
    if base.duration_weeks >= 45:
        return "multi_year"
    return "standard_vcp"


# -----------------------------------------------------------------------------
# 3.7 Micro-Footprints (Squat ⚑ & Failed Poke ✕)
# -----------------------------------------------------------------------------
def detect_squat(bars: pd.DataFrame, T: int, pivot: float, vol_sma20_prior: float) -> Optional[MicroEvent]:
    h = float(bars['high'].iloc[T])
    l = float(bars['low'].iloc[T])
    c = float(bars['close'].iloc[T])
    v = float(bars['volume'].iloc[T])

    if h < pivot * SQUAT_POKE_PROXIMITY:
        return None
    rng = h - l
    cir = (c - l) / rng if rng > 0 else 0.5
    if not (c < pivot or cir <= SQUAT_CIR_MAX):
        return None
    if v < max(vol_sma20_prior, 1.0) * SQUAT_VOLUME_RATIO_MIN:
        return None

    return MicroEvent(
        event_type="squat",
        date=str(bars['date'].iloc[T]),
        price=h,
        volume_ratio=round(v / max(vol_sma20_prior, 1.0), 2),
        close_in_range=round(cir, 2)
    )


def detect_failed_poke(bars: pd.DataFrame, T: int, highest_close_prior: float, base_end_idx: int) -> Optional[MicroEvent]:
    if T >= base_end_idx - 2:
        return None
    c = float(bars['close'].iloc[T])
    h = float(bars['high'].iloc[T])
    if not (c > highest_close_prior * 0.995 and c < h):
        return None
    future = bars.iloc[T + 1: min(len(bars), T + 1 + FAILED_POKE_LOOKAHEAD_DAYS)]
    if future.empty:
        return None
    if future['close'].min() < c * FAILED_POKE_RECLAIM_PCT:
        return MicroEvent(event_type="failed_poke", date=str(bars['date'].iloc[T]), price=h)
    return None


def count_squats_in_base(bars: pd.DataFrame, base: BaseObject, vol_sma20_prior: float) -> int:
    cnt = 0
    for idx in range(base.peak_idx + 1, len(bars)):
        sq = detect_squat(bars, idx, base.pivot, vol_sma20_prior)
        if sq is not None:
            cnt += 1
    return cnt


def count_failed_pokes_in_base(bars: pd.DataFrame, base: BaseObject) -> int:
    cnt = 0
    highest_c = float(bars['close'].iloc[base.peak_idx:len(bars)].max())
    for idx in range(base.peak_idx + 1, len(bars) - 2):
        fp = detect_failed_poke(bars, idx, highest_c, len(bars) - 1)
        if fp is not None:
            cnt += 1
    return cnt


def last_squat_idx(bars: pd.DataFrame, base: BaseObject) -> int:
    for idx in range(len(bars) - 1, base.peak_idx, -1):
        v_prior = bars['volume'].iloc[max(0, idx - 20):idx].mean() if idx >= 5 else 1.0
        if detect_squat(bars, idx, base.pivot, v_prior) is not None:
            return idx
    return base.peak_idx


# -----------------------------------------------------------------------------
# 3.8 RS Rating (Front-Weighted ROC Percentile)
# -----------------------------------------------------------------------------
def compute_rs_scores(universe_close_df: pd.DataFrame) -> pd.DataFrame:
    """
    Computes rolling PIT Relative Strength scores across the universe.
    Front-weighted ROC: 2.0 * 3m + 1.0 * 6m + 1.0 * 12m.
    Normalized into universe percentiles 1 to 99.
    """
    scores = {}
    last_dt = universe_close_df.index[-1] if not universe_close_df.empty else "2026-09-04"
    for sym in universe_close_df.columns:
        s = universe_close_df[sym].dropna()
        if len(s) < 250:
            continue
        c_now = float(s.iloc[-1])
        c_3m  = float(s.iloc[-63]) if len(s) >= 63 else c_now
        c_6m  = float(s.iloc[-125]) if len(s) >= 125 else c_now
        c_1y  = float(s.iloc[-250]) if len(s) >= 250 else c_now
        r = (RS_WEIGHTS[0] * (c_now / max(c_3m, 0.01) - 1.0)
             + RS_WEIGHTS[1] * (c_now / max(c_6m, 0.01) - 1.0)
             + RS_WEIGHTS[2] * (c_now / max(c_1y, 0.01) - 1.0))
        scores[sym] = r

    if not scores:
        return pd.DataFrame()

    ret = pd.DataFrame([scores], index=[last_dt])
    ranks = ret.rank(axis=1, pct=True)
    rs = (ranks * 99).clip(1, 99).round().astype("Int64")
    return rs


# -----------------------------------------------------------------------------
# 3.9 Market Breadth & Regime State
# -----------------------------------------------------------------------------
def regime_state(breadth: Dict[str, Any]) -> str:
    """
    Returns regime state: RISK_ON, NEUTRAL, or RISK_OFF.
    """
    p50 = float(breadth.get("pct_above_50dma", 50.0))
    p200 = float(breadth.get("pct_above_200dma", 50.0))
    if p50 >= 60.0 and p200 >= 60.0:
        return "RISK_ON"
    if p50 <= 35.0:
        return "RISK_OFF"
    return "NEUTRAL"
