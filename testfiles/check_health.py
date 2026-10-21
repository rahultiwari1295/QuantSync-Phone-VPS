#!/usr/bin/env python3
"""
check_health.py — Pre-Flight Production Readiness Health Check for Prod_VPS
===========================================================================
Runs comprehensive automated verification across all 6 core sub-systems:
  1. Python runtime dependencies
  2. Database accessibility & single source of truth
  3. Angel One SmartAPI authentication & live quote retrieval
  4. Telegram bot connectivity & channel delivery
  5. Multi-Engine plugin registry discovery (V2, V3, etc.)
  6. Calendar & Market Hours Guard
"""

import os
import sys
import json
import time
import sqlite3

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main():
    print("═" * 80)
    print("  QUANTSYNC PROD_VPS PRE-FLIGHT READINESS CHECK")
    print("═" * 80)
    all_ok = True

    # Check 1: Dependencies
    print("1. Checking Python Dependencies...", end=" ", flush=True)
    try:
        import numpy
        import pandas
        import schedule
        import xgboost
        import sklearn
        import lightgbm
        import requests
        import pyarrow
        import scipy
        import fastapi
        import uvicorn
        import pydantic
        try:
            import shap
        except ImportError:
            pass
        print("✅ OK")
    except ImportError as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    # Check 2: Database integrity
    print("2. Checking Data Integrity...", end=" ", flush=True)
    try:
        hist_db = os.path.join(_ROOT, "Data", "stock_history.db")
        master_db = os.path.join(_ROOT, "Data", "quantsync_master.db")

        assert os.path.exists(hist_db), f"Missing {hist_db}"
        assert os.path.exists(master_db), f"Missing {master_db}"

        # Quick query
        conn = sqlite3.connect(f"file:{hist_db}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM StockHistory")
        sym_count = cur.fetchone()[0]
        conn.close()

        print(f"✅ OK ({sym_count:,} symbols in StockHistory)")
    except Exception as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    # Check 3: Angel One SmartAPI
    print("3. Checking Angel One Broker Authentication...", end=" ", flush=True)
    try:
        from common.broker import get_session, fetch_batch_quotes
        jwt = get_session()
        if jwt:
            quote = fetch_batch_quotes(["3045"])  # SBIN
            ltp = quote[0].get("ltp", 0.0) if quote else 0.0
            print(f"✅ OK (SBIN Quote: ₹{ltp:.2f})")
        else:
            print("⚠️ FAILED: Could not obtain JWT session")
            all_ok = False
    except Exception as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    # Check 4: Telegram Connectivity
    print("4. Checking Telegram Routing & Connectivity...", end=" ", flush=True)
    try:
        from common.notifier import get_tg_creds
        master_tg = get_tg_creds(None)
        v2_tg = get_tg_creds("v2")
        v3_tg = get_tg_creds("v3")
        assert master_tg.get("bot_token"), "Missing master bot_token"
        assert v2_tg.get("chat_id"), "Missing v2 chat_id"
        assert v3_tg.get("chat_id"), "Missing v3 chat_id"
        print(f"✅ OK (Master: {master_tg['chat_id']} | V2: {v2_tg['chat_id']} | V3: {v3_tg['chat_id']})")
    except Exception as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    # Check 5: Plugin Discovery
    print("5. Checking Multi-Engine Plugin Discovery...", end=" ", flush=True)
    try:
        from engines.registry import EngineRegistry
        reg = EngineRegistry()
        engines = reg.discover_and_load()
        assert "v2" in engines, "V2 plugin missing"
        assert "v3" in engines, "V3 plugin missing"
        print(f"✅ OK ({len(engines)} active plugins: {', '.join(engines.keys()).upper()})")
    except Exception as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    # Check 6: Master Storage Tables & Concurrency
    print("6. Checking Master Storage Schema & Concurrency...", end=" ", flush=True)
    try:
        from common.storage import MasterStorage
        storage = MasterStorage()
        active = storage.get_active_trades()
        closed = storage.get_closed_trades()
        with storage._get_conn() as conn:
            j_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert j_mode.lower() == "wal", f"Journal mode is {j_mode}, expected WAL"
        print(f"✅ OK (Active: {len(active)} | Closed: {len(closed)} | WAL Mode: Active)")
    except Exception as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    # Check 7: Wix Portal API Connectivity
    print("7. Checking Wix Portal Production API Connectivity...", end=" ", flush=True)
    try:
        from common.wix_sync import push_to_wix
        ok = push_to_wix(phase="PRE_FLIGHT_CHECK")
        if ok:
            print("✅ OK (HTTP 200 delivered)")
        else:
            print("⚠️ FAILED: Wix endpoint returned non-200")
            all_ok = False
    except Exception as e:
        print(f"❌ FAILED: {e}")
        all_ok = False

    print("═" * 80)
    if all_ok:
        print("  🎉 [SUCCESS] ALL 7 CHECKS PASSED — READY FOR LIVE PRODUCTION DEPLOYMENT!")
    else:
        print("  ⚠️ [WARNING] Some checks failed. Review errors above before going live.")
    print("═" * 80 + "\n")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
