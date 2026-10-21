# QuantSync Architecture Blueprint & Ingestion Master Plan

> **Database Engine:** SQLite 3 with Write-Ahead Logging (WAL)  
> **Production Location:** `/root/quantsync/Data/stock_history.db`  
> **Total Active Equities Universe:** 2,744+ NSE Listed Equities  
> **Architecture Level:** 7 Core Tables Across 3 Functional Layers  

---

## Executive Summary: External vs. Internal Data & Update Process

### 1. Which Tables Depend on Outside Data (External Ingestion)
These tables fetch their data **directly from external APIs, web feeds, or exchanges**:

| Table | External Source | What Data Enters Here |
| :--- | :--- | :--- |
| **`StockHistory`** | **Angel One SmartAPI** | Raw daily OHLCV candles (~8 years / up to 2,000 trading sessions per stock). |
| **`CompanyMaster`** *(Raw Columns)* | **Yahoo Finance**, **Screener.in**, **NSE India Official Website** | Market Cap, PE, PB, Borrowings, Reserves, Shareholding %, 3Y Growth, Nifty 50/200/500 flags. |
| **`CorporateEvents`** | **NSE India Announcements & Feeds** | Dividends, stock splits, bonus issues, board meetings, and bulk/block deals. |
| **`CompanyFundamentalsHistory`** | **Screener.in Statement Parser** | Multi-quarter historical P&L, Balance Sheet, and Cash Flow audited statements. |

### 2. Which Tables are Calculated Internally (Zero External Calls)
These tables and columns are computed **100% locally from your own database**:

| Table / Area | Calculated From | What is Calculated Locally |
| :--- | :--- | :--- |
| **`StockTechnicalsDaily`** | `StockHistory.rawCandles` | RSI (14), 20/50/200 EMAs, 52-week High/Low distance, Volume Spike ratio, Mansfield RS Rating (1–99), Weinstein Stage (1–4), Minervini VCP score. |
| **`SectorMetricsDaily`** | `CompanyMaster` + `StockTechnicalsDaily` | Sector Rotation (RRG quadrants: Leading, Weakening, Lagging, Improving), Industry Median P/E, Sector Top 5 RS Leaders. |
| **Derived `CompanyMaster` Fields** | Raw columns inside `CompanyMaster` + `StockHistory` | Net Debt (`borrowings - cash`), Piotroski F-Score (0–9), CFO/PAT ratio, Market Cap Tier (Large/Mid/Small), 3Y/5Y Median P/E. |

### 3. The End-to-End Update Process (Daily Post-Market Workflow)
Every trading day after market close, the automated pipeline runs in **4 sequential steps**:

```
3:35 PM IST  ──► STEP 1: Download today's price candle from Angel One (StockHistory)
                   │
3:45 PM IST  ──► STEP 2: Calculate Technicals locally from candles (StockTechnicalsDaily)
                   │
4:00 PM IST  ──► STEP 3: Sync Company profile & balance sheet (CompanyMaster)
                   │     Then compute derived math (Net Debt, Piotroski, 3Y Median PE)
                   │
4:30 PM IST  ──► STEP 4: Calculate Sector Rotation & Industry P/E (SectorMetricsDaily)
```

**Key Takeaway**: External data enters through **`StockHistory`**, **`CompanyMaster`**, and **`CorporateEvents`**. Everything else is **derived internally** using high-speed local vector math.

---

## 1. The 3-Level System Architecture

To prevent data corruption, maintain point-in-time backtesting integrity, and provide sub-50ms query latency for UI screeners and Stock DNA, all data is partitioned into 3 strict architectural tiers:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   stock_history.db                                     │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ LEVEL 1: RAW PRIMARY SOURCE (Audited / As-Reported by Feeds)                           │
│   • StockHistory                  → Raw 2,000 daily OHLCV candlestick time series     │
│   • CompanyMaster                 → 64-column profile, balance sheet, and live ratios  │
│   • CorporateEvents               → Dividends, splits, bonuses, bulk/block deals       │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ LEVEL 2: DERIVED COMPUTATIONS (Nightly Cron / Batch Engines)                          │
│   • CompanyFundamentalsHistory    → Multi-quarter point-in-time financial statements   │
│   • StockTechnicalsDaily          → Technical indicators (RSI, EMA, RS, VCP, Stage)    │
│   • SectorMetricsDaily            → RRG quadrants, sector median PE/PB, rotation score │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ LEVEL 3: TAXONOMY & AI INTELLIGENCE                                                    │
│   • SectorMaster                  → Official index constituents and mapping            │
│   • Narrative AI Fields           → Concall summaries, segment KPIs (future pipeline)  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Comprehensive Inventory of All Database Tables

### Table 1: `StockHistory` (Candlestick Time-Series Engine)
* **Purpose**: Preserves full historical daily OHLCV bars (~8 years / up to 2,000 trading sessions) per stock.
* **Primary Key**: `symbol`
* **Key Columns**:
  * `rawCandles`: JSON array of `[{"t": "YYYY-MM-DD", "o": ..., "h": ..., "l": ..., "c": ..., "v": ...}, ...]`
  * `candleCount`: Total candles stored.
  * `isAlgoEligible`: `1` = High-liquidity universe (1,305 stocks for V2/V3 strategies), `0` = Illiquid/SME research-only (1,439 stocks).
* **Population Source**: Angel One SmartAPI (`getCandleData` endpoint) via dual-broker rotation.
* **Cron Schedule**: Daily at 3:35 PM IST (`sync_missing_stocks_dual_broker.py`).

---

### Table 2: `CompanyMaster` (64-Column Primary Source Core)
* **Purpose**: Master entity record for company profile, live valuation, capitalization, and verified source metrics.
* **Primary Key**: `symbol`
* **Column Breakdown (64 Columns)**:
  * **Identity (8)**: `symbol`, `company_name`, `sector`, `industry`, `about`, `group_name`, `is_psu`, `is_fno`
  * **Valuation & Market (7)**: `market_cap_cr`, `pe_ratio`, `pb_ratio`, `peg_ratio`, `ev_ebitda`, `dividend_yield_pct`, `beta`
  * **Operating Compounding (8)**: `sales_cagr_3y`, `profit_cagr_3y`, `eps_growth_3y`, `opm_pct`, `roce_pct`, `roe_pct`, `debtor_days`, `cash_conversion_cycle`
  * **Balance Sheet & Solvency (8)**: `debt_to_equity`, `borrowings_cr`, `reserves_cr`, `cash_cr`, `total_assets_cr`, `equity_capital_cr`, `face_value`, `book_value_per_share`
  * **Cash Flow (2)**: `cfo_cr`, `free_cash_flow_cr`
  * **Shareholding (5)**: `promoter_holding_pct`, `promoter_pledged_pct`, `fii_holding_pct`, `dii_holding_pct`, `fii_change_qoq`
  * **Index Memberships (3)**: `is_nifty50`, `is_nifty200`, `is_nifty500`
  * **Historical Raw Feed (4)**: `quarterly_history_json`, `last_results_date`, `updated_at`, `group_source`
  * **Derived Level 2 Fields (19 - Kept NULL until Step 2/3)**: `industry_pe`, `median_pe_3y`, `median_pe_5y`, `pe_premium_discount_pct`, `net_debt_cr`, `interest_coverage`, `cfo_to_pat_ratio`, `delivery_pct_avg`, `acc_dist_grade`, `piotroski_score`, `contingent_liabilities_cr`, `next_results_date`, `pat_cr`, `operating_revenue_cr`, `revenue_by_segment_json`, `sector_kpis_json`, `concall_summary`, `mcap_tier`, `ttm_eps`.
* **Population Source**:
  1. **Yahoo Finance**: Real-time market cap, PE, PB, PEG, EV/EBITDA, OPM, Cash, Beta, About summary.
  2. **Screener.in**: Compounded 3Y Sales/Profit/EPS growth, ROCE, ROE, D/E ratio, Borrowings, Reserves, Shareholding.
  3. **NSE India CSVs**: Nifty 50, Nifty 200, Nifty 500 official constituent lists.

---

### Table 3: `StockTechnicalsDaily` (Pre-Computed Scanner & Screener Engine)
* **Purpose**: Houses pre-calculated daily technical indicators, volume profiles, and momentum rankings to power sub-10ms UI screeners without recalculating 2,000 candles per query.
* **Primary Key**: `symbol`
* **Key Columns**:
  * **Price & Moving Averages**: `cmp`, `change_1d_pct`, `change_1m_pct`, `change_3m_pct`, `change_6m_pct`, `change_1y_pct`, `ema20`, `ema50`, `ema200`, `sma200_slope`, `ema20_slope`
  * **Momentum & Oscillators**: `rsi14`, `stoch_rsi`, `adx14`, `macd_hist`, `bb_width_pct`
  * **High/Low & Ranges**: `high_52w`, `low_52w`, `dist_52w_high_pct`, `near_52w_high`, `atr14`
  * **Institutional Volume & Delivery**: `vol_20d_avg`, `vol_ratio`, `delivery_pct`, `delivery_qty`, `delivery_spike`, `acc_dist_grade`
  * **Relative Strength & Setup Quality**: `rs_rating` (Mansfield RS 1-99), `stage` (Stan Weinstein Stages 1-4), `vcp_score` (Mark Minervini Volatility Contraction Pattern), `minervini_stage`
* **Population Source**: Derived mathematically from `StockHistory.rawCandles`.
* **Cron Schedule**: Daily at 3:45 PM IST (`compute_daily_technicals.py`).

---

### Table 4: `CompanyFundamentalsHistory` (Point-in-Time Accounting Ledger)
* **Purpose**: Time-stamped historical quarterly and annual balance sheets, P&L, and cash flow statements for historical backtesting and fundamental trend charts.
* **Primary Key / Unique**: `(symbol, period_end_date, period_type)`
* **Key Columns**:
  * **Time & Provenance**: `quarter` ("Sep 2025"), `period_end_date` ("2025-09-30"), `reported_date`, `available_date`, `snapshot_date`, `source` ("SCREENER_CONSOLIDATED").
  * **P&L**: `revenue_cr`, `gross_profit_cr`, `op_profit_cr`, `opm_pct`, `net_profit_cr`, `eps`, `interest_expense_cr`.
  * **Balance Sheet**: `total_assets_cr`, `current_assets_cr`, `current_liabilities_cr`, `long_term_debt_cr`, `equity_capital_cr`, `reserves_cr`, `borrowings_cr`, `cash_cr`, `shares_outstanding`.
  * **Cash Flow & Ratios**: `cfo_cr`, `capex_cr`, `free_cash_flow_cr`, `roce_pct`, `roe_pct`, `debt_to_equity`.
  * **Shareholding**: `promoter_holding_pct`, `promoter_pledged_pct`, `fii_holding_pct`, `dii_holding_pct`, `public_holding_pct`.
* **Population Source**: Screener.in full financial statement table parser (`extract_statement_history.py`).

---

### Table 5: `CorporateEvents` (Actions & Smart Money Tracker)
* **Purpose**: Tracks dividends, stock splits, bonus issues, board meetings, and bulk/block deal filings.
* **Primary Key / Unique**: `(source, source_event_id)`
* **Key Columns**: `symbol`, `event_date`, `event_type` ("DIVIDEND" | "SPLIT" | "BULK_DEAL"), `ex_date`, `record_date`, `amount`, `quantity`, `deal_value_cr`, `counterparty` ("Dolly Khanna"), `buy_sell` ("BUY" | "SELL"), `details_json`.
* **Population Source**: NSE Corporate Actions API & Bulk Deal RSS feed.

---

### Table 6: `SectorMaster` (Taxonomy Registry)
* **Purpose**: Canonical registry of sectors and thematic indices.
* **Primary Key**: `sector_index` ("NIFTY IT", "NIFTY AUTO", "NIFTY PHARMA", etc.)
* **Key Columns**: `display_name`, `nifty_symbol`, `constituent_count`, `updated_at`.
* **Population Source**: NSE Indices constituent definitions.

---

### Table 7: `SectorMetricsDaily` (Relative Rotation & Macro Breadth)
* **Purpose**: Julius de Kempenaer Relative Rotation Graph (RRG) metrics and aggregated industry valuations.
* **Primary Key**: `(trade_date, sector_index)`
* **Key Columns**:
  * **RRG Vector**: `rs_ratio` (benchmark relative strength >100), `rs_momentum` (rate of change), `rrg_quadrant` ("Leading" | "Weakening" | "Lagging" | "Improving").
  * **Aggregated Multiples**: `median_pe`, `median_pb`, `median_roce`, `median_rs_rating`.
  * **Action & Leaders**: `sector_change_1d_pct`, `sector_change_1m_pct`, `top_stocks_json`, `lagging_stocks_json`.
* **Population Source**: Calculated daily after `StockTechnicalsDaily` and `CompanyMaster` are updated.

---

## 3. Plan for Populating the `NULL` Columns

The 29 `NULL` columns in `CompanyMaster` will be systematically populated through 3 targeted phases:

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                        SEQUENTIAL EXECUTION PIPELINE                                   │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 1: Raw Ingestion Engine [RUNNING NOW]                                            │
│   Script: populate_company_master.py                                                   │
│   Populates: 35 Raw Source Columns (Yahoo, Screener, NSE CSVs)                         │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 2: Derived Fundamental Engine [STEP 2]                                           │
│   Script: compute_derived_fundamentals.py                                              │
│   Populates: pat_cr, operating_revenue_cr, net_debt_cr, fii_change_qoq,                │
│              cfo_to_pat_ratio, piotroski_score, mcap_tier, is_psu                      │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 3: Technical & Historical Valuation Engine [STEP 3]                              │
│   Script: compute_daily_technicals.py                                                  │
│   Populates: median_pe_3y, median_pe_5y, pe_premium_discount_pct,                      │
│              industry_pe, delivery_pct_avg, acc_dist_grade                             │
├────────────────────────────────────────────────────────────────────────────────────────┤
│ Phase 4: Corporate Actions & Event Calendar [STEP 4]                                   │
│   Script: sync_corporate_events.py                                                     │
│   Populates: next_results_date, CorporateEvents table                                  │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Detailed Field Resolution Plan

| Column | Phase | Computation / Logic Applied |
| :--- | :--- | :--- |
| **`pat_cr`** | Phase 2 | $\sum$ `net_profit` across last 4 quarters from `quarterly_history_json`. |
| **`operating_revenue_cr`** | Phase 2 | $\sum$ `sales` across last 4 quarters from `quarterly_history_json`. |
| **`net_debt_cr`** | Phase 2 | `borrowings_cr - cash_cr` (negative indicates net cash balance). |
| **`fii_change_qoq`** | Phase 2 | `fii_holding_pct (current quarter) - fii_holding_pct (previous quarter)`. |
| **`cfo_to_pat_ratio`** | Phase 2 | `cfo_cr / pat_cr` (checks cash conversion quality). |
| **`piotroski_score`** | Phase 2 | 9-point score evaluation across profitability, leverage, and efficiency from historical statements. |
| **`mcap_tier`** | Phase 2 | `Large Cap` (>₹20,000 Cr), `Mid Cap` (₹5,000–₹20,000 Cr), `Small Cap` (<₹5,000 Cr). |
| **`is_psu`** | Phase 2 | `1` if "Government of India" or "President of India" in shareholding, else `0`. |
| **`median_pe_3y`** | Phase 3 | 50th percentile of `(close / ttm_eps)` over 750 trading sessions in `StockHistory`. |
| **`median_pe_5y`** | Phase 3 | 50th percentile of `(close / ttm_eps)` over 1,250 trading sessions in `StockHistory`. |
| **`pe_premium_discount_pct`** | Phase 3 | `((current_pe - median_pe_3y) / median_pe_3y) * 100`. |
| **`industry_pe`** | Phase 3 | Market-cap weighted median P/E for all stocks belonging to the same `industry`. |
| **`delivery_pct_avg`** | Phase 3 | 30-day simple moving average of NSE delivery percentages. |
| **`acc_dist_grade`** | Phase 3 | Chaikin Money Flow and volume Accumulation/Distribution grade (`A`, `B`, `C`, `D`, `E`). |
| **`next_results_date`** | Phase 4 | Ingested directly from NSE India Corporate Events Calendar. |
| **`concall_summary`** | Phase 5 | LLM transcript summarizer extracting management commentary, headwinds, and guidance. |

---

## 4. Operational Runbook

### Running Daily Production Ingestion
1. **At 3:35 PM IST (After Market Close)**:
   ```bash
   /root/quantsync/venv/bin/python3 /root/quantsync/scripts/sync_missing_stocks_dual_broker.py
   ```
2. **At 3:45 PM IST (Technical Engine & Scanners)**:
   ```bash
   /root/quantsync/venv/bin/python3 /root/quantsync/scripts/compute_daily_technicals.py
   ```
3. **At 4:00 PM IST (Fundamental Sync & Derived Fields)**:
   ```bash
   /root/quantsync/venv/bin/python3 /root/quantsync/scripts/populate_company_master.py
   /root/quantsync/venv/bin/python3 /root/quantsync/scripts/compute_derived_fundamentals.py
   ```
4. **At 4:30 PM IST (Sector Rotation RRG Engine)**:
   ```bash
   /root/quantsync/venv/bin/python3 /root/quantsync/scripts/compute_sector_metrics.py
   ```
