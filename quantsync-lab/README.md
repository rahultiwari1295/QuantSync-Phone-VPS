# QuantSync Lab: VPS backend

Isolated service (own Linux user, venv, ports, SQLite, Parquet). It reads the live platform's candle files and never writes to them.

```
lab/features.py   indicators (no look-ahead)      lab/rules.py      rule JSON -> validated, hashed, parameterised SQL
lab/store.py      cook + DuckDB scans             lab/simulator.py  one trade simulator for history and live
lab/ai.py         LightGBM gate, walk-forward     lab/pipeline.py   scan / backtest / enhance / enhance-exit
lab/jobs.py       SQLite queue (fast, ai lanes)   lab/service.py    strategies, job handlers, live tracker
lab/api.py        FastAPI /lab/v1                 lab/worker.py     queue workers
```

## Run tests (laptop or VPS)
`pip install -r requirements-dev.txt && pytest -q`   (uses synthetic candles; 20 tests incl. no-lookahead, parity, leak, rule fuzz, golden trades)

## Install on the VPS (Phases 0-9 of the plan)
1. Candles: set `candles_db` in config.yaml to the live `Data/stock_history.db` (read-only, `StockHistory.rawCandles`), or put one `<SYMBOL>.parquet` per stock in `candles_dir`.
2. `useradd -r -m qslab`; `mkdir -p /opt/quantsync-lab/{app,data,db,models,runs,logs,backups,run}`; copy this folder to `app/`; `python3 -m venv ../venv && ../venv/bin/pip install -r requirements.txt`.
3. `cp config.example.yaml /opt/quantsync-lab/config.yaml` and set `firebase_project_id`; add a 2 GB swapfile if none.
4. `sudo -u qslab ../venv/bin/python -m lab.cli cook`, check the printed counts, then copy `deploy/*.service|timer` to `/etc/systemd/system/`, `systemctl enable --now qslab-api qslab-worker-fast qslab-worker-ai qslab-cook.timer`.
5. Add `deploy/nginx-lab.conf` to the server block, `nginx -t && systemctl reload nginx`. In Vercel add rewrite `/lab/api/:path*` -> `https://quantsync-api.duckdns.org:8443/lab/:path*`.
6. Seed flagships: `python -m lab.cli seed-flagship "AI Alpha" rule.json` (rule.json = `{"entry":{...},"exit":{...}}`).
7. Rollback: disable the services, remove the nginx include and the Vercel rewrite. Nothing else changes.

## Known gaps to close with the developer
- `pattern_floor` / `stops_matching` exits, the chart-pattern scan and `/patterns` need the pattern-line output of the live engine.
- Flagship AI Alpha / AI Pattern rules, EOD bar source, delisted stocks, delivery % and pricing limits are open questions in the plan.
- Per-symbol Parquet files replace year partitions (same DuckDB queries, simpler incremental cook).
