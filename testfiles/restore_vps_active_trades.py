#!/usr/bin/env python3
"""
restore_vps_active_trades.py
Restores the active_trades table to match the VPS live state shown in the user's
screenshots, includes V3 active trade (SUNFLAG), cleans up wrongly closed trades,
regenerates the unified V2+V3 snapshot, and syncs directly to Wix.
"""

import os
import sys
import json
import sqlite3
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

from common.storage import MasterStorage
from common.wix_sync import push_to_wix
import snapshot_manager

MASTER_DB = os.path.join(_DIR, "Data", "quantsync_master.db")

# Exact active trades from the user's VPS live screenshots + V3 active trade
ACTIVE_TRADES = [
    {
        "trade_id": "V2_ACT_20260928_NURECA",
        "engine_version": "v2",
        "symbol": "NURECA",
        "strategy_id": "Leader",
        "entry_date": "2026-09-28",
        "entry_price": 365.0,
        "current_stop": 343.0,
        "target_price": 409.0,
        "shares": 32,
        "invested_capital": 11680.0,
        "current_ltp": 350.80,
        "unrealized_pnl": round((350.80 - 365.0) * 32, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V2_ACT_20260928_SANGAMIND",
        "engine_version": "v2",
        "symbol": "SANGAMIND",
        "strategy_id": "Leader",
        "entry_date": "2026-09-28",
        "entry_price": 634.0,
        "current_stop": 603.0,
        "target_price": 696.0,
        "shares": 18,
        "invested_capital": 11412.0,
        "current_ltp": 634.55,
        "unrealized_pnl": round((634.55 - 634.0) * 18, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V2_ACT_20260917_YATHARTH",
        "engine_version": "v2",
        "symbol": "YATHARTH",
        "strategy_id": "Breakout",
        "entry_date": "2026-09-17",
        "entry_price": 1066.0,
        "current_stop": 1004.0,
        "target_price": 1226.0,
        "shares": 14,
        "invested_capital": 14924.0,
        "current_ltp": 1033.70,
        "unrealized_pnl": round((1033.70 - 1066.0) * 14, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V2_ACT_20260915_EMUDHRA",
        "engine_version": "v2",
        "symbol": "EMUDHRA",
        "strategy_id": "Breakout",
        "entry_date": "2026-09-15",
        "entry_price": 593.0,
        "current_stop": 556.0,
        "target_price": 682.0,
        "shares": 25,
        "invested_capital": 14825.0,
        "current_ltp": 580.0,
        "unrealized_pnl": round((580.0 - 593.0) * 25, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V2_ACT_20260910_ELLEN",
        "engine_version": "v2",
        "symbol": "ELLEN",
        "strategy_id": "Breakout",
        "entry_date": "2026-09-10",
        "entry_price": 355.0,
        "current_stop": 332.0,
        "target_price": 408.0,
        "shares": 42,
        "invested_capital": 14910.0,
        "current_ltp": 341.70,
        "unrealized_pnl": round((341.70 - 355.0) * 42, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V2_ACT_20260908_SYRMA",
        "engine_version": "v2",
        "symbol": "SYRMA",
        "strategy_id": "Breakout",
        "entry_date": "2026-09-08",
        "entry_price": 1635.0,
        "current_stop": 1528.0,
        "target_price": 1880.0,
        "shares": 9,
        "invested_capital": 14715.0,
        "current_ltp": 1717.90,
        "unrealized_pnl": round((1717.90 - 1635.0) * 9, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V2_ACT_20260828_MANOMAY",
        "engine_version": "v2",
        "symbol": "MANOMAY",
        "strategy_id": "Leader",
        "entry_date": "2026-08-28",
        "entry_price": 228.0,
        "current_stop": 212.0,
        "target_price": 262.0,
        "shares": 66,
        "invested_capital": 15048.0,
        "current_ltp": 240.08,
        "unrealized_pnl": round((240.08 - 228.0) * 66, 2),
        "status": "OPEN",
    },
    {
        "trade_id": "V3_ACT_20260928_SUNFLAG",
        "engine_version": "v3",
        "symbol": "SUNFLAG",
        "strategy_id": "52W_HIGH",
        "entry_date": "2026-09-28",
        "entry_price": 449.20,
        "current_stop": 413.26,
        "target_price": 489.63,
        "shares": 26,
        "invested_capital": 11679.20,
        "current_ltp": 449.20,
        "unrealized_pnl": 0.0,
        "status": "OPEN",
    }
]


def main():
    conn = sqlite3.connect(MASTER_DB)
    cur = conn.cursor()

    # 1. Clear current active_trades
    cur.execute("DELETE FROM active_trades")
    print(f"Cleared active_trades table.")

    # 2. Get column names
    cur.execute("PRAGMA table_info(active_trades)")
    cols = [c[1] for c in cur.fetchall()]
    print(f"Active trades columns: {cols}")

    # 3. Insert the correct active trades (7 V2 + 1 V3)
    for trade in ACTIVE_TRADES:
        values = []
        for col in cols:
            values.append(trade.get(col))

        placeholders = ", ".join(["?" for _ in cols])
        col_names = ", ".join(cols)
        cur.execute(f"INSERT OR REPLACE INTO active_trades ({col_names}) VALUES ({placeholders})", values)
        print(f"  ✅ Inserted [{trade['engine_version'].upper()}]: {trade['symbol']} ({trade['strategy_id']}) | Entry: {trade['entry_date']} | CMP: ₹{trade['current_ltp']}")

    # 4. Remove incorrectly-created closed trades that should still be active or were replay artifacts
    wrongly_closed = [
        ("YATHARTH", "2026-09-30", "BREAKEVEN_RATCHET"),
        ("EMUDHRA", "2026-09-30", "BREAKEVEN_RATCHET"),
        ("ELLEN", "2026-09-30", "BREAKEVEN_RATCHET"),
        ("AXISCADES", "2026-10-01", "TARGET_HIT"),
    ]
    for sym, exit_date, reason in wrongly_closed:
        cur.execute("DELETE FROM closed_trades WHERE symbol = ? AND exit_date = ? AND exit_reason = ?",
                    (sym, exit_date, reason))
        deleted = cur.rowcount
        if deleted:
            print(f"  🗑️ Removed wrongly-closed: {sym} ({reason} on {exit_date})")

    conn.commit()

    # 5. Verify Active Trades
    cur.execute("SELECT engine_version, symbol, strategy_id, entry_date, entry_price, current_ltp, unrealized_pnl FROM active_trades ORDER BY entry_date")
    print(f"\n=== RESTORED ACTIVE TRADES ===")
    for r in cur.fetchall():
        print(f"  {r}")

    # 6. Verify Closed Trades Summary
    cur.execute("SELECT engine_version, COUNT(*) FROM closed_trades GROUP BY engine_version")
    print(f"\n=== CLOSED TRADES SUMMARY ===")
    for r in cur.fetchall():
        print(f"  {r}")

    conn.close()

    # 7. Regenerate the unified V2+V3 snapshot
    storage = MasterStorage()
    snap_json = snapshot_manager.generate_terminal_v2_snapshot(storage)
    snap_obj = json.loads(snap_json)

    print(f"\n=== REGENERATED SNAPSHOT ===")
    print(f"Active count: {len(snap_obj.get('active', []))}")
    for a in snap_obj.get('active', []):
        print(f"  - {a.get('sym')} [{a.get('engineBadge', a.get('engine'))}] | Entry: {a.get('entryDate')} @ ₹{a.get('entryPrice')} | CMP: ₹{a.get('currentLtp')}")
    print(f"Closed count: {len(snap_obj.get('closed', []))}")
    print(f"Hero stats: {snap_obj.get('hero')}")

    # 8. Write to all frontend JSON locations
    locations = [
        os.path.join(_DIR, "Data", "terminal_data_v2.json"),
        os.path.join(_DIR, "Data", "terminal_v2_snapshot.json"),
        os.path.join(_DIR, "Data", "terminal_data_v3.json"),
        os.path.join(_DIR, "..", "RSITradingPlatform", "public", "terminal_data_v2.json"),
        os.path.join(_DIR, "..", "RSITradingPlatform", "public", "terminal_data_v3.json"),
        os.path.join(_DIR, "..", "RSITradingPlatform", "dist", "terminal_data_v2.json"),
        os.path.join(_DIR, "..", "RSITradingPlatform", "dist", "terminal_data_v3.json"),
    ]
    for loc in locations:
        try:
            os.makedirs(os.path.dirname(loc), exist_ok=True)
            with open(loc, "w", encoding="utf-8") as f:
                json.dump(snap_obj, f, separators=(",", ":"), ensure_ascii=False)
            print(f"  ✓ Written to {os.path.abspath(loc)}")
        except Exception as e:
            print(f"  ✗ Failed: {loc}: {e}")

    # 9. Direct live sync to Wix
    print("\n📡 Pushing restored state directly to Wix...")
    try:
        wix_ok = push_to_wix("RESTORE_VPS_STATE")
        if wix_ok:
            print("  ✅ Wix sync completed successfully!")
        else:
            print("  ⚠️ Wix sync returned False or was disabled.")
    except Exception as e:
        print(f"  ❌ Wix push error: {e}")

    print(f"\n🎉 RESTORATION FULLY COMPLETE!")


if __name__ == "__main__":
    main()
