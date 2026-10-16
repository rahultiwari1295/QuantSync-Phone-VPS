# QuantSync Production VPS Platform (`Prod_VPS`)
=====================================================
**Autonomous Multi-Engine Quantitative Trading & Real-Time Portfolio Telemetry Platform**

---

## 1. System Overview & Architecture

`Prod_VPS` is the unified production execution environment for the QuantSync algorithmic trading systems. It hosts multiple independent quantitative engines (such as **QuantSync-V2** and **QuantSync-V3**), manages automated broker authentication, executes intraday price sweeps and end-of-day decision cycles, handles statutory friction accounting, dispatches instant multi-channel Telegram alerts, and synchronizes real-time portfolio telemetry to the public Wix Studio portal.

### High-Level Architecture Diagram

```
                              ┌───────────────────────────────────┐
                              │     Angel One Broker (SmartAPI)   │
                              │  (TOTP Auth, Hist DB, Live LTP)  │
                              └─────────────────┬─────────────────┘
                                                │
                                                ▼
┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 QUANTSYNC PROD_VPS PLATFORM                                      │
│                                                                                                  │
│  ┌────────────────────────┐       ┌────────────────────────┐       ┌──────────────────────────┐  │
│  │   scheduler.py daemon  │──────▶│   Engine Registry      │──────▶│ Multi-Engine Plugins     │  │
│  │  (Systemd Service 24/7)│       │ (Auto-discovery in     │       │ • QuantSync-V2 (Dual)    │  │
│  │  09:15-15:30: 5m LTP   │       │  engines/*/engine.py)  │       │ • QuantSync-V3 (WalkFwd) │  │
│  │  15:15: Decision Cycle │       └────────────────────────┘       │ • Future Plugins (V4...) │  │
│  │  15:45: EOD Finalize   │                                        └────────────┬─────────────┘  │
│  └───────────┬────────────┘                                                     │                │
│              │                                                                  │                │
│              ▼                                                                  ▼                │
│  ┌────────────────────────┐                                        ┌──────────────────────────┐  │
│  │ Master Storage Layer   │◀───────────────────────────────────────│ Trade Execution Ledger   │  │
│  │ (quantsync_master.db)  │  • active_trades: Open positions       │ • Buy / Exit Fills       │  │
│  │                        │  • closed_trades: Completed ledger     │ • Statutory Friction     │  │
│  │                        │  • daily_nav: Mark-to-Market curves    │ • MTM Accounting         │  │
│  └───────────┬────────────┘                                        └──────────────────────────┘  │
└──────────────┼───────────────────────────────────────────────────────────────────┼───────────────┘
               │                                                                   │
               ▼                                                                   ▼
┌───────────────────────────────┐                                   ┌──────────────────────────────┐
│  Wix Studio Portal Functions  │                                   │   Telegram Broadcast System  │
│  • Real-time Active Cards     │                                   │   • Master: @option_call_alert
│  • Completed Trade Ledger     │                                   │   • V2: @rt1295_buyalert     │
│  • Live NAV & Drawdown Curve  │                                   │   • V3: @rt1295_sellalert    │
└───────────────────────────────┘                                   └──────────────────────────────┘
```

---

## 2. Directory Structure & Component Roles

```text
Prod_VPS/
├── README.md                      # [THIS FILE] Complete platform architectural documentation
├── config.json                    # Central configuration (engines, credentials, limits, endpoints)
├── scheduler.py                   # 24/7 background scheduler (LTP sync, 15:15 decision, 15:45 EOD)
├── quantsync.service              # Linux Systemd unit file for 24/7 daemon management
├── check_health.py                # 7-point automated pre-flight system diagnostics script
├── cli.py                         # Unified operator CLI (status, single-day run, historical replays)
│
├── common/                        # Shared platform infrastructure
│   ├── broker.py                  # Angel One SmartAPI client, TOTP auth, quote fetchers
│   ├── cost_model.py              # Exact Indian statutory friction model (STT, stamp, exchange, GST)
│   ├── notifier.py                # Multi-channel Telegram dispatcher with rich formatting & proof cards
│   ├── storage.py                 # SQLite persistence layer (quantsync_master.db)
│   └── wix_sync.py                # Public Wix Studio API client (POSTs portfolio telemetry)
│
├── engines/                       # Engine plugin ecosystem
│   ├── README.md                  # Comprehensive Engine Developer Guide & Plugin Specification
│   ├── base_engine.py             # Abstract Base Class defining the plugin contract
│   ├── registry.py                # Auto-discovery loader discovering engines/*/engine.py
│   ├── v2/                        # QuantSync-V2 (Dual-Sleeve Breakout + Alpha Momentum)
│   └── v3/                        # QuantSync-V3 (Multi-Timeframe Walk-Forward ML Ensemble)
│
├── scripts/                       # High-performance simulation, replay & testing utilities
│   ├── stream_simulation.py       # Multi-engine day-by-day historical stream and ledger builder
│   ├── test_optimal_comparison.py # Multi-year walk-forward comparative benchmark (2024-2026)
│   ├── ablation_study_rules.py    # Modular ablation study testing individual quantitative guards
│   ├── compare_v2_universes.py    # Side-by-side comparison of 1,300 vs 800 universes
│   └── sync_angel_candles.py      # EOD candle synchronization from Angel One
│
├── Data/                          # Persistent data storage
│   ├── stock_history.db           # Canonical SQLite database of 1,319 NSE equities (daily candles)
│   ├── quantsync_master.db        # Production ledger (active_trades, closed_trades, daily_nav)
│   ├── v2_candidates_cache.pkl    # Precomputed ML setup candidates for instant replays
│   ├── current_ltp.json           # Cached real-time LTP quotes
│   └── logs/                      # Rotating application logs (scheduler.log, stream_run.log)
│
└── reports/                       # Exported benchmark CSV reports and audit summaries
```

---

## 3. Central Configuration (`config.json`)

All platform parameters are defined in `config.json`:
* **`paths`**: Data locations (`history_db`, `master_db`, `ltp_cache`, `logs_dir`, `reports_dir`).
* **`engines`**: Configuration for each active engine:
  * `capital`: Starting capital allocation (e.g. ₹3,00,000.00).
  * `capital_per_pos`: Position sizing per slot (e.g. ₹10,000.00).
  * `max_slots`: Maximum concurrent positions.
  * Engine-specific parameters (e.g. `sleeve1_slots`, `sleeve2_slots`, `sleeve2_gate`, `threshold`).
  * `telegram`: Specific bot tokens and channel chat IDs.
* **`broker`**: Angel One SmartAPI credentials (`client_id`, `password`, `api_key`, `totp_secret`).
* **`wix`**: Target webhook endpoint (`update_url`) for real-time portal synchronization.

---

## 4. Master Persistence Layer (`quantsync_master.db`)

Managed by `common/storage.py`, providing atomic, multi-engine persistence:

1. **`active_trades` Table**: Tracks all currently open positions across all engines.
   * Fields: `trade_id`, `engine_version`, `symbol`, `strategy_id`, `entry_date`, `entry_price`, `current_stop`, `target_price`, `shares`, `invested_capital`, `current_ltp`, `unrealized_pnl`, `status`.
2. **`closed_trades` Table**: Complete historical completed trade ledger.
   * Fields: `trade_id`, `engine_version`, `symbol`, `strategy_id`, `entry_date`, `entry_price`, `exit_date`, `exit_price`, `exit_reason`, `shares`, `allocated_capital`, `gross_pnl`, `total_costs`, `realized_pnl`, `realized_r`, `return_pct`, `days_held`.
3. **`daily_nav` Table**: Mark-to-market daily portfolio NAV tracking for equity curve generation.
   * Fields: `date`, `engine_version`, `cash`, `invested_capital`, `total_nav`, `active_positions_count`, `drawdown_inr`, `drawdown_pct`, `peak_equity`.

---

## 5. Automated Execution Schedule (`scheduler.py`)

The platform runs 24/7 as a Linux Systemd daemon (`quantsync.service`). During Indian market days (Monday to Friday, 09:15 to 15:30 IST), it autonomously triggers:

| Time (IST) | Scheduled Job | Description |
| :--- | :--- | :--- |
| **09:15 – 15:30** *(Every 5 min)* | `job_ltp_sync` | Fetches live LTP quotes from Angel One for open positions, evaluates intraday stop losses / targets, and pushes real-time PnL to Wix. |
| **14:30, 15:00, 15:10** | `job_market_sweep` | Pre-close quote sweep updating current daily candle state. |
| **15:15** | `job_decision_1515` | **Primary Decision Engine**: Evaluates ML setup candidates, checks price sanity and liquidity filters, executes fills, allocates capital, updates ledger, and pushes decision alerts. |
| **15:35** | `job_1535_candle_sync`| Downloads official finalized EOD daily candles from Angel One into `stock_history.db`. |
| **15:45** | `job_finalize_1545` | Final EOD reconciliation, Mark-to-Market NAV calculation, peak equity tracking, audit proof dispatch, and final Wix portal sync. |

---

## 6. Operation & CLI Reference

### 6.1 Checking System Health
Run the automated pre-flight audit at any time to verify all 7 external integrations:
```bash
python3 check_health.py
```
Checks verified: Python dependencies, database candle integrity, broker authentication, Telegram routing, plugin discovery, SQLite schema, and Wix API connectivity.

### 6.2 Managing the 24/7 Service (VPS)
```bash
# Check service status
systemctl status quantsync

# View live streaming logs
journalctl -u quantsync.service -f

# Restart daemon after code update
systemctl restart quantsync

# Stop or Start daemon
systemctl stop quantsync
systemctl start quantsync
```

### 6.3 Running Multi-Day Replays & Streams
To rebuild the master database or test historical performance:
```bash
# Stream 2026 day-by-day (suppressing past telegram notifications):
python3 scripts/stream_simulation.py --start 2026-01-01 --end 2026-09-04 --notify-start 2026-09-08 --delay 0.0

# Multi-year walk-forward benchmark (2024 to 2026):
python3 scripts/test_optimal_comparison.py
```

### 6.4 Operator CLI (`cli.py`)
```bash
# Check current platform status across all engines:
python3 cli.py --status

# Execute a single-day dry-run (paper test):
python3 cli.py --date 2026-09-04 --dry-run
```
