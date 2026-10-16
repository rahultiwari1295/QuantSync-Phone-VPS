"""
core/features.py — Lookahead-Free Fibonacci Feature Pipeline Generator
======================================================================
Implements Section 10 of Specification:
Exposes Trend-Based Fibonacci structural state and events as ML-ready features.
Every feature is computed strictly using information available at bar T close.
Zero lookahead bias.
"""

import logging
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import numpy as np

from core.swing_detector import SwingDetector, FibStructure
from core.signal_detector import SignalDetector
from core.fibonacci_calculator import compute_level_distance, compute_extension_position

logger = logging.getLogger("QuantSync.Lines.Features")

FIBONACCI_FEATURE_COLUMNS = [
    "fib_status",
    "fib_breakout_1_0",
    "fib_breakout_1_272",
    "fib_breakout_1_618",
    "fib_distance_1_0",
    "fib_distance_1_272",
    "fib_distance_1_618",
    "fib_extension_position",
    "fib_breakout_volume_ratio",
    "fib_breakout_candle_strength",
    "fib_structure_age",
    "fib_impulse_pct",
    "fib_pullback_pct",
    "fib_c_to_b_pct",
    "fib_confluence_score"
]


def compute_fibonacci_features(
    symbol: str,
    weekly_df: pd.DataFrame,
    detector: Optional[SwingDetector] = None,
    signal_det: Optional[SignalDetector] = None
) -> pd.DataFrame:
    """
    Augments weekly_df with all standardized Fibonacci feature columns.
    Enforces point-in-time causality: At row i, only pivots and signals
    confirmed at or before i are evaluated.
    """
    df = weekly_df.copy()
    n = len(df)
    if n == 0:
        for col in FIBONACCI_FEATURE_COLUMNS:
            df[col] = 0.0
        return df

    det = detector or SwingDetector()
    sig_det = signal_det or SignalDetector()

    # Discover all structures across time
    structures = det.discover_structures(symbol, df)

    # Initialize feature columns
    feat_data = {col: [0.0] * n for col in FIBONACCI_FEATURE_COLUMNS}
    feat_data["fib_status"] = ["NO_VALID_STRUCTURE"] * n

    # Pre-calculate volume SMA10
    vol_series = df['volume']
    vol_sma = vol_series.rolling(10, min_periods=3).mean()

    # Pre-calculate 10w EMA for confluence
    ema10 = df['close'].ewm(span=10, adjust=False).mean()

    # Precompute breakout events across all structures
    breakout_events_by_bar = {}
    for struct in structures:
        bo_events, _ = sig_det.detect_signals_for_structure(struct, df)
        for ev in bo_events:
            if ev.bar_index not in breakout_events_by_bar:
                breakout_events_by_bar[ev.bar_index] = []
            breakout_events_by_bar[ev.bar_index].append(ev)

    for i in range(n):
        close_p = float(df['close'].iloc[i])
        high_p = float(df['high'].iloc[i])
        low_p = float(df['low'].iloc[i])
        vol_p = float(df['volume'].iloc[i])
        v_sma = float(vol_sma.iloc[i]) if not np.isnan(vol_sma.iloc[i]) and vol_sma.iloc[i] > 0 else 1.0

        # Volume ratio and candle strength
        rng = max(high_p - low_p, 1e-6)
        v_ratio = vol_p / v_sma
        candle_strength = (close_p - low_p) / rng
        feat_data["fib_breakout_volume_ratio"][i] = round(v_ratio, 2)
        feat_data["fib_breakout_candle_strength"][i] = round(candle_strength, 4)

        # Check for breakout events on this specific bar
        if i in breakout_events_by_bar:
            evs = breakout_events_by_bar[i]
            for ev in evs:
                if ev.level_ratio == 1.000:
                    feat_data["fib_breakout_1_0"][i] = 1.0
                elif ev.level_ratio == 1.272:
                    feat_data["fib_breakout_1_272"][i] = 1.0
                elif ev.level_ratio == 1.618:
                    feat_data["fib_breakout_1_618"][i] = 1.0

        # Get active structure at bar i
        active_struct = det.get_active_structure_at_bar(structures, i, df)
        if active_struct is None:
            feat_data["fib_status"][i] = "NO_VALID_STRUCTURE"
            continue

        feat_data["fib_status"][i] = "ACTIVE_STRUCTURE"
        proj = active_struct.projection

        # Distances to levels
        feat_data["fib_distance_1_0"][i] = compute_level_distance(close_p, proj.level_1_000)
        feat_data["fib_distance_1_272"][i] = compute_level_distance(close_p, proj.level_1_272)
        feat_data["fib_distance_1_618"][i] = compute_level_distance(close_p, proj.level_1_618)
        feat_data["fib_extension_position"][i] = compute_extension_position(close_p, proj)

        # Structural attributes
        age_weeks = i - active_struct.c_point.bar_index
        feat_data["fib_structure_age"][i] = max(0, age_weeks)
        feat_data["fib_impulse_pct"][i] = proj.impulse_pct
        feat_data["fib_pullback_pct"][i] = proj.pullback_pct

        c_to_b = ((active_struct.b_price - active_struct.c_price) / active_struct.b_price) * 100.0
        feat_data["fib_c_to_b_pct"][i] = round(c_to_b, 2)

        # Confluence Score calculation (0 to 100)
        confluence = 30.0  # Base for valid structure
        if v_ratio >= 1.20:
            confluence += 25.0
        elif v_ratio >= 1.0:
            confluence += 15.0

        if candle_strength >= 0.65:
            confluence += 20.0
        elif candle_strength >= 0.50:
            confluence += 10.0

        if close_p > float(ema10.iloc[i]):
            confluence += 15.0

        # Healthy Fibonacci retracement (between 38.2% and 61.8%)
        if 38.2 <= proj.pullback_pct <= 61.8:
            confluence += 10.0

        feat_data["fib_confluence_score"][i] = round(confluence, 1)

    for col in FIBONACCI_FEATURE_COLUMNS:
        df[col] = feat_data[col]

    return df
