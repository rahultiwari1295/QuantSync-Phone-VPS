# QuantSync Institutional Database Architecture & Data Ingestion Guide

> **Authoritative Technical Blueprint**  
> **Database Engine:** SQLite 3 with Write-Ahead Logging (WAL)  
> **Production Location:** `/root/quantsync/Data/stock_history.db`  
> **Scope:** Full Indian Equities Universe (2,744+ NSE Listed Equities)

---

## 1. Architectural Overview: The Triad Table Design

To deliver an institutional-grade platform combining **Stock DNA**, a **High-Speed Screener**, and a **Custom Strategy Creator**, the database separates high-frequency time series data from fundamental metadata and pre-computed scanner metrics.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   stock_history.db                                     │
├──────────────────────────┬─────────────────────────────┬───────────────────────────────┤
│   1. StockHistory        │   2. CompanyMaster          │   3. StockTechnicalsDaily     │
│   (Candlestick Engine)   │   (Profile & Fundamentals)  │   (Pre-Computed Scanner)      │
├──────────────────────────┼─────────────────────────────┼───────────────────────────────┤
│ • symbol (PK)            │ • symbol (PK)               │ • symbol (PK)                 │
│ • token (Angel One)      │ • company_name              │ • cmp (Last Traded Price)     │
│ • rawCandles (2000 bars) │ • sector & industry         │ • rsi14 (Wilder's RSI)        │
│ • candleCount            │ • group_name & is_psu       │ • ema20, ema50, ema200        │
│ • isAlgoEligible (0 / 1) │ • is_fno & mcap_tier        │ • dist_52w_high_pct           │
│ • isV3Eligible (0 / 1)   │ • pe, pb, ev_ebitda         │ • vol_ratio (Volume Spike)    │
│                          │ • roce, roe, d_to_e         │ • rs_rating (1 to 99)         │
│                          │ • cfo_cr, fcf_cr            │ • stage (Stan Weinstein 1-4)  │
│                          │ • fii_dii_holdings          │ • atr14 (Volatility)          │
│                          │ • piotroski_score (0-9)     │                               │
└──────────────────────────┴─────────────────────────────┴───────────────────────────────┘
```

---

## 2. Table 1: `StockHistory` (OHLCV Time-Series)

Stores up to 2,000 daily candles (~8 years of price action) for all 2,744 NSE equities.

### Schema Definition
```sql
CREATE TABLE StockHistory (
    symbol TEXT PRIMARY KEY,
    token TEXT,
    rawCandles TEXT,           -- JSON array of {"t": "YYYY-MM-DD", "o": ..., "h": ..., "l": ..., "c": ..., "v": ...}
    candleCount INTEGER,       -- Total bars stored (up to 2,000)
    isAlgoEligible INTEGER DEFAULT 0,  -- 1 = Liquid Trading Basket (V2/V3), 0 = DNA Research Only
    isV3Eligible INTEGER DEFAULT 0     -- Synchronized with isAlgoEligible for legacy compatibility
);
CREATE INDEX idx_sh_algo ON StockHistory(isAlgoEligible);
```

### Purpose of `isAlgoEligible` (V2 & V3 Safety Shield)
* **`1` (1,305 Stocks)**: Liquid, institutionally traded equities. V2 and V3 algorithms scan **only** these stocks.
* **`0` (1,437 Stocks)**: Non-algorithmic, SME, and circuit-prone stocks. Fully queryable in Stock DNA, but invisible to backtests and live trading signals, preventing corrupted executions and circuit traps.

### How Data is Ingested
* **Source**: Angel One SmartAPI (`getCandleData` endpoint).
* **Dual-Broker Engine**: [`vps_data_live/scripts/sync_missing_stocks_dual_broker.py`](file:///Users/rahultiwari/Projects/WIX_with_local/wix_latest_code/QuantSync/vps_data_live/scripts/sync_missing_stocks_dual_broker.py).
* **Throughput**: ~5.6 requests/second across Broker 1 (`S817363`) and Broker 2 (`R281124`).
* **Update Frequency**: Daily EOD at 3:35 PM IST via automated cron job.

---

## 3. Table 2: `CompanyMaster` (Profile & Audited Fundamentals)

Stores static corporate metadata, sector taxonomy, shareholding patterns, and quarterly audited financials.

### 1. `CompanyMaster` (Strict Source-Only Factual Core)

Stores **only audited primary source data** received directly from **Yahoo Finance** and **Screener.in**.
**Golden Ingestion Rule**:
* *Value received from Yahoo/Screener* $\rightarrow$ Store exact value.
* *Value not received or unavailable* $\rightarrow$ Store `NULL`.
* **Zero synthetic defaults, zero hardcoded strings, zero mathematical reconstructions in this table.**

```sql
CREATE TABLE CompanyMaster (
    -- 1. Identity & Classification (Source: Yahoo Finance)
    symbol TEXT PRIMARY KEY,
    company_name TEXT,             -- Yahoo: longName / shortName (NULL if missing)
    sector TEXT,                   -- Yahoo: sector (NULL if missing)
    industry TEXT,                 -- Yahoo: industry (NULL if missing)
    about TEXT,                    -- Yahoo: longBusinessSummary (Exact full unedited text)
    group_name TEXT,               -- NULL (reserved for authoritative exchange feed)
    is_psu INTEGER,                -- NULL (reserved for authoritative CPSE index list)
    is_fno INTEGER DEFAULT 0,      -- 1 = Active in NSE F&O Segment, 0 = Cash segment

    -- 2. Valuation Benchmarks (Source: Yahoo Finance & Screener)
    market_cap_cr REAL,            -- Yahoo: marketCap / Screener Market Cap
    pe_ratio REAL,                 -- Yahoo: trailingPE / Screener Stock P/E
    pb_ratio REAL,                 -- Yahoo: priceToBook (NULL if missing, no CMP/BV reconstruction)
    peg_ratio REAL,                -- Yahoo: pegRatio (NULL if missing, no PE/growth reconstruction)
    ev_ebitda REAL,                -- Yahoo: enterpriseToEbitda (NULL if missing)
    dividend_yield_pct REAL,       -- Yahoo: dividendYield / Screener Dividend Yield

    -- 3. Compounding, Profitability & Margins (Source: Yahoo & Screener)
    sales_cagr_3y REAL,            -- Screener: 3-Year Compounded Sales Growth
    profit_cagr_3y REAL,           -- Screener: 3-Year Compounded Profit Growth
    opm_pct REAL,                  -- Yahoo: operatingMargins (NULL if missing)
    roce_pct REAL,                 -- Screener: ROCE (NULL if missing)
    roe_pct REAL,                  -- Screener: ROE (NULL if missing)

    -- 4. Balance Sheet & Working Capital (Source: Screener)
    borrowings_cr REAL,            -- Screener Balance Sheet: Borrowings
    reserves_cr REAL,              -- Screener Balance Sheet: Reserves
    cash_cr REAL,                  -- Yahoo: totalCash (NULL if missing)
    debt_to_equity REAL,           -- Screener Reported Debt to Equity (NULL if missing)
    interest_coverage REAL,        -- NULL (no hardcoded 99.9)
    debtor_days INTEGER,           -- Screener Ratios: Debtor Days
    cash_conversion_cycle INTEGER, -- Screener Ratios: Cash Conversion Cycle
    cfo_cr REAL,                   -- Screener Cash Flow: Cash from Operating Activity
    free_cash_flow_cr REAL,        -- Screener Cash Flow: Free Cash Flow

    -- 5. Shareholding (Source: Screener Shareholding Section)
    promoter_holding_pct REAL,     -- Screener: Promoters %
    promoter_pledged_pct REAL,     -- Screener: Pledged % (NULL if absent, no 0.0 assumption)
    fii_holding_pct REAL,          -- Screener: FIIs %
    dii_holding_pct REAL,          -- Screener: DIIs %

    -- 6. Technicals & Audit Trail
    beta REAL,                     -- Yahoo: beta (NULL if missing)
    quarterly_history_json TEXT,   -- Screener: Recent 4 Quarters Audited P&L (NULL for missing cells)
    updated_at TEXT                -- ISO Date of ingestion

    -- NOTE: Derived model analytics (Net Debt, Acc/Dist grade, Piotroski score, Industry PE medians)
    -- are stored strictly as NULL or evaluated in separate derived analytical views/tables.
);

CREATE INDEX idx_cm_sector ON CompanyMaster(sector);
CREATE INDEX idx_cm_industry ON CompanyMaster(industry);
CREATE INDEX idx_cm_mcap_tier ON CompanyMaster(mcap_tier);
CREATE INDEX idx_cm_is_fno ON CompanyMaster(is_fno);
CREATE INDEX idx_cm_is_psu ON CompanyMaster(is_psu);
```

### Real-World Data Sources (Zero Synthetic Data)

| Category | Source | How It Is Fetched |
| :--- | :--- | :--- |
| **Symbols & Names** | NSE Official `EQUITY_L.csv` | Direct HTTP download from NSE Archives |
| **F&O Status** | NSE Derivatives Scrip Master | Parsed from Angel One / NSE F&O Master |
| **Corporate Groups** | BSE/NSE Listed Group Registry | Tagged via authoritative corporate group mapping |
| **Audited Financials** | Screener.in Consolidated Filings | Ingested via `vps_data_live/fundamental_fetcher.py` |
| **Shareholding & Pledges** | SEBI Quarterly Shareholding Filings | Extracted from Screener/NSDL depository reports |
| **Piotroski F-Score** | Mathematical Accounting Formula | Calculated from 9 balance sheet & P&L ratios |

---

## 4. Table 3: `StockTechnicalsDaily` (Pre-Computed Daily Scanner Engine)

Pre-computed every evening at 3:35 PM IST after market close. This eliminates real-time candle crunching and allows custom screener queries to execute in **under 15 milliseconds**.

### Schema Definition
```sql
CREATE TABLE StockTechnicalsDaily (
    symbol TEXT PRIMARY KEY,
    cmp REAL,                      -- Current Market Price (Last Close)
    change_1d_pct REAL,            -- Daily Percentage Change (%)
    change_1m_pct REAL,            -- 1-Month Percentage Change (%)
    ema20 REAL,                    -- 20-Day Exponential Moving Average
    ema50 REAL,                    -- 50-Day Exponential Moving Average
    ema200 REAL,                   -- 200-Day Exponential Moving Average
    rsi14 REAL,                    -- 14-Day Wilder's Relative Strength Index
    high_52w REAL,                 -- 52-Week High
    low_52w REAL,                  -- 52-Week Low
    dist_52w_high_pct REAL,        -- Distance from 52-Week High (%)
    vol_20d_avg INTEGER,           -- 20-Day Average Traded Volume
    vol_ratio REAL,                -- Today's Volume / vol_20d_avg (Volume Breakout)
    rs_rating INTEGER,             -- Relative Strength Rank (1 to 99 vs Nifty 50)
    stage INTEGER,                 -- Stan Weinstein Stage (1=Base, 2=Markup, 3=Top, 4=Decline)
    atr14 REAL,                    -- 14-Day Average True Range
    updated_at TEXT                -- ISO Date of computation
);

CREATE INDEX idx_tech_rs ON StockTechnicalsDaily(rs_rating);
CREATE INDEX idx_tech_rsi ON StockTechnicalsDaily(rsi14);
CREATE INDEX idx_tech_vol_ratio ON StockTechnicalsDaily(vol_ratio);
CREATE INDEX idx_tech_dist_52w ON StockTechnicalsDaily(dist_52w_high_pct);
CREATE INDEX idx_tech_stage ON StockTechnicalsDaily(stage);
```

### Exact Mathematical Formulas Used

1. **RSI 14 (Wilder's Standard)**:
   $$\text{RS} = \frac{\text{EMA}(\text{Gain}, 14)}{\text{EMA}(\text{Loss}, 14)}, \quad \text{RSI} = 100 - \left(\frac{100}{1 + \text{RS}}\right)$$
2. **52-Week High Distance**:
   $$\text{dist\_52w\_high\_pct} = \frac{\text{CMP} - \text{High}_{52w}}{\text{High}_{52w}} \times 100$$
3. **Volume Spike Ratio**:
   $$\text{vol\_ratio} = \frac{\text{Latest Volume}}{\text{Average Volume of Prior 20 Sessions}}$$
4. **William O'Neil RS Rating (Percentile 1–99)**:
   $$\text{Weighted Return} = 2 \times R_{Q4} + R_{Q3} + R_{Q2} + R_{Q1}$$
   Where $R_{Q4}$ is return over the most recent 63 trading days. All 2,744 stocks are ranked into percentiles $1$ (worst) to $99$ (best).
5. **Stan Weinstein Stage Analysis (1 to 4)**:
   * **Stage 2 (Markup / Bullish)**: $\text{CMP} > \text{EMA}_{50} > \text{EMA}_{200}$ and $\text{EMA}_{50}$ sloping upward.
   * **Stage 4 (Decline / Bearish)**: $\text{CMP} < \text{EMA}_{50} < \text{EMA}_{200}$.
   * **Stage 1 (Base / Accumulation)**: Flat $\text{EMA}_{200}$ with price oscillating tightly.
   * **Stage 3 (Distribution / Top)**: High volatility around a flattening $\text{EMA}_{50}$.

---

## 5. Execution Pipeline: How Records are Fetched and Maintained

### Automated Cron Schedule (VPS)

```cron
# 1. 15:35 IST (Mon-Fri): Sync daily closing candles into StockHistory
35 15 * * 1-5 /root/quantsync/venv/bin/python3 /root/quantsync/common/candle_sync.py >> /root/quantsync/Data/daily_candle_sync.log 2>&1

# 2. 15:45 IST (Mon-Fri): Pre-compute StockTechnicalsDaily (RSI, EMAs, RS Rank, Stages)
45 15 * * 1-5 /root/quantsync/venv/bin/python3 /root/quantsync/scripts/compute_daily_technicals.py >> /root/quantsync/Data/daily_technicals.log 2>&1

# 3. 02:00 IST (Saturdays): Refresh audited quarterly financials in CompanyMaster
0 2 * * 6 /root/quantsync/venv/bin/python3 /root/quantsync/scripts/update_company_master.py >> /root/quantsync/Data/weekly_fundamentals.log 2>&1
```

---

## 6. High-Performance SQL Strategy Examples

Because all tables share `symbol` as their primary key, custom user strategies run in milliseconds:

### A. The Institutional "CANSLIM" Leader Screen
```sql
SELECT c.symbol, c.company_name, c.sector, t.cmp, t.rs_rating, c.roce_pct, c.profit_cagr_3y
FROM CompanyMaster c
JOIN StockTechnicalsDaily t ON c.symbol = t.symbol
WHERE t.stage = 2
  AND t.rs_rating >= 85
  AND t.dist_52w_high_pct >= -10.0
  AND c.roce_pct >= 20.0
  AND c.profit_cagr_3y >= 25.0
  AND c.debt_to_equity <= 0.3
ORDER BY t.rs_rating DESC;
```

### B. The Whale Breakout Screen (FII Accumulation + Volume Spike)
```sql
SELECT c.symbol, c.company_name, t.cmp, t.vol_ratio, c.fii_change_qoq, c.promoter_pledged_pct
FROM CompanyMaster c
JOIN StockTechnicalsDaily t ON c.symbol = t.symbol
WHERE c.fii_change_qoq >= 1.0
  AND c.promoter_pledged_pct = 0.0
  AND t.vol_ratio >= 2.5
  AND t.cmp > t.ema20
ORDER BY t.vol_ratio DESC;
```

### C. The Undervalued Solvency Fortress Screen
```sql
SELECT c.symbol, c.company_name, c.pe_ratio, c.industry_pe, c.piotroski_score, c.free_cash_flow_cr
FROM CompanyMaster c
JOIN StockTechnicalsDaily t ON c.symbol = t.symbol
WHERE c.piotroski_score >= 8
  AND c.pe_ratio < c.industry_pe
  AND c.debt_to_equity <= 0.2
  AND c.free_cash_flow_cr > 0
ORDER BY c.piotroski_score DESC;
```
