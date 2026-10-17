# QuantSync intelligence (`intel/`)

Collects every public filing, insider trade, pledge change, bulk/block deal, board meeting, FII/DII flow, global market move and news headline, scores each one 0–100, reads the important PDFs with the local model, and builds morning and evening briefs. Everything lands in `Data/intel.db` and is served at `/api/intel2/*`. The Lab scanner joins the same data, point-in-time, so users can scan and backtest on it.

## Install (once)

```bash
cd /root/quantsync
./venv/bin/pip install -r requirements.txt          # adds pypdf
./venv/bin/python -m intel.run all                  # first full collection (creates Data/intel.db)
./venv/bin/python -m intel.run fields               # company + ownership facts
./venv/bin/python -m intel.run morning              # first brief
crontab crontab.txt                                 # schedules (times are IST)
sudo systemctl restart quantsync_api                # mounts /api/intel2
```

Filing reader (optional, needs Ollama on the phone):

```bash
ollama pull gemma3n:e4b
./venv/bin/python -m intel.run read
# Long documents on the PC instead:  export QS_OLLAMA_HEAVY=http://<pc-ip>:11434  QS_LLM_HEAVY_MODEL=gemma3:12b
```

Lab (scanner fields + strategy library):

```bash
# /opt/quantsync-lab/config.yaml — the lab user must be able to read these two files
intel_db: /root/quantsync/Data/intel.db
hist_db: /root/quantsync/Data/stock_history.db

sudo -u qslab QSLAB_CONFIG=/opt/quantsync-lab/config.yaml /opt/quantsync-lab/venv/bin/python -m lab.cli cook      # FSV 3: full rebuild
sudo -u qslab QSLAB_CONFIG=/opt/quantsync-lab/config.yaml /opt/quantsync-lab/venv/bin/python -m lab.cli library   # 31 strategies, real past tests
```

## Jobs

| Job | What | When (IST) |
| --- | --- | --- |
| `filings` | NSE + BSE announcements (PDF links queued for the reader) | every 5 min, 08:00–20:59, Mon–Fri |
| `meetings` | Board meeting calendar (results dates) | hourly |
| `insider` | Insider trades (SEBI PIT), SAST, promoter pledges | every 30 min, 09:00–21:59 |
| `flows` | Bulk and block deals (NSE archive CSV), FII/DII | 18:30, 20:30 |
| `world` | 19 global markets, 11 news and policy feeds | hourly |
| `read` | Local-model reading of pending PDFs, highest impact first | every 20 min (18-minute budget) |
| `fields` | `facts_daily` + `facts_owner` for the scanner | 16:45 |
| `morning` / `evening` | Sweep + brief + Telegram | 07:00 / 17:00 |

Each job takes a lock, so a slow run never overlaps the next. A failing source is recorded and skipped; see `/api/intel2/status`.

## API (`/api/intel2`)

`events` (feed; filters: symbol, type, category, min_impact, days, q, before) · `events/{id}` · `top` · `stock/{symbol}` (timeline, insiders, deals, meetings, pledges, owners, facts in one call) · `brief?kind=morning|evening|auto` · `deals` · `insiders` · `calendar` · `markets` · `status` · `types`

## Rules that keep it honest

- Exact numbers come from structured sources (deal CSVs, insider disclosures, ratios). The model's quoted numbers are kept only if they appear in the document text.
- `known_at` = the first day the market could act on a fact (filings after 15:30 count from the next day; shareholding 21 days after quarter end). Backtests only see a fact from that day.
- No buy/sell calls anywhere. Facts, summaries and user-written rules only.

Tests: `python -m pytest tests/test_intel.py` and `cd quantsync-lab && python -m pytest`.

## Fast data (precomputed)
- `GET /api/intel2/markets` now returns per-tile `prev`, `hi`, `lo`, `spark` (today's line) and `grp` (India / World / Commodities & FX). Job `markets` (every 15 min). Yahoo tickers for Sensex (`^BSESN`), Nifty Midcap (`^NSEMDCP50`) and Smallcap (`^CNXSC`) must be checked on the VPS; an unknown ticker is skipped and the tile just does not show.
- `GET /api/intel2/home` is the News "Today" page written by job `home` (every 15 min and after the morning/evening briefs). Counts only; no model text.
- Lab: `GET /lab/v1/screens/board` (all 59 screens in one cacheable response), `GET /lab/v1/screens/{id}`, `GET /lab/v1/changes`, `GET /lab/v1/universe`. See quantsync-lab/README.
