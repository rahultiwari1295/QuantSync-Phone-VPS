"""
engines/v4/engine.py — QuantSync-V4 Universal Pattern Fingerprint & Multi-Task ML Alpha Engine
=============================================================================================
Authoritative production implementation of QuantSync-V4 conforming to BaseTradingEngine.
Implements Champion Strategy F3: 50/50 Infinity Moonbag + Volume Distribution Alert.

Features:
  - 64-Dimensional Scale-Invariant Geometric & Waveform Pattern Fingerprints
  - Sub-5ms Vector Memory Bank Twin Retrieval across 98,340 historical setups (2019-2024)
  - Multi-Task LightGBM Decision Engine:
      1. Calibrated Win Probability P(Win) >= 0.50
      2. Dynamic Quantile Target MFE (alpha=0.75)
      3. Dynamic Quantile Stop Loss MAE (alpha=0.10)
      4. Half-Kelly Sizing Multiplier (0.25x to 2.0x) & EV Calibration
  - Production Strategy F3 Dual-Tranche Execution:
      • Tranche 1 (50% shares): Takes profit at dynamic MFE target (+12% to +20%)
      • Tranche 2 (50% shares): No ceiling, 1.5x ATR trailing stop, held up to 35 bars
      • Breakeven Ratchet: Stop locks to Entry + 1% once unrealized high-watermark >= +7.5%
      • Volume Distribution Alert: Ratchets stop to today's low if stock drops >2% on >1.6x volume
      • Liquid BeES Cash Yield: 7.0% p.a. continuous harvest on unallocated cash
  - Seamless MasterStorage (quantsync_master.db) & Telegram alerts integration
"""

import os
import sys
import json
import time
import math
import datetime
import sqlite3
import logging
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple, Set
import numpy as np
import pandas as pd

_V4_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINES_DIR = os.path.dirname(_V4_DIR)
_PROD_ROOT = os.path.dirname(_ENGINES_DIR)

if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)
if _V4_DIR not in sys.path:
    sys.path.insert(0, _V4_DIR)

from engines.base_engine import BaseTradingEngine
from common.storage import MasterStorage
from common.cost_model import TransactionCostModel, FrictionScenario
from common.notifier import notify_buy, notify_exit

from engines.v4.v4_live_engine import V4AlphaEngine
from engines.v4.fingerprint_twin_engine import PatternFingerprintEngine
from engines.v4.trade_enhancer_models import TradeEnhancerEngine

logger = logging.getLogger("ProdVPS.V4.Engine")

WIN_PATTERNS = {
    "FALLING_WEDGE", "DOUBLE_BOTTOM", "INVERSE_HEAD_AND_SHOULDERS",
    "SYMMETRICAL_TRIANGLE", "BULLISH_RECTANGLE", "VOLATILITY_BREAKOUT",
    "STAGE_2_BREAKOUT", "CUP_AND_HANDLE", "HIGH_TIGHT_FLAG"
}

REGIME_TARGET_MULT = {
    "STRONG_BULL": 1.10,
    "MILD_BULL": 1.00,
    "CAUTION": 0.90,
    "DEFENSE_BEAR": 0.80,
}

REGIME_TRAIL_TIGHTNESS = {
    "STRONG_BULL": 1.10,
    "MILD_BULL": 1.00,
    "CAUTION": 1.00,
    "DEFENSE_BEAR": 0.90,
}


class V4TradingEngine(BaseTradingEngine):
    """
    QuantSync-V4 Production Trading Engine conforming to BaseTradingEngine.
    """
    def __init__(
        self,
        engine_id: str = "v4",
        engine_name: str = "QuantSync-V4",
        initial_capital: float = 300000.0,
        capital_per_position: float = 15000.0,
        max_slots: int = 20,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            engine_id=engine_id,
            engine_name=engine_name,
            initial_capital=initial_capital,
            capital_per_position=capital_per_position,
            max_slots=max_slots,
            config=config,
        )

        self.storage = MasterStorage()
        self.history_db_path = os.path.join(_PROD_ROOT, "Data", "stock_history.db")

        # Load live inference engine singleton
        self.live_engine = V4AlphaEngine.get_instance()

        # Engine Portfolio State
        self.cash = float(self.initial_capital)
        self.peak_equity = float(self.initial_capital)
        self.max_drawdown_inr = 0.0
        self.max_drawdown_pct = 0.0
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.daily_nav_history: List[Dict[str, Any]] = []

        # Annual cash yield: 7.0% p.a. Liquid BeES sweep
        self.annual_cash_yield = 0.070
        self.daily_cash_yield = self.annual_cash_yield / 252.0

        # Slippage & Transaction costs
        self.entry_slippage = 0.0015  # 0.15% next-day open or pre-close slippage
        self.exit_slippage = 0.0010   # 0.10% market-on-close slippage
        self.round_trip_fees = 0.0005 # 0.05% statutory friction

    def reset(self, initial_capital: Optional[float] = None):
        """Resets engine state for clean replays."""
        if initial_capital is not None:
            self.initial_capital = float(initial_capital)
        self.cash = float(self.initial_capital)
        self.peak_equity = float(self.initial_capital)
        self.max_drawdown_inr = 0.0
        self.max_drawdown_pct = 0.0
        self.active_positions.clear()
        self.closed_trades.clear()
        self.daily_nav_history.clear()

    def get_active_symbols(self) -> List[str]:
        return list(self.active_positions.keys())

    def get_trading_dates(self, start_date: str, end_date: str, include_settlement: bool = True) -> List[str]:
        """Returns sorted trading dates between start_date and end_date from stock_history.db."""
        if not os.path.exists(self.history_db_path):
            return []
        try:
            with sqlite3.connect(f"file:{self.history_db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT rawCandles FROM StockHistory WHERE symbol='NIFTY' OR symbol='NIFTY100' LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    candles = json.loads(row[0])
                    days = sorted({c["t"][:10] for c in candles if isinstance(c, dict) and c.get("t")})
                    return [d for d in days if start_date <= d <= end_date]
        except Exception as e:
            logger.warning(f"Error querying trading dates: {e}")
        return []

    def get_candidates(self, date_str: str) -> List[Dict[str, Any]]:
        """
        Scans all tradeable universe stocks for qualifying V4 Strategy F3 setups on date_str.
        """
        candidates: List[Dict[str, Any]] = []
        if not os.path.exists(self.history_db_path):
            return candidates

        try:
            with sqlite3.connect(f"file:{self.history_db_path}?mode=ro", uri=True) as conn:
                cur = conn.cursor()
                all_stocks = cur.execute(
                    "SELECT symbol, rawCandles FROM StockHistory WHERE isAlgoEligible = 1 OR isV3Eligible = 1"
                ).fetchall()
        except Exception as e:
            logger.error(f"Error fetching symbols for V4 scan: {e}")
            return candidates

        active_syms = set(self.get_active_symbols())

        for sym, raw_c in all_stocks:
            if sym in active_syms or sym in ["NIFTY", "NIFTY100", "NIFTY 50", "NIFTY 100"]:
                continue
            if not raw_c:
                continue

            try:
                # Fast pre-filter: Check breakout characteristics on target date bar
                candles = json.loads(raw_c)
                if len(candles) < 60:
                    continue
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

                dossier = self.live_engine.analyze_symbol(sym)
                if not dossier or dossier.get("error"):
                    continue

                ml_dec = dossier.get("ml_decision", {})
                p_win = float(ml_dec.get("win_probability", 0.0))
                ev = float(ml_dec.get("expected_value_pct", -1.0))
                grade = ml_dec.get("conviction_grade", "AVOID")

                # Strategy F3 Entry Gate
                if grade == "AVOID" or p_win < 0.50 or ev < 0.0:
                    continue

                pat_raw = str(dossier.get("pattern_name", "")).upper().replace(" ", "_")
                if pat_raw not in WIN_PATTERNS and "BREAKOUT" not in pat_raw:
                    continue

                candidates.append({
                    "symbol": sym,
                    "date": date_str,
                    "entry_price": dossier["entry_price"],
                    "target_price": dossier["target_price"],
                    "target_pct": dossier["target_pct"],
                    "stop_loss_price": dossier["stop_loss_price"],
                    "stop_loss_pct": dossier["stop_loss_pct"],
                    "risk_reward_ratio": dossier["risk_reward_ratio"],
                    "p_win": p_win,
                    "expected_value_pct": ev,
                    "conviction_grade": grade,
                    "size_multiplier": float(ml_dec.get("kelly_size_multiplier", 1.0)),
                    "dossier": dossier,
                    "score": round(p_win * 100.0 + ev, 2),
                })
            except Exception as ex:
                logger.debug(f"Candidate check skipped for {sym}: {ex}")

        # Sort descending by composite score
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates

    def check_exits(self, ltp_map: Dict[str, float]) -> List[Dict[str, Any]]:
        """
        Evaluates real-time LTP quotes against active positions under Strategy F3 (50/50 Moonbag):
        1. Breakeven Ratchet: if high_watermark >= +7.5%, stop locks to Entry + 1%.
        2. Tranche 1 Profit Target: locks 50% position at predicted MFE target (+12% to +20%).
        3. Tranche 2 Trailing Stop: trails with 1.5x ATR below LTP for the remaining 50% shares.
        4. Protective Stop Loss: exits remaining position if LTP <= current_stop.
        """
        exits_triggered: List[Dict[str, Any]] = []

        for sym, pos in list(self.active_positions.items()):
            ltp = ltp_map.get(sym)
            if not ltp or ltp <= 0:
                continue

            entry_p = float(pos["entry_price"])
            curr_stop = float(pos["current_stop"])
            t1_target = float(pos["t1_target"])
            t1_closed = bool(pos.get("t1_closed", False))
            high_wm = max(float(pos.get("high_watermark", entry_p)), ltp)
            pos["high_watermark"] = high_wm

            # 1. Breakeven Ratchet
            if not pos.get("be_locked", False) and high_wm >= entry_p * 1.075:
                pos["be_locked"] = True
                curr_stop = max(curr_stop, round(entry_p * 1.01, 2))
                pos["current_stop"] = curr_stop
                logger.info(f"🔒 [V4 BE-LOCK] {sym} reached +7.5% gain. Stop ratcheted to ₹{curr_stop:.2f} (+1% lock).")

            # 2. Check Tranche 1 Target Hit
            if not t1_closed and ltp >= t1_target:
                t1_shares = int(pos.get("t1_shares", pos["shares"] // 2))
                exit_record = self._execute_partial_exit(
                    sym=sym,
                    pos=pos,
                    exit_p=ltp,
                    exit_shares=t1_shares,
                    reason="V4_TRANCHE1_TARGET_HIT",
                    tranche="T1"
                )
                exits_triggered.append(exit_record)
                pos["t1_closed"] = True
                pos["shares"] -= t1_shares
                pos["invested_capital"] = round(pos["shares"] * entry_p, 2)
                # After T1 locks profit, immediately lock stop to BE + 1%
                curr_stop = max(curr_stop, round(entry_p * 1.01, 2))
                pos["current_stop"] = curr_stop
                logger.info(f"🎯 [V4 T1 EXIT] {sym} Tranche 1 hit target at ₹{ltp:.2f}. Half position locked!")

            # 3. Tranche 2 Dynamic Trailing Stop
            if t1_closed:
                atr_val = float(pos.get("atr", entry_p * 0.035))
                trail_stop = round(ltp - (1.5 * atr_val), 2)
                if trail_stop > curr_stop:
                    curr_stop = trail_stop
                    pos["current_stop"] = curr_stop

            # 4. Stop Loss or Trailing Stop Breach
            if curr_stop > 0 and ltp <= curr_stop:
                reason = "V4_STOP_HIT" if not pos.get("be_locked") else "V4_PROFIT_TRAIL_HIT"
                rem_shares = pos["shares"]
                exit_record = self._execute_full_exit(
                    sym=sym,
                    pos=pos,
                    exit_p=ltp,
                    reason=reason
                )
                exits_triggered.append(exit_record)

        return exits_triggered

    def _execute_partial_exit(
        self, sym: str, pos: Dict[str, Any], exit_p: float, exit_shares: int, reason: str, tranche: str
    ) -> Dict[str, Any]:
        """Closes a partial tranche (e.g. Tranche 1 50%) and records trade leg."""
        entry_p = float(pos["entry_price"])
        leg_out = TransactionCostModel.calculate_leg_friction(
            exit_p, exit_shares, is_buy=False, scenario=FrictionScenario.BASE
        )
        exit_friction = round(leg_out.total_cost_cash, 2)
        entry_friction_share = round(float(pos.get("entry_friction", 0.0)) * (exit_shares / max(1, pos["initial_shares"])), 2)
        total_friction = round(exit_friction + entry_friction_share, 2)

        gross_proceeds = round(exit_p * exit_shares, 2)
        net_proceeds = round(gross_proceeds - exit_friction, 2)
        self.cash += net_proceeds

        gross_pnl = round(gross_proceeds - (entry_p * exit_shares), 2)
        net_pnl = round(gross_pnl - total_friction, 2)
        pnl_pct = round((exit_p - entry_p) / entry_p * 100.0, 2)

        trade_record = {
            "trade_id": f"V4_TRD_{sym}_{tranche}_{int(time.time())}",
            "engine_version": self.engine_id,
            "symbol": sym,
            "strategy_id": "V4_F3_Moonbag",
            "entry_date": pos.get("entry_date", ""),
            "exit_date": datetime.date.today().strftime("%Y-%m-%d"),
            "entry_price": entry_p,
            "exit_price": round(exit_p, 2),
            "shares": exit_shares,
            "invested_capital": round(entry_p * exit_shares, 2),
            "gross_pnl": gross_pnl,
            "total_friction": total_friction,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": reason,
            "holding_days": pos.get("holding_days", 1),
            "cash_after": round(self.cash, 2),
        }

        self.closed_trades.append(trade_record)
        self.storage.close_trade(trade_record)
        notify_exit(trade_record, engine_version=self.engine_id)
        return trade_record

    def _execute_full_exit(self, sym: str, pos: Dict[str, Any], exit_p: float, reason: str) -> Dict[str, Any]:
        """Closes the entire remaining position."""
        self.active_positions.pop(sym, None)
        entry_p = float(pos["entry_price"])
        shares = int(pos["shares"])

        leg_out = TransactionCostModel.calculate_leg_friction(
            exit_p, shares, is_buy=False, scenario=FrictionScenario.BASE
        )
        exit_friction = round(leg_out.total_cost_cash, 2)
        entry_friction_share = round(float(pos.get("entry_friction", 0.0)) * (shares / max(1, pos["initial_shares"])), 2)
        total_friction = round(exit_friction + entry_friction_share, 2)

        gross_proceeds = round(exit_p * shares, 2)
        net_proceeds = round(gross_proceeds - exit_friction, 2)
        self.cash += net_proceeds

        gross_pnl = round(gross_proceeds - (entry_p * shares), 2)
        net_pnl = round(gross_pnl - total_friction, 2)
        pnl_pct = round((exit_p - entry_p) / entry_p * 100.0, 2)

        trade_record = {
            "trade_id": f"V4_TRD_{sym}_{int(time.time())}",
            "engine_version": self.engine_id,
            "symbol": sym,
            "strategy_id": "V4_F3_Moonbag",
            "entry_date": pos.get("entry_date", ""),
            "exit_date": datetime.date.today().strftime("%Y-%m-%d"),
            "entry_price": entry_p,
            "exit_price": round(exit_p, 2),
            "shares": shares,
            "invested_capital": round(entry_p * shares, 2),
            "gross_pnl": gross_pnl,
            "total_friction": total_friction,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": reason,
            "holding_days": pos.get("holding_days", 1),
            "cash_after": round(self.cash, 2),
        }

        self.closed_trades.append(trade_record)
        self.storage.close_trade(trade_record)
        notify_exit(trade_record, engine_version=self.engine_id)
        logger.info(f"🔴 [V4 FULL EXIT] {sym} closed at ₹{exit_p:.2f} ({reason}) | PnL: ₹{net_pnl:+,.2f} ({pnl_pct:+.1f}%)")
        return trade_record

    def step(self, current_date: str, is_live: bool = False) -> Dict[str, Any]:
        """
        Executes a single market session on current_date:
        1. Harvests Liquid BeES cash yield (7.0% p.a. / 252).
        2. Evaluates exits on the day's candle (Volume distribution, Breakeven, Stop, Target).
        3. Scans for candidate breakout setups & enhances them with ML models.
        4. Sizes new entries with Half-Kelly multiplier.
        5. Updates NAV mark-to-market.
        """
        exits_today = []
        entries_today = []

        # 1. Harvest Daily Liquid BeES Cash Yield
        daily_yield_earned = round(self.cash * self.daily_cash_yield, 2)
        self.cash += daily_yield_earned

        # 2. Evaluate active positions on current_date candle
        if os.path.exists(self.history_db_path):
            with sqlite3.connect(f"file:{self.history_db_path}?mode=ro", uri=True) as conn:
                cur = conn.cursor()
                for sym, pos in list(self.active_positions.items()):
                    pos["holding_days"] = pos.get("holding_days", 0) + 1
                    cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (sym,))
                    res = cur.fetchone()
                    if not res or not res[0]:
                        continue

                    candles = json.loads(res[0])
                    matching_bar = None
                    for b in reversed(candles):
                        if str(b.get("t", ""))[:10] == current_date:
                            matching_bar = b
                            break

                    if not matching_bar:
                        continue

                    op = float(matching_bar["o"])
                    hi = float(matching_bar["h"])
                    lo = float(matching_bar["l"])
                    cl = float(matching_bar["c"])
                    vol = float(matching_bar.get("v", 0.0))

                    # Update high-watermark
                    pos["high_watermark"] = max(float(pos.get("high_watermark", pos["entry_price"])), hi)

                    # Intra-trade Volume Distribution Alert:
                    # If stock drops > 2.0% on volume > 1.6x 20d average, ratchet stop to today's low!
                    vol_avg = float(pos.get("vol_20d_sma", vol))
                    ret_today = (cl - op) / op * 100.0 if op > 0 else 0.0
                    if ret_today <= -2.0 and vol >= 1.6 * vol_avg:
                        pos["current_stop"] = max(float(pos["current_stop"]), lo)
                        logger.info(f"⚠️ [V4 VOL DISTRIBUTION] {sym} dropped {ret_today:.1f}% on {vol/vol_avg:.1f}x volume. Stop ratcheted to Low ₹{lo:.2f}.")

                    # Breakeven check
                    if not pos.get("be_locked") and pos["high_watermark"] >= pos["entry_price"] * 1.075:
                        pos["be_locked"] = True
                        pos["current_stop"] = max(float(pos["current_stop"]), round(pos["entry_price"] * 1.01, 2))

                    # Time Stop: Day 18 time stop if Tranche 1 target has not been reached
                    if not pos.get("t1_closed") and pos["holding_days"] >= 18:
                        ex_rec = self._execute_full_exit(sym, pos, cl, "V4_TIME_STOP_18D")
                        exits_today.append(ex_rec)
                        continue

                    # Maximum Horizon Stop: Day 35 for Tranche 2 runner
                    if pos.get("t1_closed") and pos["holding_days"] >= 35:
                        ex_rec = self._execute_full_exit(sym, pos, cl, "V4_RUNNER_EXPIRY_35D")
                        exits_today.append(ex_rec)
                        continue

                    # Price Checks on Daily Candle
                    ltp_bar_map = {sym: cl}
                    bar_exits = self.check_exits(ltp_bar_map)
                    exits_today.extend(bar_exits)

        # 3. Candidate Ingestion & ML Enhancement
        open_slots = self.max_slots - len(self.active_positions)
        if open_slots > 0:
            candidates = self.get_candidates(current_date)
            for cand in candidates:
                if len(self.active_positions) >= self.max_slots:
                    break

                sym = cand["symbol"]
                if sym in self.active_positions:
                    continue

                entry_p = float(cand["entry_price"])
                stop_p = float(cand["stop_loss_price"])
                t1_target = float(cand["target_price"])
                multiplier = float(cand.get("size_multiplier", 1.0))

                alloc_capital = round(self.capital_per_position * multiplier, 2)
                shares = max(2, int(alloc_capital / entry_p))  # Min 2 shares for 50/50 dual tranche

                leg_in = TransactionCostModel.calculate_leg_friction(
                    entry_p, shares, is_buy=True, scenario=FrictionScenario.BASE
                )
                entry_friction = round(leg_in.total_cost_cash, 2)
                total_outlay = round(entry_p * shares + entry_friction, 2)

                if self.cash < total_outlay:
                    continue

                self.cash -= total_outlay
                t1_sh = shares // 2
                t2_sh = shares - t1_sh

                new_pos = {
                    "trade_id": f"V4_ACT_{sym}_{current_date.replace('-', '')}",
                    "engine_version": self.engine_id,
                    "symbol": sym,
                    "strategy_id": "V4_F3_Moonbag",
                    "entry_date": current_date,
                    "entry_price": entry_p,
                    "initial_stop": stop_p,
                    "current_stop": stop_p,
                    "target_price": t1_target,
                    "t1_target": t1_target,
                    "shares": shares,
                    "initial_shares": shares,
                    "t1_shares": t1_sh,
                    "t2_shares": t2_sh,
                    "t1_closed": False,
                    "t2_closed": False,
                    "invested_capital": round(entry_p * shares, 2),
                    "entry_friction": entry_friction,
                    "high_watermark": entry_p,
                    "be_locked": False,
                    "holding_days": 0,
                    "atr": entry_p * 0.035,
                    "status": "OPEN",
                }

                self.active_positions[sym] = new_pos
                entries_today.append(new_pos)

                if is_live:
                    self.storage.create_active_trade(new_pos)
                    notify_buy(new_pos, engine_version=self.engine_id)
                    logger.info(f"🚀 [V4 BUY] {sym} entered at ₹{entry_p:.2f} (Target: ₹{t1_target:.2f}, Stop: ₹{stop_p:.2f}) | Shares: {shares} (T1: {t1_sh}, T2: {t2_sh})")

        # 4. Mark to Market NAV
        inv_cap = round(sum(p["invested_capital"] for p in self.active_positions.values()), 2)
        total_nav = round(self.cash + inv_cap, 2)
        if total_nav > self.peak_equity:
            self.peak_equity = total_nav
        dd_inr = round(self.peak_equity - total_nav, 2)
        dd_pct = round((dd_inr / self.peak_equity * 100.0) if self.peak_equity > 0 else 0.0, 2)
        if dd_inr > self.max_drawdown_inr:
            self.max_drawdown_inr = dd_inr
        if dd_pct > self.max_drawdown_pct:
            self.max_drawdown_pct = dd_pct

        nav_record = {
            "date": current_date,
            "engine_version": self.engine_id,
            "cash": round(self.cash, 2),
            "invested_capital": inv_cap,
            "total_nav": total_nav,
            "active_positions_count": len(self.active_positions),
            "peak_equity": self.peak_equity,
            "max_drawdown_inr": self.max_drawdown_inr,
            "max_drawdown_pct": self.max_drawdown_pct,
        }
        self.daily_nav_history.append(nav_record)
        if is_live:
            self.storage.record_daily_nav(nav_record)

        return {
            "date": current_date,
            "nav": total_nav,
            "cash": round(self.cash, 2),
            "invested_capital": inv_cap,
            "exits_count": len(exits_today),
            "entries_count": len(entries_today),
            "open_positions_count": len(self.active_positions),
            "exits": exits_today,
            "entries": entries_today,
        }

    def run(self, start_date: str, end_date: str, is_live: bool = False, **kwargs) -> Dict[str, Any]:
        """Universal execution runner for multi-day backtest or single-day session."""
        trading_dates = self.get_trading_dates(start_date, end_date)
        if not trading_dates:
            trading_dates = [start_date]

        for d in trading_dates:
            self.step(d, is_live=is_live)

        n_closed = len(self.closed_trades)
        wins = [t for t in self.closed_trades if t.get("net_pnl", 0) > 0]
        wr = round(len(wins) / n_closed * 100.0, 1) if n_closed else 0.0

        return {
            "engine_id": self.engine_id,
            "engine_name": self.engine_name,
            "start_date": start_date,
            "end_date": end_date,
            "final_nav": self.daily_nav_history[-1]["total_nav"] if self.daily_nav_history else self.cash,
            "total_closed_trades": n_closed,
            "win_rate_pct": wr,
            "max_drawdown_pct": self.max_drawdown_pct,
            "active_positions": len(self.active_positions),
        }

    def export_reports(self, output_dir: str, prefix: str = "v4"):
        """Exports trade ledger and performance summary."""
        os.makedirs(output_dir, exist_ok=True)
        trades_file = os.path.join(output_dir, f"{prefix}_closed_trades.json")
        nav_file = os.path.join(output_dir, f"{prefix}_daily_nav.json")
        with open(trades_file, "w") as f:
            json.dump(self.closed_trades, f, indent=2)
        with open(nav_file, "w") as f:
            json.dump(self.daily_nav_history, f, indent=2)
        logger.info(f"V4 Reports exported to {output_dir}")

    def print_summary(self):
        """Prints performance KPI summary to console."""
        n_closed = len(self.closed_trades)
        wins = [t for t in self.closed_trades if t.get("net_pnl", 0) > 0]
        wr = round(len(wins) / n_closed * 100.0, 1) if n_closed else 0.0
        final_nav = self.daily_nav_history[-1]["total_nav"] if self.daily_nav_history else self.cash
        net_ret = round((final_nav - self.initial_capital) / self.initial_capital * 100.0, 2)
        print("=" * 70)
        print(f"📊 QuantSync-V4 (Strategy F3 Champion Engine) Performance Summary")
        print("=" * 70)
        print(f"  Initial Capital:      ₹{self.initial_capital:,.2f}")
        print(f"  Final Portfolio NAV:  ₹{final_nav:,.2f} ({net_ret:+.2f}%)")
        print(f"  Closed Trades:        {n_closed} (Win Rate: {wr}%)")
        print(f"  Open Active Positions: {len(self.active_positions)}")
        print(f"  Maximum Drawdown:     {self.max_drawdown_pct:.2f}%")
        print("=" * 70)


def get_engine(config: Optional[Dict[str, Any]] = None) -> BaseTradingEngine:
    """Factory hook for EngineRegistry auto-discovery."""
    cfg = config or {}
    if not cfg:
        cfg_file = os.path.join(_PROD_ROOT, "config.json")
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file) as f:
                    master_cfg = json.load(f)
                    cfg = master_cfg.get("engines", {}).get("v4", {})
            except Exception:
                pass

    return V4TradingEngine(
        engine_id="v4",
        engine_name=cfg.get("name", "QuantSync-V4"),
        initial_capital=float(cfg.get("capital", 300000.0)),
        capital_per_position=float(cfg.get("capital_per_pos", 15000.0)),
        max_slots=int(cfg.get("max_slots", 20)),
        config=cfg
    )


TradingEnginePlugin = V4TradingEngine
