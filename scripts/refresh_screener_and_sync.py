#!/usr/bin/env python3
"""
refresh_screener_and_sync.py — Automated Screener ingestion from Mac to VPS.

Why:
  screener.in blocks the VPS datacenter IP (Cloudflare 403), but resolves cleanly
  from residential/office IP (Mac). This script:
  1. Identifies symbols needing fresh quarterly results (stale > 120 days or board meeting passed).
  2. Scrapes them locally on Mac using fundamental_fetcher.py.
  3. Stream-syncs updated fundamental cache records to the VPS SQLite database over SSH.
  4. Triggers collectors results (mode=from_cache) on the VPS to validate and promote to CompanyFundamentalsHistory.

Usage:
  python scripts/refresh_screener_and_sync.py [--limit N] [--symbols SYM1,SYM2] [--dry-run]
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from typing import List, Set

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fundamental_fetcher

VPS_SSH = ["ssh", "-p", "20106", "root@37.187.159.65"]
VPS_ROOT = "/root/quantsync"


def get_due_symbols(explicit_symbols: str = "") -> List[str]:
    if explicit_symbols:
        return [s.strip().upper() for s in explicit_symbols.split(",") if s.strip()]

    # Query VPS for due symbols:
    # 1. Newest quarter older than 120 days
    # 2. Board meetings in last 7 days for results
    sql_cmd = """
cd /root/quantsync && ./venv/bin/python3 -c "
import sqlite3, json, datetime
today = datetime.date.today()
cutoff = (today - datetime.timedelta(days=120)).isoformat()
week_ago = (today - datetime.timedelta(days=7)).isoformat()

# 1. Check collectors.db dataset_status
c_db = sqlite3.connect('Data/collectors.db')
due_status = set(r[0] for r in c_db.execute(\\\"SELECT symbol FROM dataset_status WHERE dataset='results' AND state IN ('STALE','DEAD','MISSING')\\\").fetchall())

# 2. Check board meetings
bm_symbols = set(r[0] for r in c_db.execute(\\\"SELECT symbol FROM board_meetings WHERE is_results=1 AND meeting_date >= ?\\\", (week_ago,)).fetchall())

due = sorted(due_status | bm_symbols)
print(json.dumps(due))
"
"""
    try:
        proc = subprocess.run(VPS_SSH + [sql_cmd], capture_output=True, text=True, check=True)
        return json.loads(proc.stdout.strip())
    except Exception as e:
        sys.stderr.write(f"Failed to query due symbols from VPS: {e}\n")
        return []


def sync_records_to_vps(records: List[dict]) -> bool:
    """Stream records as JSON lines into VPS python script that updates fundamental_cache.db."""
    if not records:
        return True

    vps_receiver_code = """
import sys, sqlite3, json
conn = sqlite3.connect('/root/quantsync/Data/fundamental_cache.db')
count = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    item = json.loads(line)
    sym = item['symbol']
    data_json = item['data_json']
    updated_at = item.get('updated_at', '')
    conn.execute('''
        INSERT INTO stock_fundamentals (symbol, data_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            data_json = excluded.data_json,
            updated_at = excluded.updated_at
    ''', (sym, data_json, updated_at))
    count += 1
conn.commit()
conn.close()
print(f'Synced {count} records to VPS fundamental_cache.db')
"""
    cmd = VPS_SSH + [f"cd {VPS_ROOT} && ./venv/bin/python3 -c \"{vps_receiver_code}\""]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for r in records:
        proc.stdin.write(json.dumps(r) + "\n")
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        sys.stderr.write(f"Sync error: {stderr}\n")
        return False
    print(stdout.strip())
    return True


def trigger_vps_promotion(symbols: List[str]) -> bool:
    """Run results mode=from_cache on VPS for updated symbols."""
    if not symbols:
        return True
    sym_str = ",".join(symbols)
    cmd = VPS_SSH + [f"cd {VPS_ROOT} && ./venv/bin/python3 -m collectors.run results_from_cache symbols='{sym_str}' && ./venv/bin/python3 -m collectors.run status"]
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"VPS promotion failed: {e}\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="Refresh Screener cache on Mac and sync to VPS.")
    parser.add_argument("--limit", type=int, default=0, help="Max symbols to process.")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated symbols to process.")
    parser.add_argument("--dry-run", action="store_true", help="Only show due symbols.")
    args = parser.parse_args()

    due = get_due_symbols(args.symbols)
    print(f"Found {len(due)} symbols due for Screener refresh.")
    if args.limit > 0:
        due = due[:args.limit]
        print(f"Limiting to first {len(due)} symbols.")

    if args.dry_run:
        print("Dry run: due symbols are:", due[:20], "...")
        return

    updated_records = []
    processed_symbols = []

    print(f"Starting Screener scrape for {len(due)} symbols...")
    for i, sym in enumerate(due, 1):
        try:
            res = fundamental_fetcher.fetch_fundamentals(sym, force_refresh=True)
            qp = res.get("quarterly_performance", [])
            latest = qp[-1].get("quarter") if qp else None
            print(f"[{i:3d}/{len(due)}] {sym:12}: qp={len(qp):2} | latest={latest} | url={res.get('source_url')}")
            
            # Read from local SQLite
            local_conn = sqlite3_connect()
            row = local_conn.execute("SELECT symbol, data_json, updated_at FROM stock_fundamentals WHERE symbol=?", (sym,)).fetchone()
            local_conn.close()
            if row:
                updated_records.append({"symbol": row[0], "data_json": row[1], "updated_at": row[2]})
                processed_symbols.append(sym)
        except Exception as e:
            sys.stderr.write(f"Error fetching {sym}: {e}\n")

        time.sleep(1.0)  # Gentle rate limit for screener.in

        # Sync in batches of 50
        if len(updated_records) >= 50:
            sync_records_to_vps(updated_records)
            trigger_vps_promotion(processed_symbols)
            updated_records.clear()
            processed_symbols.clear()

    if updated_records:
        sync_records_to_vps(updated_records)
        trigger_vps_promotion(processed_symbols)

    print("Screener refresh and VPS sync completed successfully.")


def sqlite3_connect():
    import sqlite3
    return sqlite3.connect(os.path.join(ROOT, "Data", "fundamental_cache.db"))


if __name__ == "__main__":
    main()
