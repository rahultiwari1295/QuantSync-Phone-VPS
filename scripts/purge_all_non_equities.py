#!/usr/bin/env python3
"""
scripts/purge_all_non_equities.py
==================================
Uses the official National Stock Exchange of India (NSE) ETF Security List
(https://archives.nseindia.com/content/equities/eq_etfseclist.csv) to permanently
purge all ETFs, Benchmark Indices, and Mock Test symbols from:
- stock_history.db (StockHistory, StockTechnicalsDaily, CompanyMaster)
- stock_dna_cache.db (stock_screener_rows, stock_dna_reports)
- Data/dna_charts/*.png
- Rebuilds fresh screener snapshot with 100% pure operating corporate equities.
"""

import os
import sys
import csv
import io
import glob
import sqlite3
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT = os.path.dirname(SCRIPT_DIR) if SCRIPT_DIR.endswith("scripts") else SCRIPT_DIR
DATA_DIR = os.path.join(PROD_ROOT, "Data")
HIST_DB = os.path.join(DATA_DIR, "stock_history.db")
CACHE_DB = os.path.join(DATA_DIR, "stock_dna_cache.db")
CHARTS_DIR = os.path.join(DATA_DIR, "dna_charts")

# 1. Fetch official NSE ETF list
print("[*] Downloading official NSE ETF Master Security List...")
url = "https://archives.nseindia.com/content/equities/eq_etfseclist.csv"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=10) as resp:
    content = resp.read().decode("utf-8", errors="ignore")

reader = csv.reader(io.StringIO(content))
next(reader)
nse_etf_symbols = set(row[0].strip().upper() for row in reader if row and row[0])
print(f"  • Loaded {len(nse_etf_symbols)} official NSE ETFs")

# 2. Add Benchmark Indices & Mock Test Symbols
BENCHMARK_INDICES = {
    "NIFTY", "BANKNIFTY", "NIFTY100", "NIFTY500", "NIFTYMIDCAP50", 
    "NIFTYMIDCAP100", "NIFTYNXT50", "NIFTYIT", "INDIAVIX", "SENSEX"
}

# 3. Find all matches in local databases
conn_h = sqlite3.connect(HIST_DB)
cur_h = conn_h.cursor()
cur_h.execute("SELECT symbol FROM StockHistory")
sh_syms = set(r[0].upper() for r in cur_h.fetchall())

conn_c = sqlite3.connect(CACHE_DB)
cur_c = conn_c.cursor()
cur_c.execute("SELECT DISTINCT symbol FROM stock_screener_rows")
scr_syms = set(r[0].upper() for r in cur_c.fetchall())

all_db_syms = sh_syms.union(scr_syms)

# Known legitimate corporate equities to NEVER delete
WHITELIST = {
    "JETFREIGHT", "GOLDIAM", "SILVERTUC", "SILVEROAK", "GOLDKRISH", "GOLDKART", 
    "GOLDENTOBC", "BETAFASPH", "BETAPHARMA", "FIRSTCRY", "GROWW", "BALPHARMA", 
    "CHEMBOND", "EUROBOND", "SKYGOLD", "SHANTIGOLD", "DECNGOLD", "GINNIFILA"
}

non_equities_to_purge = set()
for s in all_db_syms:
    if s in WHITELIST:
        continue
    if s in nse_etf_symbols or s in BENCHMARK_INDICES or s.endswith("NSETEST") or s.startswith("011NSE"):
        non_equities_to_purge.add(s)

print(f"[*] Total non-equity symbols identified to purge: {len(non_equities_to_purge)}")

# 4. Purge from stock_history.db
print("[*] Purging stock_history.db...")
tables_h = [
    "StockHistory", "StockTechnicalsDaily", "CompanyMaster", "CompanyFundamentalsHistory",
    "daily_v2_stock_state", "daily_v2_buy_live", "daily_v2_active_live", "daily_v2_closed_live",
    "daily_v2_terminal_summary_live", "symbol_metadata", "CorporateEvents"
]
for tbl in tables_h:
    try:
        cur_h.execute(f"DELETE FROM {tbl} WHERE symbol IN ({','.join(['?']*len(non_equities_to_purge))})", list(non_equities_to_purge))
        print(f"  • {tbl}: Cleaned")
    except Exception as e:
        pass

conn_h.commit()
print("[*] Vacuuming stock_history.db...")
conn_h.execute("VACUUM")
conn_h.close()

# 5. Purge from stock_dna_cache.db
print("[*] Purging stock_dna_cache.db...")
tables_c = ["stock_screener_rows", "stock_dna_reports"]
for tbl in tables_c:
    try:
        cur_c.execute(f"DELETE FROM {tbl} WHERE symbol IN ({','.join(['?']*len(non_equities_to_purge))})", list(non_equities_to_purge))
        print(f"  • {tbl}: Cleaned")
    except Exception as e:
        pass

conn_c.commit()
print("[*] Vacuuming stock_dna_cache.db...")
conn_c.execute("VACUUM")
conn_c.close()

# 6. Purge chart PNG images
print("[*] Purging chart image files...")
deleted_charts = 0
for sym in non_equities_to_purge:
    for img in glob.glob(os.path.join(CHARTS_DIR, f"{sym}*.png")):
        try:
            os.remove(img)
            deleted_charts += 1
        except Exception:
            pass
print(f"  • Removed {deleted_charts} chart images from {CHARTS_DIR}")

# 7. Rebuild clean screener snapshot
print("[*] Rebuilding clean screener snapshot...")
sys.path.append(PROD_ROOT)
import build_screener_snapshot
build_screener_snapshot.build_all()

# 8. Final verification
conn_check = sqlite3.connect(HIST_DB)
cur_check = conn_check.cursor()
cur_check.execute("SELECT count(*) FROM StockHistory")
rem_sh = cur_check.fetchone()[0]
conn_check.close()

conn_c_check = sqlite3.connect(CACHE_DB)
cur_c_check = conn_c_check.cursor()
cur_c_check.execute("SELECT count(*) FROM stock_screener_rows WHERE scan_date = (SELECT max(scan_date) FROM stock_screener_rows)")
rem_scr = cur_c_check.fetchone()[0]
conn_c_check.close()

print("\n=======================================================")
print(f"[✓] PURGE COMPLETE!")
print(f"  • Remaining Corporate Equities in StockHistory: {rem_sh}")
print(f"  • Remaining Corporate Equities in Screener:     {rem_scr}")
print("=======================================================")
