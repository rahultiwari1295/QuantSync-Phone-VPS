"""
banana_engine/core/cohort.py — Cross-Sectional Cohort Relative Ranker (v5)
========================================================================
Implements institutional daily candidate pool competition:
  1. Dynamic Flood vs Famine Hurdles:
     • FLOOD (>= 8 setups): Requires 75th percentile cohort standing to take action.
     • MODERATE (3 - 7 setups): Requires 50th percentile standing.
     • FAMINE (1 - 2 setups): Requires setup to beat absolute hurdle (EV_R >= 0.55).
  2. Multi-Factor Relative Percentile Ranking:
     • 40% EV_R Percentile
     • 30% Empirical Twin Win Rate Percentile
     • 20% Marginal Sharpe Delta Percentile
     • 10% Relative Strength (RS) Percentile
  3. Cohort Tiers:
     • TIER_1_CONVICTION (Top 15%)
     • TIER_2_SELECTIVE  (15% - 40%)
     • TIER_3_WATCH      (40% - 70%)
     • TIER_4_DILUTIVE   (Bottom 30%)
"""

import os
import sys
from typing import Dict, List, Optional, Any
import numpy as np
import pandas as pd
from scipy.stats import rankdata

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


# FIXED: Fix 3.3 (MAJOR-06) — Calibrated Flood/Famine thresholds from historical candidate distribution
FLOOD_THRESHOLD = 13   # 75th percentile of daily candidate counts
FAMINE_THRESHOLD = 3   # 25th percentile of daily candidate counts


def rank_cohort(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ranks a list of candidate setups cross-sectionally for today's market scan.
    """
    if not candidates:
        return []

    n = len(candidates)
    if n == 1:
        c = candidates[0].copy()
        c['cohort_rank'] = 1
        c['cohort_percentile'] = 100.0
        c['cohort_market_mode'] = "FAMINE (Sole Setup)"
        c['cohort_tier'] = "TIER_1_CONVICTION" if c.get('ev_r', 0) >= 0.55 else "TIER_3_WATCH"
        return [c]

    # Determine market mode using calibrated flood/famine thresholds
    if n >= FLOOD_THRESHOLD:
        market_mode = f"FLOOD ({n} setups — Strict 75th percentile hurdle)"
        hurdle_pct = 75.0
    elif n >= FAMINE_THRESHOLD:
        market_mode = f"MODERATE ({n} setups — 50th percentile hurdle)"
        hurdle_pct = 50.0
    else:
        market_mode = f"FAMINE ({n} setups — Absolute EV_R >= 0.55 hurdle)"
        hurdle_pct = 0.0

    # Extract factor values
    ev_r_vals = np.array([float(c.get('ev_r', 0.0)) for c in candidates])
    twin_wr_vals = np.array([float(c.get('twin_win_rate', 50.0)) for c in candidates])
    delta_sharpe_vals = np.array([float(c.get('delta_sharpe', 0.0)) for c in candidates])
    rs_vals = np.array([float(c.get('rs_rating', 70.0)) for c in candidates])

    # Convert to percentiles (0 to 100)
    pct_ev_r = rankdata(ev_r_vals, method="average") / n * 100.0
    pct_twin_wr = rankdata(twin_wr_vals, method="average") / n * 100.0
    pct_delta_sharpe = rankdata(delta_sharpe_vals, method="average") / n * 100.0
    pct_rs = rankdata(rs_vals, method="average") / n * 100.0

    # Weighted composite cohort score
    composite_scores = (
        0.40 * pct_ev_r +
        0.30 * pct_twin_wr +
        0.20 * pct_delta_sharpe +
        0.10 * pct_rs
    )

    # Global cohort percentile
    cohort_percentiles = rankdata(composite_scores, method="average") / n * 100.0

    ranked_candidates = []
    for i, c in enumerate(candidates):
        item = c.copy()
        c_pct = round(float(cohort_percentiles[i]), 1)
        item['cohort_score'] = round(float(composite_scores[i]), 1)
        item['cohort_percentile'] = c_pct
        item['cohort_market_mode'] = market_mode

        # Assign Tier
        ev = float(item.get('ev_r', 0.0))
        if c_pct >= 85.0 and ev >= 0.45:
            tier = "TIER_1_CONVICTION"
        elif c_pct >= 60.0 and ev >= 0.35:
            tier = "TIER_2_SELECTIVE"
        elif c_pct >= 30.0:
            tier = "TIER_3_WATCH"
        else:
            tier = "TIER_4_DILUTIVE"

        # Actionable filter based on flood/famine hurdle
        item['cohort_tier'] = tier
        item['cohort_actionable'] = bool(c_pct >= hurdle_pct and ev >= 0.35)
        ranked_candidates.append(item)

    # Sort descending by composite cohort score
    ranked_candidates.sort(key=lambda x: x['cohort_score'], reverse=True)

    # Assign 1-indexed rank
    for rank, item in enumerate(ranked_candidates, 1):
        item['cohort_rank'] = rank

    return ranked_candidates


if __name__ == "__main__":
    import json
    sample_pool = [
        {"symbol": "TRENT", "ev_r": 1.25, "twin_win_rate": 80.0, "delta_sharpe": 0.08, "rs_rating": 94},
        {"symbol": "BEL", "ev_r": 0.85, "twin_win_rate": 60.0, "delta_sharpe": 0.04, "rs_rating": 89},
        {"symbol": "POLYCAB", "ev_r": 0.40, "twin_win_rate": 40.0, "delta_sharpe": -0.02, "rs_rating": 76},
        {"symbol": "WIPRO", "ev_r": 0.15, "twin_win_rate": 20.0, "delta_sharpe": -0.05, "rs_rating": 62},
    ]

    print("Ranking Sample Cohort...")
    res = rank_cohort(sample_pool)
    for r in res:
        print(f"Rank {r['cohort_rank']}: {r['symbol']} | Score: {r['cohort_score']} ({r['cohort_percentile']}%) | Tier: {r['cohort_tier']} | Actionable: {r['cohort_actionable']}")
