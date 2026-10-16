"""
engines/v3/fibonacci.py — Trend-Based Fibonacci & Structural Swing Calculator
=============================================================================
Ported from QuantSync_Lines for production V3 execution.
Mathematical formulation:
  Structure: A (Confirmed Swing Low) -> B (Confirmed Swing High) -> C (Pullback Low)
  Impulse:   B - A
  Extension: level(r) = C + (B - A) * r
  Key Expansion Ratios: 0.000, 0.382, 0.500, 0.618, 1.000, 1.272, 1.618, 2.000
"""

import math
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import pandas as pd

logger = logging.getLogger("ProdVPS.V3.Fibonacci")

DEFAULT_FIB_RATIOS = [0.000, 0.236, 0.382, 0.500, 0.618, 0.786, 1.000, 1.272, 1.618, 2.000]


@dataclass(frozen=True)
class FibProjection:
    a_price: float
    b_price: float
    c_price: float
    impulse: float
    impulse_pct: float
    pullback_pct: float
    levels: Dict[float, float]

    @property
    def level_1_000(self) -> float:
        return self.levels.get(1.000, self.b_price)

    @property
    def level_1_272(self) -> float:
        return self.levels.get(1.272, round(self.c_price + self.impulse * 1.272, 2))

    @property
    def level_1_618(self) -> float:
        return self.levels.get(1.618, round(self.c_price + self.impulse * 1.618, 2))

    @property
    def level_2_000(self) -> float:
        return self.levels.get(2.000, round(self.c_price + self.impulse * 2.000, 2))


def calculate_trend_fibonacci(
    a_price: float,
    b_price: float,
    c_price: float,
    ratios: Optional[List[float]] = None
) -> FibProjection:
    if a_price <= 0:
        raise ValueError(f"A price must be positive: {a_price}")
    if b_price <= a_price:
        raise ValueError(f"B price ({b_price}) must be greater than A price ({a_price})")
    if c_price <= a_price:
        raise ValueError(f"C price ({c_price}) must be greater than A price ({a_price})")
    if c_price >= b_price:
        raise ValueError(f"C price ({c_price}) must be less than B price ({b_price})")

    if ratios is None:
        ratios = DEFAULT_FIB_RATIOS

    impulse = b_price - a_price
    impulse_pct = (impulse / a_price) * 100.0
    pullback_amount = b_price - c_price
    pullback_pct = (pullback_amount / impulse) * 100.0

    levels = {}
    for r in ratios:
        price_level = c_price + (impulse * r)
        levels[round(r, 3)] = round(price_level, 2)

    return FibProjection(
        a_price=round(a_price, 2),
        b_price=round(b_price, 2),
        c_price=round(c_price, 2),
        impulse=round(impulse, 2),
        impulse_pct=round(impulse_pct, 2),
        pullback_pct=round(pullback_pct, 2),
        levels=levels
    )


@dataclass
class SwingPoint:
    bar_index: int
    date: str
    price: float
    pivot_type: str  # "LOW" or "HIGH"
    confirmed_bar_index: int
    confirmed_date: str

    def is_confirmed_by(self, current_bar_index: int) -> bool:
        return self.confirmed_bar_index <= current_bar_index


@dataclass
class FibStructure:
    structure_id: str
    symbol: str
    a_point: SwingPoint
    b_point: SwingPoint
    c_point: SwingPoint
    activated_bar_index: int
    activated_date: str
    projection: FibProjection
    status: str = "ACTIVE"

    @property
    def a_price(self) -> float:
        return self.a_point.price

    @property
    def b_price(self) -> float:
        return self.b_point.price

    @property
    def c_price(self) -> float:
        return self.c_point.price


class SwingDetector:
    """
    Lookahead-free weekly swing detector:
      - Pivot confirmation: 3 bars prior (left), 2 bars subsequent (right).
      - Strict point-in-time activation: Bar i is invisible until bar i + 2 closes.
    """
    def __init__(
        self,
        pivot_left_bars: int = 3,
        pivot_right_bars: int = 2,
        min_impulse_pct: float = 20.0,
        min_swing_distance: int = 2,
        min_pullback_pct: float = 15.0,
        max_pullback_pct: float = 95.0
    ):
        self.pivot_left_bars = pivot_left_bars
        self.pivot_right_bars = pivot_right_bars
        self.min_impulse_pct = min_impulse_pct
        self.min_swing_distance = min_swing_distance
        self.min_pullback_pct = min_pullback_pct
        self.max_pullback_pct = max_pullback_pct

    def detect_swings(self, df: pd.DataFrame) -> List[SwingPoint]:
        n = len(df)
        if n < self.pivot_left_bars + self.pivot_right_bars + 1:
            return []

        highs = df['high'].values
        lows = df['low'].values
        dates = df['date'].astype(str).str[:10].values
        swings: List[SwingPoint] = []

        for i in range(self.pivot_left_bars, n - self.pivot_right_bars):
            # Check swing high
            is_high = True
            for l in range(1, self.pivot_left_bars + 1):
                if highs[i - l] >= highs[i]:
                    is_high = False
                    break
            if is_high:
                for r in range(1, self.pivot_right_bars + 1):
                    if highs[i + r] >= highs[i]:
                        is_high = False
                        break
            if is_high:
                conf_idx = i + self.pivot_right_bars
                swings.append(SwingPoint(
                    bar_index=i,
                    date=dates[i],
                    price=float(highs[i]),
                    pivot_type="HIGH",
                    confirmed_bar_index=conf_idx,
                    confirmed_date=dates[conf_idx]
                ))

            # Check swing low
            is_low = True
            for l in range(1, self.pivot_left_bars + 1):
                if lows[i - l] <= lows[i]:
                    is_low = False
                    break
            if is_low:
                for r in range(1, self.pivot_right_bars + 1):
                    if lows[i + r] <= lows[i]:
                        is_low = False
                        break
            if is_low:
                conf_idx = i + self.pivot_right_bars
                swings.append(SwingPoint(
                    bar_index=i,
                    date=dates[i],
                    price=float(lows[i]),
                    pivot_type="LOW",
                    confirmed_bar_index=conf_idx,
                    confirmed_date=dates[conf_idx]
                ))

        swings.sort(key=lambda s: s.bar_index)
        return swings

    def build_active_structure(
        self,
        symbol: str,
        df: pd.DataFrame,
        current_bar_index: int
    ) -> Optional[FibStructure]:
        swings = self.detect_swings(df)
        confirmed_swings = [s for s in swings if s.is_confirmed_by(current_bar_index)]
        if len(confirmed_swings) < 3:
            return None

        # Look backwards for valid C -> B -> A
        for c_idx in range(len(confirmed_swings) - 1, -1, -1):
            c_pt = confirmed_swings[c_idx]
            if c_pt.pivot_type != "LOW":
                continue

            for b_idx in range(c_idx - 1, -1, -1):
                b_pt = confirmed_swings[b_idx]
                if b_pt.pivot_type != "HIGH":
                    continue
                if (c_pt.bar_index - b_pt.bar_index) < self.min_swing_distance:
                    continue

                for a_idx in range(b_idx - 1, -1, -1):
                    a_pt = confirmed_swings[a_idx]
                    if a_pt.pivot_type != "LOW":
                        continue
                    if (b_pt.bar_index - a_pt.bar_index) < self.min_swing_distance:
                        continue

                    # Validation
                    if b_pt.price <= a_pt.price:
                        continue
                    impulse = b_pt.price - a_pt.price
                    impulse_pct = (impulse / a_pt.price) * 100.0
                    if impulse_pct < self.min_impulse_pct:
                        continue

                    if c_pt.price <= a_pt.price or c_pt.price >= b_pt.price:
                        continue
                    pullback_pct = ((b_pt.price - c_pt.price) / impulse) * 100.0
                    if not (self.min_pullback_pct <= pullback_pct <= self.max_pullback_pct):
                        continue

                    # Check current price has not broken C
                    cur_close = float(df['close'].iloc[current_bar_index])
                    if cur_close < c_pt.price:
                        continue

                    try:
                        proj = calculate_trend_fibonacci(a_pt.price, b_pt.price, c_pt.price)
                        return FibStructure(
                            structure_id=f"{symbol}_{a_pt.date}_{b_pt.date}_{c_pt.date}",
                            symbol=symbol,
                            a_point=a_pt,
                            b_point=b_pt,
                            c_point=c_pt,
                            activated_bar_index=c_pt.confirmed_bar_index,
                            activated_date=c_pt.confirmed_date,
                            projection=proj,
                            status="ACTIVE"
                        )
                    except Exception:
                        continue
        return None


def aggregate_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Converts daily OHLCV DataFrame into weekly ISO bars."""
    if daily_df is None or daily_df.empty:
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])

    df = daily_df.copy()
    if 't' in df.columns and 'date' not in df.columns:
        df.rename(columns={'t': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)

    df['date_dt'] = pd.to_datetime(df['date'])
    df.sort_values('date_dt', inplace=True)

    iso = df['date_dt'].dt.isocalendar()
    df['iso_year'] = iso.year
    df['iso_week'] = iso.week

    weekly_df = (
        df.groupby(['iso_year', 'iso_week'], as_index=False, sort=False)
        .agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
            'date': 'last'
        })
    )

    cols = ['date', 'open', 'high', 'low', 'close', 'volume']
    return weekly_df[cols].sort_values('date').reset_index(drop=True)
