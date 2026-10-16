#!/usr/bin/env python3
"""
daily_processor_v3.py — Stateless Single-Day Production Processor for QuantSync-V3
================================================================================
Weekly Trend-Based Fibonacci & Champion Breakout Engine execution pipeline:
  1. Reads active V3 positions from quantsync_master.db
  2. Fetches each symbol's candle from stock_history.db for date_str
  3. Evaluates structural trailing stop-loss / Fibonacci target exits
  4. Scans for new Champion breakout + Weekly Fibonacci confluence candidates
  5. Sizing & Risk Management (₹12,000/pos, max 25 slots, ₹300,000 capital)
  6. Updates DB atomically: close exited trades, insert new active trades with geometry
  7. Dispatches real-time Telegram alerts [V3-PROD] & updates Wix portal
"""

import os
import sys
import json
import time
import sqlite3
import datetime
import logging
import argparse
from collections import defaultdict
from typing import Dict, List, Any, Optional

_PROD_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from common.storage import MasterStorage
from common.cost_model import TransactionCostModel, FrictionScenario
from common.notifier import notify_buy, notify_exit
from engines.v3.engine import V3TradingEngine

log_dir = os.path.join(_PROD_ROOT, "Data", "logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DailyProcessorV3] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "daily_processor_v3.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("DailyProcessorV3")

ENGINE_ID = "v3"
ENGINE_NAME = "QuantSync-V3"
INITIAL_CAPITAL = 300000.0
CAPITAL_PER_POSITION = 12000.0
MAX_SLOTS = 25

DB_MASTER = os.path.join(_PROD_ROOT, "Data", "quantsync_master.db")
DB_HISTORY = os.path.join(_PROD_ROOT, "Data", "stock_history.db")


def _get_ist_today() -> str:
    utc_now = datetime.datetime.utcnow()
    ist_now = utc_now + datetime.timedelta(hours=5, minutes=30)
    return ist_now.strftime("%Y-%m-%d")


def _get_symbol_candle(cur_hist, symbol: str, date_str: str) -> Optional[Dict[str, Any]]:
    cur_hist.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (symbol,))
    row = cur_hist.fetchone()
    if not row or not row[0]:
        return None
    try:
        candles = json.loads(row[0])
        for c in reversed(candles):
            if str(c.get("t", ""))[:10] == date_str:
                return c
    except Exception:
        pass
    return None


def process_daily_v3(
    date_str: Optional[str] = None,
    dry_run: bool = False,
    scan_buys: bool = True
) -> Dict[str, Any]:
    """
    Executes a single-day production session for QuantSync-V3.
    """
    date_str = date_str or _get_ist_today()
    t0 = time.time()
    storage = MasterStorage()

    logger.info("═" * 80)
    logger.info(f"🚀 QuantSync-V3 DAILY PROCESSOR — {date_str} (DryRun={dry_run}, ScanBuys={scan_buys})")
    logger.info("═" * 80)

    if not os.path.exists(DB_HISTORY):
        logger.error(f"stock_history.db not found at {DB_HISTORY}")
        return {"status": "ERROR", "message": "stock_history.db not found"}

    conn_hist = sqlite3.connect(DB_HISTORY)
    cur_hist = conn_hist.cursor()

    # ── Restore Account State ────────────────────────────────────────────────
    latest_nav = storage.get_latest_nav(ENGINE_ID)
    current_cash = float(latest_nav.get("cash", INITIAL_CAPITAL)) if latest_nav else INITIAL_CAPITAL
    active_trades = storage.get_active_trades(ENGINE_ID)

    logger.info(f"Initial State: Cash=₹{current_cash:,.2f}, Active Positions={len(active_trades)}")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 1: EXIT EVALUATION
    # ════════════════════════════════════════════════════════════════════════
    held_prior = [p for p in active_trades if str(p.get("entry_date", "")) < date_str]
    logger.info(f"[PHASE 1] Checking exits on {len(held_prior)} V3 positions held prior to {date_str}...")

    exits_today = []

    for pos in held_prior:
        sym = pos["symbol"]
        candle = _get_symbol_candle(cur_hist, sym, date_str)
        if not candle:
            logger.debug(f"  No candle found for {sym} on {date_str}")
            continue

        open_p = float(candle.get("o", 0.0))
        high_p = float(candle.get("h", 0.0))
        low_p = float(candle.get("l", 0.0))
        close_p = float(candle.get("c", 0.0))

        ep = float(pos.get("entry_price", 0.0))
        shares = int(pos.get("shares", 1))
        current_stop = float(pos.get("current_stop") or ep * 0.92)
        target_p = float(pos.get("target_price") or ep * 1.20)

        # Check Breakeven Lock trigger
        be_lock = float(pos.get("be_lock_price") or 0.0)
        if be_lock > 0 and high_p >= be_lock and current_stop < ep:
            logger.info(f"  🔒 Breakeven Lock triggered for {sym}: Stop trailed {current_stop:.2f} -> {ep:.2f}")
            current_stop = ep
            if not dry_run:
                storage.update_stop_level(pos["trade_id"], current_stop)
            pos["current_stop"] = current_stop

        exit_reason = None
        exit_price = None

        # Check Stop Hit
        if low_p <= current_stop:
            exit_reason = "STOP_LOSS"
            exit_price = min(open_p, current_stop) if open_p < current_stop else current_stop
        # Check Target Hit
        elif high_p >= target_p:
            exit_reason = "TARGET_HIT"
            exit_price = max(open_p, target_p) if open_p > target_p else target_p

        if exit_reason and exit_price:
            leg_out = TransactionCostModel.calculate_leg_friction(
                exit_price, shares, is_buy=False, scenario=FrictionScenario.BASE
            )
            proceeds = round(exit_price * shares - leg_out.total_cost_cash, 2)
            current_cash += proceeds

            gross_pnl = round((exit_price - ep) * shares, 2)
            entry_friction = float(pos.get("entry_friction", 0.0)) or TransactionCostModel.calculate_leg_friction(
                ep, shares, is_buy=True, scenario=FrictionScenario.BASE
            ).total_cost_cash
            tot_friction = round(entry_friction + leg_out.total_cost_cash, 2)
            net_pnl = round(gross_pnl - tot_friction, 2)
            roi_pct = round(((exit_price - ep) / ep) * 100.0, 2)

            d_entry = datetime.datetime.strptime(str(pos.get("entry_date", date_str))[:10], "%Y-%m-%d").date()
            d_exit = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            holding_days = max(1, (d_exit - d_entry).days)

            closed_trade = {
                "trade_id": pos["trade_id"],
                "engine_version": ENGINE_ID,
                "symbol": sym,
                "strategy_id": pos.get("strategy_id", "HTF"),
                "entry_date": pos.get("entry_date", date_str),
                "exit_date": date_str,
                "entry_price": ep,
                "exit_price": exit_price,
                "shares": shares,
                "invested_capital": pos.get("invested_capital", ep * shares),
                "gross_pnl": gross_pnl,
                "total_friction": tot_friction,
                "net_pnl": net_pnl,
                "roi_pct": roi_pct,
                "exit_reason": exit_reason,
                "holding_days": holding_days,
                "cash_after": round(current_cash, 2)
            }

            levels = {"entry": ep, "exit": exit_price, "stop": current_stop, "target": target_p}
            mc = storage.build_mini_chart_data(sym, levels=levels)
            closed_trade["mini_chart"] = mc
            closed_trade["sparkline"] = mc.get("sparkline", [])

            logger.info(f"  🛑 EXIT: {sym} ({exit_reason}) @ ₹{exit_price:.2f} | Net PnL: ₹{net_pnl:,.2f} ({roi_pct:+.2f}%) | Cash: ₹{current_cash:,.2f}")
            exits_today.append(closed_trade)

            if not dry_run:
                try:
                    storage.close_trade(closed_trade)
                    notify_exit(closed_trade, engine_version=ENGINE_ID)
                except Exception as ex:
                    logger.error(f"Error persisting/alerting exit for {sym}: {ex}")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 2: ENTRY SCANNING & SIZING
    # ════════════════════════════════════════════════════════════════════════
    entries_today = []
    if scan_buys:
        # Re-fetch active symbols
        active_now = storage.get_active_trades(ENGINE_ID)
        active_syms = set(p["symbol"] for p in active_now)
        # Exclude today's exits from blocking if needed
        open_slots = MAX_SLOTS - len(active_now)
        logger.info(f"\n[PHASE 2] Scanning V3 candidates for {date_str} (Open Slots: {open_slots}/25)...")

        if open_slots > 0 and current_cash >= CAPITAL_PER_POSITION:
            engine = V3TradingEngine(
                engine_id=ENGINE_ID,
                initial_capital=INITIAL_CAPITAL,
                capital_per_position=CAPITAL_PER_POSITION,
                max_slots=MAX_SLOTS
            )
            engine.load_universe()
            candidates = engine.scan_date(date_str)
            logger.info(f"Found {len(candidates)} raw V3 candidates for {date_str}.")

            for cand in candidates:
                sym = cand["symbol"]
                if sym in active_syms:
                    continue
                if open_slots <= 0 or current_cash < CAPITAL_PER_POSITION:
                    break

                ep = float(cand["entry_price"])
                shares = int(CAPITAL_PER_POSITION // ep)
                if shares <= 0:
                    continue

                leg_in = TransactionCostModel.calculate_leg_friction(
                    ep, shares, is_buy=True, scenario=FrictionScenario.BASE
                )
                cost_total = round(ep * shares + leg_in.total_cost_cash, 2)
                if cost_total > current_cash:
                    continue

                current_cash -= cost_total
                open_slots -= 1
                active_syms.add(sym)

                geom_cand = cand.get("geometry", {})
                levels_cand = {
                    "entry": ep,
                    "current": ep,
                    "stop": float(cand["stop_price"]),
                    "target": float(cand["target_price"]),
                    "be_lock": float(cand["be_lock_price"]),
                    "pivot": float(geom_cand.get("pivot", ep)),
                    "floor": float(geom_cand.get("floor", ep * 0.92))
                }
                mc_cand = storage.build_mini_chart_data(sym, levels=levels_cand)

                trade_id = f"V3_{sym}_{date_str.replace('-', '')}"
                pos = {
                    "trade_id": trade_id,
                    "engine_version": ENGINE_ID,
                    "symbol": sym,
                    "strategy_id": cand.get("strategy_id", "HTF"),
                    "strategy_name": cand.get("strategy_name", "High-Tight Flag"),
                    "entry_date": date_str,
                    "entry_price": ep,
                    "current_stop": float(cand["stop_price"]),
                    "target_price": float(cand["target_price"]),
                    "be_lock_price": float(cand["be_lock_price"]),
                    "shares": shares,
                    "invested_capital": round(ep * shares, 2),
                    "current_ltp": ep,
                    "unrealized_pnl": 0.0,
                    "roi_pct": 0.0,
                    "entry_friction": round(leg_in.total_cost_cash, 2),
                    "ml_score": float(cand.get("ml_score", 0.50)),
                    "ml_size_multiplier": float(cand.get("ml_size_multiplier", 1.0)),
                    "analog_win_rate": float(cand.get("analog_win_rate", 0.50)),
                    "conviction_score": float(cand.get("conviction_score", 0.0)),
                    "status": "OPEN",
                    "geometry": geom_cand,
                    "mini_chart": mc_cand,
                    "sparkline": mc_cand.get("sparkline", [])
                }

                logger.info(f"  🟢 BUY: {sym} ({pos['strategy_id']}) {shares} shares @ ₹{ep:.2f} | Conviction: {pos['conviction_score']} | ML Score: {pos['ml_score']*100:.0f}% | Cost: ₹{cost_total:,.2f} | Stop: ₹{pos['current_stop']:.2f} | Target: ₹{pos['target_price']:.2f}")
                entries_today.append(pos)

                if not dry_run:
                    try:
                        storage.create_active_trade(pos)
                        notify_buy(pos, engine_version=ENGINE_ID)
                    except Exception as ex:
                        logger.error(f"Error persisting/alerting buy for {sym}: {ex}")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 3: NAV SETTLEMENT
    # ════════════════════════════════════════════════════════════════════════
    active_final = storage.get_active_trades(ENGINE_ID)
    equity_holdings = sum(float(p.get("current_ltp") or p["entry_price"]) * int(p["shares"]) for p in active_final)
    total_nav = round(current_cash + equity_holdings, 2)

    nav_entry = {
        "engine_version": ENGINE_ID,
        "date": date_str,
        "total_nav": total_nav,
        "cash": round(current_cash, 2),
        "invested_capital": round(equity_holdings, 2),
        "open_positions": len(active_final),
        "active_positions_count": len(active_final)
    }

    if not dry_run:
        try:
            storage.record_daily_nav(nav_entry)
        except Exception as ex:
            logger.error(f"Error recording daily NAV: {ex}")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 4: WIX SYNC DISPATCH
    # ════════════════════════════════════════════════════════════════════════
    if not dry_run:
        try:
            from common.wix_sync import push_to_wix
            push_to_wix(phase="V3_DAILY")
        except Exception as ex:
            logger.warning(f"Error dispatching to Wix: {ex}")

    conn_hist.close()
    elapsed = time.time() - t0

    logger.info("═" * 80)
    logger.info(f"✅ V3 Process Daily Completed in {elapsed:.2f}s: Exits={len(exits_today)}, Entries={len(entries_today)}, NAV=₹{total_nav:,.2f}, Active={len(active_final)}")
    logger.info("═" * 80)

    return {
        "status": "SUCCESS",
        "date": date_str,
        "engine_version": ENGINE_ID,
        "exits": exits_today,
        "entries": entries_today,
        "active_count": len(active_final),
        "total_nav": total_nav,
        "cash": round(current_cash, 2),
        "elapsed_s": round(elapsed, 2)
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantSync-V3 Single-Day Production Processor")
    parser.add_argument("--date", type=str, help="Trading date to process (YYYY-MM-DD)", default=None)
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying DB or sending alerts")
    parser.add_argument("--no-scan", action="store_true", help="Skip scanning for new buys")
    args = parser.parse_args()

    res = process_daily_v3(date_str=args.date, dry_run=args.dry_run, scan_buys=not args.no_scan)
    print(json.dumps(res, indent=2, default=str))
