#!/usr/bin/env python3
"""
cli.py — Unified Multi-Engine CLI & Driver for Prod_VPS
=======================================================
Single CLI entrypoint for status, multi-year replays, and live production runs
across all discovered trading engines (V2, V3, V4, etc.).

Usage:
  - Portfolio Status:       python3 cli.py --status
  - Replay Specific Engine: python3 cli.py --replay --engine v3 --start 2026-01-01 --end 2026-06-03
  - Replay Specific Engine: python3 cli.py --replay --engine v2 --start 2024-01-01 --end 2025-12-31
  - Replay All Engines:     python3 cli.py --replay --all --start 2026-01-01 --end 2026-06-03
  - Live Daily Run:         python3 cli.py --run [--dry-run | --live] [--date YYYY-MM-DD]
"""

import os
import sys
import json
import time
import argparse
import datetime
from typing import Optional, Dict, Any

_PROD_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from engines.registry import EngineRegistry
from common.storage import MasterStorage


def show_portfolio_status(storage: MasterStorage, registry: EngineRegistry):
    active_all = storage.get_active_trades()
    closed_all = storage.get_closed_trades()
    engines = registry.discover_and_load()

    print("\n" + "═" * 95)
    print("  QUANTSYNC MULTI-ENGINE PORTFOLIO STATUS")
    print("═" * 95)
    print(f"  Active Engines: {len(engines)} ({', '.join(engines.keys()).upper()})")
    print(f"  Total Active Positions across all engines: {len(active_all)}")
    print("─" * 95)

    if not active_all:
        print("  No active positions currently open.")
    else:
        print(f"  {'Engine':<8} | {'Symbol':<12} | {'Strategy':<26} | {'Entry Date':<10} | {'Entry':>8} | {'Stop':>8} | {'Target':>8} | {'Shares':>6} | {'LTP':>8}")
        print("  " + "-" * 91)
        for p in active_all:
            ltp_str = f"₹{p['current_ltp']:.2f}" if p.get('current_ltp') else "N/A"
            print(f"  {p['engine_version'].upper():<8} | {p['symbol']:<12} | {p['strategy_id']:<26} | {p['entry_date']:<10} | ₹{p['entry_price']:>7.2f} | ₹{p['current_stop']:>7.2f} | ₹{p['target_price']:>7.2f} | {p['shares']:>6} | {ltp_str:>8}")

    print("─" * 95)
    print("  HISTORICAL SUMMARY BY ENGINE:")
    for eng_id in engines:
        eng_closed = [t for t in closed_all if t["engine_version"].lower() == eng_id.lower()]
        wins = [t for t in eng_closed if t["net_pnl"] > 0]
        net_profit = sum(t["net_pnl"] for t in eng_closed)
        wr = (len(wins) / len(eng_closed) * 100.0) if eng_closed else 0.0
        latest_nav = storage.get_latest_nav(eng_id)
        nav_str = f"₹{latest_nav['total_nav']:,.2f}" if latest_nav else "N/A"
        print(f"  • [{eng_id.upper()}] Closed Trades: {len(eng_closed):<5} | Win Rate: {wr:>5.1f}% | Realized PnL: ₹{net_profit:>12,.2f} | Latest NAV: {nav_str}")

    print("═" * 95 + "\n")


def run_replay(registry: EngineRegistry, engine_id: Optional[str], start_date: str, end_date: str, is_all: bool = False):
    engines = registry.discover_and_load()
    target_engines = list(engines.keys()) if is_all else ([engine_id.lower()] if engine_id else list(engines.keys()))

    reports_dir = os.path.join(_PROD_ROOT, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    print("═" * 95)
    print(f"QUANTSYNC MULTI-ENGINE REPLAY ({start_date} to {end_date})")
    print(f"Engines Selected: {', '.join(target_engines).upper()}")
    print("═" * 95, flush=True)

    for eng_key in target_engines:
        engine = engines.get(eng_key)
        if not engine:
            print(f"❌ Error: Engine [{eng_key}] not found in registry.")
            continue

        prefix = f"replay_{eng_key}_{start_date[:4]}_{end_date[:4]}"
        res = engine.run(start_date=start_date, end_date=end_date, is_live=False, verbose=True)
        engine.export_reports(output_dir=reports_dir, prefix=prefix)


def run_daily_cycle(registry: EngineRegistry, target_date: str, dry_run: bool = True, live: bool = False):
    engines = registry.discover_and_load()
    is_live = (live and not dry_run)
    mode_str = "LIVE BROKER EXECUTION" if is_live else "PAPER TRADING / DRY-RUN"

    print("═" * 95)
    print(f"🚀 QUANTSYNC DAILY CYCLE — {target_date} [{mode_str}]")
    print(f"Active Engines: {', '.join(engines.keys()).upper()}")
    print("═" * 95, flush=True)

    for eng_id, engine in engines.items():
        print(f"\n--- Executing [{engine.engine_name}] ---")
        day_res = engine.run(start_date=target_date, end_date=target_date, is_live=is_live)
        print(f"  [{eng_id.upper()}] NAV = ₹{day_res.get('nav', 0.0):,.2f} | Open = {day_res.get('open_positions_count', 0)} | Exits = {day_res.get('exits_count', 0)} | Entries = {day_res.get('entries_count', 0)}")


def main():
    parser = argparse.ArgumentParser(description="QuantSync-VPS Multi-Engine CLI")
    parser.add_argument("--status", action="store_true", help="Display combined multi-engine portfolio status")
    parser.add_argument("--replay", action="store_true", help="Run historical replay simulation")
    parser.add_argument("--engine", type=str, default=None, help="Target engine ID (e.g. v2, v3)")
    parser.add_argument("--all", action="store_true", help="Target all active engines")
    parser.add_argument("--start", type=str, default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-06-03", help="End date (YYYY-MM-DD)")
    parser.add_argument("--run", action="store_true", help="Run daily production session")
    parser.add_argument("--date", type=str, default=datetime.date.today().strftime("%Y-%m-%d"), help="Target session date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Run in paper trading mode")
    parser.add_argument("--live", action="store_true", help="Run in live broker execution mode")
    args = parser.parse_args()

    registry = EngineRegistry()
    storage = MasterStorage()

    if args.status:
        show_portfolio_status(storage, registry)
    elif args.replay:
        run_replay(registry, args.engine, args.start, args.end, is_all=args.all)
    elif args.run:
        run_daily_cycle(registry, args.date, dry_run=args.dry_run, live=args.live)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
