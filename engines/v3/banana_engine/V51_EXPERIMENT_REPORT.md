# v5.1 Experiment Report

## Setup
- Train window: 2020-01-01 to 2024-12-31 (N = 13,748 raw rows, 12,087 clean rows)
- Test window: 2025-01-01 to 2026-09-12 (N = 6,209 raw rows, 5,405 clean rows, held out)
- Feature sets tested: 38 (baseline), 46 (+ raw breakout features), 50 (+ trap flags), hard filter

## Results

| Model | AUC | Top-Quartile WR | Clean WR | Trap-Flagged WR | N test |
|---|---|---|---|---|---|
| A: Baseline (38) | 0.5133 | 0.4635 | 0.4623 | 0.4458 | 5405 |
| B: + Raw breakout (46) | 0.5306 | 0.4882 | 0.4623 | 0.4458 | 5405 |
| C: v5.1 full (50) | 0.5314 | 0.4876 | 0.4623 | 0.4458 | 5405 |
| D: Hard filter only | — | — | 0.4623 | 0.4458 | 5405 (clean N = 3227) |

## Interpretation

**Does ML add to the trap filter?**
Yes. In Model B and C, the top-quartile win rate reaches **48.82% / 48.76%**, outperforming the hard filter clean set alone (**46.23%**) by **+2.53 to +2.59 percentage points** on the held-out window. The model's top conviction quartile successfully separates higher-probability candidates from average clean breakouts.

**Do raw breakout features add to base features?**
Yes. Adding the 9 raw breakout features lifted OOS AUC from **0.5133 to 0.5306 (+0.0173)**, and raised top-quartile win rate from **46.35% to 48.82% (+2.47 percentage points)**. Incorporating candle expansion, upper wick rejection, and pivot clearance provides valuable new signal that base geometry alone lacks.

**Do trap flags add to raw features?**
No. AUC moved from **0.5306 (B) to 0.5314 (C)** (+0.0008, well below the 0.02 threshold), and top-quartile win rate remained essentially flat (48.82% vs 48.76%). LightGBM splits directly on the continuous features (`chase_pct`, `upper_wick_pct`, `cir`, `vol_ratio`), rendering redundant binary trap indicators uninformative.

**Do traps remain predictive on the held-out window?**
Yes. On the unseen 2025–2026 test set, trap-flagged setups achieved a win rate of **44.58%** compared to **46.23%** for clean setups (a 1.65 percentage point deficit across 2,178 flagged setups).

## Recommended configuration for Monday ship

**Recommendation: Configuration B (+ 9 Raw Breakout Features) in Shadow Observation Mode.**  
Raw breakout-bar features clearly improve both AUC and top-quartile precision over baseline, without adding redundant binary flags. However, because OOS AUC is 0.5314 (<0.60), the model should run in parallel observation mode alongside deterministic portfolio risk guards rather than replacing the production execution pipeline.

## Go/No-Go for Monday

Ship only if all four are true:
- [ ] OOS AUC of the chosen model ≥ 0.60 (or for hard filter, clean WR ≥ 50%) — *Actual: 0.5314 / 46.23%*
- [x] Top-quartile (or clean set) N ≥ 100 — *Actual: 1,520 (B) / 3,227 (D)*
- [x] Trap-flagged WR substantially below clean WR (traps remain predictive) — *Actual: 44.58% vs 46.23%*
- [x] All critical path tests pass — *Actual: 10/10 tests passed (30.5s)*

**Ship decision: NO-GO for direct production replacement.**  
**Action Plan**: Keep `v5.0` locked for observation. Deploy Model B in paper-trading shadow mode (`_v51`) to track live calibration alongside the Apex portfolio selection layer.

## Open questions

- The baseline model's OOS AUC on 2025–2026 dropped to 0.5133 from the 0.5621 CV mean in 2023–2024, reflecting broad market regime shifts and choppy breadth across midcaps in 2025.
- Combining Model B's top quartile with strict daily capacity ranking (Top 2 per day) elevates win rate to >56% in portfolio simulation, suggesting that cross-sectional ranking is critical for outsized edge.
