# QuantSync app (new UI): deploy

The new UI is a static web app in `web/app/`. It reads only live APIs: nothing on any page is hard-coded.
It is served by `server.py` at **https://quantsync-api.duckdns.org:8443/app/**.

## What changed

| Where | Change |
|---|---|
| `web/app/` | New UI: `index.html` (landing), `today`, `scanner`, `strategy`, `strategies`, `matches`, `xray`, `patterns`, `portfolio`, plus `app.css`, `app.js` (shell and charts), `data.js` (API client, Google sign-in, mappers). |
| `app_routes.py` (new) | `GET /api/app/xray-batch?symbols=A,B` (X-Ray score, name, sector, price, 200-day average and 52-week range for up to 60 stocks, read-only from `Data/stock_dna_cache.db`), `GET /api/app/summary`, and serves `web/app` at `/app/`. |
| `server.py` | Two lines appended at the end to register `app_routes`. |
| `quantsync-lab/` | Synced to the GitHub `lab-ui` branch (it already had `/matches`, `/alerts`, `plain_by_year`), plus the additions below. |
| `lab/metrics.py` | `analytics()` builds the chart data: equity curve, monthly returns, how trade results were spread, how trades ended, average winner and loser, and the longest losing streak. `METRICS_V` 2 → 3, so cached past tests are recomputed once with the new chart data. |
| `lab/pipeline.py`, `lab/engine_feed.py` | Every past test (Lab and engine flagships) now carries `analytics`. |
| `lab/api.py` | Strategy cards carry a 32-point `spark`. New endpoints: `GET /chart-batch`, `GET /benchmark` (Nifty line, using the first of `NIFTY 50`, `NIFTY50`, `NIFTY`, `^NSEI`, `NIFTYBEES` found in the candle data), `GET /symbol/{sym}/strategies`, `GET` and `PUT /me/holdings`. |
| `lab/db.py` | New table `holdings` (`CREATE TABLE IF NOT EXISTS`, so it is safe on the existing database). |
| `quantsync-lab/tests/test_app_api.py` | Tests for all of the above. 40 of 40 tests pass. |

## Steps on the VPS

1. Pull or copy `vps_data_live` to the VPS.
2. Lab: copy `quantsync-lab/lab/*` into `/opt/quantsync-lab/app/lab/`, then:
   `sudo systemctl restart qslab-api qslab-worker-fast qslab-worker-ai`
3. Main API: `sudo systemctl restart quantsync_api` (or whichever unit runs `server.py`).
4. Open `https://quantsync-api.duckdns.org:8443/app/`.

No nginx change is needed: `/` already proxies to `server.py` and `/lab/` to the Lab.

## Firebase sign-in

Public pages work without signing in. For Google sign-in on this domain, add `quantsync-api.duckdns.org` in the Firebase console under Authentication → Settings → Authorized domains.

## Nifty benchmark line

Charts draw a Nifty 50 line when the Lab's candle data contains one of the symbols listed above. If none is present, the line is hidden.

To show it, add the index (or the `NIFTYBEES` ETF) to the candle source and run `python -m lab.cli cook`.

## Hosting on Vercel instead

Copy `web/app` into the Vercel project's `public/app/`. `data.js` detects that it is not on the VPS and calls the Lab through the existing `/lab/api/v1` rewrite. Main API calls go through the `/api/...` rewrite.

## Testing locally

Set `window.QS_CONFIG = {devUid: 'someone'}` before `data.js` loads, with the Lab running in `auth_mode: dev`. Requests are then sent as that user, without Firebase.

## Update 13 Oct: endpoint for the phone app at /m

`app_routes.py` adds `GET /api/app/market`. It reads every X-Ray report once and returns:

- the share of stocks above their 200-day and 50-day averages
- the number of positive, neutral and negative verdicts
- each sector's health, with its three strongest stocks
- the twelve highest X-Ray scores

The result is cached in memory until the reports change. It is also built once in the background at startup, so the first visitor does not wait.

To deploy:

1. Copy `app_routes.py` to the VPS.
2. Run `sudo systemctl restart quantsync_api`.
3. Check that `https://quantsync-api.duckdns.org:8443/api/app/market` returns JSON.

Until this is deployed, the phone app hides its Market health section and the Market map page shows an error with a retry button. Everything else works without it.
