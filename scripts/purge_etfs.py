#!/usr/bin/env python3
"""
scripts/purge_etfs.py
======================
Completely removes all 120 ETF tickers from QuantSync databases:
- stock_history.db (StockHistory, StockTechnicalsDaily, CompanyMaster)
- stock_dna_cache.db (stock_screener_rows, stock_dna_reports)
- Data/dna_charts/*.png
- Compacts databases via VACUUM
- Rebuilds fresh screener snapshot
"""

import os
import sys
import shutil
import sqlite3
import glob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT = os.path.dirname(SCRIPT_DIR) if SCRIPT_DIR.endswith("scripts") else SCRIPT_DIR
DATA_DIR = os.path.join(PROD_ROOT, "Data")
HIST_DB = os.path.join(DATA_DIR, "stock_history.db")
CACHE_DB = os.path.join(DATA_DIR, "stock_dna_cache.db")
CHARTS_DIR = os.path.join(DATA_DIR, "dna_charts")

sys.path.append(os.path.join(PROD_ROOT, "scripts"))
try:
    import list_etfs
    etf_symbols = set()
    for cat, items in list_etfs.etfs_by_cat.items():
        for it in items:
            etf_symbols.add(it['symbol'])
except Exception:
    # Fallback to direct query if module import fails
    conn = sqlite3.connect(HIST_DB)
    cur = conn.cursor()
    cur.execute("SELECT symbol FROM StockHistory WHERE symbol LIKE '%BEES%' OR symbol LIKE '%ETF%'")
    etf_symbols = set(r[0] for r in cur.fetchall())
    conn.close()

print(f"[*] Target ETF count to purge: {len(etf_symbols)}")

# 1. Backups
print("[*] Creating pre-purge backups...")
shutil.copy2(HIST_DB, HIST_DB + ".bak_pre_etf_purge")
shutil.copy2(CACHE_DB, CACHE_DB + ".bak_pre_etf_purge")

# 2. Purge stock_history.db
print("[*] Purging stock_history.db...")
conn_h = sqlite3.connect(HIST_DB)
cur_h = conn_h.cursor()

tables_to_clean = ["StockHistory", "StockTechnicalsDaily", "CompanyMaster", "daily_v2_stock_state", "symbol_metadata"]
for tbl in tables_to_clean:
    try:
        cur_h.execute(f"SELECT count(*) FROM {tbl} WHERE symbol IN ({','.join(['?']*len(etf_symbols))})", list(etf_symbols))
        count = cur_h.fetchone()[0]
        cur_h.execute(f"DELETE FROM {tbl} WHERE symbol IN ({','.join(['?']*len(etf_symbols))})", list(etf_symbols))
        print(f"  • {tbl}: Deleted {count} ETF records")
    except sqlite3.OperationalError:
        pass

conn_h.commit()
print("[*] Running VACUUM on stock_history.db...")
conn_h.execute("VACUUM")
conn_h.close()

# 3. Purge stock_dna_cache.db
print("[*] Purging stock_dna_cache.db...")
conn_c = sqlite3.connect(CACHE_DB)
cur_c = conn_c.cursor()

cache_tables = ["stock_screener_rows", "stock_dna_reports"]
for tbl in cache_tables:
    try:
        cur_c.execute(f"SELECT count(*) FROM {tbl} WHERE symbol IN ({','.join(['?']*len(etf_symbols))})", list(etf_symbols))
        count = cur_c.fetchone()[0]
        cur_c.execute(f"DELETE FROM {tbl} WHERE symbol IN ({','.join(['?']*len(etf_symbols))})", list(etf_symbols))
        print(f"  • {tbl}: Deleted {count} ETF records")
    except sqlite3.OperationalError:
        pass

conn_c.commit()
print("[*] Running VACUUM on stock_dna_cache.db...")
conn_c.execute("VACUUM")
conn_c.close()

# 4. Purge pre-generated chart images
print("[*] Removing ETF chart PNG images...")
chart_deleted = 0
for sym in etf_symbols:
    for img_pattern in [f"{sym}.png", f"{sym}_*.png"]:
        matches = glob.glob(os.path.join(CHARTS_DIR, img_pattern))
        for m in matches:
            try:
                os.remove(m)
                chart_deleted += 1
            except Exception:
                pass
print(f"  • Removed {chart_deleted} ETF chart images from {CHARTS_DIR}")

# 5. Rebuild screener snapshot
print("[*] Rebuilding clean screener snapshot...")
sys.path.append(PROD_ROOT)
import build_screener_snapshot
build_screener_snapshot.build_all()

# 6. Verification
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
