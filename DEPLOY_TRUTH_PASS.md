# QuantSync "Truth Pass" — VPS deployment runbook

Everything below was built and tested against a copy of the live databases (data to 7 Oct 2026).
Tests: `PYTHONPATH=. ./venv/bin/python3 -m pytest -q tests/` (11 tests).

## What changed (backend)

| Area | Fix |
|---|---|
| Candles | Market sweep bars are marked provisional (`"p":1`) and use FULL quotes (volume). Official EOD sync always re-checks the last 3 sessions and overwrites provisional / zero-volume bars. |
| Calendar | `common/market_calendar.py` — NSE holidays 2018-2026 (verified day-by-day against 7.8 years of candles) + Muhurat/Budget weekend sessions. Used by scheduler, orchestrator, candle sync, screener. |
| Quality gate | `common/data_quality.py` — step 1 measures official coverage; FAILED blocks V2/V3 settlement; status published at `/api/data-status`. |
| Splits/bonus | `common/corporate_actions.py` — NSE corporate actions → verified against the real price gap → history back-adjusted once (backup kept). Unexplained gaps quarantine the symbol from Radar and are flagged on DNA. |
| Technicals | Volume ratio uses official bars only (NULL when missing, never 0). Delivery % finally joined (was NULL for every stock). `updated_at` = data date. |
| DNA | Volume claims only with real volume; Wilder RSI (same as everywhere else); quality pillar fixed (was the default 12/20 for all 2,337 stocks); measured "History check" base rates; data-check line; pattern/regime and V3 reconciliation no longer contradict or overrule a BEARISH read; no invented targets. |
| AI Buy/Sell | Every trade labelled LIVE (entered ≥ 2026-09-08) or SIMULATED; exits clamped to the day's real range; no placeholder PF (1.92 / 2.18); no demo-data seeding; `/api/track-record`. |
| Radar | One shared feed (`common/radar_feed.py`) for API + snapshot; V3 candidates use the DNA's stance (was hard-coded BULLISH) and no invented sector/score/RSI/volume/RR; 3% "near" everywhere; quarantined symbols excluded; each card carries `pattern_edge`. |
| Pattern stats | `scripts/build_historical_breakout_ledger.py` rebuilt: entry at next open near the pivot, one record per pattern instance, returns from the real entry, fair yardstick (+10% before −7%) and `pattern_edge` vs a random-day baseline. `/api/screener/pattern-edge`. |
| Sectors | Whole-word sector mapping (IT was matching "Diversified Consumer Services"); unmatched sectors omitted instead of a fake 0.0%; `as_of` returned. |

## Deploy (in order)

```bash
cd /root/quantsync
systemctl stop quantsync quantsync_api
cp Data/stock_history.db Data/stock_history.pre_truthpass.db
cp Data/quantsync_master.db Data/quantsync_master.pre_truthpass.db
cp Data/stock_dna_cache.db Data/stock_dna_cache.pre_truthpass.db

# 1. copy the changed files from vps_data_live (list at the bottom), then
./venv/bin/pip install pytest && PYTHONPATH=. ./venv/bin/python3 -m pytest -q tests/

# 2. heal the candle store: drop 2 Oct ghost bars, repair OHLC, re-pull official candles from 24 Sep
./venv/bin/python3 scripts/repair_candles.py --dry-run
./venv/bin/python3 scripts/repair_candles.py --refetch            # prints quality_after; expect status OK

# 3. split/bonus history (first run backfills since 2018; review UNVERIFIED rows afterwards)
./venv/bin/python3 common/corporate_actions.py --history-from 2018-01-01
sqlite3 Data/stock_history.db "select symbol,ex_date,factor,observed_ratio,status from CorporateActionsAdj where status!='APPLIED'"

# 4. label the AI ledger (origin + fill audit)
./venv/bin/python3 common/ledger_truth.py

# 5. recompute everything on the healed data
./venv/bin/python3 eod_pipeline_orchestrator.py --force --date $(date +%F -d "last weekday")

# 6. honest pattern ledger + edge (heavy: run overnight; set workers to cores-1)
LEDGER_WORKERS=3 ./venv/bin/python3 scripts/build_historical_breakout_ledger.py
./venv/bin/python3 scripts/train_breakout_ml_gates.py

crontab crontab.txt
systemctl start quantsync quantsync_api
curl -s localhost:8443/api/data-status
```

## Check after deploy
- `/api/data-status` → `"status": "OK"`, `official_coverage` ≥ 0.9
- `/api/track-record` → live and simulated summaries
- `/api/screener/pattern-edge` → rows with `fair_win_rate`, `baseline`, `edge_pts`
- Trendlyne sector data stopped updating on 18 Sep — check `scripts/sync_trendlyne.sh` / `trendlyne_collector.py` logs.

## Still open (not done in this pass)
- Security (auth on POST routes, CORS, secrets) — scheduled last, as agreed.
- V4 win-probability model: hidden in the UI; needs retraining before it is shown again.
- Fundamentals source licensing (screener.in scraping) before a paid tier.
