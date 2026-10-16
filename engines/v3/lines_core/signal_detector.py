"""
core/signal_detector.py — Weekly Fibonacci Breakout & Event Detection
=====================================================================
Performs strict point-in-time event detection on weekly candles:
  - FIB_1_000_BREAKOUT: prev_close <= Fib_1_000 and curr_close > Fib_1_000
  - FIB_1_272_BREAKOUT: prev_close <= Fib_1_272 and curr_close > Fib_1_272
  - FIB_1_618_BREAKOUT: prev_close <= Fib_1_618 and curr_close > Fib_1_618
  - Rejection at level
  - False breakout detection
  - Volume surge confirmation
  - Duplicate signal prevention per structure
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Any, Set
import pandas as pd
import numpy as np

from core.swing_detector import FibStructure
from core.fibonacci_calculator import compute_level_distance

logger = logging.getLogger("QuantSync.Lines.SignalDetector")


@dataclass
class BreakoutEvent:
    """
    Standardized, lookahead-free Fibonacci breakout event record.
    """
    signal_id: str
    structure_id: str
    symbol: str
    signal_date: str
    bar_index: int
    event_type: str  # "FIB_1_000_BREAKOUT", "FIB_1_272_BREAKOUT", "FIB_1_618_BREAKOUT"
    level_ratio: float
    level_price: float
    prev_close: float
    curr_close: float
    candle_open: float
    candle_high: float
    candle_low: float
    candle_range_pct: float
    candle_strength: float  # (close - low) / (high - low)
    volume: float
    volume_sma10: float
    volume_ratio: float
    has_volume_confirmation: bool
    pct_above_fib: float
    distance_from_1_618: float
    distance_to_next_fib: float
    weeks_since_c: int
    structure: FibStructure

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['structure_a_date'] = self.structure.a_date
        d['structure_a_price'] = self.structure.a_price
        d['structure_b_date'] = self.structure.b_date
        d['structure_b_price'] = self.structure.b_price
        d['structure_c_date'] = self.structure.c_date
        d['structure_c_price'] = self.structure.c_price
        del d['structure']
        return d


@dataclass
class RejectionEvent:
    """
    Recorded when a weekly candle tests a Fibonacci level but fails to close above it.
    """
    event_id: str
    structure_id: str
    symbol: str
    date: str
    bar_index: int
    level_ratio: float
    level_price: float
    high: float
    close: float
    rejection_pct: float


class SignalDetector:
    """
    Scans weekly candles post-structure activation to detect Fibonacci breakout events.
    """
    def __init__(
        self,
        volume_sma_period: int = 10,
        volume_surge_ratio: float = 1.20,
        rejection_wick_pct: float = 0.30
    ):
        self.volume_sma_period = volume_sma_period
        self.volume_surge_ratio = volume_surge_ratio
        self.rejection_wick_pct = rejection_wick_pct
        self.key_levels = [1.000, 1.272, 1.618]
        self.event_names = {
            1.000: "FIB_1_000_BREAKOUT",
            1.272: "FIB_1_272_BREAKOUT",
            1.618: "FIB_1_618_BREAKOUT"
        }
        self.next_level_map = {
            1.000: 1.272,
            1.272: 1.618,
            1.618: 2.000
        }

    def detect_signals_for_structure(
        self,
        structure: FibStructure,
        weekly_df: pd.DataFrame
    ) -> Tuple[List[BreakoutEvent], List[RejectionEvent]]:
        """
        Evaluates weekly candles from structure activation onwards for breakout and rejection events.
        Enforces:
          - Strict causal evaluation bar by bar
          - At most one breakout event per (structure_id, level_ratio)
        """
        breakouts: List[BreakoutEvent] = []
        rejections: List[RejectionEvent] = []

        n = len(weekly_df)
        start_bar = structure.activated_bar_index
        if start_bar >= n:
            return breakouts, rejections

        # Calculate volume SMA
        vol_series = weekly_df['volume']
        vol_sma = vol_series.rolling(window=self.volume_sma_period, min_periods=3).mean()

        fired_levels: Set[float] = set()

        for i in range(start_bar, n):
            row = weekly_df.iloc[i]
            prev_row = weekly_df.iloc[i - 1] if i > 0 else row

            c_close = float(row['close'])
            p_close = float(prev_row['close'])
            c_open = float(row['open'])
            c_high = float(row['high'])
            c_low = float(row['low'])
            c_vol = float(row['volume'])
            c_date = str(row['date'])

            # Structural Invalidation Check: Close below C invalidates future breakouts
            if c_close < structure.c_point.price:
                break

            rng = max(c_high - c_low, 1e-6)
            candle_strength = (c_close - c_low) / rng
            candle_range_pct = (rng / c_low * 100.0) if c_low > 0 else 0.0

            v_sma = float(vol_sma.iloc[i - 1]) if i > 0 and not np.isnan(vol_sma.iloc[i - 1]) else float(vol_sma.iloc[i])
            v_ratio = (c_vol / v_sma) if v_sma > 0 else 1.0
            has_vol = v_ratio >= self.volume_surge_ratio

            # Check each key level
            for level in self.key_levels:
                if level in fired_levels:
                    continue

                level_price = structure.projection.levels[level]
                level_1_618 = structure.projection.level_1_618
                next_lvl = self.next_level_map.get(level, 2.618)
                next_lvl_price = structure.projection.levels.get(next_lvl, level_price * 1.2)

                # BREAKOUT EVENT: previous weekly close <= level and current weekly close > level
                if p_close <= level_price and c_close > level_price:
                    fired_levels.add(level)
                    pct_above = ((c_close - level_price) / level_price * 100.0) if level_price > 0 else 0.0
                    dist_1618 = ((c_close - level_1_618) / level_1_618 * 100.0) if level_1_618 > 0 else 0.0
                    dist_next = ((next_lvl_price - c_close) / c_close * 100.0) if c_close > 0 else 0.0
                    weeks_since = i - structure.c_point.bar_index

                    evt_id = f"{structure.structure_id}_{str(level).replace('.', '_')}_{c_date}"
                    event = BreakoutEvent(
                        signal_id=evt_id,
                        structure_id=structure.structure_id,
                        symbol=structure.symbol,
                        signal_date=c_date,
                        bar_index=i,
                        event_type=self.event_names[level],
                        level_ratio=level,
                        level_price=round(level_price, 4),
                        prev_close=round(p_close, 4),
                        curr_close=round(c_close, 4),
                        candle_open=round(c_open, 4),
                        candle_high=round(c_high, 4),
                        candle_low=round(c_low, 4),
                        candle_range_pct=round(candle_range_pct, 2),
                        candle_strength=round(candle_strength, 4),
                        volume=c_vol,
                        volume_sma10=round(v_sma, 2),
                        volume_ratio=round(v_ratio, 2),
                        has_volume_confirmation=has_vol,
                        pct_above_fib=round(pct_above, 2),
                        distance_from_1_618=round(dist_1618, 2),
                        distance_to_next_fib=round(dist_next, 2),
                        weeks_since_c=weeks_since,
                        structure=structure
                    )
                    breakouts.append(event)

                # REJECTION EVENT: tested above level during week but closed below
                elif c_high >= level_price and c_close < level_price and p_close < level_price:
                    upper_wick = c_high - max(c_open, c_close)
                    if (upper_wick / rng) >= self.rejection_wick_pct:
                        rej_id = f"REJ_{structure.structure_id}_{str(level).replace('.', '_')}_{c_date}"
                        rejections.append(RejectionEvent(
                            event_id=rej_id,
                            structure_id=structure.structure_id,
                            symbol=structure.symbol,
                            date=c_date,
                            bar_index=i,
                            level_ratio=level,
                            level_price=round(level_price, 4),
                            high=round(c_high, 4),
                            close=round(c_close, 4),
                            rejection_pct=round(((c_high - c_close) / level_price) * 100.0, 2)
                        ))

        return breakouts, rejections
