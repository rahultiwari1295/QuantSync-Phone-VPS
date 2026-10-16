# QuantSync data collectors + ops page — deploy notes

What this adds (all backend code is in `collectors/`, 56 tests pass: `PYTHONPATH=. ./venv/bin/python3 -m pytest -q tests/`):

| Piece | Purpose |
|---|---|
| `collectors/core` | fetch with fallback (direct → headless Chromium → BSE → home relay), validate, then promote; nothing reaches live tables unvalidated |
| `collectors/jobs` | listing master, corporate actions/dividends, news + board meetings, results, shareholding, ratios, coverage status, route probe |
| `collectors/run.py daemon` | one scheduler service (replaces many cron lines); also runs whatever you queue from the ops page |
| `collectors/readers.py` | DNA reads fundamentals through this: hides pre-listing years, unvalidated quarters; stale/missing results ⇒ quality pillar "not scored" |
| `collectors/admin` | `/api/admin/*` (owner-only, server-verified Firebase token) and `/api/relay/*` |
| Frontend `/ops` (also `/rahuldata`) | phone + PC console: coverage, jobs, sources, review queue, performance |

## Deploy order (VPS)
```bash
cd /root/quantsync
systemctl stop quantsync quantsync_api
cp Data/stock_history.db Data/stock_history.pre_collectors.db
# 1. copy files from vps_data_live, then
./venv/bin/pip install -r requirements.txt
./venv/bin/playwright install --with-deps chromium        # ~400 MB, once
PYTHONPATH=. ./venv/bin/python3 -m pytest -q tests/

# 2. which routes work from this server? (saves raw samples into Data/collectors.db)
./venv/bin/python3 -m collectors.run probe
sqlite3 Data/collectors.db "select route,ok_count,fail_count,last_error from source_health"

# 3. first fill (order matters)
./venv/bin/python3 -m collectors.run listing                  # listing dates -> hides pre-IPO data
./venv/bin/python3 -m collectors.run results_from_cache       # re-validate the existing results cache
./venv/bin/python3 -m collectors.run shareholding
./venv/bin/python3 -m collectors.run corp_actions_history     # dividends/splits since 2018
./venv/bin/python3 -m collectors.run news
./venv/bin/python3 -m collectors.run ratios
./venv/bin/python3 -m collectors.run status

# 4. scheduler service
cp deploy/quantsync_collectors.service /etc/systemd/system/
printf 'QS_RELAY_TOKEN=%s\n' "$(openssl rand -hex 24)" > .env.collectors && chmod 600 .env.collectors
systemctl daemon-reload && systemctl enable --now quantsync_collectors
systemctl start quantsync quantsync_api
curl -s localhost:8443/api/data-status | head -c 400
```

## Admin access
* The server accepts only a valid Firebase ID token (project `scantoknow-a1593`) for `rahultiwari1295@gmail.com`
  (override with `QS_ADMIN_EMAILS`, project with `QS_FIREBASE_PROJECT`). Anyone else gets 401/403; the page then shows
  "Please visit the main website".
* Open `/ops` while signed in to the main site with that account.

## If NSE blocks the VPS (see `probe` output)
Chromium is tried automatically. If that is blocked too, run the relay on a machine with a home IP
(your Mac, or the 192.168.29.17 box):
```bash
QS_VPS=https://quantsync-api.duckdns.org:8443 QS_RELAY_TOKEN=<same token> python3 collectors/relay/relay_client.py
```
It fetches only URLs the VPS failed on, and pushes raw bodies; the VPS validates them like any other route.

## Not verified against the live services (do this first)
* NSE JSON field names for results (`results-comparision`), announcements and board meetings are written from the
  documented shape. `probe` saves the real responses in `raw_fetch`; if a job reports "0 parsed" the shape differs —
  send me the sample and the parser is a one-line change.
* The Chromium route has only been exercised against a blocked network here.
* Shareholding and dividends fall back to the screener cache until the NSE routes are confirmed working.
