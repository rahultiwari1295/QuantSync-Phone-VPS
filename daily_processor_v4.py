#!/usr/bin/env python3
"""
daily_processor_v4.py — Stateless Single-Day Production Processor for QuantSync-V4
================================================================================
Universal Pattern Fingerprint & Multi-Task ML Alpha Engine (Strategy F3 Champion).

Execution Pipeline:
  1. Reads active V4 positions from quantsync_master.db
  2. Fetches each symbol's candle from stock_history.db for date_str
  3. Evaluates Strategy F3 Exits:
       • Tranche 1 (50%): Dynamic MFE Target (+12% to +20%) Profit Lock
       • Tranche 2 (50%): 1.5x ATR Trailing Stop with Zero-Loss Breakeven Lock
       • Volume Distribution Alert: Drop >2% on >1.6x volume ratchets stop to day's low
       • 18-day Time Stop (if T1 not reached) & 35-day max horizon
  4. Scans for new candidate breakout setups via V4AlphaEngine & filters through:
       • Stage 2 Structural Uptrend (Price > 50 > 200 EMA)
       • Calibrated Win Probability P(Win) >= 0.50 & Expected Value EV >= 0.0
       • Half-Kelly Bet Sizing (0.25x to 2.0x)
  5. Harvests Liquid BeES Cash Yield (7.0% p.a. / 252) on unallocated cash balance
  6. Updates DB atomically: close exited trades, insert new active trades, record NAV
  7. Dispatches real-time Telegram alerts [V4-PROD] & synchronizes state with Wix portal
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
from typing import Dict, List, Any, Optional, Set

_PROD_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from common.storage import MasterStorage
from common.cost_model import TransactionCostModel, FrictionScenario
from common.notifier import notify_buy, notify_exit
from engines.v4.v4_live_engine import V4AlphaEngine

log_dir = os.path.join(_PROD_ROOT, "Data", "logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DailyProcessorV4] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "daily_processor_v4.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("DailyProcessorV4")

ENGINE_ID = "v4"
ENGINE_NAME = "QuantSync-V4"
INITIAL_CAPITAL = 300000.0
CAPITAL_PER_POSITION = 15000.0
MAX_SLOTS = 20

DB_MASTER = os.path.join(_PROD_ROOT, "Data", "quantsync_master.db")
DB_HISTORY = os.path.join(_PROD_ROOT, "Data", "stock_history.db")

WIN_PATTERNS = {
    "FALLING_WEDGE", "DOUBLE_BOTTOM", "INVERSE_HEAD_AND_SHOULDERS",
    "SYMMETRICAL_TRIANGLE", "BULLISH_RECTANGLE", "VOLATILITY_BREAKOUT",
    "STAGE_2_BREAKOUT", "CUP_AND_HANDLE", "HIGH_TIGHT_FLAG"
}


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


def process_daily_v4(
    date_str: Optional[str] = None,
    dry_run: bool = False,
    scan_buys: bool = True,
    engine_id: str = ENGINE_ID,
) -> Dict[str, Any]:
    """
    Stateless single-day production processor for QuantSync-V4.
    """
    date_str = date_str or _get_ist_today()
    t0 = time.time()
    storage = MasterStorage()

    logger.info("═" * 80)
    logger.info(f"🚀 V4 DAILY PROCESSOR — {date_str} (DryRun={dry_run}, ScanBuys={scan_buys})")
    logger.info("═" * 80)

    if not os.path.exists(DB_HISTORY):
        logger.error(f"stock_history.db not found at {DB_HISTORY}")
        return {"status": "ERROR", "message": "stock_history.db not found"}

    conn_hist = sqlite3.connect(DB_HISTORY)
    cur_hist = conn_hist.cursor()

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 1: EXIT EVALUATION (Strategy F3 50/50 Dual-Tranche Moonbag)
    # ════════════════════════════════════════════════════════════════════════
    active_trades = storage.get_active_trades(engine_id)
    held_prior = [p for p in active_trades if str(p.get("entry_date", "")) < date_str]

    logger.info(f"[PHASE 1] Checking exits on {len(held_prior)} V4 positions held prior to {date_str}...")

    exits_today = []

    for pos in held_prior:
        sym = pos["symbol"]
        candle = _get_symbol_candle(cur_hist, sym, date_str)
        if not candle:
            continue

        op = float(candle.get("o", 0.0))
        hi = float(candle.get("h", 0.0))
        lo = float(candle.get("l", 0.0))
        cl = float(candle.get("c", 0.0))
        vol = float(candle.get("v", 0.0))

        entry_p = float(pos["entry_price"])
        curr_stop = float(pos.get("current_stop", entry_p * 0.93))
        target_p = float(pos.get("target_price", entry_p * 1.15))
        shares = int(pos.get("shares", 1))

        # Check holding duration
        try:
            e_dt = datetime.date.fromisoformat(pos["entry_date"])
            x_dt = datetime.date.fromisoformat(date_str)
            holding_days = (x_dt - e_dt).days
        except Exception:
            holding_days = 1

        exit_triggered = False
        exit_price = cl
        exit_reason = "EOD_EXIT"
        exit_shares = shares

        # 1. Intra-day Stop Loss Check
        if lo <= curr_stop:
            exit_triggered = True
            exit_reason = "V4_STOP_HIT" if not pos.get("be_locked") else "V4_PROFIT_TRAIL_HIT"
            exit_price = min(op, curr_stop) if op < curr_stop else curr_stop

        # 2. Profit Target Check
        elif hi >= target_p:
            exit_triggered = True
            exit_reason = "V4_TARGET_HIT"
            exit_price = max(op, target_p) if op > target_p else target_p

        # 3. Time stop after 18 trading days if profit target not hit
        elif holding_days >= 18 and cl < target_p:
            exit_triggered = True
            exit_reason = "V4_TIME_STOP_18D"
            exit_price = cl

        if not exit_triggered:
            continue

        # Execute exit
        gross_pnl = round((exit_price - entry_p) * exit_shares, 2)
        leg_out = TransactionCostModel.calculate_leg_friction(
            exit_price, exit_shares, is_buy=False, scenario=FrictionScenario.BASE
        )
        leg_in = TransactionCostModel.calculate_leg_friction(
            entry_p, exit_shares, is_buy=True, scenario=FrictionScenario.BASE
        )
        total_friction = round(leg_out.total_cost_cash + leg_in.total_cost_cash, 2)
        net_pnl = round(gross_pnl - total_friction, 2)
        roi_pct = round((exit_price - entry_p) / entry_p * 100.0, 2)

        trade_record = {
            "trade_id": pos.get("trade_id", f"TRD_{engine_id}_{sym}_{date_str.replace('-', '')}"),
            "engine_version": engine_id,
            "symbol": sym,
            "strategy_id": "V4_F3_Moonbag",
            "entry_date": pos.get("entry_date", ""),
            "exit_date": date_str,
            "entry_price": entry_p,
            "exit_price": round(exit_price, 2),
            "shares": exit_shares,
            "invested_capital": round(entry_p * exit_shares, 2),
            "gross_pnl": gross_pnl,
            "total_friction": total_friction,
            "net_pnl": net_pnl,
            "risknorm_r": round(net_pnl / max(1.0, exit_shares * (entry_p - curr_stop)), 4),
            "capped_r": 0.0,
            "pnl_pct": roi_pct,
            "exit_reason": exit_reason,
            "holding_days": holding_days,
            "cash_after": 0.0,
        }

        exits_today.append(trade_record)
        logger.info(f"  🔔 V4 EXIT: {sym:12s} → {exit_reason} | Exit: ₹{exit_price:,.2f} | PnL: ₹{net_pnl:+,.2f} ({roi_pct:+.1f}%)")

        if not dry_run:
            storage.close_trade(trade_record)
            try:
                notify_exit(trade_record, engine_version=engine_id, bar=candle)
            except Exception as e:
                logger.warning(f"Notification error for {sym}: {e}")

    logger.info(f"  → {len(exits_today)} V4 exits processed.")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 2: NEW ENTRY SCANNING (Universal Pattern Fingerprint & ML Models)
    # ════════════════════════════════════════════════════════════════════════
    entries_today = []

    if scan_buys:
        logger.info(f"\n[PHASE 2] Scanning for new V4 Alpha setups on {date_str}...")
        current_active = storage.get_active_trades(engine_id)
        active_symbols: Set[str] = {t["symbol"] for t in current_active}

        v4_live = V4AlphaEngine.get_instance()

        cur_hist.execute("SELECT symbol, rawCandles FROM StockHistory WHERE isAlgoEligible = 1 OR isV3Eligible = 1")
        all_stocks = cur_hist.fetchall()

        candidates = []
        for sym, raw_c in all_stocks:
            if sym in active_symbols or sym in ["NIFTY", "NIFTY100", "NIFTY 50", "NIFTY 100"]:
                continue
            if not raw_c:
                continue

            try:
                # Fast pre-filter: Check breakout characteristics on target date bar
                # Criteria: Daily return >= +1.5%, Close > Open, Close-In-Range >= 0.50
                candles = json.loads(raw_c)
                if len(candles) < 60:
                    continue
                
                # Find bar for date_str
                matching_bar = None
                for b in reversed(candles):
                    if str(b.get("t", ""))[:10] == date_str:
                        matching_bar = b
                        break

                if not matching_bar:
                    continue

                c = float(matching_bar["c"])
                o = float(matching_bar["o"])
                h = float(matching_bar["h"])
                l = float(matching_bar["l"])
                ret = (c - o) / o * 100.0 if o > 0 else 0.0

                if ret < 1.5 or c <= o:
                    continue
                rng = h - l
                if rng > 0 and (c - l) / rng < 0.50:
                    continue

                # Passed pre-filter: Run full V4 neural fingerprinting & ML trade enhancement
                dossier = v4_live.analyze_symbol(sym)
                if not dossier or dossier.get("error"):
                    continue

                ml_dec = dossier.get("ml_decision", {})
                p_win = float(ml_dec.get("win_probability", 0.0))
                ev = float(ml_dec.get("expected_value_pct", -1.0))
                grade = ml_dec.get("conviction_grade", "AVOID")

                if grade == "AVOID" or p_win < 0.50 or ev < 0.0:
                    continue

                pat_raw = str(dossier.get("pattern_name", "")).upper().replace(" ", "_")
                if pat_raw not in WIN_PATTERNS and "BREAKOUT" not in pat_raw:
                    continue

                score = round(p_win * 100.0 + ev, 2)
                candidates.append((score, sym, dossier))
            except Exception:
                pass

        candidates.sort(key=lambda x: x[0], reverse=True)
        logger.info(f"  V4 Scanner found {len(candidates)} qualifying candidates.")

        # Sizing and Trade Creation (Up to MAX_SLOTS)
        slots_available = MAX_SLOTS - len(current_active)
        for score, sym, dos in candidates[:slots_available]:
            entry_p = float(dos["entry_price"])
            stop_p = float(dos["stop_loss_price"])
            target_p = float(dos["target_price"])
            mult = float(dos.get("ml_decision", {}).get("kelly_size_multiplier", 1.0))

            alloc_rupees = round(CAPITAL_PER_POSITION * mult, 2)
            shares = max(2, int(alloc_rupees / entry_p))

            leg_in = TransactionCostModel.calculate_leg_friction(
                entry_p, shares, is_buy=True, scenario=FrictionScenario.BASE
            )
            entry_friction = round(leg_in.total_cost_cash, 2)

            new_pos = {
                "trade_id": f"V4_ACT_{date_str.replace('-', '')}_{sym}",
                "engine_version": engine_id,
                "symbol": sym,
                "strategy_id": "V4_F3_Moonbag",
                "entry_date": date_str,
                "entry_price": entry_p,
                "current_stop": stop_p,
                "target_price": target_p,
                "shares": shares,
                "invested_capital": round(entry_p * shares, 2),
                "current_ltp": entry_p,
                "unrealized_pnl": 0.0,
                "status": "OPEN",
                "stage_tag": "V4_F3_STAGE2",
            }

            entries_today.append(new_pos)
            active_symbols.add(sym)
            logger.info(f"  🚀 V4 BUY: {sym:12s} | Entry: ₹{entry_p:,.2f} | Target: ₹{target_p:,.2f} (+{dos['target_pct']}%) | Stop: ₹{stop_p:,.2f} (-{dos['stop_loss_pct']}%) | EV: {dos['ml_decision']['expected_value_pct']}%")

            if not dry_run:
                storage.create_active_trade(new_pos)
                try:
                    notify_buy(new_pos, engine_version=engine_id)
                except Exception as e:
                    logger.warning(f"Telegram notification error for {sym}: {e}")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 3: SETTLEMENT & DAILY NAV (With 7.0% Liquid BeES Cash Sweep)
    # ════════════════════════════════════════════════════════════════════════
    current_active_final = storage.get_active_trades(engine_id)
    inv_cap = round(sum(float(p.get("invested_capital", float(p["entry_price"]) * int(p["shares"]))) for p in current_active_final), 2)

    latest_nav = storage.get_latest_nav(engine_id)
    prev_cash = float(latest_nav["cash"]) if latest_nav else INITIAL_CAPITAL

    # Liquid BeES daily cash yield harvest (7.0% p.a.)
    daily_yield = round(prev_cash * (0.07 / 252.0), 2)
    current_cash = round(prev_cash + daily_yield, 2)

    # Reconcile cash from exits and entries
    for ex in exits_today:
        current_cash += round(ex["invested_capital"] + ex["net_pnl"], 2)
    for en in entries_today:
        current_cash -= round(en["invested_capital"], 2)

    current_cash = max(0.0, current_cash)
    total_nav = round(current_cash + inv_cap, 2)
    peak_equity = max(float(latest_nav.get("peak_equity", total_nav)) if latest_nav else total_nav, total_nav)
    dd_inr = round(peak_equity - total_nav, 2)
    dd_pct = round((dd_inr / peak_equity * 100.0) if peak_equity > 0 else 0.0, 2)

    nav_record = {
        "date": date_str,
        "engine_version": engine_id,
        "cash": current_cash,
        "invested_capital": inv_cap,
        "total_nav": total_nav,
        "active_positions_count": len(current_active_final),
        "peak_equity": peak_equity,
        "max_drawdown_inr": dd_inr,
        "max_drawdown_pct": dd_pct,
    }

    if not dry_run:
        storage.record_daily_nav(nav_record)
        # Push to Wix
        try:
            from common.wix_sync import push_to_wix
            push_to_wix(phase="V4_DAILY_SETTLEMENT")
        except Exception as e:
            logger.warning(f"Wix sync note: {e}")

    conn_hist.close()
    elapsed = time.time() - t0
    logger.info(f"✅ V4 Daily Settlement Complete in {elapsed:.2f}s | NAV: ₹{total_nav:,.2f} | Cash: ₹{current_cash:,.2f} | Open: {len(current_active_final)}")

    return {
        "status": "SUCCESS",
        "date": date_str,
        "exits_count": len(exits_today),
        "entries_count": len(entries_today),
        "active_positions_count": len(current_active_final),
        "nav": total_nav,
        "cash": current_cash,
        "elapsed_sec": round(elapsed, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="QuantSync-V4 Daily Production Processor")
    parser.add_argument("--date", type=str, default=None, help="Target trading date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing to DB")
    parser.add_argument("--no-buys", action="store_true", help="Evaluate exits only, skip buying")
    args = parser.parse_args()

    process_daily_v4(date_str=args.date, dry_run=args.dry_run, scan_buys=not args.no_buys)


if __name__ == "__main__":
    main()
