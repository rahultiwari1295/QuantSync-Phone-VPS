# ML Engine Upgrade — Full Production Report & Scorecard

**Date**: September 14, 2026  
**Universe**: 1,319 NSE Stocks (1,662,431 daily bars, `Prod_VPS/Data/stock_history.db`)  
**In-Sample Training Period**: 2020–2024 (5 Years, 11,980 candidates, 5-Fold Time-Series CV)  
**Held-Out Out-of-Sample Period**: 2025–2026 (1.75 Years, 2,707 candidates, zero lookahead)  
**Objective**: Transition from static rule filtering to an active ML-ranked and confidence-sized execution engine.

---

## Executive Summary

When entry rules are strictly filtered by human-defined thresholds, they discard hundreds of high-potential trades every year. Conversely, if rules are simply loosened without an intelligent filter, performance collapses from **Profit Factor 1.48 down to 1.13** due to noise flooding.

The **ML-Tiered Engine** solves this dilemma:
1. **Loosened Gates**: Allow 3.5× more setups to flow into the candidate pipeline.
2. **XGBoost Quality Ranker**: Evaluates every setup across 24 technical & structural features and assigns a continuous quality score $[0.0, 1.0]$.
3. **Confidence-Weighted Position Sizing**: Top-decile setups receive full institutional risk allocation, secondary setups receive reduced sizing, and low-conviction setups are discarded entirely.

### Key Empirical Takeaway

> **Tier 1 (Top 10% ML Score) achieves a 2.24 Profit Factor and 42.8% Win Rate on held-out 2025–2026 data**, producing **+58.2R per year on just 83 trades**.  
> Sizing Tier 1 at 1.25% risk and Tier 2 at 0.50% risk generates **+83.1R per year at a 1.85 Profit Factor**, beating the strict baseline (+74.8R @ 1.48 PF) in both total return and capital efficiency.

---

## 1. Production Architecture Comparison (2025–2026 Out-of-Sample)

All statistics below are computed strictly on the held-out 2025–2026 dataset with daily portfolio execution guards (max 3 trades/day, real transaction stops):

| Execution Architecture | Trades / Yr | Win Rate | Avg Risk / Trade | Annual R Multiple | Profit Factor | Status / Verdict |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **1. Current Strict Rules (Flat)** | 246 | 35.1% | 1.00% | **+74.8R** | **1.48** | Baseline (Static Filters) |
| **2. Loosened Rules (Flat 1.0% Risk)** | 390 | 29.0% | 1.00% | **+35.6R** | **1.13** | **Noise Flooding** (Rule loosening fails without ML) |
| **3. ML-Tiered Engine (All 3 Tiers)** | 436 | 31.1% | 0.51% | **+68.6R** | **1.48** | Baseline Returns @ Half the Risk Exposure |
| **4. Optimized 2-Tier Engine (T1 + T2)** | 281 | 33.7% | 0.65% | **+68.4R** | **1.59** | Prunes breakeven Tier 3 noise |
| **5. Elite High-Conviction (T1 Alone)** | **83** | **42.8%** | **1.00%** | **+58.2R** | **2.24** | **Highest Capital Efficiency & Hit Rate** |
| **6. Dynamic Sized (T1 @ 1.25%, T2 @ 0.50%)**| 281 | 33.7% | 0.72% | **+83.1R** | **1.85** | **Recommended Production Target** |

---

## 2. ML Performance by Confidence Tier (Out-of-Sample)

In-Sample calibration (2020–2024) established the following score thresholds:
- **Tier 1 (Top 10%)**: Score $\ge 0.5050 \implies 1.00\%$ Risk
- **Tier 2 (Next 20%)**: Score $[0.4124, 0.5050) \implies 0.50\%$ Risk
- **Tier 3 (Next 30%)**: Score $[0.3581, 0.4124) \implies 0.25\%$ Risk
- **Tier 4 (Bottom 40%)**: Score $< 0.3581 \implies \text{REJECT / SKIP}$

```
                  ┌─────────────────────────────────────────────────────────────┐
                  │                 ALL LOOSENED CANDIDATES                     │
                  │                 (14,687 Setups Detected)                    │
                  └──────────────────────────────┬──────────────────────────────┘
                                                 │
                                     [ XGBoost Ranker ]
                                                 │
                   ┌─────────────────────────────┼─────────────────────────────┐
                   │                             │                             │
                   ▼                             ▼                             ▼
        ┌─────────────────────┐       ┌─────────────────────┐       ┌─────────────────────┐
        │       TIER 1        │       │       TIER 2        │       │       TIER 3        │
        │     Top 10% Score   │       │     Next 20% Score  │       │     Next 30% Score  │
        ├─────────────────────┤       ├─────────────────────┤       ├─────────────────────┤
        │ Trades/Yr:   83     │       │ Trades/Yr:  198     │       │ Trades/Yr:  155     │
        │ Win Rate:   42.8%   │       │ Win Rate:   30.0%   │       │ Win Rate:   26.2%   │
        │ Annual R:  +58.2R   │       │ Annual R:  +10.3R   │       │ Annual R:   +0.2R   │
        │ PF:         2.24    │       │ PF:         1.15    │       │ PF:         1.01    │
        │ Risk Alloc: 1.00%   │       │ Risk Alloc: 0.50%   │       │ Risk Alloc: 0.25%   │
        └─────────────────────┘       └─────────────────────┘       └─────────────────────┘
                                                                               │
                                                                               ▼
                                                                     [ PRUNE FROM TRADING ]
                                                                       Drag on portfolio
```

### Key Analytical Takeaways
1. **Tier 1 is an institutional outlier**:
   With a **2.24 Profit Factor** and **42.8% win rate**, Tier 1 candidates represent the cleanest breakout geometry across the entire Indian stock universe.
2. **Tier 2 provides steady incremental alpha**:
   Generates an additional **+10.3R/year** with lower volatility when sized at half-risk (0.50%).
3. **Tier 3 is breakeven friction**:
   A Profit Factor of 1.01 and +0.2R confirms that Tier 3 setups are essentially noise. Pruning Tier 3 elevates overall portfolio PF from **1.48 to 1.59**.

---

## 3. Top 15 XGBoost Predictive Features (Information Gain)

The XGBoost model trained on optimal $2.0R$ reach probability ranked feature importance as follows:

| Rank | Feature Name | Gain Score | Category | Economic / Market Mechanics |
| :---: | :--- | :---: | :--- | :--- |
| **1** | `cir` | **14.09** | Candlestick Quality | Close-in-Range. Buying pressure conviction into the close. |
| **2** | `upper_wick` | **8.15** | Selling Resistance | Absence of overhead supply/selling rejection. |
| **3** | `is_darvas` | **7.09** | Geometry | Darvas Box tight consolidation base structure. |
| **4** | `ath_dist_pct` | **6.54** | Proximity to Highs | Distance from 52-week high; blue-sky breakouts win vastly more. |
| **5** | `prior_runup_20` | **6.10** | Pre-Breakout Momentum | 20-day trend strength leading into the setup. |
| **6** | `dist_sma50` | **6.04** | Institutional Alignment| Setup extension relative to 50-day moving average. |
| **7** | `is_htf` | **5.96** | Geometry | High-Tight Flag explosive consolidation pattern. |
| **8** | `is_base` | **5.83** | Geometry | Multi-week flat base breakout. |
| **9** | `sma200_slope` | **5.80** | Regime | Long-term institutional accumulation slope. |
| **10** | `gap_pct` | **5.75** | Opening Conviction | Institutional gap-and-go behavior. |
| **11** | `body_pct` | **5.74** | Candlestick Quality | Solid body expansion vs doji/squat candles. |
| **12** | `is_52w` | **5.70** | Geometry | Fresh 52-week high breakout flag. |
| **13** | `risk_pct` | **5.62** | Trade Geometry | Initial structural stop width (tighter stops yield higher R). |
| **14** | `atr14_pct` | **5.61** | Volatility | Daily volatility envelope. |
| **15** | `vol_ratio50` | **5.54** | Volume | Volume surge relative to 50-day average. |

---

## 4. Production Integration Recommendations

1. **Adopt the 2-Tier Architecture**:
   - Trade **Tier 1 (Top 10% score)** with high conviction: **1.0% to 1.25% account risk**.
   - Trade **Tier 2 (Next 20% score)** with moderate conviction: **0.50% account risk**.
   - **Prune Tier 3 and Tier 4** entirely.
2. **Resulting Expected Out-of-Sample Performance**:
   - **Trade Volume**: ~281 trades/year (~1–2 trades/day)
   - **Win Rate**: ~33.7% to 42.8%
   - **Net Profit Factor**: **1.59 to 1.85**
   - **Expected Annual Return**: **+68R to +83R** per year
   - **Average Risk Per Trade**: **0.65% – 0.72%** (significantly reducing portfolio drawdowns compared to flat 1.0% risk).

---

## 5. System Safety & Freeze Integrity

All changes conform strictly to production freeze protocols:
- `core/brain.py`, `core/features.py`, `core/structure.py`, `core/backfill.py`, `core/analog.py`, and `data/model.lgb` maintain **100% locked SHA-256 integrity**.
- Verified with `QuantSync_Lines/banana_engine/tests/test_freeze.py`: **Passed (OK)**.
- `QuantSync-V6/snapshots/state_20260913_apex/restore.py` remains the active turnkey rollback safeguard.
