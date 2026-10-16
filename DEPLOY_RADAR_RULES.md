# Radar trade engine — what changed and how to deploy

One definition of "breakout", "retest" and "failed" now lives in `common/trade_rules.py`. It replaces four conflicting ones
(pattern detector: any close above the level; screener: >3% above; radar_feed SQL guesses; the app: distance from pivot).

## What the data said (1,445 liquid NSE stocks, 2019-2026, entry next open, stop = level - 1 ATR, target = 2R, 0.25% costs)
| Setup | n | wins | reached +2R | avg R | vs random entry | verdict |
|---|---|---|---|---|---|---|
| Daily, any close above level (old rule) | 9,042 | 38% | 29% | +0.00 | | no edge |
| Daily breakout, grade A | 1,419 | 47% | 28% | +0.20 | +0.19 | VALIDATED (in-sample +0.19, out-of-sample +0.20) |
| Daily breakout, grade B / C | 938 / 6,685 | 40% / 35% | | +0.03 / -0.05 | | no edge |
| Daily retest (any grade) | 4,729 | 38-43% | 24-25% | -0.02..+0.10 | | no validated edge |
| Weekly breakout, grade A | 311 | 55% | 27% | +0.37 | +0.18 | VALIDATED |
| Monthly (any) | 71 | 23-33% | | negative | | too few, no edge: context only |

Grade A = close >= 0.8 ATR beyond the level + 52-week high less than 3 ATR above the level + close above the long EMA.
Tested and NOT useful: volume spike, close location in the bar, base tightness (VCP), touches, relative strength rank, market
breadth/regime, an XGBoost/LightGBM model on all of these (out-of-sample AUC 0.53). The old ML gate's 70-89% win rates are not
reproducible; the UI no longer uses them for rows that carry a trade call.

## Deploy (after the collectors are running)
```bash
cd /root/quantsync
# copy the changed files from vps_data_live, then:
PYTHONPATH=. ./venv/bin/python3 -m pytest -q tests/                      # expect 82 passed
# 1. measure the edge on YOUR full 2000-candle history (about 2 minutes)
./venv/bin/python3 scripts/build_breakout_edge.py                        # writes Data/breakout_edge.json
# 2. rebuild what Radar reads
./venv/bin/python3 -c "import build_screener_snapshot as b; print(b.build_all())"
./venv/bin/python3 -c "import snapshot_manager as s; print(s.sync_v3_pattern_candidates(None))"
systemctl restart quantsync quantsync_api
curl -s localhost:8443/api/radar/edge | head -c 300
```
Re-run step 1 monthly (or after any big data repair). If `Data/breakout_edge.json` is missing the engine falls back to the two
setups that passed (daily A, weekly A) and shows "No track record" for everything else.

## New columns (added automatically)
`stock_screener_rows`: trade_state, trade_grade, trade_action, trade_json.
`v3_pattern_candidates`: trade_state, trade_action, trade_grade, is_primary, trade_json (one primary timeframe per stock).

## Behaviour changes to expect
* Far fewer "Good to buy" cards. On 250 sample stocks the old rule showed 43 CONFIRMED buys. Under the new rules: 14 were marginal
  closes (not a buy), 12 were already running (not a fresh entry), 8 were back at the broken level (retest watch), 5 were stale or had
  hit target, and 4 were fresh breakouts, of which 1 passed the validated-edge bar.
* A daily buy against a failed/weak weekly chart is shown as WATCH with the conflict named. Monthly never leads.
* Retests are recognised and shown, but are watch-only (no measured edge over the breakout itself).
* Radar no longer depends on DNA (`common/radar_feed.py: REQUIRE_DNA = False`); DNA stays on the stock page. Price-data quality is still a hard gate.
* `breakout_status` is 'NA' for stocks with no real resistance level (the old code used the current price as the level).
* Fixed: `build_screener_snapshot` crashed when candle volume was null.

## Not verified live
* The edge table in this folder was measured on a copy with ~1,000 candles per stock; rebuild it on the VPS (step 1).
* Pattern-geometry levels (wedges/triangles) were validated only through flat-resistance events. The same state machine runs on
  them, but their own track record needs the full-universe ledger run (`scripts/build_historical_breakout_ledger.py`).
* Results are regime dependent: the daily grade-A average by year was +0.15, +0.47, -0.03, +0.31, +0.12, +0.11, +0.15 (2020-2026).
