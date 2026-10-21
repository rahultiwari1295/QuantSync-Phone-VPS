#!/usr/bin/env python3
"""
trendlyne_collector.py — Trendlyne Market Intelligence Database & Ingestion Engine
Collects Sector/Industry/Index Day & Week performance and Market Insights into SQLite.
"""

import sys, os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for _sub in ("common", "core", "api", "pipelines", "engines", "collectors", "intel"):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


import os
import sys
import json
import sqlite3
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
import urllib.request
import urllib.error

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TrendlyneCollector")

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Determine DB path (supports both VPS and local repo layouts)
DEFAULT_DB_DIR = os.path.join(SCRIPT_DIR, "Data")
if not os.path.exists(DEFAULT_DB_DIR):
    DEFAULT_DB_DIR = os.path.join(SCRIPT_DIR, "data")
if not os.path.exists(DEFAULT_DB_DIR):
    os.makedirs(DEFAULT_DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DEFAULT_DB_DIR, "market_intel.db")

ENDPOINTS = {
    "day_change": "https://trendlyne.com/equity/sector-industry-analysis/overall/day-changeP/",
    "week_change": "https://trendlyne.com/equity/sector-industry-analysis/overall/week-changeP/",
    "market_insight": "https://trendlyne.com/equity/api/market-insight/?rangeType=last-week&stockGroup=All"
}

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://trendlyne.com/",
}


def init_db(db_path: str = DB_PATH):
    """Creates tables for performance metrics, news/insights, and snapshots."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        
        # Performance Table: Sectors, Industries, and Indices (Day / Week)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trendlyne_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL,          -- 'day' or 'week'
                category TEXT NOT NULL,        -- 'sector', 'industry', 'index'
                item_id INTEGER,
                name TEXT NOT NULL,
                disp_value TEXT,
                change_pct REAL,
                color TEXT,
                cell_url TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(period, category, name, fetched_at)
            )
        """)
        
        # Latest Performance Snapshot (fast lookup for current state)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trendlyne_current_performance (
                period TEXT NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                item_id INTEGER,
                disp_value TEXT,
                change_pct REAL,
                color TEXT,
                cell_url TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (period, category, name)
            )
        """)

        # Market Insights / News / Catalysts Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trendlyne_market_insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stock_code TEXT,
                stock_name TEXT,
                notification TEXT NOT NULL,
                time_stamp TEXT NOT NULL,
                label TEXT,
                n_color TEXT,
                screen_redirect_url TEXT,
                complete_url TEXT,
                nse_code TEXT,
                bse_code TEXT,
                isin TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(notification, time_stamp)
            )
        """)

        # Raw Ingestion Logs & Snapshots
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trendlyne_fetch_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_name TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                status TEXT NOT NULL,
                records_count INTEGER DEFAULT 0,
                error_message TEXT
            )
        """)

        # Create indexes for fast querying
        cur.execute("CREATE INDEX IF NOT EXISTS idx_perf_lookup ON trendlyne_performance (period, category, fetched_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_insights_stock ON trendlyne_market_insights (stock_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_insights_time ON trendlyne_market_insights (time_stamp)")
        conn.commit()
    logger.info(f"Initialized Trendlyne SQLite DB schema at: {db_path}")


def fetch_json(url: str, timeout: int = 15) -> Dict[str, Any]:
    """Fetches JSON from URL with browser headers."""
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} received from {url}")
        content = resp.read().decode("utf-8")
        return json.loads(content)


def store_performance(data: Dict[str, Any], period: str, db_path: str = DB_PATH) -> int:
    """Stores sector, industry, and index data for a given period ('day' or 'week')."""
    now_iso = datetime.utcnow().isoformat()
    total_stored = 0
    body = data.get("body", {})

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for category in ["sector", "industry", "index"]:
            items = body.get(category, {}).get("chart", [])
            for it in items:
                name = it.get("name")
                if not name:
                    continue
                item_id = it.get("id")
                disp_val = it.get("disp_value")
                change_pct = it.get("param_value")
                color = it.get("color")
                cell_url = it.get("cell_url")

                # Insert into history table
                cur.execute("""
                    INSERT OR IGNORE INTO trendlyne_performance 
                    (period, category, item_id, name, disp_value, change_pct, color, cell_url, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (period, category, item_id, name, disp_val, change_pct, color, cell_url, now_iso))

                # Upsert into current state table
                cur.execute("""
                    INSERT INTO trendlyne_current_performance 
                    (period, category, name, item_id, disp_value, change_pct, color, cell_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(period, category, name) DO UPDATE SET
                        item_id = excluded.item_id,
                        disp_value = excluded.disp_value,
                        change_pct = excluded.change_pct,
                        color = excluded.color,
                        cell_url = excluded.cell_url,
                        updated_at = excluded.updated_at
                """, (period, category, name, item_id, disp_val, change_pct, color, cell_url, now_iso))

                total_stored += 1

        # Log fetch
        cur.execute("""
            INSERT INTO trendlyne_fetch_logs (feed_name, fetched_at, status, records_count)
            VALUES (?, ?, ?, ?)
        """, (f"{period}_change", now_iso, "SUCCESS", total_stored))
        conn.commit()

    logger.info(f"Stored {total_stored} {period} performance records (sectors/industries/indices)")
    return total_stored


def store_market_insights(data: Dict[str, Any], db_path: str = DB_PATH) -> int:
    """Stores news / market insights items into DB with deduplication."""
    now_iso = datetime.utcnow().isoformat()
    insights = data.get("body", {}).get("marketInsights", [])
    stored_count = 0

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for item in insights:
            notification = item.get("notification", "").strip()
            time_stamp = item.get("timeStamp", "").strip()
            if not notification:
                continue

            cur.execute("""
                INSERT OR IGNORE INTO trendlyne_market_insights (
                    stock_code, stock_name, notification, time_stamp,
                    label, n_color, screen_redirect_url, complete_url,
                    nse_code, bse_code, isin, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("stockCode"),
                item.get("stockName"),
                notification,
                time_stamp,
                item.get("label"),
                item.get("nColor"),
                item.get("screenRedirectUrl"),
                item.get("completeUrl"),
                item.get("NSEcode"),
                item.get("BSEcode"),
                item.get("ISIN"),
                now_iso
            ))
            if cur.rowcount > 0:
                stored_count += 1

        cur.execute("""
            INSERT INTO trendlyne_fetch_logs (feed_name, fetched_at, status, records_count)
            VALUES (?, ?, ?, ?)
        """, ("market_insight", now_iso, "SUCCESS", len(insights)))
        conn.commit()

    logger.info(f"Stored {stored_count} new market insights (total in feed: {len(insights)})")
    return stored_count


def execute_full_cycle(db_path: str = DB_PATH) -> Dict[str, Any]:
    """Executes a full fetch cycle for all 3 APIs and stores in SQLite."""
    init_db(db_path)
    results = {}

    # 1. Day Change
    try:
        day_data = fetch_json(ENDPOINTS["day_change"])
        cnt = store_performance(day_data, period="day", db_path=db_path)
        results["day_change"] = {"status": "SUCCESS", "records": cnt}
    except Exception as e:
        logger.error(f"Failed day_change fetch: {e}")
        results["day_change"] = {"status": "FAILED", "error": str(e)}

    # 2. Week Change
    try:
        week_data = fetch_json(ENDPOINTS["week_change"])
        cnt = store_performance(week_data, period="week", db_path=db_path)
        results["week_change"] = {"status": "SUCCESS", "records": cnt}
    except Exception as e:
        logger.error(f"Failed week_change fetch: {e}")
        results["week_change"] = {"status": "FAILED", "error": str(e)}

    # 3. Market Insight
    try:
        insight_data = fetch_json(ENDPOINTS["market_insight"])
        cnt = store_market_insights(insight_data, db_path=db_path)
        results["market_insight"] = {"status": "SUCCESS", "new_records": cnt}
    except Exception as e:
        logger.error(f"Failed market_insight fetch: {e}")
        results["market_insight"] = {"status": "FAILED", "error": str(e)}

    return results


def get_db_stats(db_path: str = DB_PATH) -> Dict[str, Any]:
    """Returns row counts and latest updates for all tables."""
    if not os.path.exists(db_path):
        return {"error": f"DB not found at {db_path}"}
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trendlyne_performance")
        perf_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM trendlyne_current_performance")
        curr_perf_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM trendlyne_market_insights")
        news_count = cur.fetchone()[0]
        cur.execute("SELECT MAX(fetched_at) FROM trendlyne_fetch_logs WHERE status='SUCCESS'")
        last_sync = cur.fetchone()[0]
    return {
        "db_path": db_path,
        "performance_history_rows": perf_count,
        "current_performance_rows": curr_perf_count,
        "market_insights_rows": news_count,
        "last_sync": last_sync
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Trendlyne Market Intel Collector")
    parser.add_argument("--fetch", action="store_true", help="Run full fetch cycle")
    parser.add_argument("--stats", action="store_true", help="Show DB statistics")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to SQLite DB")
    args = parser.parse_args()

    target_db = args.db
    if args.fetch:
        res = execute_full_cycle(target_db)
        print(json.dumps(res, indent=2))
        stats = get_db_stats(target_db)
        print("\nDB Stats:")
        print(json.dumps(stats, indent=2))
    elif args.stats:
        stats = get_db_stats(target_db)
        print(json.dumps(stats, indent=2))
    else:
        # Default: run fetch and print stats
        res = execute_full_cycle(target_db)
        print(json.dumps(res, indent=2))
        stats = get_db_stats(target_db)
        print("\nDB Stats:")
        print(json.dumps(stats, indent=2))
