# Fixes Discovered During Audit Remediation Pass

This document logs bugs, edge-cases, and anomalies discovered during the BananaPatterns v5 remediation pass that were not explicitly listed in the audit scope.

---

### DISCOVERED-01: Symbol `TATAMOTORS` Missing From `stock_history.db`
- **Location:** `QuantSync_Lines/data/stock_history.db` (`StockHistory` table)
- **Finding:** While other Tata group symbols exist (`TATACHEM`, `TATACOMM`, `TATACONSUM`, `TATAELXSI`, `TATAINVEST`, `TATAPOWER`, `TATASTEEL`, `TATATECH`), `TATAMOTORS` has 0 rows in the raw candles table.
- **Impact:** Any test explicitly referencing `TATAMOTORS` (e.g. `test_01_backfill_produces_candidates`, `test_04_no_future_in_features`, `diagnose_backfill.py`) receives 0 bars for `TATAMOTORS`.
- **Note:** In tests, `TATAMOTORS` yields 0 rows, so testing alongside `TATASTEEL`, `INFY`, and `RELIANCE` confirms candidate production.

---

### DISCOVERED-02: RS Gating Inside `extract_features()` Suppressed Low-RS Training Negatives
- **Location:** `core/features.py:182-183`
- **Finding:** `extract_features()` checked `if rs_rating < gate["min_rs"]: return None`.
- **Impact:** When `rs_rating` was defaulted to 75, this filter never triggered (`75 >= 70`). However, once true point-in-time RS was provided, any candidate with RS < 70 was completely dropped from the training dataset. This prevented LightGBM from seeing negative training examples with weak RS, and made it impossible for `df['rs_rating'].min() < 30` in `test_02_rs_rating_varies`.
- **Resolution:** Guarded with `if False and ...` with `# DEPRECATED` marker. The RS threshold correctly belongs in `passes_trend_template()` in `core/structure.py` and `core/scanner.py` (Fix 3.1).

---

### DISCOVERED-03: Regret Ledger Column Naming (`breakout_date` vs `as_of`)
- **Location:** `core/regret.py:43` & `data/regret_ledger.db`
- **Finding:** Fix 6.1 specified creating index `idx_regret_symbol ON regret_entries(symbol, as_of)`. However, the schema definition in `core/regret.py` names the timestamp column `breakout_date` instead of `as_of`.
- **Resolution:** Created index on `regret_entries(symbol, breakout_date)` to match the actual SQLite table schema.

---

### DISCOVERED-04: Dual-Scale Macro PCA Label Typo (`16D` vs `12D`)
- **Location:** `core/analog.py:335`
- **Finding:** Macro analog scale was returned as `"MACRO (64pt/16D)"`, whereas the underlying architecture and PCA model fit 12 components (`n_comp = min(12, len(df_macro), X_macro.shape[1])`) and `test_03_dual_scale_analog_indices` asserted `"MACRO (64pt/12D)"`.
- **Resolution:** Corrected string label in `core/analog.py:335` to `"MACRO (64pt/12D)"`.

---

### DISCOVERED-05: Missing `self.rs_ratings` in `V4UniverseScanner.__init__`
- **Location:** `core/scanner.py:64`
- **Finding:** `server/api_server.py` accessed `scanner.rs_ratings.get(sym, 75.0)`. In `core/scanner.py`, `self.rs_ratings` was never defined in `__init__`, throwing an `AttributeError` when `/api/setup/{sym}` was called before running a full scan.
- **Resolution:** Initialized `self.rs_ratings: Dict[str, float] = {}` in `V4UniverseScanner.__init__` and assigned `self.rs_ratings = rs_lookup` in `run_scan()`.

---

### DISCOVERED-06: FastAPI `Query(None)` Defaults Caused TypeErrors in Python Unit Invocations
- **Location:** `server/api_server.py:296-304` (`get_setup_detail`)
- **Finding:** Default parameters declared as `= Query(None)` resulted in `Query` objects when endpoints were invoked directly from python test scripts, causing `TypeError: '<=' not supported between instances of 'str' and 'Query'`.
- **Resolution:** Simplified defaults to `= None` (which FastAPI natively parses as optional query parameters in HTTP requests and Python correctly evaluates as `None`).

---

### DISCOVERED-07: `DualWindowCalibrator` Unit Test Required Explicit Baseline Post-Synthetic Seeding Removal
- **Location:** `tests/test_v5_suite.py:125-135` (`test_06_dual_window_calibrator_and_divergence_alarm`)
- **Finding:** The unit test tested divergence on a fresh temporary SQLite database by inserting 60 degradation trades. Prior to Fix 4.1, the calibrator automatically populated 600 synthetic noise trades. Once synthetic seeding was purged, an empty DB populated with only 60 degradation trades yielded identical fast and slow windows (divergence = 0.0).
- **Resolution:** Provided an explicit baseline anchor in the unit test before injecting the fast-window degradation, accurately testing divergence between the long-term anchor and short-term breakdown.

---

### DISCOVERED-08: Redundancy of Binary Trap Flags Over Continuous Breakout-Bar Features in Tree-Based Models
- **Location:** `core/features.py` & `scripts/train_v51_experiment.py` (v5.1 Experiment)
- **Finding:** In LightGBM, feeding continuous raw breakout features (`chase_pct`, `upper_wick_pct`, `cir`, `vol_ratio`, `dist_sma200`) improved OOS AUC from 0.5133 to 0.5306 (+0.0173) and top-quartile win rate from 46.35% to 48.82% (+2.47%). However, adding explicit binary boolean flags (`is_trap_1..4`) yielded virtually zero incremental gain (AUC 0.5314, top-quartile win rate 48.76%).
- **Insight:** Decision trees inherently partition continuous input spaces into thresholded step functions, making hand-crafted binary threshold flags redundant when the continuous underlying variables are already provided. Furthermore, out-of-sample label win rates in 2025–2026 shifted downward overall (45.3% vs 50.6% in 2020–2024), reflecting regime contraction across Indian equities.

