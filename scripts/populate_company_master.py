#!/usr/bin/env python3
"""
populate_company_master.py
==========================
Strict Source-Only Ingestion Engine for CompanyMaster (64-column schema).
Adaptive Dynamic Multi-Threaded Ingestion Engine with Congestion Control.

ARCHITECTURAL PRINCIPLES:
─────────────────────────
- Only primary source data is stored (Yahoo Finance, Screener.in, NSE CSVs).
- Derived/calculated fields are strictly set to NULL here (pat_cr, operating_revenue_cr,
  net_debt_cr, fii_change_qoq, piotroski_score, industry_pe, median_pe_3y/5y, etc.).
- Source failure protection:
    * If Yahoo fails -> skip Yahoo UPDATE block, keep existing Yahoo data.
    * If Screener fails -> skip Screener UPDATE block, keep existing Screener data.
    * If field missing but source succeeded -> write NULL.
    * If both sources fail -> skip entirely, preserve DB row.
- Adaptive Dynamic Concurrency:
    * Starts with MAX_WORKERS parallel threads.
    * Automatically throttles down and pauses if rate-limiting or network backpressure is detected.
    * Thread-safe SQLite connections per thread with PRAGMA busy_timeout = 30000.
"""

import sys
import os
import re
import time
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import io
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT  = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROD_ROOT)

import yfinance as yf
from fundamental_fetcher import fetch_fundamentals, to_float

DB_PATH = os.path.join(PROD_ROOT, "Data", "stock_history.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.nseindia.com",
    "Accept":     "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Dynamic Congestion Controller ─────────────────────────────────────────────

class AdaptiveRateLimiter:
    """
    Adaptive dynamic rate controller.
    Tracks success vs. error/throttle events to dynamically regulate delay.
    """
    def __init__(self, initial_delay=0.1, min_delay=0.05, max_delay=3.0):
        self.delay = initial_delay
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.lock = threading.Lock()
        self.consecutive_success = 0

    def wait(self):
        with self.lock:
            cur_delay = self.delay
        if cur_delay > 0:
            time.sleep(cur_delay)

    def on_success(self):
        with self.lock:
            self.consecutive_success += 1
            if self.consecutive_success >= 15:
                # Speed up gradually if smooth sailing
                self.delay = max(self.min_delay, self.delay * 0.85)
                self.consecutive_success = 0

    def on_throttle(self, reason="Rate limit / slow response"):
        with self.lock:
            self.consecutive_success = 0
            # Back off dynamically
            self.delay = min(self.max_delay, max(0.5, self.delay * 2.0))
            print(f"  [BACKOFF] {reason} -> Throttling delay to {self.delay:.2f}s", flush=True)
        time.sleep(self.delay)

RATE_LIMITER = AdaptiveRateLimiter()

# ── NSE index constituent sets (fetched ONCE at startup) ─────────────────────

NSE_CSV_URLS = {
    "nifty50":  "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
}


def _fetch_nse_csv(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers=NSE_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8-sig", errors="replace")
        reader  = csv.DictReader(io.StringIO(raw))
        symbols = set()
        for row in reader:
            sym = (row.get("Symbol") or row.get("symbol") or "").strip().upper()
            if sym:
                symbols.add(sym)
        if not symbols:
            print(f"  [WARN] {url} returned 0 symbols — treating as fetch failure.")
            return {"symbols": set(), "available": False}
        return {"symbols": symbols, "available": True}
    except Exception as e:
        print(f"  [WARN] NSE fetch failed {url}: {e}")
        return {"symbols": set(), "available": False}


def fetch_nse_index_sets() -> dict:
    print("\nFetching NSE index constituent lists...")
    result = {}
    for key, url in NSE_CSV_URLS.items():
        info = _fetch_nse_csv(url)
        result[key] = info
        status = f"{len(info['symbols'])} constituents" if info["available"] else "FAILED"
        print(f"  {key}: {status}")
    return result


def nse_flag(sym: str, index_info: dict):
    if not index_info["available"]:
        return None
    return 1 if sym in index_info["symbols"] else 0


# ── HTML helpers ──────────────────────────────────────────────────────────────

def clean_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", str(s)).replace("&nbsp;", " ").replace("+", "").strip()


def fetch_screener_source_data(symbol: str) -> dict:
    sym_raw    = symbol.strip().upper()
    candidates = [sym_raw, sym_raw.replace("&", ""), sym_raw.replace("&", "").replace("-", "")]
    html       = ""
    for c in candidates:
        for url in [
            f"https://www.screener.in/company/{c}/consolidated/",
            f"https://www.screener.in/company/{c}/",
        ]:
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=6) as resp:
                    if resp.status == 200:
                        html = resp.read().decode("utf-8", errors="replace")
                        break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    RATE_LIMITER.on_throttle(f"Screener HTTP 429 on {symbol}")
                continue
            except Exception:
                continue
        if html:
            break
    if not html:
        return {}

    source = {}

    # 1. Compounded ranges table
    for tbl in re.findall(r'<table[^>]*class="ranges-table"[^>]*>(.*?)</table>', html, re.DOTALL):
        th_m  = re.search(r'<th[^>]*>(.*?)</th>', tbl)
        title = clean_html(th_m.group(1)) if th_m else ""
        for tr in re.findall(r'<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>', tbl):
            label = clean_html(tr[0])
            val   = to_float(clean_html(tr[1]))
            if "Sales Growth" in title and "3 Years" in label:
                source["sales_cagr_3y"]  = val
            elif "Profit Growth" in title and "3 Years" in label:
                source["profit_cagr_3y"] = val
            elif "EPS Growth" in title and "3 Years" in label:
                source["eps_growth_3y"]  = val

    # 2. Operating Ratios section
    m_rat = re.search(r'<section id="ratios".*?</section>', html, re.DOTALL)
    if m_rat:
        for tr in re.findall(r'<tr[^>]*>\s*<td[^>]*class="text"[^>]*>(.*?)</td>(.*?)</tr>', m_rat.group(0), re.DOTALL):
            c_title = clean_html(tr[0])
            vals    = re.findall(r'<td[^>]*>\s*([\d,\.\-]+%?)\s*</td>', tr[1])
            if vals:
                last_val = to_float(vals[-1])
                if "Debtor Days" in c_title:
                    source["debtor_days"] = int(last_val) if last_val is not None else None
                elif "Cash Conversion" in c_title:
                    source["cash_conversion_cycle"] = int(last_val) if last_val is not None else None

    # 3. Cash Flow section
    m_cf = re.search(r'<section id="cash-flow".*?</section>', html, re.DOTALL)
    if m_cf:
        for tr in re.findall(r'<tr[^>]*>\s*<td[^>]*class="text"[^>]*>(.*?)</td>(.*?)</tr>', m_cf.group(0), re.DOTALL):
            c_title = clean_html(tr[0])
            vals    = re.findall(r'<td[^>]*>\s*([\d,\.\-]+%?)\s*</td>', tr[1])
            if vals:
                last_val = to_float(vals[-1])
                if "Cash from Operating" in c_title:
                    source["cfo_cr"] = last_val
                elif "Free Cash Flow" in c_title:
                    source["free_cash_flow_cr"] = last_val

    # 4. Balance Sheet section
    m_bs = re.search(r'<section id="balance-sheet".*?</section>', html, re.DOTALL)
    if m_bs:
        for tr in re.findall(r'<tr[^>]*>\s*<td[^>]*class="text"[^>]*>(.*?)</td>(.*?)</tr>', m_bs.group(0), re.DOTALL):
            c_title = clean_html(tr[0])
            vals    = re.findall(r'<td[^>]*>\s*([\d,\.\-]+%?)\s*</td>', tr[1])
            if vals:
                last_val = to_float(vals[-1])
                if c_title in ("Total Assets", "Total assets"):
                    source["total_assets_cr"] = last_val
                elif c_title in ("Equity Capital", "Share Capital"):
                    source["equity_capital_cr"] = last_val

    return source


# ── Thread-Local Connection ───────────────────────────────────────────────────

_THREAD_LOCAL = threading.local()

def get_thread_connection() -> sqlite3.Connection:
    if not hasattr(_THREAD_LOCAL, "conn") or _THREAD_LOCAL.conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        _THREAD_LOCAL.conn = conn
    return _THREAD_LOCAL.conn


# ── Core processor ────────────────────────────────────────────────────────────

def process_single_stock_strict(
    sym: str,
    nifty50_info:  dict,
    nifty200_info: dict,
    nifty500_info: dict,
) -> bool:
    RATE_LIMITER.wait()
    conn = get_thread_connection()
    try:
        cur = conn.cursor()
        fetch_sym = "ETERNAL" if sym == "ZOMATO" else ("TMPV" if sym == "TATAMOTORS" else sym)

        # 1. Screener
        fund = {}
        screener_ok = False
        try:
            fund = fetch_fundamentals(fetch_sym, force_refresh=False)
            screener_ok = bool(fund and not fund.get("error"))
        except Exception as e:
            screener_ok = False

        if not screener_ok:
            fund = {}

        kr     = fund.get("key_ratios", {})
        sh     = fund.get("shareholding_pattern", {})
        bs     = fund.get("balance_sheet", {})
        q_perf = fund.get("quarterly_performance", [])

        sc_deep = fetch_screener_source_data(fetch_sym) if screener_ok else {}

        # 2. Yahoo Finance
        yinfo = {}
        t_start = time.time()
        try:
            yinfo = yf.Ticker(f"{sym}.NS").info or {}
            # If yfinance took unusually long (>5s), apply slight backoff
            if (time.time() - t_start) > 5.0:
                RATE_LIMITER.on_throttle(f"Yahoo slow response (>5s) on {sym}")
            else:
                RATE_LIMITER.on_success()
        except Exception as e:
            if "429" in str(e):
                RATE_LIMITER.on_throttle(f"Yahoo 429 on {sym}")

        yahoo_ok = bool(
            yinfo.get("marketCap") or
            yinfo.get("regularMarketPrice") or
            yinfo.get("currentPrice")
        )

        if not screener_ok and not yahoo_ok:
            return False

        # STEP 1 — Clear derived / model fields unconditionally
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

        # STEP 2 — Yahoo Finance UPDATE (only if Yahoo responded)
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
            beta = (
                round(yinfo["beta"], 3)
                if yinfo.get("beta") is not None else None
            )
            ttm_eps = yinfo.get("trailingEps")
            book_value_per_share_yahoo = yinfo.get("bookValue")

            cur.execute("""
                UPDATE CompanyMaster SET
                    company_name         = ?,
                    sector               = ?,
                    industry             = ?,
                    about                = ?,
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

        # STEP 3 — Screener UPDATE (only if Screener responded)
        if screener_ok:
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
                if kr.get("dividend_yield_pct") is not None:
                    cur.execute(
                        "UPDATE CompanyMaster SET dividend_yield_pct = ? WHERE symbol = ?",
                        (kr.get("dividend_yield_pct"), sym)
                    )

        # STEP 4 — NSE Index Memberships (COALESCE preservation)
        cur.execute("""
            UPDATE CompanyMaster SET
                is_nifty50  = COALESCE(?, is_nifty50),
                is_nifty200 = COALESCE(?, is_nifty200),
                is_nifty500 = COALESCE(?, is_nifty500)
            WHERE symbol = ?
        """, (
            nse_flag(sym, nifty50_info),
            nse_flag(sym, nifty200_info),
            nse_flag(sym, nifty500_info),
            sym,
        ))

        conn.commit()
        return True

    except Exception as e:
        conn.rollback()
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    MAX_WORKERS = 5  # Optimal parallel workers for 1 vCPU with I/O bound network requests

    print("=" * 65)
    print(f"CompanyMaster Ingestion — Adaptive Multi-Worker Engine ({MAX_WORKERS} Threads)")
    print("Strict Source-Only (v3.1) | Dynamic Congestion Control")
    print("=" * 65)

    nse           = fetch_nse_index_sets()
    nifty50_info  = nse["nifty50"]
    nifty200_info = nse["nifty200"]
    nifty500_info = nse["nifty500"]

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    cur.execute("""
        SELECT c.symbol, c.is_fno, s.isAlgoEligible
        FROM CompanyMaster c
        LEFT JOIN StockHistory s ON c.symbol = s.symbol
        ORDER BY c.is_fno DESC, s.isAlgoEligible DESC, c.symbol ASC
    """)
    rows  = cur.fetchall()
    conn.close()

    total = len(rows)
    print(f"\nTotal stocks to synchronize: {total} across {MAX_WORKERS} workers\n", flush=True)

    completed = 0
    success   = 0
    t0        = time.time()
    lock      = threading.Lock()

    def worker_task(sym):
        return sym, process_single_stock_strict(sym, nifty50_info, nifty200_info, nifty500_info)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(worker_task, r[0]): r[0] for r in rows}

        for future in as_completed(futures):
            sym, ok = future.result()
            with lock:
                completed += 1
                if ok:
                    success += 1

                if completed % 10 == 0 or completed == total:
                    elapsed = time.time() - t0
                    rate    = completed / elapsed if elapsed > 0 else 0
                    eta_min = (total - completed) / rate / 60 if rate > 0 else 0
                    print(
                        f"[{completed:>4}/{total}] ({completed/total*100:4.1f}%) "
                        f"| OK: {success:>4} "
                        f"| {rate:4.1f} stocks/s "
                        f"| ETA: {eta_min:4.1f} min "
                        f"| Last: {sym:<12}",
                        flush=True
                    )

    print("\n" + "=" * 65, flush=True)
    print(f"Ingestion Completed. {success}/{total} stocks synchronized.", flush=True)
    print("=" * 65, flush=True)


if __name__ == "__main__":
    main()
