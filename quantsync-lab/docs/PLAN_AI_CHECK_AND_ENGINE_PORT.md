# QuantSync Lab — Honest results, 3-stage AI check, and porting the engine strategies

Owner: Rahul. Written 2026-10-18 for the implementing agent (Gemini). Read this whole file before writing code.

Two repos:
- Backend (this repo): `rahultiwari1295/QuantSync-Phone-VPS`, Lab code in `quantsync-lab/lab/`, tests in `quantsync-lab/tests/`.
- Website: `rahultiwari1295/RSITradingPlatform`, app in `public/app/` (vanilla ES modules). Work on branch `lab-ui`, push with `git push origin lab-ui lab-ui:main`.

---

## 0. Ground rules (apply to every phase)

1. **Do not modify `engines/`** (v2, v3, v4, v5). They trade live. This plan only *reads* them and re-implements their logic inside the Lab.
2. **Point-in-time only.** Every value used on bar *t* may use bars `<= t` only. Entries fill at the next bar's open (the simulator already does this). Any new feature must have a test that proves it does not change when future bars are appended.
3. **One phase = one commit**, tests green before each commit: `cd quantsync-lab && python -m pytest tests -q`. Add tests for every new behaviour (synthetic data in `lab/synth.py` / `tests/conftest.py`).
4. **Bump `metrics.METRICS_V` exactly once**, at the end (Phase 7), so the phone recomputes cached past tests one time, not after every phase.
5. **The 2026 data is a blind test.** No code path may choose parameters, gates, features or models using any trade with `entry_date >= 2025-01-01`. 2025 is validation (looked at once per frozen configuration), 2026 is the final blind test. Add an assertion in code (see 3.3).
6. **Phone budget.** Everything heavy runs in the worker's `ai` lane, sequentially, `n_jobs=1`, under `nice -n 10`. One strategy's AI check must finish in under ~5 minutes on the phone; cap training rows (see 3.6).
7. Plain-English UI copy. No jargon like "meta-labelling", "z-score", "walk-forward" on screen; put those in the "How this was tested" sheet only.
8. When anything in this plan conflicts with the code, the code's tests win; write down the conflict in the commit message and in section 9 of this file.

---

## 1. Phase 1 — Honest equity curve and drawdown (bug fix)

### Problem
`metrics.analytics()` and `metrics.summarize()` average the trades that close on each day and then **compound 100% of capital through every exit day**. Trades overlap (a weekly strategy holds 10–26 bars), so this multiplies the same money many times. Result on screen: ₹1,00,000 → ₹12,101 lakh and a "-72%" dip that has nothing to do with a real account.

### Fix: a simple slot-based portfolio simulation from the trade list
Add `metrics.portfolio(trades, slots=10, capital=100_000, seed=7) -> dict` and use it for the curve and drawdown.

Rules (write them in the docstring, they are shown in the "How this was tested" sheet):
- Capital is split into `slots` equal parts. A trade takes one free slot at its `entry_date`; the slot's money is `equity_at_entry / slots`.
- If more trades start on a day than there are free slots, choose which ones to take with a **seeded random shuffle** (seed fixed, so results are reproducible and no future information is used to pick). Count the ones not taken (`skipped`).
- A slot is freed on the trade's `exit_date`; its money comes back as `allocated * (1 + ret_pct/100)` (ret_pct already includes costs).
- Equity is marked at each exit day (realised). Optional v2: daily mark-to-market using bars; not required now.
- Idle cash earns nothing.
- Return: `{"equity":[{"d","v"}...], "max_dd", "cagr", "final", "taken", "skipped", "avg_slots_used", "slots", "capital"}`; downsample `equity` to 400 points like today.

Use it:
- `analytics()["equity"]`, `analytics()["spark"]` and `summarize()["max_dd"]` come from `portfolio()`.
- Keep the per-trade numbers (`trades`, `win_rate`, `avg_ret`, `profit_factor`) computed over **all** trades, as now (they describe the rule). Add `"portfolio": {...}` to the summary so the UI can say "taken 640 of 1,265 matches with 10 slots".
- Config: `portfolio_slots: int = 10` in `lab/config.py`.

Website (`public/app/js/parts.js`, around the "Value of ₹1,00,000 following every match" block, and `views/strategies.js` "₹1,00,000 following every trade"):
- Caption: "₹1,00,000 split across up to 10 trades at a time".
- Show CAGR and "taken X of Y matches" under the chart.
- Draw Nifty 50 from `GET /lab/v1/benchmark?tf=…&start=<first date>` rebased to ₹1,00,000 (the legend already has a Nifty entry).
- "Deepest dip" now uses `portfolio.max_dd`.

Tests (`tests/test_portfolio.py`):
- 1,000 overlapping trades all `+4%`, 10 slots → final equity is bounded by the hand-computed value (each trade moves equity by ~0.4%), and far below the old compounding.
- Two-trade hand example (one slot) matches exact numbers.
- `max_dd` is always between -100 and 0.
- Same input twice → identical output (seed).

---

## 2. Phase 2 — Liquidity measured on each day (remove selection bias)

### Problem
`universe.py` ranks stocks by **today's** turnover and the past test uses that list for 2018–2026. That favours stocks that became liquid because they went up. Also, the candle database only holds stocks listed today (delisted names are missing), which flatters results a little more.

### Fix
1. New daily feature `turnover_med_20` = median of `close*volume` over the last 20 bars (₹), point-in-time, in `features.compute()` (daily only; weekly = NaN).
2. A cross-sectional pass after cooking: `store.build_xsec(cfg, tf="1D")` writes `xsec_1D.parquet` with columns `date, symbol, liq_rank, rs60_pct`:
   - `liq_rank` = rank of `turnover_med_20` among all symbols **on that date** (1 = most traded). Use DuckDB over the feature parquet files: `rank() over (partition by date order by turnover_med_20 desc)`.
   - `rs60_pct` = percentile (0–100) of `ret_60 - nifty_ret_60` among all symbols on that date (needed by the RS Leader Refined port, section 6). Nifty from the first symbol in `api.BENCH` that exists; if none, use `ret_60` alone and log a warning.
   - Run it inside `cli cook` after the snapshot, and in `cli snapshot`.
3. `pipeline.run_trades()` replaces `universe.restrict()` with `universe.restrict_asof(cfg, sg)`: keep a signal only if `liq_rank <= cfg.free_universe_size` **on the signal date**. Keep the `universe` table for live tracking and for the label, change the label text to "the 500 most traded stocks on each day".
4. Expose `liq_rank` and `rs60_pct` as rule fields in `catalog.py` (group "Market", labels "Liquidity rank (1 = most traded)" and "Relative strength vs Nifty, 60 bars (percentile)") by joining `xsec` into the scan frame in `store.signals()` (snapshot path too).
5. Survivorship: check whether StockHistory has symbols with no bars in the last 30 days (delisted). If it has none, add the sentence "Stocks that were delisted are not in the data, which makes past results look slightly better." to the "How this was tested" sheet. Do not fetch delisted data now.

Tests: a synthetic stock that is illiquid in 2019 and very liquid in 2026 must produce **no** 2019 trades and must produce 2026 trades; `liq_rank` for a date must not change when later dates are added.

---

## 3. Phase 3 — The 3-stage AI check (one protocol for everyone)

Replace `ai.enhance_entry()` with `ai.check(ds, cfg) -> dict` (keep the old function name as a thin wrapper so `pipeline.enhance()` callers keep working). Same code is used for the automatic check on QuantSync strategies and for a user's own "AI coach" run.

### 3.1 Data
- `ds` = closed past-test trades with the features of the signal bar (`pipeline._dataset`). Label `y = ret_pct > 0` (costs are already in `ret_pct`).
- Features: current `MODEL_FEATURES` plus `ret_60, dist_ath_pct, vol_surge, close_loc, upper_wick_pct, dist_ema_20_pct, sma_50_slope, atr_pct, log10(turnover_med_20), liq_rank, rs60_pct` and two market features computed from Nifty at the signal date: `mkt_ret_20`, `mkt_dist_sma_200_pct`. Max 25 features. All scale-free (no raw prices).

### 3.2 Windows (from config, not hard-coded)
```
ai_tune_years  = [2022, 2023, 2024]   # out-of-sample years used to choose the model, gate and features
ai_valid_year  = 2025                 # validation: looked at once per frozen choice
ai_blind_year  = 2026                 # final blind test (year to date)
```
Training for any scored year Y uses only trades whose `exit_date < Y-01-01` (a trade that was still open at the start of Y is not in its training set). This is the existing `oos_scores` rule; keep it.

### 3.3 Stage A — tuning loop on ≤ 2024 (the "loop for more enhancement")
Candidates (at most 8, run in this order, each one scored walk-forward on 2022, 2023 and 2024):
1. LightGBM current `PARAMS`.
2. LightGBM shallow: `num_leaves=7, max_depth=3, min_child_samples=80, n_estimators<=200`.
3. LightGBM shallow on the compact feature set (top 10 features by gain importance learned on trades with `exit_date < 2022-01-01` only).
4. XGBoost `max_depth=3, eta=0.05, subsample=0.8, colsample_bytree=0.7, reg_lambda=5, min_child_weight=10, n_estimators<=300` (skip silently if `xgboost` is not installed; add `xgboost>=2.0` to `requirements.txt` as optional).
5. Logistic regression (standardised, L2, C=0.5) — the simple baseline.
6. Equal-weight average of the best tree model and logistic regression.
(7–8 reserved; leave unused unless 1–6 all fail to run.)

For each candidate and each gate in `{50, 60, 70}` compute on the pooled 2022–2024 out-of-sample trades: kept trades, win rate, average return, and `lift_avg = avg_ret(kept) - avg_ret(all)`.
Pick the (candidate, gate) with the highest `lift_avg` among those that keep at least 40 trades and at least 25% of trades; ties → the simpler model (lower number in the list). If none qualifies → verdict `no_edge`, stop.

Then run the existing shuffled-label leak test on the chosen candidate over 2022–2024; failing it → verdict `no_edge` with reason "Leak check failed".

Guard: `assert ds_used_for_choice.entry_date.max() < pd.Timestamp("2025-01-01")` inside the tuning function.

### 3.4 Stage B — validation on 2025 (frozen)
Train the chosen candidate on trades with `exit_date < 2025-01-01`, score every 2025 trade, apply the chosen gate. Record plain vs gated: trades, win rate, average return, and the z-value of the win-rate lift (existing `_z`).

### 3.5 Stage C — blind test on 2026 (frozen, scored once per data refresh)
Train on trades with `exit_date < 2026-01-01`, score 2026 trades, apply the same gate. The choice made in Stage A is never revisited because of 2025 or 2026 results.

### 3.6 Verdict (exact rule — this is what turns the "Enhanced AI" option on)
`improved` only if all hold:
- 2025: gated avg return > plain avg return, **and** gated win rate ≥ plain win rate;
- 2025: gated keeps ≥ 30 trades and ≥ 20% of the plain trades;
- 2025 and 2026 combined: win-rate lift z ≥ 1.5;
- leak test passed;
- 2026: if gated has ≥ 20 trades, gated avg return ≥ plain avg return − 0.25 pp. If fewer than 20, verdict can still be `improved` but set `blind_note = "Too few 2026 trades to confirm"`.

Other verdicts: `no_edge` (any rule fails), `insufficient` (< 120 closed trades before 2025, or < 40 in 2025), `failed` (exception; keep the message).

Speed: if more than 30,000 training rows, keep the most recent 30,000 (time order is preserved). Cache the dataset per strategy version and data `as_of`.

### 3.7 Result JSON (store in `ai_enhancements.result_json`, protocol `"v2"`)
```json
{
  "protocol": "v2", "verdict": "improved|no_edge|insufficient|failed", "reason": "…",
  "model": "lgbm_shallow", "gate": 60, "features": ["…"],
  "tune":  {"years": [2022,2023,2024], "plain": {…}, "ai": {…}, "candidates": [{"name","gate","trades","win_rate","avg_ret","lift_avg"}…]},
  "valid": {"year": 2025, "plain": {…}, "ai": {…}, "z": 1.9},
  "blind": {"year": 2026, "plain": {…}, "ai": {…}, "z": 0.8, "note": null},
  "leak":  {"mean_lift": 0.6, "passed": true},
  "drivers": ["Volume vs prior 20-bar avg higher than usual helped", "…"],
  "as_of": "2026-10-16", "checked_at": 1760000000
}
```
`{…}` = `{"trades","wins","losses","win_rate","avg_ret"}`.

Also save the final scorer (trained on everything before the blind year, i.e. `exit_date < 2026-01-01`) with `Scorer.save()` and store its path, so live tracking can score new signals.

### 3.8 Exits
Keep `enhance_exit` but change its selection window to 2022–2024 and report 2025 and 2026 separately in the same shape. It is optional and only offered after an `improved` entry check.

### Tests (`tests/test_ai_check.py`)
- Planted edge (a feature that predicts `y` in every year) → `improved`.
- Random labels → `no_edge`, and leak test passes.
- A spy proves no row with `entry_date >= 2025-01-01` reaches the tuning function.
- Determinism: same input → same verdict and numbers.
- Missing `xgboost` → still runs (candidate skipped).

---

## 4. Phase 4 — Automatic AI check for QuantSync strategies

Decision (Rahul): **every prebuilt strategy (library + flagship) gets the AI check automatically; strategies users create are checked only when the user presses "AI coach"** (existing daily limits stay).

1. DB migration (`lab/db.py`): `ai_enhancements` add `kind TEXT DEFAULT 'user'` (`user|system`), `protocol TEXT`, `verdict TEXT`, `as_of TEXT`; `live_trades` add `variant TEXT DEFAULT 'plain'` (`plain|ai`). `live_trades` has `UNIQUE(version_id, symbol, signal_date)`; SQLite cannot alter a constraint, so rebuild the table in a migration (create `live_trades_new` with `UNIQUE(version_id, variant, symbol, signal_date)`, copy rows with `variant='plain'`, swap names) inside one transaction, and make `db.migrate` idempotent (check `PRAGMA table_info` first). Add columns with `ALTER TABLE … ADD COLUMN` guarded the same way.
2. New job kind `ai_check` (lane `ai`, `uid=NULL`), handled in `service.handle` → runs Phase 3, stores the result with `kind='system'`. If verdict is `improved`, also run and cache the gated past test (`run_gated_backtest`) so the card can show both.
3. CLI: `python -m lab.cli ai-check [--all|--stale] [--name "…"]`. `--stale` (default) queues only library/flagship versions whose latest system check is missing, has a different `as_of` month, or a different protocol. Monthly is enough (models do not need daily retraining); the 2026 blind numbers are refreshed by re-scoring with the stored scorer, which is cheap, weekly.
4. Crontab (repo `crontab.txt`): 1st of each month 03:15 IST `ai-check --stale`; Sundays 03:15 `ai-check --rescore` (rescore blind year only, no retraining).
5. **No new strategy version** for system checks (that would reset the live record). The plain version stays; the AI variant is an alternate view.
6. Live tracking: `service.update_live` also writes `variant='ai'` trades for library/flagship versions whose latest system verdict is `improved`, using the stored scorer and gate.
7. Live re-check (monthly, in the same job): if the AI variant has ≥ 20 closed live trades and over the last 6 months its average return is below the plain variant's, set verdict `paused_live` and default the card back to plain. Show "AI filter paused: it stopped beating the plain rules in live tracking."
8. Users' own strategies: the "AI coach" button runs the same `check()`; result stored with `kind='user'`. Turning the filter on still creates a new version (existing behaviour).

Tests: `ai-check --stale` queues once and not again for the same month; a `paused_live` case flips the default.

---

## 5. Phase 5 — API and website

API (`lab/api.py`):
- `_card()` adds:
```json
"ai": {"status": "done|queued|none", "verdict": "improved|no_edge|insufficient|paused_live|failed",
       "kind": "system|user", "gate": 60, "default": "ai|plain",
       "plain": {"trades","win_rate","avg_ret"}, "enhanced": {"trades","win_rate","avg_ret"},
       "valid": {…}, "blind": {…}, "checked_at": 1760000000}
```
  `default` is `"ai"` only when verdict is `improved`.
- `GET /strategies/{id}/backtest?variant=ai|plain` returns the cached gated or plain past test.
- `GET /strategies/{id}/enhancement` returns the new schema to **anyone who can see the strategy** (already read-only; keep).
- `POST /strategies/{id}/enhance` stays owner-only (users' own strategies).

Website (`public/app/js/views/strategy.js`):
- Bug: line ~51 returns "Only the owner can run the coach on a strategy." **before** showing an existing result. Render the result for everyone; only the *Start* button requires `s.mine`.
- Library/flagship: no Start button. Show "AI-checked by QuantSync · <date>" and the result. Add "Make my copy" (uses existing `POST /strategies/{id}/copy`) with the text "Copy it to change the rules and run the coach yourself."
- AI coach tab shows a 3-column table: "Learned on 2022–24 / Checked on 2025 / Blind test 2026", rows "Plain rules" and "With AI filter": trades, ended higher %, average per trade. Verdict line on top:
  - improved: "The AI filter kept the stronger matches in years it never saw."
  - no_edge: "AI tested, no improvement. The plain rules are shown."
  - insufficient: "Not enough past trades for the AI to learn from."
  - paused_live: see 4.7.
- Test tab and strategy cards (`views/strategies.js`): a `Plain | AI` segmented switch, shown only when verdict is `improved`, default AI. When verdict is `no_edge` show a small grey chip "AI tested · no gain". Remember the viewer's last choice per strategy in `localStorage` (wrap in try/catch).
- `parts.js`: equity chart changes from Phase 1.
- Bump `index.html` cache keys (`lab.css?v=`, `data.js?v=`) after edits.

Check with Playwright (or a manual phone check): library card with `improved`, with `no_edge`, a user strategy (owner and non-owner), and dark/light themes.

---

## 6. Phase 6 — Port every QuantSync engine strategy into the Lab

### 6.1 What exists already
`lab/signals.py` already ports engine **v2/v5's five signals** as point-in-time columns: `sig_rs_leader` (P1), `sig_stage2_base` (P9), `sig_power_gap` (P4), `sig_apex_base` (P2), `sig_high_tight_flag` (P5). Library entries `lib-rs, lib-stage2, lib-vcp, lib-htf, lib-pgap` use them with simple stop/target/time exits; flagships "AI Alpha" (= v2) and "AI Pattern" (= v3) in `lab/flagship.py`.

Not ported yet: **v3's four champion patterns** (HTF Ultra, Darvas Box Ultra, 52-Week High Ultra, Ultra Base) with v3's frozen quality guards, **v3 RS Leader Refined**, **v4 F3 "Moonbag"** (classical chart patterns + stage 2), **v5 Apex Base with DNA checks**, and the engines' **real exits** (break-even locks, half-off targets, structural stops, EMA/close trailing).

### 6.2 New exit types (needed to match the engines) — `simulator.py`, `rules.normalise_exit`, `catalog.EXIT_TYPES`
Mark these `"system": true` in the catalog so the builder UI can hide them at first.
| type | value | meaning |
|---|---|---|
| `breakeven` | `[trigger_pct, lock_pct]` | when the high reaches entry×(1+trigger), raise the stop to entry×(1+lock) |
| `scale_out` | `[target_pct, fraction]` | sell `fraction` (e.g. 0.5) at entry×(1+target); the rest continues under the other exits; `ret_pct` = weighted average |
| `stop_field` | feature key | initial stop % taken from the signal bar's column (e.g. `setup_risk_pct`) |
| `target_r` | `multiple` | target = entry + multiple × initial risk (from `stop_field` or `stop_pct`) |
| `trail_close_below` | feature key (`ema_20`, `sma_7`) + optional `min_bars` and `only_in_profit` | exit at next open when close < that average |

Order inside a bar stays pessimistic: stop first, then targets. Add `sma_7` to features. Live tracking uses the same simulator, so parity holds automatically. Tests for each type with hand-built bars.

### 6.3 Daily pick limit (v3 takes only the top 2 per day)
Optional strategy setting `pick: {"max_per_day": 2, "rank_by": "<feature>", "desc": true}` stored with the version, applied in `run_trades` before simulation and in live tracking. Default off.

### 6.4 New signal / feature columns (all in `lab/signals.py` or `features.py`, point-in-time, daily only)
For heavy pattern searches use the existing approach: cheap vectorised pre-filter, then the exact engine logic only on candidate bars.

| column | source to copy exactly | notes |
|---|---|---|
| `sig_v3_htf`, `sig_v3_darvas`, `sig_v3_52wh`, `sig_v3_base` | `engines/v3/banana_engine/core/champion_patterns.py::detect_all_champion_patterns` | evaluate **in the engine's order** HTF → Darvas → 52W → Base and set only the first that matches (engine returns the first). Use the engine's own gates (CIR ≥ 0.85, wick ≤ 0.10, vol ≥ 2.5× prior-20 mean, within 20% of the 250-bar high, chase ≤ 8%, risk ≤ 6.5%). |
| `v3_risk_pct`, `v3_conviction` | same file + `engines/v3/engine.py::scan_date` (conviction formula) | used by `stop_field`/`target_r` and `pick.rank_by` |
| `sig_v3_rs_refined` | `engines/v3/strategies/rs_leader_refined_live.py::evaluate` | needs `rs60_pct >= 95` from Phase 2 (the engine's cross-sectional 95th percentile). |
| `sig_v4_f3` + `v4_pattern` (category) | `engines/v4/v4_champion_strategy.py::evaluate_entry_gate` and `engines/v4/v4_live_engine.py` (features, `stage_2_confirmed` = ≥ 40 of the last 50 closes above EMA 200) with `classical_pattern_dna.ClassicalPatternAnalystDNA.analyze` | keep only bullish patterns in `WIN_PATTERNS`; **drop the ML gates (`ml_pwin`, `ml_ev`)** — the Lab's AI check replaces them. Pre-filter: stage-2, dist EMA50 ≤ 12%, dist EMA200 ≤ 15%, CIR ≥ 0.65, wick ≤ 0.28. Run the pattern detector on the last 120 bars only, on pre-filtered bars only. Budget: cook must stay under +10 min on the phone; if not, compute this column weekly. |
| `is_churn`, `rsi_bear_div`, `dna_health` | `engines/v5/dna_core.py::compute_dna_profile` | vectorise per bar |
| `above_ema200_50` | count of last 50 closes above EMA 200 | for v4 |
| `sma_7` | 7-bar SMA of close | for v2 runner exit |

### 6.5 Library entries to add (`lab/library.py`, family `"QuantSync engines"`)
Keep each entry ≤ 12 conditions. Where an entry needs more, the logic lives in its `sig_*` column.

1. **QS v3 · High-tight flag Ultra**: `sig_v3_htf` AND v3 guards (below). Exit: `stop_pct 8`, `target_r 1.8` (risk from `v3_risk_pct`), `breakeven [0.8R→0]`, time 60. Pick top 2/day by `v3_conviction` when used inside the v3 combo.
2. **QS v3 · Darvas box Ultra**: `sig_v3_darvas` + guards, same exits.
3. **QS v3 · 52-week high Ultra**: `sig_v3_52wh` + guards, same exits.
4. **QS v3 · Ultra base breakout**: `sig_v3_base` + guards, same exits.
5. **QS v3 · All champions (as the engine trades it)**: any of the four + guards, `pick max_per_day 2 rank_by v3_conviction`, same exits. This is the closest Lab twin of the live v3 engine.
   v3 guards as rule conditions: `is_green`; `turnover between [1.5e7, 2.5e9]`; `ret_1 >= 4`; `vol_surge >= 2.5`; `close_loc >= 85`; `upper_wick_pct <= 10`; `ret_60 >= 15`; `dist_hh52_pct >= -20`. (The engine's "parabolic" guard — >30% above SMA 200 *and* wick > 10% — can never trigger once wick ≤ 10% is required; leave it out and say so in a comment.)
   Exit note: in the engine the stop is always widened to 8% (pattern risk is ≤ 6.5%, then `min_stop_distance_pct = 8` overrides it), while the target and break-even use the *original* pattern risk. Port it exactly that way; it is a finding, not a bug to fix here.
6. **QS v3 · RS leader refined**: `sig_v3_rs_refined`. Exit: stop = min(bar low, −8%) → `stop_pct 8`; `target_r 2.5`; `trail_close_below ema_20 min_bars 3`; time 60.
7. **QS v2 · RS leader (engine exits)**: `sig_rs_leader`. Exit: `stop_field` (P1 stop, emit `p1_risk_pct` from signals.py), `trail_close_below ema_20 min_bars 3 only_in_profit`, time 60. Source: `engines/v2/engine.py` RS block.
8. **QS v2 · Apex base (engine exits)**: `sig_apex_base` + the engine's candidate filters (vol ≥ 2.5, CIR ≥ 0.90 → `close_loc >= 90`, wick ≤ 0.05 → `upper_wick_pct <= 5`, base ≥ 4.5 weeks, within 35% of ATH) and the 4 "trap" exclusions. Exit: stop = max(breakout low, −4.5%); `breakeven [5, 1.5]`; `scale_out [12, 0.5]` then stop to +6%; trail 7% from the peak; runner `trail_close_below ema_20 min_bars 10` while > +6%; time 50.
9. **QS v2 · High tight flag / Stage-2 base / Power gap (engine exits)** — one entry each: their `sig_*`, turnover ≥ ₹2.5 Cr, CIR ≥ 0.65, wick ≤ 0.20. Exit (the engine's shared pool exit): stop from the setup (≈ 6.5%); at +12% stop to +4%; `scale_out [15, 0.5]` and stop to +5%; runner `trail_close_below sma_7 min_bars 5`. Read the time cap from `engines/v2/engine.py` (`_load_candidate_pool`) and copy it.
10. **QS v4 · F3 classical pattern + stage 2**: `sig_v4_f3`. Exit: `stop_pct 7`; `scale_out [14, 0.5]` then stop to +1%; after +7.5% trail `max(close×0.94, close − 1.5×ATR)` → use `trail_pct 6` + `trail_atr 1.5` (the simulator takes the higher stop); rule exit "down day on volume > 1.6× 20-day average after 3 bars" → `rule` exit `ret_1 < 0 AND vol_ratio > 1.6`; time 35. (The engine's target was 12–20% from an ML MFE model and regime multipliers; use a fixed 14% and say so.)
11. **QS v5 · Apex base with DNA**: `close_loc >= 90`, `upper_wick_pct <= 5`, `vol_surge >= 2.5`, `dist_hh52_pct >= -35`, `close > ema_50`, `dna_health >= 65`, `rsi_bear_div` is false. (With close > EMA 50 the engine's stage check is automatically met — note it in a comment.) Exit: stop = max(low, −4.5%); `breakeven [4.5, 0]`; `scale_out [12, 0.5]`; target 25% for the rest; time: copy the forward window length from `engines/v5/engine.py` / `live_scanner.py`.

Update the two flagships to use the engine-exact exits: "AI Alpha" = entries 7–9 combined (v2 priority order APEX_BASE, POWER_EARNINGS_GAP, HIGH_TIGHT_FLAG, RS_LEADER, STAGE2_BASE, one position per stock); "AI Pattern" = entry 5. A rule change creates a new version (existing `flagship.ensure`), which is expected.

### 6.6 Parity check (must do, report the numbers)
For 1 Jan 2025 → latest, compare the Lab port's **entry dates and symbols** with the engine's own signals:
- v3: run `engines/v3/engine.py::scan_date` read-only for each date (do not write to MasterStorage — call the detector functions directly) and compare with `sig_v3_*` (+ guards). Target ≥ 95% overlap; list every mismatch with the reason.
- v2 five: compare with the engine's parquet trajectories in `engines/v2/data/*.parquet` (`entry_date`, `symbol`).
- v4/v5: spot-check 30 random signals each.
Write the table into section 9 of this file.

### 6.7 Findings in the engines (report only; do not change `engines/`)
1. **v3 RS Leader Refined never fires live.** `engines/v3/engine.py::scan_date` calls `rs_leader_refined_live.evaluate(...)` without `all_bundles`/`nifty_map`, so `threshold_95` defaults to 999999 and every setup is rejected.
2. **v5 two-tranche simulator is optimistic.** In `engines/v5/arbiter.py::execute_two_tranche_simulation` targets are checked before the stop inside the same bar, and the runner's time-exit return is floored at 0 (`max(0.0, …)`).
3. **v2 track-record candidate selection uses the whole period.** `_load_candidate_pool` keeps the top `max_tr_yr × 1.7` rows by `ens_prob` across all of 2025–26 at once, which uses information from later dates to decide earlier trades. Also the APEX block checks break-even/targets before the stop in the same bar.
4. **v3 stop/target mismatch.** Target and break-even use the pattern's original risk (≤ 6.5%) while the stop is widened to 8%, so "1.8R" is really < 1.5R of the actual risk.
The Lab's honest numbers will therefore be lower than the engines' published ones. That is expected and should be explained, not tuned away.

---

## 7. Phase 7 — Roll-out on the phone

1. Pull both repos; `pip install -r quantsync-lab/requirements.txt` (adds `xgboost`).
2. Bump `METRICS_V` to 5 (one time) and run, under `nice -n 10`:
   `lab.cli cook` (builds `turnover_med_20`, new signals, snapshot, xsec) → `lab.cli universe` → `lab.cli library` → `lab.cli board --force` → `lab.cli ai-check --all`.
3. Make sure both workers are running (`quantsync_worker_fast`, `quantsync_worker_ai` tmux sessions, started by `~/start_quantsync.sh`).
4. Report a before/after table for every library + flagship strategy: trades, win rate, avg return, old max dip vs new max dip, CAGR (new), AI verdict, 2025 and 2026 plain vs AI.
5. Verify in the app: a library card shows the AI switch only when verdict is `improved`; the AI tab shows results to non-owners; the equity chart shows ₹1,00,000 split across 10 slots with Nifty.

## 8. Order and size

| Phase | Depends on | Size |
|---|---|---|
| 1 Equity curve | — | small |
| 2 As-of liquidity + xsec | — | medium |
| 3 AI check | 2 (features) | large |
| 4 Auto check | 3 | medium |
| 5 API + UI | 1, 3, 4 | medium |
| 6 Engine port | 2 (rs60_pct), exit types | large |
| 7 Roll-out | all | — |

Phases 1, 2 and the exit types of 6.2 can be done in parallel.

## 9. Implementation notes and parity results (fill in while working)

- **Phase 1 (Honest equity curve & portfolio simulation)**:
  - Added `metrics.portfolio(trades, slots=10, capital=100_000, seed=7) -> dict` implementing slot allocation, deterministic seeded random shuffling for day-level ties, realized equity marking upon exit dates, max drawdown computation from running peak, CAGR, and downsampled equity curves (400 points).
  - Updated `metrics.summarize()` to include `"portfolio": port` and set `max_dd` from `portfolio["max_dd"]`.
  - Updated `metrics.analytics()` to use `portfolio["equity"]` and `portfolio["spark"]`.
  - Added `portfolio_slots: int = 10` to `Config` in `lab/config.py` and passed it from `pipeline.summarise_trades`.
  - Updated website (`parts.js` and `views/strategies.js`) to display "₹1,00,000 split across up to 10 trades at a time", show CAGR and "Taken X of Y matches", handle equity scaling, and display honest max drawdown.
  - Test suite: Added `tests/test_portfolio.py` (5 tests covering two-trade hand example, 1000 overlapping trades, max_dd bounds, determinism, summarize/analytics integration). Updated `test_app_api.py::test_analytics_shape_and_math` for new portfolio equity. All 60 tests passed.

- **Phase 2 (Liquidity measured on each day / Remove selection bias)**:
  - Added `turnover_med_20` = rolling 20-bar median of `close * volume` (1D only; NaN for 1W) in `features.compute()`.
  - Added `store.build_xsec(cfg, tf="1D")` producing `xsec_1D.parquet` with columns `date, symbol, liq_rank, rs60_pct` using DuckDB window ranking over cooked feature parquets and joining Nifty benchmark (`api.BENCH`) for 60-bar relative return. Called automatically in `store.cook()` and `cli.py snapshot`.
  - Added `universe.restrict_asof(cfg, sg, tf="1D")` to filter past signals by `liq_rank <= cfg.free_universe_size` on the exact signal date. Replaced `universe.restrict()` in `pipeline.run_trades()`. Updated label text to `"the 500 most traded stocks on each day"`.
  - Exposed `liq_rank` and `rs60_pct` under group `"Market"` in `catalog.py`. Joined `xsec` in `store.signals()` (for both full history and fast snapshot scans).
  - Verified `StockHistory` delisted count on phone VPS: 0 symbols with no bars in last 30 days (all 2371 symbols active). Added survivorship bias note to `HONEST` notice in `public/app/js/parts.js`.
- **Phase 3 (3-Stage AI Check Protocol v2)**:
  - Added `ai_tune_years = [2022, 2023, 2024]`, `ai_valid_year = 2025`, `ai_blind_year = 2026` to `Config` in `lab/config.py`.
  - Defined 25 scale-free features `AI_FEATURES_25` in `lab/ai.py` (including log turnover, daily liquidity rank, 60-bar relative strength vs Nifty, and market regime return/distance to SMA 200).
  - Implemented 6 candidate models: `lgbm_default`, `lgbm_shallow`, `lgbm_compact` (top 10 gain features on <= 2021 trades), `xgboost` (optional with graceful fallback), `logreg` (liblinear solver with RobustScaler), and `ensemble` (weighted average of best tree and logreg).
  - Created `Scorer` class with probability prediction, calibration percentile scoring, pickle/json serialization, and ensemble column routing.
  - Implemented `ai.check()`: Stage A (tuning tournament on <= 2024 across models and gates 50, 60, 70), Stage B (validation on 2025), Stage C (blind test on 2026), label-shuffled leak check, and plain-English feature drivers. Added strict assertion `assert ds_tune.empty or ds_tune.entry_date.max() < pd.Timestamp("2025-01-01")`.
  - Added `tests/test_ai_check.py` covering planted edge, pure noise no_edge, spy test proving zero 2025+ trades reach tuning loop, determinism, and missing xgboost handling.
  - Maintained full backward compatibility with `pipeline.enhance()`, `pipeline.enhance_exit()`, and legacy gates/tournament UI payloads. All 68 tests passing.

- **Phase 4 (Automatic AI Check for QuantSync Strategies)**:
  - Database schema & idempotent migration in `lab/db.py`: Added `kind` (`user`|`system`), `protocol` (`v2`), `verdict`, and `as_of` to `ai_enhancements`. Rebuilt `live_trades` to include `variant` (`plain`|`ai`) with composite unique constraint `UNIQUE(version_id, variant, symbol, signal_date)`.
  - Enqueueing & job handling in `lab/jobs.py` and `lab/service.py`: Registered `"ai_check": "ai"` lane in `jobs.LANE`. Added `_ai_check` handler in `service.py` to run system checks, store models, and automatically cache gated backtest (`run_gated_backtest`) when verdict is `improved`.
  - Live performance re-check (Phase 4.7): `_ai_check` inspects closed live trades over the last 6 months; if AI variant has >= 20 trades and underperforms plain rules, verdict transitions to `paused_live` ("AI filter paused: it stopped beating the plain rules in live tracking.").
  - Dual live tracker in `service.update_live`: Concurrently writes and maintains `variant='plain'` and `variant='ai'` positions for improved/flagship strategies using the point-in-time stored model and gate.
  - CLI `lab.cli ai-check`: Added subcommands `--stale` (default, skips completed checks matching current month), `--all`, `--rescore` (Sunday blind year re-scoring with stored model), and `--name`.
  - Cron automation in `crontab.txt`: Scheduled 1st of month 03:15 IST `ai-check --stale` and Sunday 03:15 IST `ai-check --rescore`.
  - Test suite in `tests/test_auto_ai.py`: 4 tests covering DB migration idempotency, `ai-check --stale` queuing deduplication, `paused_live` condition, and dual live tracking. Full test suite: 72 passed.

- **Phase 5 (API and Website)**:
  - Backend API (`lab/api.py` and `lab/service.py`):
    - Added `"ai"` metadata block to `_card()` returning `status`, `verdict`, `kind`, `gate`, `default` ("ai" only when `verdict == "improved"`), `plain` stats, `enhanced` stats, `valid`, `blind`, and `checked_at`.
    - Updated `strategy_backtest` (`GET /strategies/{sid}/backtest?variant=ai|plain`) and `service.ensure_backtest` to look up and cache the requested variant.
    - Updated `GET /strategies/{sid}/enhancement` to expose `kind`, `protocol`, `verdict`, `as_of`, `reason`, and full result schema to any user with view access.
    - Added test `test_phase5_api_endpoints` in `tests/test_auto_ai.py`. All 73 tests in `quantsync-lab` green.
  - Website (`RSITradingPlatform` branch `lab-ui`):
    - Fixed ownership check in `views/strategy.js`: Anyone can view existing AI coach results; only the "Start the AI coach" button is restricted to the owner (`s.mine`).
    - Prebuilt library / flagship strategies show `"AI-checked by QuantSync · <date>"`, verdict banner, and a `"Make my copy"` button with copy attribution guidance.
    - Rendered 3-column stage table in AI coach view: "Learned on 2022–24" / "Checked on 2025" / "Blind test 2026", comparing "Plain rules" and "With AI filter" across trades, ended higher %, and avg return.
    - Added verdict banner copy: `improved` ("The AI filter kept the stronger matches in years it never saw."), `no_edge` ("AI tested, no improvement. The plain rules are shown."), `insufficient` ("Not enough past trades for the AI to learn from."), and `paused_live` ("AI filter paused: it stopped beating the plain rules in live tracking.").
    - Added interactive `Plain | AI` segmented switch in both the Test tab (`views/strategy.js`) and library cards (`views/strategies.js`) when verdict is `improved` (default AI), dynamically switching backtests and tile metrics; grey chip `"AI tested · no gain"` when verdict is `no_edge`. Preferences persisted per strategy in `localStorage`.
- **Phase 6 (Port QuantSync engine strategies and exits into Lab)**:
  - Exit rules & validator in `lab/rules.py` and `lab/catalog.py`: Added support and validation for engine exit types: `breakeven` (`[trigger_pct, lock_pct]`), `scale_out` (`[target_pct, fraction, lock_pct]`), `stop_field` (`field`), `target_r` (`v`), and `trail_close_below` (`field, min_bars, only_if_profit`).
  - Feature calculations in `lab/features.py`: Added `sma_7`, `above_ema200_50`, `dist_ema_50_pct`, `dist_ema_200_pct`, and vectorized DNA matrix indicators (`is_churn`, `rsi_bear_div`, `dna_health`).
  - Pattern and champion signals in `lab/signals.py`:
    - Ported v3 4-champion sequential detection (`HTF -> Darvas -> 52W -> Base`) and setup metrics (`v3_risk_pct`, `v3_conviction`).
    - Ported `sig_v3_rs_refined` (using cross-sectional `rs60_pct >= 95`).
    - Ported `sig_v4_f3` + `v4_pattern` via `ClassicalPatternAnalystDNA.analyze()` on candidate breakout bars.
  - Simulator enhancements in `lab/simulator.py`: Implemented intra-bar exit ordering: stop first, scale-out with stop ratchet, target, breakeven trigger/lock, trail close below MA, and weighted average exit returns.
  - Pick ranking & dynamic columns in `lab/pipeline.py`, `lab/db.py`, and `lab/service.py`:
    - Dynamic indicator loading via `_sim_cols(exit_)` to load moving averages or stop fields required by exit rules.
    - Added `pick` support (`max_per_day` ranked by feature, e.g. `v3_conviction`) and persisted `pick_json` in `strategy_versions`.
    - Handled empty trades/signals gracefully in `_dataset()` and `enhance()`.
  - Strategy catalog in `lab/library.py` and `lab/flagship.py`:
    - Added 11 QuantSync engine strategies under family `"QuantSync engines"` (v3 4-champions, v3 individual patterns, v3 RS refined leader, v4 classical chart patterns, and v5 pullback/trend/dip models).
    - Updated flagships "AI Alpha" and "AI Pattern".
  - Test suite in `tests/test_phase6.py`: Added 5 unit tests verifying breakeven stop ratchet, scale-out exit blending, stop_field & target_r risk multipliers, trail_close_below next-open fills, and pick ranking deduplication. Full test suite: 78 passed.
