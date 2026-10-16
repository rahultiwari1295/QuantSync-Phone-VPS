#!/usr/bin/env python3
"""
migrate_schema_v2.py
====================
Idempotent DDL migration for QuantSync 7-Table Architecture.

Run ONCE on the VPS:
    python3 /root/quantsync/scripts/migrate_schema_v2.py

Operations (all idempotent — safe to re-run):
  1. StockHistory     → add isAlgoEligible (if missing)
  2. CompanyMaster    → CREATE TABLE IF NOT EXISTS (48 base cols)
  3. CompanyMaster    → ADD 16 new columns
  4. StockTechnicalsDaily → CREATE TABLE IF NOT EXISTS (17 base cols)
  5. StockTechnicalsDaily → ADD 21 new columns
  6. CompanyFundamentalsHistory → CREATE (35 cols, 3 indexes)
  7. CorporateEvents  → CREATE (15 cols, 4 indexes)
  8. SectorMaster     → CREATE static reference + seed 15 NSE sectors
  9. SectorMetricsDaily → CREATE time-series RRG (composite PK)
"""

import os
import sys
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT  = os.path.dirname(SCRIPT_DIR)
DB_PATH    = os.path.join(PROD_ROOT, "Data", "stock_history.db")


# ── helpers ──────────────────────────────────────────────────────────────────

def col_exists(cur: sqlite3.Cursor, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == col for row in cur.fetchall())


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def add_col(cur: sqlite3.Cursor, table: str, col: str, col_type: str):
    if not col_exists(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        print(f"  + {table}.{col}  [{col_type}]")
    else:
        print(f"  ✓ {table}.{col}  (exists)")


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")

    print("=" * 64)
    print("QuantSync DDL Migration — 7-Table Architecture (v2)")
    print(f"Database : {DB_PATH}")
    print("=" * 64)

    # ── 1. StockHistory — add isAlgoEligible ─────────────────────────────────
    print("\n[1/9]  StockHistory — isAlgoEligible")
    add_col(cur, "StockHistory", "isAlgoEligible", "INTEGER DEFAULT 0")

    # ── 2. CREATE CompanyMaster ───────────────────────────────────────────────
    print("\n[2/9]  CompanyMaster — CREATE TABLE IF NOT EXISTS")
    if not table_exists(cur, "CompanyMaster"):
        cur.execute("""
            CREATE TABLE CompanyMaster (
                -- Identity & Classification
                symbol                      TEXT PRIMARY KEY,
                company_name                TEXT,
                sector                      TEXT,
                industry                    TEXT,
                about                       TEXT,
                group_name                  TEXT,
                is_psu                      INTEGER,
                is_fno                      INTEGER DEFAULT 0,

                -- Valuation
                market_cap_cr               REAL,
                pe_ratio                    REAL,
                industry_pe                 REAL,
                median_pe_3y                REAL,
                median_pe_5y                REAL,
                pe_premium_discount_pct     REAL,
                peg_ratio                   REAL,
                pb_ratio                    REAL,
                ev_ebitda                   REAL,
                dividend_yield_pct          REAL,

                -- Compounding & Margins
                sales_cagr_3y               REAL,
                profit_cagr_3y              REAL,
                opm_pct                     REAL,
                roce_pct                    REAL,
                roe_pct                     REAL,

                -- Balance Sheet
                borrowings_cr               REAL,
                reserves_cr                 REAL,
                cash_cr                     REAL,
                net_debt_cr                 REAL,
                debt_to_equity              REAL,
                interest_coverage           REAL,

                -- Working Capital & Cash Flow
                debtor_days                 INTEGER,
                cash_conversion_cycle       INTEGER,
                cfo_cr                      REAL,
                free_cash_flow_cr           REAL,
                cfo_to_pat_ratio            REAL,

                -- Shareholding
                promoter_holding_pct        REAL,
                promoter_pledged_pct        REAL,
                fii_holding_pct             REAL,
                dii_holding_pct             REAL,
                fii_change_qoq              REAL,

                -- Legacy / Misc (kept for backward compatibility)
                delivery_pct_avg            REAL,
                acc_dist_grade              TEXT,
                beta                        REAL,
                piotroski_score             REAL,
                contingent_liabilities_cr   REAL,
                next_results_date           TEXT,
                mcap_tier                   TEXT,
                quarterly_history_json      TEXT,
                updated_at                  TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_sector   ON CompanyMaster(sector)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_industry ON CompanyMaster(industry)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_is_fno   ON CompanyMaster(is_fno)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_is_psu   ON CompanyMaster(is_psu)")
        print("  ✓ Created (48 base cols, 4 indexes)")
    else:
        print("  ✓ Already exists")

    # ── 3. CompanyMaster — ADD 16 new columns ────────────────────────────────
    print("\n[3/9]  CompanyMaster — ADD 16 new columns")
    for col, col_type in [
        # Piotroski inputs + daily valuation denominators
        ("face_value",              "REAL"),   # Screener: face value per share
        ("ttm_eps",                 "REAL"),   # Yahoo: trailingEps — daily P/E denominator
        ("book_value_per_share",    "REAL"),   # Screener: book value — daily P/B denominator
        ("total_assets_cr",         "REAL"),   # Screener: total assets — Piotroski ROA denom
        ("equity_capital_cr",       "REAL"),   # Screener: equity capital
        ("pat_cr",                  "REAL"),   # Screener: net profit TTM
        ("operating_revenue_cr",    "REAL"),   # Screener: sales TTM
        ("eps_growth_3y",           "REAL"),   # Screener: 3Y EPS growth CAGR
        # Flexible JSON stores (include "period" key inside the JSON)
        ("revenue_by_segment_json", "TEXT"),   # product / geography breakdown
        ("sector_kpis_json",        "TEXT"),   # NIM, GNPA, ARPOB etc.
        ("concall_summary",         "TEXT"),   # NULL until Phase 2 CompanyConcalls table
        # Index membership (V1 snapshot — not historically accurate for backtests)
        ("is_nifty50",              "INTEGER"),
        ("is_nifty200",             "INTEGER"),
        ("is_nifty500",             "INTEGER"),
        # Event calendar
        ("last_results_date",       "TEXT"),   # most recent board meeting / results date
        ("group_source",            "TEXT"),   # source that confirmed group_name
    ]:
        add_col(cur, "CompanyMaster", col, col_type)

    # ── 4. CREATE StockTechnicalsDaily ───────────────────────────────────────
    print("\n[4/9]  StockTechnicalsDaily — CREATE TABLE IF NOT EXISTS")
    if not table_exists(cur, "StockTechnicalsDaily"):
        cur.execute("""
            CREATE TABLE StockTechnicalsDaily (
                symbol              TEXT PRIMARY KEY,
                cmp                 REAL,
                change_1d_pct       REAL,
                change_1m_pct       REAL,
                ema20               REAL,
                ema50               REAL,
                ema200              REAL,
                rsi14               REAL,
                high_52w            REAL,
                low_52w             REAL,
                dist_52w_high_pct   REAL,
                vol_20d_avg         INTEGER,
                vol_ratio           REAL,
                rs_rating           INTEGER,
                stage               INTEGER,
                atr14               REAL,
                updated_at          TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tech_rs    ON StockTechnicalsDaily(rs_rating)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tech_rsi   ON StockTechnicalsDaily(rsi14)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tech_vol   ON StockTechnicalsDaily(vol_ratio)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tech_52w   ON StockTechnicalsDaily(dist_52w_high_pct)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tech_stage ON StockTechnicalsDaily(stage)")
        print("  ✓ Created (17 base cols, 5 indexes)")
    else:
        print("  ✓ Already exists")

    # ── 5. StockTechnicalsDaily — ADD 21 new columns ─────────────────────────
    print("\n[5/9]  StockTechnicalsDaily — ADD 21 new columns")
    for col, col_type in [
        # Trend direction (slope-based)
        ("sma200_slope",        "REAL"),   # +ve = upward slope (stage analysis)
        ("ema20_slope",         "REAL"),   # short-term momentum direction
        # Multi-period returns
        ("change_3m_pct",       "REAL"),
        ("change_6m_pct",       "REAL"),
        ("change_1y_pct",       "REAL"),
        # NSE Bhavcopy fields (populated at 6:30 PM from NSE official CSV)
        ("delivery_pct",        "REAL"),   # NSE official delivery %
        ("delivery_qty",        "INTEGER"),
        ("delivery_spike",      "INTEGER"),# 1 if delivery >2x 20d avg AND price >+1.5%
        ("upper_circuit",       "REAL"),   # NSE price band
        # Daily valuation (CMP / CompanyMaster quarterly fundamentals)
        ("pe_ttm",              "REAL"),   # CMP / ttm_eps
        ("pb",                  "REAL"),   # CMP / book_value_per_share
        ("market_cap_cr",       "REAL"),   # CMP × shares / 1e7
        # Pattern & stage intelligence
        ("acc_dist_grade",      "TEXT"),   # Marketsmith A+/A/B/C/D/E (13-week CLV)
        ("vcp_score",           "REAL"),   # VCP tightness (lower = more coiled)
        ("minervini_stage",     "TEXT"),   # 'Stage 2 Uptrend' / 'Stage 4 Decline' etc.
        ("near_52w_high",       "INTEGER"),# 1 if within 10% of 52-week high
        ("industry_rs_rank",    "INTEGER"),# RS rank within own sector (1 = best)
        # Additional indicators
        ("adx14",               "REAL"),   # ADX trend strength (>25 = trending)
        ("macd_hist",           "REAL"),   # MACD histogram (>0 = bullish)
        ("bb_width_pct",        "REAL"),   # Bollinger Band Width % (low = VCP squeeze)
        ("stoch_rsi",           "REAL"),   # Stochastic RSI 0–100
    ]:
        add_col(cur, "StockTechnicalsDaily", col, col_type)

    # ── 6. CREATE CompanyFundamentalsHistory ─────────────────────────────────
    print("\n[6/9]  CompanyFundamentalsHistory — CREATE TABLE IF NOT EXISTS")
    if not table_exists(cur, "CompanyFundamentalsHistory"):
        cur.execute("""
            CREATE TABLE CompanyFundamentalsHistory (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol                  TEXT NOT NULL,

                -- 4 time fields (all distinct purposes — mandatory for point-in-time research)
                quarter                 TEXT NOT NULL,   -- "Sep 2025" (human label)
                period_type             TEXT DEFAULT 'QUARTERLY',
                period_end_date         TEXT NOT NULL,   -- "2025-09-30" (accounting period end)
                reported_date           TEXT,            -- company officially published
                available_date          TEXT,            -- first date QuantSync could legally use
                snapshot_date           TEXT,            -- when QuantSync fetched & stored

                -- Source provenance (who said this number?)
                source                  TEXT,            -- "SCREENER_CONSOLIDATED" | "SCREENER_STANDALONE"
                source_url              TEXT,
                source_fetched_at       TEXT,

                -- Quarterly P&L (Source: Screener)
                revenue_cr              REAL,
                gross_profit_cr         REAL,   -- Piotroski: gross margin signal
                op_profit_cr            REAL,
                opm_pct                 REAL,
                net_profit_cr           REAL,   -- Piotroski: ROA numerator
                eps                     REAL,
                interest_expense_cr     REAL,

                -- Balance Sheet at period end (Piotroski underlying fields)
                total_assets_cr         REAL,   -- Piotroski: ROA & leverage denominator
                current_assets_cr       REAL,   -- Piotroski: current ratio
                current_liabilities_cr  REAL,   -- Piotroski: current ratio
                long_term_debt_cr       REAL,   -- Piotroski: leverage signal
                equity_capital_cr       REAL,
                reserves_cr             REAL,
                borrowings_cr           REAL,
                cash_cr                 REAL,
                shares_outstanding      REAL,   -- Piotroski: dilution check

                -- Cash Flow (Source: Screener)
                cfo_cr                  REAL,   -- Piotroski: CFO > 0 signal
                capex_cr                REAL,
                free_cash_flow_cr       REAL,

                -- Profitability Ratios (Source: Screener)
                roce_pct                REAL,
                roe_pct                 REAL,
                debt_to_equity          REAL,

                -- Shareholding (Source: Screener, as of this quarter)
                promoter_holding_pct    REAL,
                promoter_pledged_pct    REAL,
                fii_holding_pct         REAL,
                dii_holding_pct         REAL,
                public_holding_pct      REAL,

                UNIQUE(symbol, period_end_date, period_type)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cfh_symbol          ON CompanyFundamentalsHistory(symbol)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cfh_period_end_date ON CompanyFundamentalsHistory(period_end_date)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cfh_quarter         ON CompanyFundamentalsHistory(quarter)"
        )
        print("  ✓ Created (35 cols, 3 indexes, UNIQUE on symbol+period_end_date+period_type)")
    else:
        print("  ✓ Already exists")

    # ── 7. CREATE CorporateEvents ─────────────────────────────────────────────
    print("\n[7/9]  CorporateEvents — CREATE TABLE IF NOT EXISTS")
    if not table_exists(cur, "CorporateEvents"):
        cur.execute("""
            CREATE TABLE CorporateEvents (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                event_date      TEXT NOT NULL,
                event_type      TEXT NOT NULL,

                ex_date         TEXT,
                record_date     TEXT,

                -- Financial details
                amount          REAL,       -- dividend Rs / bonus ratio / deal price
                quantity        INTEGER,    -- shares involved
                deal_value_cr   REAL,       -- total deal in Crores

                -- Smart money (bulk/block deals)
                counterparty    TEXT,       -- "HDFC Mutual Fund" / "Dolly Khanna"
                buy_sell        TEXT,       -- "BUY" | "SELL"

                -- Source & deduplication (NSE's own ID is the primary dedup key)
                source_event_id TEXT,
                source          TEXT,       -- "NSE_BULK" | "NSE_CORP_ACTION" | "BSE_FILING"
                details_json    TEXT,       -- full raw event (preserve everything)
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(source, source_event_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ce_symbol       ON CorporateEvents(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ce_event_date   ON CorporateEvents(event_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ce_event_type   ON CorporateEvents(event_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ce_counterparty ON CorporateEvents(counterparty)")
        print("  ✓ Created (15 cols, UNIQUE on source+source_event_id, 4 indexes)")
    else:
        print("  ✓ Already exists")

    # ── 8. CREATE SectorMaster (static reference) ────────────────────────────
    print("\n[8/9]  SectorMaster — CREATE TABLE IF NOT EXISTS (static reference)")
    if not table_exists(cur, "SectorMaster"):
        cur.execute("""
            CREATE TABLE SectorMaster (
                sector_index        TEXT PRIMARY KEY,
                display_name        TEXT,
                nifty_symbol        TEXT,   -- Angel One index symbol for candle fetch
                constituent_count   INTEGER,
                updated_at          TEXT
            )
        """)
        SECTORS = [
            ("NIFTY IT",            "Information Technology",   "NIFTYIT"),
            ("NIFTY BANK",          "Banking",                  "BANKNIFTY"),
            ("NIFTY AUTO",          "Automobile",               "NIFTYAUTO"),
            ("NIFTY PHARMA",        "Pharmaceuticals",          "NIFTYPHARMA"),
            ("NIFTY FMCG",          "FMCG",                     "NIFTYFMCG"),
            ("NIFTY METAL",         "Metals & Mining",          "NIFTYMETAL"),
            ("NIFTY REALTY",        "Real Estate",              "NIFTYREALTY"),
            ("NIFTY ENERGY",        "Energy & Oil",             "NIFTYENERGY"),
            ("NIFTY INFRA",         "Infrastructure",           "NIFTYINFRA"),
            ("NIFTY FIN SERVICE",   "Financial Services",       "NIFTYFINSERVICE"),
            ("NIFTY MEDIA",         "Media",                    "NIFTYMEDIA"),
            ("NIFTY PSU BANK",      "PSU Banks",                "NIFTYPSUBANK"),
            ("NIFTY HEALTHCARE",    "Healthcare",               "NIFTYHEALTHCARE"),
            ("NIFTY CONSUMER DUR",  "Consumer Durables",        "NIFTYCONSUMERDUR"),
            ("NIFTY OIL GAS",       "Oil & Gas",                "NIFTYOILGAS"),
        ]
        cur.executemany(
            "INSERT OR IGNORE INTO SectorMaster(sector_index, display_name, nifty_symbol) VALUES (?,?,?)",
            SECTORS,
        )
        print(f"  ✓ Created and seeded with {len(SECTORS)} NSE sector indices")
    else:
        print("  ✓ Already exists")

    # ── 9. CREATE SectorMetricsDaily (time-series RRG) ───────────────────────
    print("\n[9/9]  SectorMetricsDaily — CREATE TABLE IF NOT EXISTS")
    if not table_exists(cur, "SectorMetricsDaily"):
        cur.execute("""
            CREATE TABLE SectorMetricsDaily (
                trade_date              TEXT NOT NULL,   -- "2026-09-21"
                sector_index            TEXT NOT NULL,   -- "NIFTY IT"

                -- Relative Rotation Graph (Julius de Kempenaer method)
                rs_ratio                REAL,   -- X-axis: sector RS vs Nifty 50 (>100 = outperforming)
                rs_momentum             REAL,   -- Y-axis: rate of change of RS-Ratio
                rrg_quadrant            TEXT,   -- "Leading"|"Weakening"|"Lagging"|"Improving"

                -- Sector Aggregated Fundamentals (computed from CompanyMaster each day)
                median_pe               REAL,   -- true sector industry_pe for this date
                median_pb               REAL,
                median_roce             REAL,
                median_rs_rating        REAL,

                -- Daily Price Action
                sector_change_1d_pct    REAL,
                sector_change_1m_pct    REAL,
                sector_change_3m_pct    REAL,

                -- Leaderboard JSON (for UI sector cards)
                top_stocks_json         TEXT,   -- top-5 RS-rated stocks in sector
                lagging_stocks_json     TEXT,   -- bottom-5 stocks

                updated_at              TEXT,

                PRIMARY KEY (trade_date, sector_index),
                FOREIGN KEY (sector_index) REFERENCES SectorMaster(sector_index)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_smd_trade_date   ON SectorMetricsDaily(trade_date)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_smd_sector_index ON SectorMetricsDaily(sector_index)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_smd_rrg_quadrant ON SectorMetricsDaily(rrg_quadrant)"
        )
        print("  ✓ Created (15 cols, composite PK (trade_date, sector_index), 3 indexes)")
    else:
        print("  ✓ Already exists")

    # ── Commit & verify ───────────────────────────────────────────────────────
    conn.commit()

    print("\n" + "=" * 64)
    print("MIGRATION COMPLETE — Verification")
    print("=" * 64)
    EXPECTED_TABLES = [
        "StockHistory",
        "CompanyMaster",
        "StockTechnicalsDaily",
        "CompanyFundamentalsHistory",
        "CorporateEvents",
        "SectorMaster",
        "SectorMetricsDaily",
    ]
    all_ok = True
    for t in EXPECTED_TABLES:
        if table_exists(cur, t):
            cur.execute(f"PRAGMA table_info({t})")
            ncols = len(cur.fetchall())
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            nrows = cur.fetchone()[0]
            print(f"  ✓  {t:<35}  {ncols:>3} columns  |  {nrows:>7} rows")
        else:
            print(f"  ✗  {t:<35}  MISSING!")
            all_ok = False

    conn.close()

    if all_ok:
        print("\n✓ All 7 tables present. Schema is frozen. Safe to populate data.")
    else:
        print("\n✗ Some tables are missing — check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
