"""
core/order_models.py — Precise Entry, Stop Loss, and Target Calculations
========================================================================
Supports strictly separated entry definitions:
  - ENTRY_MODEL_A: Confirmed weekly close (Friday close of breakout week)
  - ENTRY_MODEL_B: Next-week executable open (Monday open of following week)

Supports configurable stop models:
  - STOP_MODEL_A: Below C low (with optional buffer)
  - STOP_MODEL_B: Below breakout level by ATR multiple
  - STOP_MODEL_C: Below most recent confirmed weekly swing low

Calculates Fibonacci target hierarchy:
  - target_1: 1.272
  - target_2: 1.618
  - target_3: 2.000
  - target_4: 2.618
"""

import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Any
import pandas as pd
import numpy as np

from core.signal_detector import BreakoutEvent

logger = logging.getLogger("QuantSync.Lines.OrderModels")


@dataclass
class EntrySpec:
    model_name: str  # "ENTRY_MODEL_A" (Close) or "ENTRY_MODEL_B" (Next Open)
    entry_price: float
    entry_date: str
    is_available: bool  # False if next bar doesn't exist yet for Model B


@dataclass
class StopSpec:
    model_name: str  # "STOP_MODEL_A", "STOP_MODEL_B", "STOP_MODEL_C"
    stop_price: float
    stop_distance_pct: float
    reference_metric: str


@dataclass
class TargetSpec:
    target_1_272: float
    target_1_618: float
    target_2_000: float
    target_2_618: float


@dataclass
class OrderSetup:
    """
    Complete order specification for a Fibonacci breakout signal.
    """
    signal_id: str
    symbol: str
    signal_date: str
    event_type: str
    level_ratio: float
    level_price: float
    entry_a: EntrySpec
    entry_b: Optional[EntrySpec]
    stop_a: StopSpec
    stop_b: StopSpec
    stop_c: StopSpec
    targets: TargetSpec

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "signal_date": self.signal_date,
            "event_type": self.event_type,
            "level_price": self.level_price,
            "entry_model_a_price": self.entry_a.entry_price,
            "entry_model_a_date": self.entry_a.entry_date,
            "entry_model_b_price": self.entry_b.entry_price if self.entry_b else None,
            "entry_model_b_date": self.entry_b.entry_date if self.entry_b else None,
            "stop_model_a_price": self.stop_a.stop_price,
            "stop_model_a_distance_pct": self.stop_a.stop_distance_pct,
            "stop_model_b_price": self.stop_b.stop_price,
            "stop_model_b_distance_pct": self.stop_b.stop_distance_pct,
            "stop_model_c_price": self.stop_c.stop_price,
            "stop_model_c_distance_pct": self.stop_c.stop_distance_pct,
            "target_1_272": self.targets.target_1_272,
            "target_1_618": self.targets.target_1_618,
            "target_2_000": self.targets.target_2_000,
            "target_2_618": self.targets.target_2_618
        }


def compute_weekly_atr(weekly_df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Computes lookahead-free 14-bar True Range smoothed average."""
    high = weekly_df['high']
    low = weekly_df['low']
    prev_close = weekly_df['close'].shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr.fillna(tr.rolling(period, min_periods=1).mean())


def build_order_setup(
    event: BreakoutEvent,
    weekly_df: pd.DataFrame,
    stop_a_buffer_pct: float = 1.0,
    stop_b_atr_mult: float = 1.5,
    stop_c_buffer_pct: float = 1.0
) -> OrderSetup:
    """
    Constructs exact entries, stops, and targets for a BreakoutEvent.
    """
    bar_idx = event.bar_index
    n = len(weekly_df)

    # ENTRY MODEL A: Weekly close of breakout candle
    entry_a = EntrySpec(
        model_name="ENTRY_MODEL_A",
        entry_price=event.curr_close,
        entry_date=event.signal_date,
        is_available=True
    )

    # ENTRY MODEL B: Open of the very next weekly candle
    entry_b = None
    if bar_idx + 1 < n:
        next_open = float(weekly_df.iloc[bar_idx + 1]['open'])
        next_date = str(weekly_df.iloc[bar_idx + 1]['date'])
        entry_b = EntrySpec(
            model_name="ENTRY_MODEL_B",
            entry_price=round(next_open, 4),
            entry_date=next_date,
            is_available=True
        )

    # Primary reference entry for stop percentage calculation
    ref_entry = entry_a.entry_price

    # STOP MODEL A: Below C low (with buffer)
    c_low = event.structure.c_point.price
    stop_a_price = round(c_low * (1.0 - stop_a_buffer_pct / 100.0), 4)
    stop_a_dist = round(((ref_entry - stop_a_price) / ref_entry) * 100.0, 2)
    stop_a = StopSpec(
        model_name="STOP_MODEL_A",
        stop_price=stop_a_price,
        stop_distance_pct=stop_a_dist,
        reference_metric=f"C_LOW_{c_low}_BUFFER_{stop_a_buffer_pct}%"
    )

    # STOP MODEL B: Below breakout level by ATR multiple
    atr_series = compute_weekly_atr(weekly_df, period=14)
    atr_val = float(atr_series.iloc[bar_idx]) if not np.isnan(atr_series.iloc[bar_idx]) else (ref_entry * 0.05)
    stop_b_price = round(event.level_price - (stop_b_atr_mult * atr_val), 4)
    # Ensure stop B does not exceed entry
    if stop_b_price >= ref_entry:
        stop_b_price = round(ref_entry * 0.95, 4)
    stop_b_dist = round(((ref_entry - stop_b_price) / ref_entry) * 100.0, 2)
    stop_b = StopSpec(
        model_name="STOP_MODEL_B",
        stop_price=stop_b_price,
        stop_distance_pct=stop_b_dist,
        reference_metric=f"LEVEL_{event.level_price}_MINUS_{stop_b_atr_mult}xATR({round(atr_val,2)})"
    )

    # STOP MODEL C: Below most recent confirmed weekly swing low
    # The most recent confirmed swing low prior to or at bar_idx
    struct_c_low = event.structure.c_point.price
    stop_c_price = round(struct_c_low * (1.0 - stop_c_buffer_pct / 100.0), 4)
    stop_c_dist = round(((ref_entry - stop_c_price) / ref_entry) * 100.0, 2)
    stop_c = StopSpec(
        model_name="STOP_MODEL_C",
        stop_price=stop_c_price,
        stop_distance_pct=stop_c_dist,
        reference_metric=f"RECENT_SWING_LOW_{struct_c_low}"
    )

    # TARGETS
    proj = event.structure.projection
    targets = TargetSpec(
        target_1_272=proj.level_1_272,
        target_1_618=proj.level_1_618,
        target_2_000=proj.level_2_000,
        target_2_618=proj.level_2_618
    )

    return OrderSetup(
        signal_id=event.signal_id,
        symbol=event.symbol,
        signal_date=event.signal_date,
        event_type=event.event_type,
        level_ratio=event.level_ratio,
        level_price=event.level_price,
        entry_a=entry_a,
        entry_b=entry_b,
        stop_a=stop_a,
        stop_b=stop_b,
        stop_c=stop_c,
        targets=targets
    )
