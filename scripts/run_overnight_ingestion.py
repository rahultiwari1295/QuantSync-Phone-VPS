#!/usr/bin/env python3
"""
run_overnight_ingestion.py
==========================
Overnight Autonomous Ingestion Orchestrator for CompanyMaster.
- Runs completely detached on the VPS (survives SSH disconnect / laptop shutdown).
- Implements two multi-pass rounds:
    Pass 1: Complete sweep over all 2,367 NSE equities + benchmark indices.
            Gentle pacing with dynamic backoff (1.2s delay per stock) to stay under Screener / Yahoo limits.
    Pass 2: Targeted Retry Sweep for any stock where fundamental data was skipped or rate-limited.
- Self-contained logging to /root/quantsync/Data/overnight_ingestion.log.
"""

import sys
import os
import time
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import io
import csv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT  = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROD_ROOT)

import yfinance as yf
from fundamental_fetcher import fetch_fundamentals, to_float
from scripts.populate_company_master import (
    fetch_nse_index_sets,
    fetch_screener_source_data,
    clean_html
)

DB_PATH  = os.path.join(PROD_ROOT, "Data", "stock_history.db")
LOG_PATH = os.path.join(PROD_ROOT, "Data", "overnight_ingestion.log")

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def process_single_stock(conn, sym, nifty50, nifty200, nifty500):
    """
    Ingests Yahoo and Screener fundamentals strictly into CompanyMaster.
    """
    cur = conn.cursor()

    # 1. Fetch Yahoo
    yinfo = {}
    yahoo_ok = False
    try:
        t = yf.Ticker(f"{sym}.NS")
        info = t.info
        if info and (info.get("symbol") or info.get("shortName") or info.get("marketCap")):
            yinfo = info
            yahoo_ok = True
    except Exception:
        yahoo_ok = False

    # 2. Fetch Screener
    screener_ok = False
    fund = {}
    sc_deep = {}
    try:
        fund = fetch_fundamentals(sym, max_cache_age_days=1)
        if fund and not fund.get("error"):
            screener_ok = True
            sc_deep = fetch_screener_source_data(sym)
    except Exception:
        screener_ok = False

    if not yahoo_ok and not screener_ok:
        return False, "BOTH_FAILED"

    # Step 1: Derived fields cleared to NULL
    cur.execute("""
        UPDATE CompanyMaster SET
            industry_pe               = NULL,
            median_pe_3y              = NULL,
            median_pe_5y              = NULL,
            pe_premium_discount_pct   = NULL,
            net_debt_cr               = NULL,
            interest_coverage         = NULL,
            cfo_to_pat_ratio          = NULL,
            fii_change_qoq            = NULL,
            delivery_pct_avg          = NULL,
            acc_dist_grade            = NULL,
            piotroski_score           = NULL,
            contingent_liabilities_cr = NULL,
            next_results_date         = NULL,
            pat_cr                    = NULL,
            operating_revenue_cr      = NULL,
            revenue_by_segment_json   = NULL,
            sector_kpis_json          = NULL,
            concall_summary           = NULL,
            group_name                = NULL,
            group_source              = NULL,
            is_psu                    = NULL,
            mcap_tier                 = NULL,
            updated_at                = date('now')
        WHERE symbol = ?
    """, (sym,))

    # Step 2: Yahoo Update
    if yahoo_ok:
        company_name = yinfo.get("longName") or yinfo.get("shortName")
        sector       = yinfo.get("sector")
        industry     = yinfo.get("industry")
        about        = yinfo.get("longBusinessSummary")

        mcap_cr_yahoo = (
            round(yinfo["marketCap"] / 1e7, 2)
            if yinfo.get("marketCap") is not None else None
        )
        pe_ratio_yahoo = yinfo.get("trailingPE")
        pb_ratio  = yinfo.get("priceToBook")
        peg_ratio = yinfo.get("pegRatio")
        ev_ebitda = (
            round(yinfo["enterpriseToEbitda"], 2)
            if yinfo.get("enterpriseToEbitda") is not None else None
        )
        div_yield_yahoo = (
            round(yinfo["dividendYield"] * 100, 2)
            if yinfo.get("dividendYield") is not None else None
        )
        opm_pct = (
            round(yinfo["operatingMargins"] * 100, 2)
            if yinfo.get("operatingMargins") is not None else None
        )
        cash_cr = (
            round(yinfo["totalCash"] / 1e7, 2)
            if yinfo.get("totalCash") is not None else None
        )
        beta = round(yinfo["beta"], 2) if yinfo.get("beta") is not None else None
        ttm_eps = yinfo.get("trailingEps")
        book_value_per_share_yahoo = yinfo.get("bookValue")

        cur.execute("""
            UPDATE CompanyMaster SET
                company_name         = COALESCE(?, company_name),
                sector               = COALESCE(?, sector),
                industry             = COALESCE(?, industry),
                about                = COALESCE(?, about),
                market_cap_cr        = ?,
                pe_ratio             = ?,
                pb_ratio             = ?,
                peg_ratio            = ?,
                ev_ebitda            = ?,
                dividend_yield_pct   = ?,
                opm_pct              = ?,
                cash_cr              = ?,
                beta                 = ?,
                ttm_eps              = ?,
                book_value_per_share = ?
            WHERE symbol = ?
        """, (
            company_name, sector, industry, about,
            mcap_cr_yahoo, pe_ratio_yahoo,
            pb_ratio, peg_ratio, ev_ebitda, div_yield_yahoo,
            opm_pct, cash_cr, beta, ttm_eps,
            book_value_per_share_yahoo,
            sym,
        ))

    # Step 3: Screener Update
    if screener_ok:
        kr = fund.get("key_ratios", {})
        bs = fund.get("balance_sheet", {})
        sh = fund.get("shareholding_pattern", {})
        q_perf = fund.get("quarterly_performance", [])

        book_value_per_share = kr.get("book_value")
        debt_to_equity       = kr.get("debt_to_equity")
        borrowings_cr        = to_float(bs.get("Borrowings"))
        reserves_cr          = to_float(bs.get("Reserves"))
        total_assets_cr      = sc_deep.get("total_assets_cr")
        equity_capital_cr    = sc_deep.get("equity_capital_cr")

        sales_cagr_3y     = sc_deep.get("sales_cagr_3y")
        profit_cagr_3y    = sc_deep.get("profit_cagr_3y")
        eps_growth_3y     = sc_deep.get("eps_growth_3y")
        roce_pct          = kr.get("roce_pct")
        roe_pct           = kr.get("roe_pct")
        face_value        = kr.get("face_value")

        debtor_days           = sc_deep.get("debtor_days")
        cash_conversion_cycle = sc_deep.get("cash_conversion_cycle")
        cfo_cr                = sc_deep.get("cfo_cr")
        free_cash_flow_cr     = sc_deep.get("free_cash_flow_cr")

        promoter_holding_pct  = sh.get("Promoters", {}).get("holding_pct")
        promoter_pledged_pct  = sh.get("Pledged percentage", {}).get("holding_pct")
        fii_holding_pct       = sh.get("FIIs", {}).get("holding_pct")
        dii_holding_pct       = sh.get("DIIs", {}).get("holding_pct")

        quarterly_history_json = json.dumps(q_perf) if q_perf else None
        last_results_date      = q_perf[-1].get("quarter") if q_perf else None

        screener_params = [
            sales_cagr_3y, profit_cagr_3y, eps_growth_3y,
            roce_pct, roe_pct,
            debt_to_equity,
            borrowings_cr, reserves_cr, total_assets_cr, equity_capital_cr,
            debtor_days, cash_conversion_cycle,
            cfo_cr, free_cash_flow_cr,
            face_value,
            promoter_holding_pct, promoter_pledged_pct,
            fii_holding_pct, dii_holding_pct,
            quarterly_history_json,
            last_results_date,
            sym,
        ]
        cur.execute("""
            UPDATE CompanyMaster SET
                sales_cagr_3y             = ?,
                profit_cagr_3y            = ?,
                eps_growth_3y             = ?,
                roce_pct                  = ?,
                roe_pct                   = ?,
                debt_to_equity            = ?,
                borrowings_cr             = ?,
                reserves_cr               = ?,
                total_assets_cr           = ?,
                equity_capital_cr         = ?,
                debtor_days               = ?,
                cash_conversion_cycle     = ?,
                cfo_cr                    = ?,
                free_cash_flow_cr         = ?,
                face_value                = ?,
                promoter_holding_pct      = ?,
                promoter_pledged_pct      = ?,
                fii_holding_pct           = ?,
                dii_holding_pct           = ?,
                quarterly_history_json    = ?,
                last_results_date         = ?
            WHERE symbol = ?
        """, screener_params)

        if book_value_per_share is not None:
            cur.execute(
                "UPDATE CompanyMaster SET book_value_per_share = ? WHERE symbol = ?",
                (book_value_per_share, sym)
            )

        if not yahoo_ok:
            if fund.get("market_cap_cr") is not None:
                cur.execute(
                    "UPDATE CompanyMaster SET market_cap_cr = ? WHERE symbol = ?",
                    (fund.get("market_cap_cr"), sym)
                )
            if kr.get("stock_pe") is not None:
                cur.execute(
                    "UPDATE CompanyMaster SET pe_ratio = ? WHERE symbol = ?",
                    (kr.get("stock_pe"), sym)
                )

    # Step 4: Index membership
    cur.execute("""
        UPDATE CompanyMaster SET
            is_nifty50  = ?,
            is_nifty200 = ?,
            is_nifty500 = ?
        WHERE symbol = ?
    """, (
        1 if sym in nifty50 else 0,
        1 if sym in nifty200 else 0,
        1 if sym in nifty500 else 0,
        sym
    ))

    conn.commit()
    return True, f"Y:{1 if yahoo_ok else 0}_S:{1 if screener_ok else 0}"


def run_ingestion_pass(conn, symbols, nifty50, nifty200, nifty500, pass_name="PASS 1", delay=1.2):
    log(f"Starting {pass_name}: {len(symbols)} symbols with {delay}s throttle delay...")
    total = len(symbols)
    ok_count = 0
    t0 = time.time()

    for idx, sym in enumerate(symbols, 1):
        try:
            ok, status = process_single_stock(conn, sym, nifty50, nifty200, nifty500)
            if ok:
                ok_count += 1
        except Exception as e:
            status = f"ERR: {str(e)[:30]}"

        if idx % 25 == 0 or idx == total:
            elapsed = time.time() - t0
            rate = idx / elapsed if elapsed > 0 else 0
            eta_min = (total - idx) / rate / 60 if rate > 0 else 0
            log(f"[{pass_name}] {idx:4d}/{total} ({idx/total*100:4.1f}%) | Success: {ok_count:4d} | Rate: {rate:3.1f} s/s | ETA: {eta_min:4.1f}m | {sym:<10} -> {status}")

        time.sleep(delay)

    log(f"Finished {pass_name}: {ok_count}/{total} updated successfully.")
    return ok_count


def main():
    log("=" * 70)
    log("QuantSync Overnight Ingestion Engine Starting")
    log("=" * 70)

    # 1. Download official NSE Index constituent files
    nse = fetch_nse_index_sets()
    nifty50  = nse.get("nifty50", {}).get("symbols", set())
    nifty200 = nse.get("nifty200", {}).get("symbols", set())
    nifty500 = nse.get("nifty500", {}).get("symbols", set())
    log(f"Loaded NSE indices: Nifty50={len(nifty50)}, Nifty200={len(nifty200)}, Nifty500={len(nifty500)}")

    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")

    cur = conn.cursor()
    cur.execute("""
        SELECT c.symbol
        FROM CompanyMaster c
        LEFT JOIN StockHistory s ON c.symbol = s.symbol
        ORDER BY c.is_fno DESC, s.isAlgoEligible DESC, s.candleCount DESC, c.symbol ASC
    """)
    all_symbols = [r[0] for r in cur.fetchall()]
    log(f"Total symbols in CompanyMaster queue: {len(all_symbols)}")

    # PASS 1: Full sequential gentle crawl (delay = 1.2s -> ~50 mins for 2367 stocks)
    run_ingestion_pass(conn, all_symbols, nifty50, nifty200, nifty500, pass_name="PASS 1 (FULL)", delay=1.2)

    # Check missing Screener / Yahoo data
    cur.execute("""
        SELECT symbol FROM CompanyMaster
        WHERE sales_cagr_3y IS NULL OR market_cap_cr IS NULL
    """)
    retry_symbols = [r[0] for r in cur.fetchall()]
    log(f"Symbols needing retry for missing fields: {len(retry_symbols)}")

    # PASS 2: Retry Sweep for missing data after a short rest (delay = 2.0s)
    if retry_symbols:
        log("Resting for 60 seconds before PASS 2 retry sweep...")
        time.sleep(60)
        run_ingestion_pass(conn, retry_symbols, nifty50, nifty200, nifty500, pass_name="PASS 2 (RETRY)", delay=2.0)

    # Final summary
    cur.execute("SELECT count(*) FROM CompanyMaster")
    total_cnt = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM CompanyMaster WHERE market_cap_cr IS NOT NULL")
    mcap_cnt = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM CompanyMaster WHERE pe_ratio IS NOT NULL")
    pe_cnt = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM CompanyMaster WHERE sales_cagr_3y IS NOT NULL")
    sales_cnt = cur.fetchone()[0]

    log("=" * 70)
    log(f"OVERNIGHT INGESTION COMPLETE")
    log(f"Total Universe : {total_cnt}")
    log(f"Market Cap Populated: {mcap_cnt} ({mcap_cnt/total_cnt*100:.1f}%)")
    log(f"P/E Ratio Populated : {pe_cnt} ({pe_cnt/total_cnt*100:.1f}%)")
    log(f"Sales 3Y Populated  : {sales_cnt} ({sales_cnt/total_cnt*100:.1f}%)")
    log("=" * 70)

    conn.close()

if __name__ == "__main__":
    main()
