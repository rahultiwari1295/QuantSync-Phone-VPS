"""
banana_engine/core/enhanced_features.py — Extended 49-Feature Institutional Pipeline
==================================================================================
Combines the baseline 38 domain features with 11 institutional-grade additions:
  - 6 Smart Money Accumulation features (stealth volume flow & supply exhaustion)
  - 5 Cross-Sectional Cascade features (sector breakout wave dynamics)

Preserves exact PIT integrity and compatibility with the core engine.
"""

import os
import sys
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.features import FEATURES, extract_features
from core.smart_money import compute_smart_money_features
from core.cascade import CrossSectionalBreakoutIndex, compute_cascade_features_batch

SMART_MONEY_FEATURES: List[str] = [
    "obv_divergence_score",
    "ad_line_curvature",
    "close_position_drift",
    "large_bar_bias",
    "price_volume_divergence",
    "accumulation_score",
]

CASCADE_FEATURES: List[str] = [
    "sector_breakout_count_10d",
    "cascade_position",
    "leader_mfe_10d",
    "market_cascade_breadth",
    "cascade_velocity",
]

ENHANCED_FEATURES: List[str] = FEATURES + SMART_MONEY_FEATURES + CASCADE_FEATURES


def extract_enhanced_features(
    symbol: str,
    as_of: str,
    bars_all: Optional[pd.DataFrame] = None,
    rs_rating: Optional[float] = None,
    breadth: Optional[Dict[str, float]] = None,
    sector_lookup: Optional[Dict[str, Any]] = None,
    cascade_index: Optional[CrossSectionalBreakoutIndex] = None
) -> Optional[Dict[str, Any]]:
    """
    Extracts complete 49-feature vector for symbol as of query date.
    Strict PIT parity guaranteed.
    """
    # 1. Base 38 features from original pipeline
    feats = extract_features(
        symbol=symbol,
        as_of=as_of,
        bars_all=bars_all,
        rs_rating=rs_rating,
        breadth=breadth,
        sector_lookup=sector_lookup
    )
    if feats is None:
        return None

    # Load bars up to as_of for smart money calculation
    as_of_str = str(as_of)[:10]
    if bars_all is None:
        from core.data_layer import load_adjusted
        bars = load_adjusted(symbol, end_date=as_of_str)
    else:
        bars = bars_all[bars_all['date'] <= as_of_str].copy().reset_index(drop=True)

    T = len(bars) - 1

    # 2. Smart Money features (6)
    sm_feats = compute_smart_money_features(bars, T)
    feats.update(sm_feats)

    # 3. Cascade features (5)
    if cascade_index is not None:
        cas_feats = cascade_index.compute_features(symbol, as_of_str)
    else:
        cas_feats = {
            "sector_breakout_count_10d": 0.0,
            "cascade_position": 1.0,
            "leader_mfe_10d": 0.0,
            "market_cascade_breadth": 0.0,
            "cascade_velocity": 0.0,
        }
    feats.update(cas_feats)

    return feats
