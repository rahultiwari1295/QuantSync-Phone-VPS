# Frozen Pipeline Bugs & Anomalies

This document logs bugs, statistical anomalies, and pipeline discrepancies discovered during the observation freeze period. In accordance with Rule 1 ("Do not modify core/brain.py, core/features.py, core/structure.py, core/backfill.py, or core/analog.py"), these findings are logged here without touching frozen code.

---

### BUG-01: Recomputed Divergence Threshold (P95 = 0.9091 vs In-Use 0.0800)
- **Component:** `core/online_calibrator.py:43` & `scripts/recompute_divergence_threshold.py`
- **Finding:** Multi-fold empirical evaluation ($N = 104$ walk-forward test predictions across 4 folds) produced an empirical 95th percentile divergence of `0.9091` (90th = 0.6000, 99th = 0.9706). This differs from the currently asserted in-use constant `0.0800` by $+0.8291$ (exceeding the $\pm 0.05$ boundary).
- **Root Cause:** In small-sample walk-forward validation folds ($N_{\text{valid}} \approx 13$), isotonic regression fits sharp piecewise step functions that jump directly between $0.0$ and $1.0$, creating large localized differences $|P_{\text{fast}} - P_{\text{slow}}|$ when evaluated against smoother historical anchor curves. In live trading with rolling buffers ($N_{\text{fast}} = 100, N_{\text{slow}} = 500$), the curves are substantially smoother.
- **Action Taken:** Empirical distribution saved to `data/divergence_threshold.json`. The running system remains frozen on `0.08` for the observation duration to maintain consistency with prior calibration.

---

### BUG-02: Historical Backfill Funnel Selectivity
- **Component:** `core/backfill.py` (`backfill_universe`)
- **Finding:** Across daily historical bars, `detect_base_at()` and `extract_features()` impose strict multi-gate criteria (requiring concurrent base detection, volume dry-up, volume surge ratio $\ge 1.2$, and pivot crossing). This concentrates candidates on confirmed high-conviction breakout setups rather than unformed daily price consolidations.
- **Action Taken:** Full universe backfill audited and documented in `PASS_2_COMPLETE.md`. Frozen code left unmodified.
