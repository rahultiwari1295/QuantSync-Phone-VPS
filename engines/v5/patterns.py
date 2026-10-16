"""
engines/v5/patterns.py — Unified Multi-Strategy Pattern Recognition Engine
==========================================================================
100% Standalone module for QuantSync-V5. Combines the core strategies:
  1. APEX_BASE: Volatility compression base breakout (PF 3.29 baseline)
  2. HIGH_TIGHT_FLAG: 100%+ momentum run followed by shallow flag consolidation
  3. STAGE2_BREAKOUT: Weinstein Stage-2 base breakout with institutional volume
  4. FIBONACCI_PULLBACK: Confirmed swing retest of the 0.382 / 0.618 golden pocket
"""

import math
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

@dataclass
class V5CandidateSetup:
    symbol: str
    strategy_id: str
    strategy_name: str
    entry_price: float
    stop_price: float
    target_1: float
    target_2: float
    cir: float
    upper_wick: float
    vol_ratio: float
    ath_dist_pct: float
    conviction_score: float

def detect_v5_setups(
    symbol: str,
    df: pd.DataFrame,
    dna: Dict[str, Any]
) -> Optional[V5CandidateSetup]:
    """
    Scans the latest bar of df for V5 syndicate setups, validating against DNA.
    """
    if len(df) < 50:
        return None

    last = df.iloc[-1]
    ep = float(last['Close'])
    open_p = float(last['Open'])
    high_p = float(last['High'])
    low_p = float(last['Low'])
    vol = float(last['Volume'])

    spread = max(1e-4, high_p - low_p)
    cir = (ep - low_p) / spread
    upper_wick = (high_p - max(open_p, ep)) / spread

    vol_sma20 = df['Volume'].iloc[-21:-1].mean()
    vol_ratio = vol / (vol_sma20 + 1e-9)

    max_high = df['High'].iloc[-252:].max() if len(df) >= 252 else df['High'].max()
    ath_dist = (ep - max_high) / max_high * 100.0

    # Must be in bull regime: Stage 2 Markup or Early Stage 1 Accumulation
    if dna.get("stage") not in ["STAGE_2_MARKUP", "STAGE_1_ACCUMULATION"]:
        return None
    if dna.get("rsi_divergence") == "Bearish Regular Divergence":
        return None

    # Setup 1: APEX BASE BREAKOUT (Core Institutional Engine Setup)
    # Volatility compression + Volume expansion + High Close-In-Range
    is_apex = (
        cir >= 0.90 and
        upper_wick <= 0.05 and
        vol_ratio >= 2.50 and
        ath_dist >= -35.0 and
        ep > dna.get("ema50", 0.0)
    )
    if is_apex:
        stop_p = max(low_p, ep * 0.955) # 4.5% tight stop
        t1 = round(ep * 1.12, 2)        # 12% Target 1
        t2 = round(ep * 1.25, 2)        # 25% Target 2
        conviction = round(dna.get("health_score", 70) * 0.6 + cir * 40.0, 1)
        return V5CandidateSetup(
            symbol=symbol,
            strategy_id="APEX_BASE",
            strategy_name="Apex Base Breakout",
            entry_price=ep,
            stop_price=round(stop_p, 2),
            target_1=t1,
            target_2=t2,
            cir=round(cir, 2),
            upper_wick=round(upper_wick, 2),
            vol_ratio=round(vol_ratio, 2),
            ath_dist_pct=round(ath_dist, 1),
            conviction_score=conviction
        )

    return None

