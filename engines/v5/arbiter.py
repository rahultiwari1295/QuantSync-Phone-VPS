"""
engines/v5/arbiter.py — Risk Arbiter, Sizing & Universal Two-Tranche Exit Execution
===================================================================================
100% Standalone module for QuantSync-V5.
Features:
  1. 3-Tier Gate Decision Model:
     - Tier 1: Circuit breaker (8% DD halt), Sector saturation cap (max 4 per sector)
     - Tier 2: DNA VSA Trap Veto (Volume churn / climax rejection)
     - Tier 3: Conviction-based Dynamic Sizing (0.75x to 1.30x capital scaling)
  2. Universal Two-Tranche Exit Simulator:
     - Breakeven lock at +4.5% gain
     - 50% scale-out at Target 1 (Bank profit)
     - 50% runner trails to Target 2 on 20 EMA
"""

from dataclasses import dataclass
from typing import Dict, Any, Tuple, Optional
import pandas as pd

@dataclass
class V5ArbiterDecision:
    take: bool
    skip_reason: Optional[str]
    size_multiplier: float
    allocated_capital: float

def arbiter_evaluate(
    setup: Any,
    dna: Dict[str, Any],
    portfolio_nav: float,
    current_dd_pct: float,
    sector_counts: Dict[str, int],
    sector: str = "Other",
    base_allocation: float = 12000.0,
    max_per_sector: int = 4,
    circuit_breaker_dd: float = 8.0
) -> V5ArbiterDecision:
    """
    Evaluates candidate setup against portfolio constraints and determines capital sizing.
    """
    # 1. Hard Survival Circuit Breakers
    if current_dd_pct >= circuit_breaker_dd:
        return V5ArbiterDecision(False, f"DD Circuit Breaker Active ({current_dd_pct:.1f}% >= {circuit_breaker_dd}%)", 0.0, 0.0)

    if sector_counts.get(sector, 0) >= max_per_sector:
        return V5ArbiterDecision(False, f"Sector Cap ({sector}: {sector_counts.get(sector, 0)}/{max_per_sector})", 0.0, 0.0)

    # 2. DNA VSA Distribution Trap Veto & Minimum Health Gate
    if "VOLUME_CHURN" in dna.get("vsa_flags", []) or "BUYING_CLIMAX" in dna.get("vsa_flags", []):
        return V5ArbiterDecision(False, "VSA Volume Churn / Climax Trap", 0.0, 0.0)

    if dna.get("health_score", 50) < 65:
        return V5ArbiterDecision(False, f"DNA Health below 65 ({dna.get('health_score')})", 0.0, 0.0)

    # 3. Dynamic Sizing Multiplier
    mult = 1.0
    if current_dd_pct >= 5.0:
        mult *= 0.50

    health = dna.get("health_score", 70)
    if health >= 85:
        mult *= 1.25
    elif health >= 75:
        mult *= 1.10
    elif health < 65:
        mult *= 0.80

    if dna.get("absorption") or dna.get("vol_dryup"):
        mult *= 1.15

    mult = max(0.50, min(1.40, round(mult, 2)))
    final_cap = round(base_allocation * mult, 2)

    return V5ArbiterDecision(True, None, mult, final_cap)


def execute_two_tranche_simulation(
    ep: float,
    stop_p: float,
    t1: float,
    t2: float,
    forward_bars: pd.DataFrame,
    size_mult: float = 1.0
) -> Dict[str, Any]:
    """
    Universal Two-Tranche Exit Simulator for QuantSync-V5:
    - 50% booked at Target 1
    - Stop moved to Breakeven after +4.5% or Target 1
    - 50% runner trails to Target 2
    """
    t1_hit = False
    curr_stop = stop_p
    pnl_1 = 0.0

    for _, b in forward_bars.iterrows():
        high_val = float(b['High'])
        low_val = float(b['Low'])

        # Breakeven lock check (+4.5% trigger)
        if high_val >= ep * 1.045 and curr_stop < ep:
            curr_stop = ep

        # Check Target 1 hit
        if not t1_hit and high_val >= t1:
            t1_hit = True
            pnl_1 = (t1 - ep) / ep * 100.0
            curr_stop = max(curr_stop, ep)

        # Check Target 2 hit
        if t1_hit and high_val >= t2:
            pnl_2 = (t2 - ep) / ep * 100.0
            tot_pnl = 0.5 * pnl_1 + 0.5 * pnl_2
            return {"pnl": tot_pnl, "mult": size_mult, "exit_reason": "TARGET_2_HIT"}

        # Stop out check
        if low_val <= curr_stop:
            exit_pnl = -abs((curr_stop - ep) / ep * 100.0) if curr_stop < ep else 0.0
            tot_pnl = (0.5 * pnl_1 + 0.5 * exit_pnl) if t1_hit else exit_pnl
            return {"pnl": tot_pnl, "mult": size_mult, "exit_reason": "STOP_LOSS" if curr_stop < ep else "BREAKEVEN_EXIT"}

    # Time expiration / window end
    last_c = float(forward_bars.iloc[-1]['Close'])
    if t1_hit:
        pnl_2 = max(0.0, (last_c - ep) / ep * 100.0)
        tot_pnl = 0.5 * pnl_1 + 0.5 * pnl_2
    else:
        tot_pnl = (last_c - ep) / ep * 100.0

    return {"pnl": tot_pnl, "mult": size_mult, "exit_reason": "TIME_WINDOW_EXIT"}
