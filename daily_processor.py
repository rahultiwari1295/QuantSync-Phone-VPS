#!/usr/bin/env python3
"""
daily_processor.py — Stateless Single-Day Production Processor for QuantSync-V2
================================================================================
Core function: process_daily(date_str)

Replaces the parquet-based replay approach with a clean, stateless daily pipeline:
  1. Reads active positions from quantsync_master.db
  2. Fetches each symbol's candle from stock_history.db for that date
  3. Evaluates stop-loss / target exits on the day's High/Low
  4. Scans for new entry signals via V11LiveScanner (5 strategy ensemble)
  5. Applies Gate 5 NIFTY500 regime filter + Portfolio Arbiter sizing
  6. Updates DB atomically: close exited trades, create new entries, record NAV
  7. Pushes to Wix and dispatches Telegram notifications

No parquets. No CSVs. No hardcoded positions. DB is the single source of truth.

Usage:
  # From scheduler (automatic):
  from daily_processor import process_daily
  process_daily("2026-09-18")

  # CLI:
  python daily_processor.py --date 2026-09-18
  python daily_processor.py --date 2026-09-18 --dry-run
  python daily_processor.py  # defaults to today IST
"""

import os
import sys
import json
import time
import sqlite3
import datetime
import logging
import argparse
import numpy as np
from collections import defaultdict
from typing import Dict, List, Any, Optional, Set

# ── Path Setup ─────────────────────────────────────────────────────────────────
_PROD_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

# ── Imports from existing modules ──────────────────────────────────────────────
from common.storage import MasterStorage
from common.cost_model import TransactionCostModel, FrictionScenario

try:
    from engines.v2.live_scanner import V11LiveScanner
except ImportError:
    from engines.v2.live_scanner import V11LiveScanner

try:
    from engines.v2.decision.arbiter import (
        PortfolioState,
        TradeCandidate,
        arbiter_decide,
    )
except ImportError:
    from engines.v2.decision.arbiter import PortfolioState, TradeCandidate, arbiter_decide

# ── Logging ────────────────────────────────────────────────────────────────────
log_dir = os.path.join(_PROD_ROOT, "Data", "logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DailyProcessor] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(log_dir, "daily_processor.log")),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("DailyProcessor")

# ── Constants ──────────────────────────────────────────────────────────────────
ENGINE_ID = "v2"
ENGINE_NAME = "QuantSync-V2"
INITIAL_CAPITAL = 300000.0
CAPITAL_PER_POSITION = 12000.0
MAX_SLOTS = 25

DB_MASTER = os.path.join(_PROD_ROOT, "Data", "quantsync_master.db")
DB_HISTORY = os.path.join(_PROD_ROOT, "Data", "stock_history.db")

STRAT_1WORD_MAP = {
    "APEX_BASE": "Apex", "Apex": "Apex",
    "HIGH_TIGHT_FLAG": "Flag", "Flag": "Flag",
    "STAGE2_BASE": "Breakout", "STAGE2_BASE_BREAKOUT": "Breakout", "Breakout": "Breakout",
    "POWER_EARNINGS_GAP": "Gap", "Gap": "Gap",
    "RS_LEADER": "Leader", "Leader": "Leader",
}

SECTOR_KEYWORDS = {
    ("BANK", "FIN", "CAP", "HOLD", "SEC"): "Financial Services",
    ("TECH", "SOFT", "INFO", "DIGI"): "Information Technology",
    ("PHARM", "LAB", "BIO", "HEALTH"): "Healthcare & Pharma",
    ("ENG", "IND", "STEEL", "POWER", "ELECT"): "Industrial Manufacturing",
    ("AUTO", "MOT", "TYRE", "GEAR"): "Automotive",
    ("OIL", "GAS", "PETRO", "ENERGY"): "Energy & Petrochemicals",
}

# Strategy-calibrated MFE from config benchmarks
CALIBRATED_MFE = {
    "APEX_BASE": 2.2, "Apex": 2.2,
    "HIGH_TIGHT_FLAG": 3.0, "Flag": 3.0,
    "POWER_EARNINGS_GAP": 2.8, "Gap": 2.8,
    "STAGE2_BASE": 2.4, "Breakout": 2.4, "STAGE2_BASE_BREAKOUT": 2.4,
    "RS_LEADER": 2.0, "Leader": 2.0,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_ist_today() -> str:
    """Returns today's date in IST as YYYY-MM-DD."""
    utc_now = datetime.datetime.utcnow()
    ist_now = utc_now + datetime.timedelta(hours=5, minutes=30)
    return ist_now.strftime("%Y-%m-%d")


def _resolve_sector(symbol: str) -> str:
    s_u = symbol.upper()
    for keys, sec in SECTOR_KEYWORDS.items():
        if any(k in s_u for k in keys):
            return sec
    return f"Sector_{abs(hash(symbol)) % 25}"


def _get_symbol_candle(cur_hist, symbol: str, date_str: str) -> Optional[Dict[str, Any]]:
    """Fetches a single day's candle for a symbol from stock_history.db."""
    cur_hist.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (symbol,))
    row = cur_hist.fetchone()
    if not row or not row[0]:
        return None
    try:
        candles = json.loads(row[0])
        # Search backwards (most recent first) for efficiency
        for c in reversed(candles):
            if str(c.get("t", ""))[:10] == date_str:
                return c
    except Exception:
        pass
    return None


def _get_latest_close(cur_hist, symbol: str) -> float:
    """Gets the most recent closing price for a symbol."""
    cur_hist.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (symbol,))
    row = cur_hist.fetchone()
    if not row or not row[0]:
        return 0.0
    try:
        candles = json.loads(row[0])
        if candles:
            return float(candles[-1].get("c", 0.0))
    except Exception:
        pass
    return 0.0


def _load_nifty500_regime(cur_hist, date_str: str) -> Dict[str, Any]:
    """
    Loads NIFTY500 candles and computes EMA20 + RSI14 up to date_str for Gate 5.
    Returns dict with 'above_ema20' and 'rsi14' for the given date.
    """
    import pandas as pd

    cur_hist.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", ("NIFTY500",))
    row = cur_hist.fetchone()
    if not row or not row[0]:
        return {"above_ema20": True, "rsi14": 55.0}  # Default: allow

    try:
        candles = json.loads(row[0])
        df = pd.DataFrame(candles)
        d_col = "t" if "t" in df.columns else "date"
        df["date_str"] = df[d_col].astype(str).str[:10]
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df = df.sort_values("date_str").drop_duplicates(subset=["date_str"]).reset_index(drop=True)

        # Only use data up to date_str (no lookahead)
        df = df[df["date_str"] <= date_str]
        if len(df) < 20:
            return {"above_ema20": True, "rsi14": 55.0}

        c = df["close"].astype(float)
        ema20 = c.ewm(span=20, adjust=False).mean()
        delta = c.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-6)
        rsi14 = 100.0 - (100.0 / (1.0 + rs))

        last_close = float(c.iloc[-1])
        last_ema20 = float(ema20.iloc[-1])
        last_rsi = float(rsi14.iloc[-1])

        return {
            "above_ema20": last_close > last_ema20,
            "rsi14": last_rsi,
            "close": last_close,
            "ema20": last_ema20,
        }
    except Exception as e:
        logger.warning(f"Error computing NIFTY500 regime: {e}")
        return {"above_ema20": True, "rsi14": 55.0}


def _passes_gate5(strategy_id: str, n500_state: Dict[str, Any]) -> bool:
    """
    Gate 5 (Optimized Hybrid Gate):
    - High-Beta Momentum Breakouts (Breakout, Leader, Flag) require N500 > EMA20 AND RSI14 > 50
    - Core Resilient Strategies (Apex, Gap) remain unconstrained
    """
    strat_1word = STRAT_1WORD_MAP.get(strategy_id, strategy_id)
    if strat_1word in ["Breakout", "Leader", "Flag"]:
        return bool(n500_state.get("above_ema20", True) and n500_state.get("rsi14", 55.0) > 50.0)
    return True


def _get_sector_counts(active_trades: List[Dict[str, Any]]) -> Dict[str, int]:
    """Calculates sector distribution of currently active positions."""
    counts = defaultdict(int)
    for pos in active_trades:
        sec = pos.get("sector", _resolve_sector(pos.get("symbol", "")))
        counts[sec] += 1
    return dict(counts)


# ── Core Daily Processor ──────────────────────────────────────────────────────

def process_daily(
    date_str: Optional[str] = None,
    dry_run: bool = False,
    scan_buys: bool = True,
    engine_id: str = ENGINE_ID,
    scanner: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Stateless single-day production processor.

    Reads active positions from DB, evaluates exits on today's candles,
    scans for new entries, updates DB, and pushes to Wix.

    Args:
        date_str: Trading date to process (YYYY-MM-DD). Defaults to today IST.
        dry_run: If True, simulates without modifying DB or sending notifications.
        scan_buys: If True, scans for new entry signals after processing exits.
        engine_id: Engine version identifier (default: "v2").

    Returns:
        Dict with processing results: exits, entries, NAV, etc.
    """
    date_str = date_str or _get_ist_today()
    t0 = time.time()
    storage = MasterStorage()

    logger.info("═" * 80)
    logger.info(f"🚀 DAILY PROCESSOR — {date_str} (DryRun={dry_run}, ScanBuys={scan_buys})")
    logger.info("═" * 80)

    # ── Open connections ───────────────────────────────────────────────────
    if not os.path.exists(DB_HISTORY):
        logger.error(f"stock_history.db not found at {DB_HISTORY}")
        return {"status": "ERROR", "message": "stock_history.db not found"}

    conn_hist = sqlite3.connect(DB_HISTORY)
    cur_hist = conn_hist.cursor()

    # ── Load NIFTY500 regime for Gate 5 ────────────────────────────────────
    n500_state = _load_nifty500_regime(cur_hist, date_str)
    logger.info(f"📊 NIFTY500 Regime: Above EMA20={n500_state.get('above_ema20')}, RSI14={n500_state.get('rsi14', 0):.1f}")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 1: EXIT EVALUATION
    # ════════════════════════════════════════════════════════════════════════
    active_trades = storage.get_active_trades(engine_id)
    # Only check positions that were opened BEFORE today (held overnight)
    held_prior = [p for p in active_trades if str(p.get("entry_date", "")) < date_str]

    logger.info(f"\n[PHASE 1] Checking exits on {len(held_prior)} positions held prior to {date_str}...")

    exits_today = []

    for pos in held_prior:
        sym = pos["symbol"]
        candle = _get_symbol_candle(cur_hist, sym, date_str)
        if not candle:
            logger.debug(f"  No candle found for {sym} on {date_str}")
            continue

        open_p = float(candle["o"])
        high_p = float(candle["h"])
        low_p = float(candle["l"])
        close_p = float(candle["c"])

        stop_p = float(pos.get("current_stop") or pos.get("stop_price") or 0.0)
        target_p = float(pos.get("target_price") or 0.0)
        entry_p = float(pos["entry_price"])
        shares = int(pos.get("shares", 1))

        exit_triggered = False
        exit_price = close_p
        exit_reason = "EOD_EXIT"

        # Check Stop Loss hit (Low touches or breaches stop)
        if stop_p > 0 and low_p <= stop_p:
            exit_triggered = True
            exit_reason = "STOP_HIT"
            # If opened below stop (gap down), exit at open
            exit_price = min(open_p, stop_p) if open_p < stop_p else stop_p

        # Check Target Price hit (High touches or exceeds target)
        elif target_p > 0 and high_p >= target_p:
            exit_triggered = True
            exit_reason = "TARGET_HIT"
            # If opened above target (gap up), exit at open
            exit_price = max(open_p, target_p) if open_p > target_p else target_p

        if not exit_triggered:
            continue

        # ── Calculate P&L with friction ────────────────────────────────────
        gross_pnl = round((exit_price - entry_p) * shares, 2)

        leg_out = TransactionCostModel.calculate_leg_friction(
            exit_price, shares, is_buy=False, scenario=FrictionScenario.BASE
        )
        exit_friction = round(leg_out.total_cost_cash, 2)

        # Estimate entry friction (already paid, but tracked for reporting)
        leg_in = TransactionCostModel.calculate_leg_friction(
            entry_p, shares, is_buy=True, scenario=FrictionScenario.BASE
        )
        entry_friction = round(leg_in.total_cost_cash, 2)
        total_friction = round(entry_friction + exit_friction, 2)

        net_pnl = round(gross_pnl - total_friction, 2)
        roi_pct = round((exit_price - entry_p) / entry_p * 100.0, 2)

        # Holding days
        try:
            e_dt = datetime.date.fromisoformat(pos["entry_date"])
            x_dt = datetime.date.fromisoformat(date_str)
            holding_days = (x_dt - e_dt).days
        except Exception:
            holding_days = 1

        # Risk-normalized R
        eff_risk = entry_p - stop_p if stop_p > 0 else entry_p * 0.08
        risknorm_r = round(net_pnl / (shares * eff_risk), 4) if eff_risk > 0 else 0.0
        capped_r = round(float(np.clip(risknorm_r, -1.0, 3.0)), 4)

        trade_record = {
            "trade_id": pos.get("trade_id", f"TRD_{engine_id}_{sym}_{date_str.replace('-', '')}"),
            "engine_version": engine_id,
            "symbol": sym,
            "strategy_id": STRAT_1WORD_MAP.get(pos.get("strategy_id", ""), pos.get("strategy_id", "")),
            "entry_date": pos.get("entry_date", ""),
            "exit_date": date_str,
            "entry_price": entry_p,
            "exit_price": round(exit_price, 2),
            "shares": shares,
            "invested_capital": round(entry_p * shares, 2),
            "gross_pnl": gross_pnl,
            "total_friction": total_friction,
            "net_pnl": net_pnl,
            "risknorm_r": risknorm_r,
            "capped_r": capped_r,
            "pnl_pct": roi_pct,
            "r_val": risknorm_r,
            "exit_reason": exit_reason,
            "holding_days": holding_days,
            "cash_after": 0.0,  # Updated after all exits
        }

        exits_today.append(trade_record)
        logger.info(
            f"  🔔 EXIT: {sym:12s} → {exit_reason:12s} | "
            f"Exit: ₹{exit_price:,.2f} | PnL: ₹{net_pnl:+,.2f} ({roi_pct:+.1f}%)"
        )

        if not dry_run:
            storage.close_trade(trade_record)
            try:
                from common.notifier import notify_exit
                notify_exit(trade_record, engine_version=engine_id, bar=candle)
            except Exception as e:
                logger.warning(f"  Telegram notification error for {sym}: {e}")

    logger.info(f"  → {len(exits_today)} exits processed.")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 2: NEW ENTRY SCANNING
    # ════════════════════════════════════════════════════════════════════════
    entries_today = []

    if scan_buys:
        logger.info(f"\n[PHASE 2] Scanning for new entry signals on {date_str}...")

        # Refresh active trades after exits
        current_active = storage.get_active_trades(engine_id)
        active_symbols: Set[str] = {t["symbol"] for t in current_active}

        # Initialize scanner
        if scanner is None:
            scanner = V11LiveScanner(db_path=DB_HISTORY)

        # Scan all stocks for signals on date_str
        candidates = scanner.scan(
            target_date=date_str,
            active_symbols=active_symbols,
            min_probability=0.55,
        )
        logger.info(f"  Scanner returned {len(candidates)} raw candidates.")

        # Build portfolio state for arbiter
        sector_counts = _get_sector_counts(current_active)
        invested_cap = sum(
            float(p.get("invested_capital", float(p["entry_price"]) * int(p.get("shares", 1))))
            for p in current_active
        )

        # Get latest NAV from daily_nav table for accurate sizing
        latest_nav_rec = storage.get_latest_nav(engine_id)
        current_nav = float(latest_nav_rec["total_nav"]) if latest_nav_rec else INITIAL_CAPITAL

        # Get peak equity for drawdown calculation
        peak_equity = float(latest_nav_rec.get("peak_equity", current_nav)) if latest_nav_rec else current_nav
        current_dd = round((peak_equity - current_nav) / peak_equity, 4) if peak_equity > 0 else 0.0

        p_state = PortfolioState(
            open_positions=len(current_active),
            sector_counts=sector_counts,
            nav=current_nav,
            current_drawdown_pct=current_dd,
        )

        # Get market regime for arbiter sizing
        regime = scanner.get_market_regime(date_str).get("regime", "BULL_VOLATILE")
        logger.info(f"  Market Regime: {regime} | NAV: ₹{current_nav:,.2f} | Open: {len(current_active)}")

        for cand in candidates:
            sym = cand["symbol"]
            strat_id = cand["strategy_id"]
            strat_1word = STRAT_1WORD_MAP.get(strat_id, strat_id)

            # ── Gate 5: NIFTY500 Regime Filter ─────────────────────────────
            if not _passes_gate5(strat_id, n500_state):
                logger.debug(f"  ⊘ Gate 5 blocked: {sym} ({strat_1word})")
                continue

            # ── Dedup Guard: 1 position per stock ──────────────────────────
            if sym in active_symbols:
                continue

            entry_p = float(cand.get("entry_price", 0.0))
            if entry_p <= 0:
                continue

            stop_p = float(cand.get("stop_price", entry_p * 0.935))
            score = float(cand.get("score", 0.60))
            trap_sc = max(0.05, min(0.95, 1.0 - score))
            sec = cand.get("sector", _resolve_sector(sym))

            calibrated_mfe = CALIBRATED_MFE.get(strat_id, CALIBRATED_MFE.get(strat_1word, 2.2))

            # Correlation estimate from sector concentration
            sec_count = p_state.sector_counts.get(sec, 0)
            corr_est = min(0.90, 0.15 + (sec_count * 0.20))

            risk_amt = max(1e-4, entry_p - stop_p)
            target_p = float(cand.get("target_price") or round(entry_p + (2.0 * risk_amt), 2))

            t_cand = TradeCandidate(
                symbol=sym,
                sector=sec,
                trap_score=trap_sc,
                est_mfe_r=calibrated_mfe,
                max_corr_to_book=round(corr_est, 2),
                pattern_type=strat_id,
                entry_price=entry_p,
                stop_price=stop_p,
                target_price=target_p,
            )

            decision, size_rupees, reason = arbiter_decide(
                t_cand, regime, p_state,
                max_positions=MAX_SLOTS, max_per_sector=4,
            )

            if decision != "TAKE":
                logger.debug(f"  ⊘ Arbiter SKIP: {sym} ({strat_1word}) — {reason}")
                continue

            # ── Size and create position ───────────────────────────────────
            alloc_rupees = max(CAPITAL_PER_POSITION, size_rupees)
            shares = max(1, int(alloc_rupees / entry_p))

            leg_in = TransactionCostModel.calculate_leg_friction(
                entry_p, shares, is_buy=True, scenario=FrictionScenario.BASE
            )
            entry_friction = round(leg_in.total_cost_cash, 2)

            new_pos = {
                "trade_id": f"V2_ACT_{date_str.replace('-', '')}_{sym}",
                "engine_version": engine_id,
                "symbol": sym,
                "strategy_id": strat_1word,
                "entry_date": date_str,
                "entry_price": entry_p,
                "current_stop": stop_p,
                "target_price": target_p,
                "shares": shares,
                "invested_capital": round(entry_p * shares, 2),
                "current_ltp": entry_p,
                "unrealized_pnl": 0.0,
                "entry_friction": entry_friction,
                "status": "OPEN",
            }

            entries_today.append(new_pos)
            active_symbols.add(sym)
            p_state.open_positions += 1
            p_state.sector_counts[sec] = p_state.sector_counts.get(sec, 0) + 1

            logger.info(
                f"  ✅ ENTRY: {sym:12s} ({strat_1word:8s}) | "
                f"Price: ₹{entry_p:,.2f} | Stop: ₹{stop_p:,.2f} | "
                f"Target: ₹{target_p:,.2f} | Shares: {shares} | Score: {score:.2f}"
            )

            if not dry_run:
                storage.create_active_trade(new_pos)
                try:
                    from common.notifier import notify_buy
                    notify_buy(new_pos, engine_version=engine_id)
                except Exception as e:
                    logger.warning(f"  Telegram buy notification error for {sym}: {e}")

        logger.info(f"  → {len(entries_today)} new entries created.")

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 3: MARK-TO-MARKET NAV SETTLEMENT
    # ════════════════════════════════════════════════════════════════════════
    logger.info(f"\n[PHASE 3] Settling Mark-to-Market NAV for {date_str}...")

    # Refresh active positions after all changes
    final_active = storage.get_active_trades(engine_id)

    # Calculate open market value at day's close
    open_mkt_val = 0.0
    for p in final_active:
        sym = p["symbol"]
        c = _get_symbol_candle(cur_hist, sym, date_str)
        if c:
            price = float(c["c"])
        else:
            price = float(p.get("current_ltp") or p["entry_price"])
        open_mkt_val += int(p.get("shares", 1)) * price

        # Update LTP in active trades
        if not dry_run and c:
            try:
                storage.update_active_ltps({sym: float(c["c"])})
            except Exception:
                pass

    # Running cash: get from latest NAV + today's realized P&L
    latest_nav_rec = storage.get_latest_nav(engine_id)
    if latest_nav_rec:
        cash_before = float(latest_nav_rec.get("cash", INITIAL_CAPITAL))
    else:
        cash_before = INITIAL_CAPITAL

    # Adjust cash for today's exits (add back proceeds) and entries (subtract outlays)
    exit_pnl = sum(t["net_pnl"] for t in exits_today)
    exit_proceeds = sum(
        round(float(t["exit_price"]) * int(t["shares"]), 2)
        for t in exits_today
    )
    exit_friction = sum(
        round(TransactionCostModel.calculate_leg_friction(
            float(t["exit_price"]), int(t["shares"]), is_buy=False, scenario=FrictionScenario.BASE
        ).total_cost_cash, 2)
        for t in exits_today
    )
    cash_from_exits = round(exit_proceeds - exit_friction, 2)

    entry_outlays = sum(
        round(float(e["entry_price"]) * int(e["shares"]) + float(e.get("entry_friction", 0)), 2)
        for e in entries_today
    )

    current_cash = round(cash_before + cash_from_exits - entry_outlays, 2)
    total_nav = round(current_cash + open_mkt_val, 2)

    # Peak equity and drawdown tracking
    peak_equity = max(
        total_nav,
        float(latest_nav_rec.get("peak_equity", INITIAL_CAPITAL)) if latest_nav_rec else INITIAL_CAPITAL
    )
    dd_inr = round(peak_equity - total_nav, 2)
    dd_pct = round(dd_inr / peak_equity * 100.0, 2) if peak_equity > 0 else 0.0

    nav_rec = {
        "date": date_str,
        "engine_version": engine_id,
        "cash": current_cash,
        "invested_capital": round(open_mkt_val, 2),
        "total_nav": total_nav,
        "active_positions_count": len(final_active),
        "drawdown_inr": dd_inr,
        "drawdown_pct": dd_pct,
        "peak_equity": round(peak_equity, 2),
    }

    if not dry_run:
        storage.record_daily_nav(nav_rec)

    logger.info(
        f"  📊 NAV: ₹{total_nav:,.2f} | Cash: ₹{current_cash:,.2f} | "
        f"Open Val: ₹{open_mkt_val:,.2f} | Positions: {len(final_active)} | "
        f"DD: {dd_pct:.2f}%"
    )

    # ════════════════════════════════════════════════════════════════════════
    # PHASE 4: WIX SYNC
    # ════════════════════════════════════════════════════════════════════════
    if not dry_run:
        logger.info(f"\n[PHASE 4] Syncing to Wix portal...")
        try:
            from common.wix_sync import push_to_wix
            wix_ok = push_to_wix(phase="DAILY_PROCESSOR")
            if wix_ok:
                logger.info("  📡 Successfully synced portfolio state to Wix!")
            else:
                logger.warning("  ⚠️ Wix sync returned False or was skipped.")
        except Exception as e:
            logger.warning(f"  ❌ Wix sync error: {e}")

        # Audit proof notification
        try:
            from common.notifier import notify_audit_proof
            notify_audit_proof(
                job_name=f"Daily Processor ({date_str})",
                schedule_str=f"{date_str} Settlement",
                duration_s=round(time.time() - t0, 2),
                symbols=len(final_active) + len(exits_today),
                orders=len(exits_today) + len(entries_today),
                status="SUCCESS",
            )
        except Exception as e:
            logger.warning(f"  Audit proof error: {e}")

    conn_hist.close()

    elapsed = round(time.time() - t0, 2)
    logger.info(f"\n{'═' * 80}")
    logger.info(f"✅ DAILY PROCESSOR COMPLETE — {date_str} | {elapsed}s")
    logger.info(f"   Exits: {len(exits_today)} | Entries: {len(entries_today)} | Active: {len(final_active)} | NAV: ₹{total_nav:,.2f}")
    logger.info(f"{'═' * 80}")

    return {
        "date": date_str,
        "engine_version": engine_id,
        "status": "SUCCESS",
        "exits_count": len(exits_today),
        "entries_count": len(entries_today),
        "active_count": len(final_active),
        "exits": exits_today,
        "entries": [{"symbol": e["symbol"], "strategy": e["strategy_id"], "price": e["entry_price"]} for e in entries_today],
        "nav": total_nav,
        "cash": current_cash,
        "drawdown_pct": dd_pct,
        "elapsed_seconds": elapsed,
        "dry_run": dry_run,
    }


def clear_master_db(engine_id: Optional[str] = None):
    """Wipes active_trades, closed_trades, daily_nav, and system_audit."""
    storage = MasterStorage()
    with storage._get_conn() as conn:
        cur = conn.cursor()
        if engine_id:
            cur.execute("DELETE FROM active_trades WHERE engine_version = ?", (engine_id,))
            cur.execute("DELETE FROM closed_trades WHERE engine_version = ?", (engine_id,))
            cur.execute("DELETE FROM daily_nav WHERE engine_version = ?", (engine_id,))
        else:
            cur.execute("DELETE FROM active_trades")
            cur.execute("DELETE FROM closed_trades")
            cur.execute("DELETE FROM daily_nav")
            cur.execute("DELETE FROM system_audit")
        conn.commit()
    logger.info("🧹 Master DB tables cleared successfully.")


def get_trading_days(start_date: str, end_date: str, db_path: str = DB_HISTORY) -> List[str]:
    """Retrieves chronological trading days from stock_history.db between start_date and end_date."""
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = 'NIFTY' OR symbol = 'NIFTY500' OR symbol = 'SBIN' LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return []
        candles = json.loads(row[0])
        return sorted([c["t"][:10] for c in candles if start_date <= c["t"][:10] <= end_date])
    except Exception as e:
        logger.error(f"Error fetching trading days: {e}")
        return []


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="QuantSync-V2 Daily Processor — Stateless single-day and multi-day production pipeline"
    )
    parser.add_argument("--date", type=str, help="Single trading date to process (YYYY-MM-DD).")
    parser.add_argument("--start", type=str, help="Start date for multi-day replay (YYYY-MM-DD).")
    parser.add_argument("--end", type=str, help="End date for multi-day replay (YYYY-MM-DD).")
    parser.add_argument("--clear-db", action="store_true", help="Clear active, closed trades, and NAV before running.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying DB or sending notifications.")
    parser.add_argument("--no-scan", action="store_true", help="Skip new entry scanning (exits only).")
    args = parser.parse_args()

    if args.clear_db:
        clear_master_db(engine_id=ENGINE_ID)

    if args.start and args.end:
        days = get_trading_days(args.start, args.end)
        logger.info(f"🚀 Starting multi-day replay from {args.start} to {args.end} ({len(days)} trading days)...")
        shared_scanner = V11LiveScanner(db_path=DB_HISTORY)
        total_exits = 0
        total_entries = 0
        for idx, dt in enumerate(days, 1):
            logger.info(f"\n[{idx}/{len(days)}] Processing date: {dt}")
            res = process_daily(
                date_str=dt,
                dry_run=args.dry_run,
                scan_buys=(not args.no_scan),
                scanner=shared_scanner,
            )
            total_exits += res.get("exits_count", 0)
            total_entries += res.get("entries_count", 0)

        logger.info(f"\n🎉 MULTI-DAY REPLAY COMPLETE: {len(days)} days processed | Total Exits: {total_exits} | Total Entries: {total_entries}")
    else:
        target_date = args.date or _get_ist_today()
        result = process_daily(
            date_str=target_date,
            dry_run=args.dry_run,
            scan_buys=(not args.no_scan),
        )
        print(f"\n📋 Result: {json.dumps(result, indent=2, default=str)}")
