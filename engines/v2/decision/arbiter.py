"""
quantsync_v11/decision/arbiter.py
=================================
Layer 5: The Arbiter (Deterministic Sizing & Risk Arbiter — Zero ML).

Calculates:
1. Expected Value (EV):
   EV_R = (1 - trap_score) * est_mfe_r - trap_score * 1.0
2. Regime-Adjusted Hurdles:
   BULL_CALM: 0.3, BULL_VOLATILE: 0.5, BEAR_CALM: 0.7, BEAR_VOLATILE: 1.0 (Cash).
3. NAV-Proportional Position Sizing:
   Scales dynamically with portfolio NAV (base 3.0%), regime multiplier, floor/cap.
4. Hard Portfolio Constraints:
   - Max concurrent positions (e.g. 25-30).
   - Max positions per sector (e.g. 4).
   - Max pairwise correlation to existing book (e.g. 0.70).
   - Drawdown circuit breaker: Halve size at 5% DD, halt new entries at 8% DD.

Critical Architectural Invariant:
Preserves the exact skip reason ('capacity', 'sector_cap', 'correlation_cap' vs
'ev_below_threshold'), which feeds directly into the dual-regret Ledger.
"""

from dataclasses import dataclass, field
from typing import Tuple, Dict, Any, Optional


REGIME_THRESHOLDS = {
    "BULL_CALM": 0.3,
    "BULL_VOLATILE": 0.5,
    "BEAR_CALM": 0.7,
    "BEAR_VOLATILE": 1.0,
}

REGIME_SIZE_MULT = {
    "BULL_CALM": 1.2,
    "BULL_VOLATILE": 1.0,
    "BEAR_CALM": 0.8,
    "BEAR_VOLATILE": 0.6,
}


@dataclass
class PortfolioState:
    open_positions: int = 0
    sector_counts: Dict[str, int] = field(default_factory=dict)
    nav: float = 300000.0
    current_drawdown_pct: float = 0.0


@dataclass
class TradeCandidate:
    symbol: str
    sector: str
    trap_score: float
    est_mfe_r: float
    max_corr_to_book: float = 0.0
    pattern_type: str = "VCP"
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0


def expected_value_r(trap_score: float, est_mfe_r: float) -> float:
    """EV in R-multiples: P(Win)*MFE - P(Fail)*1.0"""
    return (1.0 - trap_score) * est_mfe_r - trap_score * 1.0


def size_position(
    nav: float,
    regime: str,
    base_pct: float = 0.03,
    floor: float = 15000.0,
    cap: float = 50000.0,
) -> float:
    """
    Computes rupee allocation scaled to NAV and market regime.
    """
    mult = REGIME_SIZE_MULT.get(regime, 1.0)
    raw = nav * base_pct * mult
    return float(max(floor, min(cap, raw)))


def arbiter_decide(
    candidate: TradeCandidate,
    regime: str,
    portfolio: PortfolioState,
    max_positions: int = 30,
    max_per_sector: int = 4,
    max_corr: float = 0.70,
    dd_halve_at: float = 0.05,
    dd_halt_at: float = 0.08,
) -> Tuple[str, float, str]:
    """
    Decides TAKE or SKIP and calculates position size.

    Returns:
        (decision, size_rupees, reason)
        decision: 'TAKE' or 'SKIP'
        reason:
            - 'drawdown_halt' (circuit breaker)
            - 'capacity' (capacity constraint)
            - 'sector_cap' (capacity constraint)
            - 'correlation_cap' (capacity constraint)
            - 'ev_below_threshold' (economic model rejection)
            - 'ev_approved' (taken)
    """
    # 1. Circuit breaker
    if portfolio.current_drawdown_pct >= dd_halt_at:
        return "SKIP", 0.0, "drawdown_halt"

    # 2. Capacity constraints (NOT economic model rejections!)
    if portfolio.open_positions >= max_positions:
        return "SKIP", 0.0, "capacity"

    if portfolio.sector_counts.get(candidate.sector, 0) >= max_per_sector:
        return "SKIP", 0.0, "sector_cap"

    if candidate.max_corr_to_book >= max_corr:
        return "SKIP", 0.0, "correlation_cap"

    # 3. Economic EV Hurdle
    ev = expected_value_r(candidate.trap_score, candidate.est_mfe_r)
    hurdle = REGIME_THRESHOLDS.get(regime, 0.5)
    if candidate.pattern_type in ["RSI_TRENDLINE_WEEKLY", "RSI_TRENDLINE"]:
        hurdle = max(0.1, hurdle - 0.1)

    if ev < hurdle:
        return "SKIP", 0.0, "ev_below_threshold"

    # 4. Position Sizing
    size = size_position(portfolio.nav, regime)
    if candidate.pattern_type in ["RSI_TRENDLINE_WEEKLY", "RSI_TRENDLINE"]:
        size *= 0.8
    if portfolio.current_drawdown_pct >= dd_halve_at:
        size *= 0.5

    return "TAKE", float(size), "ev_approved"
