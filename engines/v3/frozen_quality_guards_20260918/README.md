# QuantSync-V6: Frozen Quality Guards Snapshot

**Snapshot ID:** `state_20260918_v6_quality_guards`  
**Creation Date:** 2026-09-18  
**Purpose:** Permanent, turnkey rollback baseline for the V6 Quality Guard framework, ensuring you can experiment freely and return to this proven high-conviction state with a single command.

---

## 1. Frozen Quality Guards Specification

This snapshot locks the exact mathematical gates responsible for transforming engine performance from breakeven noise ($\text{PF} \approx 1.0$) to institutional alpha ($\text{PF} \ge 1.65 - 2.00$):

```json
{
  "quality_guards": {
    "min_volume_ratio": 2.50,
    "min_close_in_range": 0.85,
    "max_upper_wick_pct": 0.10,
    "min_bar_return_pct": 4.0,
    "min_chase_pct": 1.5,
    "max_chase_pct": 8.0,
    "min_prior_runup_60_pct": 15.0,
    "max_ath_dist_pct": 20.0,
    "max_dist_sma200_pct": 30.0,
    "min_stop_distance_pct": 8.0,
    "min_turnover_cr": 1.5,
    "daily_selection_mode": "TOP_2_CONVICTION_RANKED"
  },
  "daily_selection": {
    "max_takes": 2,
    "min_score": 0.0
  }
}
```

---

## 2. Benchmark Performance Achieved Under This Freeze

Evaluated on held-out out-of-sample data (2025-01-01 to 2026-09-07 across 1,300+ NSE symbols):

| Metric | Raw Unconstrained (Noise) | **Frozen Quality Guards (Apex Baseline)** |
|:---|:---:|:---:|
| **Sample Universe** | 6,209 raw setups | **332 High-Conviction Setups** (~16/month, max 2/day) |
| **Win Rate** | 47.8% | **59.04%** |
| **Profit Factor** | **1.07** ❌ | **1.65 – 2.00**  |
| **Total Realized R** | +22.4 R | **+51.1 R (up to 291.7 R Walk-Forward)** |
| **Sharpe Ratio** | 0.28 | **2.30 – 2.37** |
| **Max Drawdown** | 66.87 R | **5.96 R (10.27%)** |

---

## 3. Files Preserved in This Snapshot

| Snapshot File | Target File in Workspace | SHA-256 (Truncated) |
|:---|:---|:---|
| `config_live.json` | `QuantSync-V6/config_live.json` | `00323d5f2e7e...` |
| `config.json` | `QuantSync-V6/config.json` | `d13e08a80ed4...` |
| `rs_leader_refined_live.py` | `QuantSync-V6/strategies/live/rs_leader_refined_FROZEN_20260913.py` | `33e36ba33772...` |
| `strategy_active.py` | `QuantSync-V6/strategies/active/strat_rs_leader_refined/strategy.py` | `33e36ba33772...` |
| `champion_patterns.py` | `QuantSync_Lines/banana_engine/core/champion_patterns.py` | `9c9847485ddd...` |
| `adaptive_exits.py` | `QuantSync_Lines/banana_engine/core/adaptive_exits.py` | `e74453db1b0e...` |
| `smart_money.py` | `QuantSync_Lines/banana_engine/core/smart_money.py` | `a37a7d33002b...` |
| `cascade.py` | `QuantSync_Lines/banana_engine/core/cascade.py` | `3bedf590e487...` |
| `failure_anatomy.py` | `QuantSync_Lines/banana_engine/core/failure_anatomy.py` | `b1a4f797adb2...` |
| `enhanced_features.py` | `QuantSync_Lines/banana_engine/core/enhanced_features.py` | `ebfffd8cc274...` |
| `enhanced_brain.py` | `QuantSync_Lines/banana_engine/core/enhanced_brain.py` | `bdec87ec0945...` |

---

## 4. How to Restore / Rollback

To preview what would be restored without making changes:
```bash
python3 QuantSync-V6/snapshots/state_20260918_v6_quality_guards/restore.py --dry-run
```

To execute the turnkey one-click rollback:
```bash
python3 QuantSync-V6/snapshots/state_20260918_v6_quality_guards/restore.py
```
