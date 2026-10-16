"""
core/fibonacci_calculator.py — Mathematical Trend-Based Fibonacci Projection
============================================================================
Implements mathematically explicit Trend-Based Fibonacci Extension calculations:
  Structure: A (Swing Low) -> B (Swing High) -> C (Pullback Low)
  Impulse:   B - A
  Extension: level(price) = C + (B - A) * fib_ratio
"""

from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

DEFAULT_FIB_RATIOS = [0.000, 0.236, 0.382, 0.500, 0.618, 0.786, 1.000, 1.272, 1.618, 2.000, 2.618]


@dataclass(frozen=True)
class FibProjection:
    """
    Immutable representation of calculated Fibonacci levels for an A->B->C structure.
    """
    a_price: float
    b_price: float
    c_price: float
    impulse: float
    impulse_pct: float
    pullback_pct: float
    levels: Dict[float, float]

    @property
    def level_1_000(self) -> float:
        return self.levels[1.000]

    @property
    def level_1_272(self) -> float:
        return self.levels[1.272]

    @property
    def level_1_618(self) -> float:
        return self.levels[1.618]

    @property
    def level_2_000(self) -> float:
        return self.levels[2.000]

    @property
    def level_2_618(self) -> float:
        return self.levels[2.618]


def calculate_trend_fibonacci(
    a_price: float,
    b_price: float,
    c_price: float,
    ratios: Optional[List[float]] = None
) -> FibProjection:
    """
    Calculates Trend-Based Fibonacci extension levels projected from C using the A->B impulse.

    Formula:
      impulse = B - A
      level(r) = C + impulse * r

    Validation:
      A > 0, B > A (bullish impulse)
      C > A (C must not break A)
      C < B (C is a pullback from B)
    """
    if a_price <= 0:
        raise ValueError(f"A price must be positive: {a_price}")
    if b_price <= a_price:
        raise ValueError(f"B price ({b_price}) must be strictly greater than A price ({a_price})")
    if c_price <= a_price:
        raise ValueError(f"C price ({c_price}) must be strictly greater than A price ({a_price}) for bullish structure")
    if c_price >= b_price:
        raise ValueError(f"C price ({c_price}) must be strictly less than B price ({b_price}) to represent a pullback")

    if ratios is None:
        ratios = DEFAULT_FIB_RATIOS

    impulse = b_price - a_price
    impulse_pct = (impulse / a_price) * 100.0
    pullback_amount = b_price - c_price
    pullback_pct = (pullback_amount / impulse) * 100.0

    levels = {}
    for r in ratios:
        price_level = c_price + (impulse * r)
        levels[round(r, 3)] = round(price_level, 4)

    return FibProjection(
        a_price=round(a_price, 4),
        b_price=round(b_price, 4),
        c_price=round(c_price, 4),
        impulse=round(impulse, 4),
        impulse_pct=round(impulse_pct, 4),
        pullback_pct=round(pullback_pct, 4),
        levels=levels
    )


def compute_level_distance(current_price: float, level_price: float) -> float:
    """
    Computes percentage distance from current price to target level:
    positive = current_price above level
    negative = current_price below level
    """
    if level_price <= 0:
        return 0.0
    return round(((current_price - level_price) / level_price) * 100.0, 4)


def compute_extension_position(current_price: float, fib_proj: FibProjection) -> float:
    """
    Normalizes current price position relative to the A->B->C impulse:
    position = (current_price - C) / impulse
    e.g. At C = 0.0, At 1.000 = 1.0, At 1.618 = 1.618.
    """
    if fib_proj.impulse <= 0:
        return 0.0
    return round((current_price - fib_proj.c_price) / fib_proj.impulse, 4)
