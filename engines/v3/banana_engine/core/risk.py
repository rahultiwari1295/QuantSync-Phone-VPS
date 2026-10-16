"""
banana_engine/core/risk.py — Deterministic Risk Engine (Layer 3)
================================================================
Implements deterministic risk rules: structural ATR-buffered stop losses,
fixed-fractional position sizing, target identification, regime gating,
and portfolio-level concentration checks matching Section 6 of BananaPatterns v4.
No ML by design — explainable in one sentence and reproducible to the cent.
"""

from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import pandas as pd

MAX_STOP_PCT         = 8.0     # Reject setups if stop distance > 8%
MIN_STOP_ATR         = 1.0     # Don't go tighter than 1 ATR (prevents noise tag-outs)
ATR_BUFFER           = 0.5     # Half ATR buffer below structural base floor

RISK_PCT_PER_TRADE   = 0.75    # 0.75% of portfolio equity risk per trade
MAX_POSITION_PCT     = 10.0    # No single position > 10% of portfolio equity
MAX_SECTOR_POSITIONS = 3       # Maximum 3 concurrent trades in same sector
MAX_TOTAL_POSITIONS  = 10      # Maximum 10 concurrent open portfolio trades


def compute_atr14(bars: pd.DataFrame, T: int) -> float:
    """
    Computes 14-period Average True Range as of bar T.
    """
    slice_bars = bars.iloc[max(0, T - 20):T + 1]
    if len(slice_bars) < 2:
        return float(bars['close'].iloc[T] * 0.02)

    highs = slice_bars['high'].values
    lows = slice_bars['low'].values
    closes = slice_bars['close'].values
    prev_closes = np.roll(closes, 1)
    prev_closes[0] = closes[0]

    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)))
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else float(np.mean(tr))
    return max(atr, 0.01)


def compute_stop(
    base_floor: float,
    bars: pd.DataFrame,
    T: int,
    entry: float
) -> Tuple[float, float, bool]:
    """
    Computes structural ATR-buffered stop loss.
    Returns: (stop_price, stop_distance_pct, is_valid)
    """
    atr = compute_atr14(bars, T)
    structural = base_floor - ATR_BUFFER * atr

    distance = entry - structural
    min_dist = MIN_STOP_ATR * atr
    max_dist = entry * (MAX_STOP_PCT / 100.0)

    # Clamping
    clamped_dist = min(max(distance, min_dist), max_dist)
    stop_price = round(entry - clamped_dist, 2)
    stop_dist_pct = round((clamped_dist / max(entry, 0.01)) * 100.0, 2)

    # If raw structural distance > 10% and clamped would violate base floor, flag unviable
    is_valid = bool(distance <= entry * 0.10)
    return stop_price, stop_dist_pct, is_valid


def position_size(
    equity: float,
    entry: float,
    stop: float,
    risk_pct: float = RISK_PCT_PER_TRADE,
    max_position_pct: float = MAX_POSITION_PCT
) -> Dict[str, Any]:
    """
    Fixed-fractional position sizing.
    Limits risk to risk_pct (0.75%) of equity, capped by max_position_pct (10%).
    """
    if entry <= 0 or stop >= entry or equity <= 0:
        return {"shares": 0, "allocation": 0.0, "risk_amount": 0.0}

    risk_amount = equity * (risk_pct / 100.0)
    risk_per_share = entry - stop

    qty_by_risk = int(risk_amount // risk_per_share) if risk_per_share > 0 else 0
    qty_by_cap  = int((equity * (max_position_pct / 100.0)) // entry)

    shares = max(0, min(qty_by_risk, qty_by_cap))
    alloc_amount = round(shares * entry, 2)
    actual_risk = round(shares * risk_per_share, 2)

    return {
        "shares": shares,
        "allocation_rupees": alloc_amount,
        "allocation_pct": round((alloc_amount / equity) * 100.0, 2),
        "risk_rupees": actual_risk,
        "risk_pct": round((actual_risk / equity) * 100.0, 2),
    }


def nearest_swing_high_above(bars: pd.DataFrame, T: int, pivot: float) -> Optional[float]:
    """
    Finds nearest confirmed historical resistance swing high above pivot.
    """
    highs = bars['high'].iloc[:T + 1].values
    candidates = [h for h in highs if h > pivot * 1.02]
    if candidates:
        return float(min(candidates))
    return None


def compute_target(
    base_floor: float,
    base_pivot: float,
    bars: pd.DataFrame,
    T: int,
    entry: float,
    stop: float
) -> Tuple[float, float]:
    """
    Picks the nearest structural target above entry + 1R from:
      1. Measured Move (1:1 base extension: pivot + (pivot - floor))
      2. Next Overhead Resistance
      3. Golden Fibonacci 1.618: floor + (pivot - floor) * 1.618
    Returns: (target_price, r_multiple)
    """
    base_range = base_pivot - base_floor
    measured_move = base_pivot + base_range
    fib_1618      = base_floor + base_range * 1.618
    next_resist   = nearest_swing_high_above(bars, T, base_pivot) or measured_move

    one_r = max(entry - stop, entry * 0.05)
    min_viable_target = entry + max(one_r * 1.5, entry * 0.10)

    candidates = [c for c in (measured_move, next_resist, fib_1618) if c >= min_viable_target]
    if not candidates:
        target = entry + max(one_r * 2.0, entry * 0.18)
    else:
        target = min(candidates)

    if target <= entry:
        target = entry + max(one_r * 2.0, entry * 0.18)

    r_multiple = round((target - entry) / max(one_r, 0.01), 2)
    return round(target, 2), r_multiple


def regime_gate(breadth: Dict[str, Any]) -> Tuple[str, float]:
    """
    Hard tail-risk gate:
      HALT    (0.0x size): breadth < 25%
      CAUTION (0.5x size): breadth < 35%
      NORMAL  (1.0x size): breadth >= 35%
    """
    p50 = float(breadth.get("pct_above_50dma", 50.0))
    if p50 < 25.0:
        return "HALT", 0.0
    if p50 < 35.0:
        return "CAUTION", 0.5
    return "NORMAL", 1.0


def portfolio_gates(
    symbol: str,
    sector: str,
    days_to_earnings: int,
    open_positions: List[Dict[str, Any]]
) -> Tuple[bool, List[str]]:
    """
    Verifies portfolio-level risk criteria.
    Returns: (passes_gates, warnings)
    """
    warnings: List[str] = []

    # 1. Total open positions limit
    if len(open_positions) >= MAX_TOTAL_POSITIONS:
        return False, ["Portfolio maximum open positions limit (10) reached."]

    # 2. Sector concentration limit
    n_in_sec = sum(1 for p in open_positions if p.get("sector") == sector)
    if n_in_sec >= MAX_SECTOR_POSITIONS:
        return False, [f"Sector concentration limit reached ({n_in_sec}/{MAX_SECTOR_POSITIONS} in {sector})."]

    # 3. Earnings proximity warning
    if days_to_earnings <= 5:
        warnings.append(f"Earnings in {days_to_earnings} days — overnight binary gap risk.")

    return True, warnings
