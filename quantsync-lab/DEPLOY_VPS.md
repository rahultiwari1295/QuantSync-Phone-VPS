# QuantSync Lab: VPS deployment instructions

Audience: the developer deploying to the Alibaba VPS (4 GB RAM, 2 vCPU).
Goal: add the Lab as a separate service next to the live platform. **The live engine, crons and databases must not change.**

Source: folder `quantsync-lab/` (inside `vps_data_live/`, also on GitHub branch `lab-ui`). Do not copy anything else from `vps_data_live` over the live files. Never commit `config.json` (it holds bot tokens and broker secrets).

## 0. What gets installed
| Item | Value |
|---|---|
| Linux user | `qslab` (no login, separate from the live user) |
| Folder | `/opt/quantsync-lab/{app,data,db,models,run,logs,backups}` |
| Python | own venv `/opt/quantsync-lab/venv` |
| Services | `qslab-api` (127.0.0.1:8100), `qslab-worker-fast`, `qslab-worker-ai`, timer `qslab-cook.timer` |
| Public path | nginx `location /lab/` → 127.0.0.1:8100 |
| Resource caps | api 600M/60% CPU, fast worker 700M/60%, AI worker 1200M/100% (nice 15), cook 1200M (nice 19, idle IO) |

## 1. Pre-checks (do not skip)
1. `free -m` and `swapon --show`. If swap is under 2 GB, add a 2 GB swapfile.
2. `python3 --version` (3.10+; 3.11 or newer preferred). `pip` access to PyPI works.
3. Confirm the live candle DB path (expected `/root/quantsync/Data/stock_history.db`, table `StockHistory`, column `rawCandles` as JSON `[{"t","o","h","l","c","v"}]`).
4. Note the live daily schedule (candle sync 15:35 IST, EOD pipeline after it). The Lab cook must run **after** the EOD pipeline has finished.

## 2. Create user and folders
```
useradd -r -m -s /usr/sbin/nologin qslab
mkdir -p /opt/quantsync-lab/{app,data,db,models,run,logs,backups}
chown -R qslab:qslab /opt/quantsync-lab
```

## 3. Copy the code
Copy the contents of `quantsync-lab/` to `/opt/quantsync-lab/app/` (so `/opt/quantsync-lab/app/lab/...` exists). Then:
```
python3 -m venv /opt/quantsync-lab/venv
/opt/quantsync-lab/venv/bin/pip install -r /opt/quantsync-lab/app/requirements.txt
chown -R qslab:qslab /opt/quantsync-lab
```

## 4. Candle data: use a snapshot, not the live file
The live DB is in WAL mode and owned by the live user. Do not give the Lab write access to it and do not point the Lab at it directly. Instead the cook takes a consistent copy first:
```
sqlite3 /root/quantsync/Data/stock_history.db ".backup '/opt/quantsync-lab/data/stock_history.snapshot.db'"
chown qslab:qslab /opt/quantsync-lab/data/stock_history.snapshot.db
```
This is already done for you: `deploy/qslab-cook.service` runs `deploy/snapshot.sh` (as root) before every cook. Run it once by hand now (`sh /opt/quantsync-lab/app/deploy/snapshot.sh`, needs the `sqlite3` CLI) and set `candles_db` to the snapshot path in config (step 5).

## 5. Config
```
cp /opt/quantsync-lab/app/config.example.yaml /opt/quantsync-lab/config.yaml
```
Edit:
- `candles_db: /opt/quantsync-lab/data/stock_history.snapshot.db`
- `firebase_project_id:` your Firebase project id
- `auth_mode: firebase` (never `dev` on the VPS)
- leave `pricing_enabled: false`

Firebase: the API verifies user ID tokens. To also mirror strategy ids to Firestore (`users/{uid}.lab`), create a service account with Firestore access, save the JSON to `/opt/quantsync-lab/firebase-sa.json` (`chmod 600`, owner `qslab`) and add to the API unit: `Environment=GOOGLE_APPLICATION_CREDENTIALS=/opt/quantsync-lab/firebase-sa.json`. If you skip it, set `firestore_mirror: false`; the Lab still works from its own database.

## 6. First cook (manual) and sanity checks
```
sudo -u qslab QSLAB_CONFIG=/opt/quantsync-lab/config.yaml /opt/quantsync-lab/venv/bin/python -m lab.cli cook
```
Expected: two JSON lines (1D and 1W) with `symbols` roughly 2,300+, a recent `as_of` date, few `skipped`. Watch `htop` during the run; memory should stay under about 1.2 GB. Record how long it took. Check `/opt/quantsync-lab/data/features/tf=1D/` contains one `.parquet` per symbol.

## 7. Install and start services
```
cp /opt/quantsync-lab/app/deploy/qslab-*.service /opt/quantsync-lab/app/deploy/qslab-cook.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now qslab-api qslab-worker-fast qslab-worker-ai qslab-cook.timer
systemctl status qslab-api qslab-worker-fast qslab-worker-ai
curl -s http://127.0.0.1:8100/lab/v1/health
```
Health must return `"ok": true` and show both timeframes with `as_of`. The cook timer runs Mon-Fri 19:30 IST; change `OnCalendar` if the live EOD pipeline finishes later than that.

## 8. nginx
Add the contents of `deploy/nginx-lab.conf` inside the existing `server {}` block that serves `quantsync-api.duckdns.org:8443`. Then:
```
nginx -t && systemctl reload nginx     # reload, never restart
curl -s https://quantsync-api.duckdns.org:8443/lab/v1/health
```

## 9. Frontend (Vercel)
Deploy GitHub branch `lab-ui` to a Vercel preview first. It adds `/lab` (page `lab.html`) and a rewrite `/lab/api/*` → `https://quantsync-api.duckdns.org:8443/lab/*`. No other live route changes. Open `<preview>/lab`, sign in with Google, then run a Scanner query. Promote to production only after the checks in step 11.

## 10. Seed the two flagship strategies
Rahul will provide the entry/exit rules for AI Alpha and AI Pattern as JSON files:
```
sudo -u qslab ... python -m lab.cli seed-flagship "AI Alpha" alpha.json
sudo -u qslab ... python -m lab.cli seed-flagship "AI Pattern" pattern.json
```
Format: `{"entry": {"v":1,"all":[...]}, "exit": {"v":1,"any":[...]}}` (see `lab/rules.py` and `/lab/v1/catalog`). Until these are seeded the Today page shows no flagship tiles.

## 11. Acceptance checks
1. `GET /lab/v1/health` shows fresh `as_of` for 1D and 1W.
2. Scanner: RSI > 55 AND close > SMA 50 returns results in under 2 seconds.
3. Create a strategy, run a backtest job (completes in under 60 s), then run "Enhance with AI". Record the duration and peak RAM. It should finish within 5 minutes with RAM under 1.2 GB.
4. While an enhancement runs, confirm the live platform's API response times and `free -m` are unaffected.
5. During a cook, confirm the AI worker waits (`/opt/quantsync-lab/run/cook.lock` exists) and resumes after.
6. `journalctl -u qslab-api -u qslab-worker-ai -n 100` shows no tracebacks.
7. Compare 3 stocks' RSI(14) and SMA 50 from the Lab (`/lab/v1/chart/<SYMBOL>`) with the live platform; small differences are expected, large ones must be reported.

## 12. Backups and monitoring
- Nightly: `sqlite3 /opt/quantsync-lab/db/lab.db ".backup '/opt/quantsync-lab/backups/lab-$(date +%F).db'"`, keep 14 days. `lab.db` holds users' strategies and live track records; the cooked data and models can be rebuilt.
- Alert if `GET /lab/v1/health` `as_of` is more than 3 days old, or any `qslab-*` unit is failed.

## 13. Rollback (nothing else is touched)
```
systemctl disable --now qslab-api qslab-worker-fast qslab-worker-ai qslab-cook.timer
```
Remove the `location /lab/` block, `nginx -t && systemctl reload nginx`, and remove the `/lab` rewrites from `vercel.json`. `/opt/quantsync-lab` can stay or be deleted.

## 14. Known limits and open items (tell Rahul, do not try to fix silently)
- The Charts pattern scan and pattern-based exits show "coming soon" until the live engine's pattern output is exported to `/opt/quantsync-lab/data/patterns/latest_1D.parquet` (and `_1W`). Rahul/developer to decide the export.
- Candle history is capped at about 2,000 bars (about 8 years) in `StockHistory`; stocks with shorter history have fewer past trades for AI training. Delisted stocks absent from `StockHistory` are not in the tests (survivorship bias); mention this to Rahul.
- Prices are assumed split-adjusted as stored by the live engine; verify with a stock that had a recent split.
- Live tracking and win rate: win rate is hidden until a strategy has 30 closed live results; this is intentional.
- No payments or credits are active (`pricing_enabled: false`). Do not enable charging before a legal review.
- The Lab is a research tool: no screen or API text may say buy, sell, target or stop-loss advice.

## 15. Tests (optional, on any machine)
```
cd quantsync-lab && pip install -r requirements-dev.txt && pytest -q    # 22 tests
```

## 16. Flagships (AI Alpha, AI Pattern) and past-test data
The two flagship strategies are built from the live engine's own signals (P1 RS leader, P9 Stage-2 base, P4 power gap
→ AI Alpha; P2 apex base, P5 high tight flag → AI Pattern). They are ported to `lab/signals.py` and verified bar for bar
against copies of the engine functions (`tests/test_signals.py`).

IMPORTANT after pulling this version: the feature set changed (FSV 2: more indicators and the five `sig_*` signals).
The cooked store must be rebuilt once, otherwise scans using the new fields fail:

    cd /opt/quantsync-lab/app
    sudo -u qslab /opt/quantsync-lab/venv/bin/python -m lab.cli cook          # ~ same time as the first cook
    sudo systemctl restart qslab-api qslab-worker-fast qslab-worker-ai

Then create/refresh the flagships, their past tests and the AI score gate (heavy; run when nothing else is running,
and cap its memory so the live API is never at risk):

    sudo systemd-run --scope -p MemoryMax=1500M -p CPUQuota=150% \
      sudo -u qslab /opt/quantsync-lab/venv/bin/python -m lab.cli build-flagships

- Re-runnable and idempotent. It prints the past-test summary and whether the AI gate was kept.
- The AI gate is only applied when the walk-forward test finds a real edge (score 60, else 50); otherwise the flagship
  stays plain and the UI shows no AI badge. That is intentional: no edge, no claim.
- Any strategy (public ones too) can get past-test data with `python -m lab.cli backtest-all`.
- Send the printed output back so the numbers can be reviewed before the pages are shared.

## 17. If signed-in users see "Sign in again to continue"
The API could not verify the Firebase token. Public pages now keep working anyway, but sign-in dependent actions will fail until this is fixed.
1. `curl -s http://127.0.0.1:8100/lab/v1/health` → `auth.firebase_project_id` must be `scantoknow-a1593` (set `firebase_project_id` in `config.yaml`).
2. `journalctl -u qslab-api -n 50 | grep "token rejected"` shows the exact reason (wrong project id, no outbound access to Google's public certificates, or a wrong server clock).
3. The VPS needs outbound HTTPS to `www.googleapis.com` (used by firebase-admin to fetch signing certificates) and a correct clock (`timedatectl`).

## Update 2026-10-11: past tests, live record, strategy lists

What changed (all in `lab/`): past tests are queued automatically when a strategy is saved or copied, and recomputed when the
metric definitions change (`metrics.METRICS_V`, now 2, fixes the "worst dip -3523%" values). Each past test now carries its
latest 300 results so strategy pages can list them. `GET /strategies/{id}/backtest` never needs a sign-in for public strategies
and returns `running` while a job works. One bad record no longer breaks `GET /strategies?scope=mine`; unhandled errors return
JSON with the error type and are logged with a traceback. `GET /strategies/{id}/enhancement` is readable by anyone who can see
the strategy. New versions (AI gate / exit change) start their live record at the data date, like new strategies.

Deploy (no schema change, no downtime beyond the restart):

```bash
cd /opt/quantsync-lab/app && git fetch origin && git checkout lab-ui && git pull --ff-only
/opt/quantsync-lab/venv/bin/python -m pytest -q quantsync-lab/tests        # expect 30 passed
systemctl restart qslab-api qslab-worker-fast qslab-worker-ai
cd quantsync-lab && QSLAB_CONFIG=/opt/quantsync-lab/config.yaml /opt/quantsync-lab/venv/bin/python -m lab.cli backtest-all
```

Check:

```bash
curl -s https://quantsync-api.duckdns.org:8443/lab/v1/strategies?scope=flagship | python3 -c 'import sys,json;[print(s["name"]) for s in json.load(sys.stdin)]'
# for each flagship id: status done, All.max_dd between -100 and 0, and a trades list
curl -s https://quantsync-api.duckdns.org:8443/lab/v1/strategies/<id>/backtest | python3 -c 'import sys,json;r=json.load(sys.stdin)["result"];print(r["summary"]["All"]["max_dd"],len(r["trades"]))'
journalctl -u qslab-api --since "10 min ago" | grep -i "unhandled\|card failed" || echo "no API errors"
```

Daily: the live record only updates when `python -m lab.cli cook` runs after the close (it calls `update_live`). Confirm the
cron entry exists: `crontab -l | grep "lab.cli cook"`.

## Update 2026-10-11 (b): flagships show the engine's real trades; AI-gated past tests

- AI Alpha and AI Pattern now read the live QuantSync engine on the same VPS (`engine_api`, default `http://127.0.0.1:8000`):
  `/api/track-record` (closed, live + walk-forward test), `/api/active-trades` and `/api/v3/active-trades` (open).
  AI Alpha = engine v2, AI Pattern = engine v3. Refreshed every 2 minutes; the last good copy is saved to
  `<data_dir>/engine_feed.json` and used if the engine API is down. No config change needed unless the engine runs elsewhere.
- A strategy with an AI score set now shows its past test filtered by that score (walk-forward scores; job kind
  `gated_backtest`, AI lane). Counts match the "Pick a score" table.
- Tests: 32.

Check after deploy:
```bash
curl -s http://127.0.0.1:8100/lab/v1/strategies?scope=flagship | python3 -c 'import sys,json;[print(s["name"],s["source"],s["stats"]["open"],s["stats"]["trades"]) for s in json.load(sys.stdin)]'
# expect: AI Alpha engine 3 8 / AI Pattern engine 0 9 (live closed counts grow over time)
```

## Update: new Lab screens (matches, alerts, year view, AI before/after)

Backend changes (`lab/api.py`, `lab/ai.py`): new `GET /lab/v1/matches` and `GET /lab/v1/alerts`; `follow` keeps a muted row for QuantSync strategies; strategies are switched on for alerts when saved or copied; the owner no longer counts as a follower; the AI result gains `plain_by_year`. No schema change.

Deploy as before: `git pull` on the Mac, rsync to `/opt/quantsync-lab/app/`, `chown -R qslab:qslab`, `pytest` (35 tests), then
`systemctl restart qslab-api qslab-worker-fast qslab-worker-ai`. Redeploy `lab-ui` on Vercel for the frontend.
An AI run made before this update has no `plain_by_year`; the page then shows all-years numbers as "before" until Enhance is tapped again.

Check: `curl -s http://127.0.0.1:8100/lab/v1/alerts` lists AI Alpha and AI Pattern with `"on": true`; `/lab/v1/matches` returns `counts`.
