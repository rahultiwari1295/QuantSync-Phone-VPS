# Pass 2 Complete — Observability Layer & Freeze Initialization Report

The observability layer, deferred diagnostics, and freeze enforcement for **BananaPatterns v5** are fully implemented, verified, and locked.

---

## 1. Full-Universe Backfill & Coverage Audit
- **Full Universe Candidate Yield:** Verified on cohort benchmarks at **~10.1 labeled candidates per symbol** over the 2015–2024 history ($504$ verified breakout candidates across $50$ sample stocks).
- **Extrapolated Full Yield (1,302 Non-Index Symbols):** Approximately **13,100 candidates**.
- **Yield Funnel Bottleneck:** `extract_features` enforces strict multi-gate criteria requiring concurrent base detection, volume dry-up, volume surge ratio $\ge 1.2$, and pivot crossing, dropping non-breakout daily consolidation bars.
- **Audit Flag (FROZEN_PIPELINE_BUGS.md BUG-02):** Because the feature extraction funnel drops unconfirmed consolidation days, total yield across the universe is below the 20,000 threshold. In accordance with Non-negotiable Rule 1, frozen pipeline code was left unmodified.

---

## 2. Fold 2 Inversion Diagnosis & Hypothesis
- **Validation Fold Performance:**
  - Fold 1 (2023-01-20 to 2023-07-13): $N=26, \text{AUC}=0.5686, \text{Base Rate}=65.4\%$
  - **Fold 2 (2023-07-13 to 2023-12-04): $N=26, \text{AUC}=0.3846, \text{Base Rate}=50.0\%$ (Inverted)**
  - Fold 3 (2023-12-05 to 2024-04-25): $N=26, \text{AUC}=0.6632, \text{Base Rate}=69.2\%$
  - Fold 4 (2024-04-29 to 2024-12-05): $N=26, \text{AUC}=0.6319, \text{Base Rate}=30.8\%$
- **Diagnostic Artifact:** Saved Fold 2 candidates ($N=26$) to `data/fold2_diagnosis.parquet`.
- **Hypothesis (from FOLD_2_HYPOTHESIS.md):**
  Fold 2 inverted because the model suffered from **relative strength regime mismatch and rotational churn in large-cap leadership**. During H2 2023, market leadership rotated aggressively out of mega-cap IT and private banking (which dominate the cohort, e.g., TCS, INFY, WIPRO, HDFCBANK) into broader market cyclicals and mid-caps. Average `rs_rating` of test setups collapsed from $51.81 \pm 17.88$ in Fold 1 down to $33.85 \pm 12.56$ in Fold 2, while base compression degraded (`coil_ratio` fell to $0.92$). LightGBM—trained on prior bull-market momentum continuation—assigned highest win probabilities to low-RS large-cap breakouts that repeatedly squatted and failed within 20 sessions (top 20 predictions achieved only a 40.0% win rate vs 50.0% for the bottom set).

---

## 3. Recomputed Online Calibrator Divergence Threshold
- **Evaluated Predictions:** 104 out-of-sample predictions across walk-forward folds.
- **Empirical Distribution:**
  - 50th Percentile (Median): `0.1624`
  - 75th Percentile: `0.4095`
  - **90th Percentile: `0.6000`**
  - **95th Percentile: `0.9091`**
  - **99th Percentile: `0.9706`**
- **Artifact:** Saved to `data/divergence_threshold.json`.
- **Finding (FROZEN_PIPELINE_BUGS.md BUG-01):** The empirical P95 ($0.9091$) differs from the in-use constant ($0.0800$) by $+0.8291$ due to step-function behavior of small-sample isotonic regressors during validation folds. The runtime calibrator remains frozen at $0.0800$ during the observation period.

---

## 4. Feature Parity Coverage (Test 04 Expanded)
- **Verified Active Setup Bars:** **100 distinct active breakout bars** across the universe.
- **Bit-for-Bit Parity:** 100% agreement across all 38 continuous and categorical features between full historical series and point-in-time truncated series ($bars[:T+1]$). Zero lookahead leaks detected.

---

## 5. Freeze Enforcement & Integrity Hashes
- **Freeze Start Date:** **2026-09-12**
- **Locked SHA-256 Hashes (`FROZEN_VERSIONS.md`):**
  ```
  core/brain.py                    9954a723d903322dd827e8aa8fd115c3f0a5a58b18a2a65e69c0cd9a3e726447
  core/features.py                 82c95e9ad3cc28d5f82d944313e526a5adeb7b1234eaefea4690b149c270521f
  core/structure.py                81416d768eac0f17807a8ed2a441f66ac337edc7360d21acefe0790bc51d861f
  core/backfill.py                 2fd7574c136127689266d14b110de8d99d994165305103f52e3f7fbeb880d64a
  core/analog.py                   1bfc1ac082ac60199b8c731e3c2c3d312a9cc5d7a44e15f9553d46d831a741ce
  data/model.lgb                   2ee8385ded8c27ee62200bc212bbd769cdb88b72c0e9d1299fc1396cd6c87589
  data/model_card.json             510ed758fb36ac13e51cefdf95e5a3e4b17d2b5318a5fa8f868b744f596701c0
  data/backfill_dataset.parquet    e8af87df1a590bd417e299e21664967cdba25bf8950e4c11eaa14e7bb7b5a5fd
  ```
- **Integrity Test:** `tests/test_freeze.py` asserts all 8 files match these hashes bit-for-bit on every test execution.

---

## 6. Observability Layer Deliverables
1. **Paper-Trading Logger (`core/paper_logger.py`):**
   - Stores full context in `data/paper_trading.db` (`paper_predictions` table).
   - Idempotent: `INSERT OR IGNORE` with unique index on `(symbol, as_of_date)`.
   - Resolves forward trade outcomes conservatively in R-multiples bar-by-bar with trailing stops and delisting detection.
2. **Scanner Integration (`core/scanner.py`):**
   - Automatically logs passing candidates upon scanner execution.
3. **Daily Paper Resolver (`scripts/daily_paper_resolver.py`):**
   - Evaluates mature predictions and appends summary to `data/paper_trading.log`.
4. **Tier Performance Dashboard (`web/dashboard.html` & `server/api_server.py`):**
   - Endpoints: `GET /api/paper/stats` and `GET /dashboard`.
   - Renders real-time Per-Tier and Per-Grade performance tables, HTML5 canvas 30-day rolling win-rate chart, and model version metadata with 60s auto-polling.

---

## 7. Daily Ritual (Commands for the Operator)

Each trading day after market close, the operator runs:

```bash
python3 scripts/daily_paper_resolver.py
```

This single command:
1. Resolves any predictions that hit their 20-day horizon or experienced delisting.
2. Prints and logs the updated stats to `data/paper_trading.log`.
3. Appends progress metrics to `OBSERVATION_LOG.md`.

No retraining. No threshold tuning. No model adjustments. The system is in observation mode.
