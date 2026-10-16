"""
core/swing_detector.py — Causal Weekly Swing Detection & A->B->C Structure Builder
=================================================================================
Discovers weekly swing lows and swing highs with strict point-in-time causality.
Pivots are confirmed ONLY after pivot_right_bars have elapsed.
Constructs and maintains valid Trend-Based Fibonacci A->B->C structures:
  A = Confirmed Swing Low
  B = Confirmed Swing High (after A, impulse >= min_impulse_pct)
  C = Confirmed Pullback Low (after B, C > A, pullback >= min_pullback_pct)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import numpy as np

from core.fibonacci_calculator import FibProjection, calculate_trend_fibonacci

logger = logging.getLogger("QuantSync.Lines.SwingDetector")


@dataclass
class SwingPoint:
    """
    A detected swing high or swing low.
    """
    bar_index: int
    date: str
    price: float
    pivot_type: str  # "LOW" or "HIGH"
    confirmed_bar_index: int
    confirmed_date: str

    def is_confirmed_by(self, current_bar_index: int) -> bool:
        """Returns True if this pivot is strictly confirmed at or before current_bar_index."""
        return self.confirmed_bar_index <= current_bar_index


@dataclass
class FibStructure:
    """
    A validated Trend-Based Fibonacci structure: A -> B -> C
    """
    structure_id: str
    symbol: str
    a_point: SwingPoint
    b_point: SwingPoint
    c_point: SwingPoint
    activated_bar_index: int
    activated_date: str
    projection: FibProjection
    status: str = "ACTIVE"  # "ACTIVE", "INVALIDATED", "COMPLETED", "SUPERSEDED"
    invalidation_date: Optional[str] = None
    invalidation_reason: Optional[str] = None

    @property
    def a_price(self) -> float:
        return self.a_point.price

    @property
    def b_price(self) -> float:
        return self.b_point.price

    @property
    def c_price(self) -> float:
        return self.c_point.price

    @property
    def a_date(self) -> str:
        return self.a_point.date

    @property
    def b_date(self) -> str:
        return self.b_point.date

    @property
    def c_date(self) -> str:
        return self.c_point.date


class SwingDetector:
    """
    Lookahead-free swing detector and Fibonacci structure identifier.
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

    def detect_pivots(self, weekly_df: pd.DataFrame) -> List[SwingPoint]:
        """
        Scans weekly candles and returns all confirmed swing points.
        Point-in-time confirmation: pivot at i is confirmed at i + pivot_right_bars.
        """
        n = len(weekly_df)
        if n < (self.pivot_left_bars + self.pivot_right_bars + 1):
            return []

        lows = weekly_df['low'].values
        highs = weekly_df['high'].values
        dates = weekly_df['date'].values

        pivots: List[SwingPoint] = []

        for i in range(self.pivot_left_bars, n - self.pivot_right_bars):
            left_slice_l = lows[i - self.pivot_left_bars: i]
            right_slice_l = lows[i + 1: i + self.pivot_right_bars + 1]

            left_slice_h = highs[i - self.pivot_left_bars: i]
            right_slice_h = highs[i + 1: i + self.pivot_right_bars + 1]

            current_l = lows[i]
            current_h = highs[i]

            # Swing Low check
            is_min = (current_l > 0) and (current_l <= left_slice_l.min()) and (current_l <= right_slice_l.min())
            strict_min = (current_l < left_slice_l.max()) or (current_l < right_slice_l.max())
            if is_min and strict_min:
                conf_idx = i + self.pivot_right_bars
                pivots.append(SwingPoint(
                    bar_index=i,
                    date=str(dates[i]),
                    price=float(current_l),
                    pivot_type="LOW",
                    confirmed_bar_index=conf_idx,
                    confirmed_date=str(dates[conf_idx])
                ))

            # Swing High check
            is_max = (current_h > 0) and (current_h >= left_slice_h.max()) and (current_h >= right_slice_h.max())
            strict_max = (current_h > left_slice_h.min()) or (current_h > right_slice_h.min())
            if is_max and strict_max:
                conf_idx = i + self.pivot_right_bars
                pivots.append(SwingPoint(
                    bar_index=i,
                    date=str(dates[i]),
                    price=float(current_h),
                    pivot_type="HIGH",
                    confirmed_bar_index=conf_idx,
                    confirmed_date=str(dates[conf_idx])
                ))

        # Sort strictly by bar_index, then pivot_type
        pivots.sort(key=lambda p: (p.bar_index, p.pivot_type))
        return pivots

    def discover_structures(
        self,
        symbol: str,
        weekly_df: pd.DataFrame
    ) -> List[FibStructure]:
        """
        Discovers all historical and active A->B->C Fibonacci structures across time.
        Strictly causal: A structure is born when C is confirmed.
        """
        pivots = self.detect_pivots(weekly_df)
        if not pivots:
            return []

        low_pivots = [p for p in pivots if p.pivot_type == "LOW"]
        high_pivots = [p for p in pivots if p.pivot_type == "HIGH"]

        structures: List[FibStructure] = []
        struct_seq = 1

        for b in high_pivots:
            # Candidate A: low pivots prior to B (within 52 weeks)
            valid_a_candidates = [
                a for a in low_pivots
                if a.price > 0
                and a.bar_index < b.bar_index
                and b.bar_index - a.bar_index >= self.min_swing_distance
                and b.bar_index - a.bar_index <= 52
                and b.price > a.price
                and ((b.price - a.price) / a.price * 100.0) >= self.min_impulse_pct
            ]

            for a in valid_a_candidates:
                # Ensure no intermediate high between A and B was higher than B
                inter_highs = [h for h in high_pivots if a.bar_index < h.bar_index < b.bar_index]
                if any(h.price > b.price for h in inter_highs):
                    continue

                impulse = b.price - a.price

                # Candidate C pivots: Low pivots occurring after B (within 40 weeks)
                valid_c_candidates = [
                    c for c in low_pivots
                    if c.bar_index > b.bar_index
                    and c.bar_index - b.bar_index >= self.min_swing_distance
                    and c.bar_index - b.bar_index <= 40
                    and c.price > a.price  # Invariant: C must NOT break below A
                    and c.price < b.price  # Invariant: C must be a pullback from B
                ]

                for c in valid_c_candidates:
                    # Ensure no intermediate high between B and C broke above B
                    inter_highs_bc = [h for h in high_pivots if b.bar_index < h.bar_index < c.bar_index]
                    if any(h.price > b.price for h in inter_highs_bc):
                        continue

                    pullback_amount = b.price - c.price
                    pullback_pct = (pullback_amount / impulse) * 100.0

                    # Must represent a meaningful pullback, but not an invalidating crash
                    if not (self.min_pullback_pct <= pullback_pct <= self.max_pullback_pct):
                        continue

                    # Structure is activated when C is confirmed
                    activation_idx = c.confirmed_bar_index
                    if activation_idx >= len(weekly_df):
                        continue
                    activation_date = str(weekly_df.iloc[activation_idx]['date'])

                    try:
                        projection = calculate_trend_fibonacci(a.price, b.price, c.price)
                    except ValueError:
                        continue

                    # Construct unique structure ID: e.g. IFCI_FIB_2025_001
                    a_year = str(a.date).split("-")[0]
                    struct_id = f"{symbol.upper()}_FIB_{a_year}_{struct_seq:03d}"
                    struct_seq += 1

                    structure = FibStructure(
                        structure_id=struct_id,
                        symbol=symbol.upper(),
                        a_point=a,
                        b_point=b,
                        c_point=c,
                        activated_bar_index=activation_idx,
                        activated_date=activation_date,
                        projection=projection,
                        status="ACTIVE"
                    )
                    structures.append(structure)

        # Sort structures by activation bar index
        structures.sort(key=lambda s: s.activated_bar_index)
        return structures

    def get_active_structure_at_bar(
        self,
        structures: List[FibStructure],
        bar_index: int,
        weekly_df: pd.DataFrame
    ) -> Optional[FibStructure]:
        """
        Returns the single most relevant active Fibonacci structure for a given bar_index.
        A structure is active at bar_index if:
          1. activated_bar_index <= bar_index
          2. price between activation and bar_index has not closed below C (or A)
        If multiple structures are active, returns the most recently activated one.
        If no structure is valid, returns None (fib_status = NO_VALID_STRUCTURE).
        """
        valid_active: List[FibStructure] = []

        for struct in structures:
            if struct.activated_bar_index > bar_index:
                continue

            # Check for structural invalidation between activation and bar_index
            start_check = struct.c_point.bar_index
            window = weekly_df.iloc[start_check: bar_index + 1]

            # Invalidation rule: Close below C low breaks the setup
            broke_c = (window['close'] < struct.c_point.price).any()
            broke_a = (window['low'] < struct.a_point.price).any()

            if not broke_c and not broke_a:
                valid_active.append(struct)

        if not valid_active:
            return None

        # Return the most recently activated structure
        return valid_active[-1]
