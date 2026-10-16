"""
engines/v3/patterns.py — Champion Production Breakout Patterns (Frozen Quality Guards)
======================================================================================
Frozen Baseline: state_20260918_v6_quality_guards
Integrates 4 validated champion price action patterns:
  1. High-Tight Bull Flag (HTF)
  2. Ultra Marubozu Base Breakout (BASE)
  3. 52-Week High Ultra (52W_HIGH)
  4. Nicolas Darvas Box Ultra (DARVAS)

Enforces strict institutional quality gates:
  - Close-In-Range (CIR >= 0.85)
  - Max Upper Wick (<= 0.10)
  - Volume Surge Ratio (>= 2.50x 20d SMA)
  - Max Chase (<= 8.0%)
  - Distance from 52W High / ATH (<= 20.0%)
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any, Tuple
from engines.v3.banana_engine.core.champion_patterns import (
    ChampionSetup,
    detect_all_champion_patterns as _detect_raw,
    extract_champion_features,
)


def detect_all_champion_patterns(
    bars: pd.DataFrame,
    i: int,
    symbol: str = "",
    min_cir: float = 0.85,
    max_upper_wick: float = 0.10,
    min_vol_ratio: float = 2.50,
    max_chase_pct: float = 8.0,
    max_ath_dist_pct: float = 20.0,
) -> Optional[ChampionSetup]:
    """
    Evaluates bar index i for all 4 champion patterns under Frozen Quality Guards.
    Returns the first matching ChampionSetup with complete geometry, or None.
    """
    return _detect_raw(
        bars=bars,
        i=i,
        symbol=symbol,
        min_cir=min_cir,
        max_upper_wick=max_upper_wick,
        min_vol_ratio=min_vol_ratio,
        max_chase_pct=max_chase_pct,
        max_ath_dist_pct=max_ath_dist_pct,
    )
