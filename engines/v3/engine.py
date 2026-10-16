"""
engines/v3/engine.py — QuantSync-V3 Trend-Based Fibonacci & Champion Breakout Engine
===================================================================================
Authoritative production implementation of QuantSync-V3 conforming to BaseTradingEngine.
Features:
  - Weekly Institutional Swing Discovery: A (Swing Low) -> B (Swing High) -> C (Pullback Low)
  - Multi-tier Fibonacci Extensions: 1.000 (Breakout Retest), 1.272 (Target 1), 1.618 (Major Target)
  - 4 Institutional Champion Setups:
      1. High-Tight Bull Flag (HTF)
      2. Stage 2 Base Breakout (BASE)
      3. 52-Week High Ultra (52W_HIGH)
      4. Nicolas Darvas Box Ultra (DARVAS)
  - Single Source of Truth for Data: Reads directly from Data/stock_history.db (1,318 symbols)
  - Single Source of Truth for State: Writes active & closed trades to MasterStorage (quantsync_master.db)
  - Complete Structural Geometry: Exports pivot, floor, stop, target, and 4-tier Fib lines for UI charts
"""

import os
import sys
import json
import time
import math
import datetime
import sqlite3
import logging
from bisect import bisect_right
from collections import defaultdict
from typing import Dict, List, Any, Optional, Tuple
import numpy as np
import pandas as pd

_V3_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINES_DIR = os.path.dirname(_V3_DIR)
_PROD_ROOT = os.path.dirname(_ENGINES_DIR)

if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)
if _V3_DIR not in sys.path:
    sys.path.insert(0, _V3_DIR)

from engines.base_engine import BaseTradingEngine
from common.storage import MasterStorage
from common.cost_model import TransactionCostModel, FrictionScenario
from common.notifier import notify_buy, notify_exit

from engines.v3.fibonacci import (
    SwingDetector,
    calculate_trend_fibonacci,
    aggregate_to_weekly,
    FibStructure,
    SwingPoint
)
from engines.v3.patterns import detect_all_champion_patterns, ChampionSetup
try:
    from engines.v3.strategies import rs_leader_refined_live
except ImportError:
    rs_leader_refined_live = None

logger = logging.getLogger("ProdVPS.V3.Engine")


class V3TradingEngine(BaseTradingEngine):
    """
    QuantSync-V3 Production Trading Engine.
    Executes weekly Fibonacci swing structure discovery and daily champion breakout detection
    governed by the September 18th Frozen Quality Guards Framework.
    """
    def __init__(
        self,
        engine_id: str = "v3",
        engine_name: str = "QuantSync-V3",
        initial_capital: float = 300000.0,
        capital_per_position: float = 12000.0,
        max_slots: int = 25,
        config: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            engine_id=engine_id,
            engine_name=engine_name,
            initial_capital=initial_capital,
            capital_per_position=capital_per_position,
            max_slots=max_slots,
            config=config
        )

        self.storage = MasterStorage()
        self.history_db_path = os.path.join(_PROD_ROOT, "Data", "stock_history.db")

        # Frozen Quality Guards Specification (Baseline: state_20260918_v6_quality_guards)
        cfg = self.config or {}
        if not cfg and os.path.exists(os.path.join(_PROD_ROOT, "config.json")):
            try:
                with open(os.path.join(_PROD_ROOT, "config.json")) as f:
                    master_cfg = json.load(f)
                    cfg = master_cfg.get("engines", {}).get("v3", {})
            except Exception:
                pass

        default_qg = {
            "max_price_deviation_pct": 0.03,
            "min_daily_turnover": 15000000.0,
            "max_daily_turnover": 2500000000.0,
            "min_volume_ratio": 2.50,
            "min_close_in_range": 0.85,
            "max_upper_wick_pct": 0.10,
            "min_bar_return_pct": 4.0,
            "min_chase_pct": 1.5,
            "max_chase_pct": 8.0,
            "min_prior_runup_60_pct": 15.0,
            "max_ath_dist_pct": 20.0,
            "max_dist_sma200_pct": 30.0,
            "min_stop_distance_pct": 8.0,
            "min_turnover_cr": 1.5,
            "daily_selection_mode": "TOP_2_CONVICTION_RANKED"
        }
        default_ds = {
            "max_takes": 2,
            "min_score": 0.0
        }
        self.quality_guards = dict(default_qg)
        if "quality_guards" in cfg:
            self.quality_guards.update(cfg["quality_guards"])
        self.daily_selection = dict(default_ds)
        if "daily_selection" in cfg:
            self.daily_selection.update(cfg["daily_selection"])

        self.swing_detector = SwingDetector(
            pivot_left_bars=3,
            pivot_right_bars=2,
            min_impulse_pct=20.0,
            min_swing_distance=2,
            min_pullback_pct=15.0,
            max_pullback_pct=95.0
        )

        # Banana ML Engine v2 Integration
        self.ml_engine = None
        try:
            from engines.v3.banana_engine.core.ml_engine_v2 import MLEngineV2
            self.ml_engine = MLEngineV2()
            logger.info("✅ V3 Banana ML Engine v2 loaded with pattern models & regressors.")
        except Exception as e:
            logger.warning(f"V3 Banana ML Engine v2 init notice: {e}")

        self.cash = float(self.initial_capital)
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.daily_nav_history: List[Dict[str, Any]] = []
        self._universe_daily: Dict[str, pd.DataFrame] = {}
        self._universe_weekly: Dict[str, pd.DataFrame] = {}
        self._dates_list: Dict[str, List[str]] = {}
        self._w_dates_list: Dict[str, List[str]] = {}
        self._date_to_close: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._data_loaded = False

    def load_universe(self, symbols: Optional[List[str]] = None):
        """Loads daily candles from StockHistory and prepares weekly aggregations."""
        if self._data_loaded and not symbols:
            return

        if not os.path.exists(self.history_db_path):
            logger.error(f"Stock history DB not found at: {self.history_db_path}")
            return

        conn = sqlite3.connect(f"file:{os.path.abspath(self.history_db_path)}?mode=ro", uri=True)
        cur = conn.cursor()

        query = "SELECT symbol, rawCandles FROM StockHistory"
        params = []
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            query += f" WHERE symbol IN ({placeholders})"
            params.extend(symbols)

        cur.execute(query, params)
        rows = cur.fetchall()
        conn.close()

        for sym, raw_json in rows:
            if not raw_json:
                continue
            try:
                candles = json.loads(raw_json)
                if not candles or len(candles) < 30:
                    continue
                df = pd.DataFrame(candles)
                df.rename(columns={'t': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
                df = df[(df['open'] > 0) & (df['high'] > 0) & (df['low'] > 0) & (df['close'] > 0)]
                df.sort_values('date', inplace=True)
                df.reset_index(drop=True, inplace=True)

                self._universe_daily[sym] = df
                d_strs = df['date'].astype(str).str[:10].tolist()
                self._dates_list[sym] = d_strs
                closes = df['close'].values
                for dt_s, cl_val in zip(d_strs, closes):
                    self._date_to_close[dt_s][sym] = float(cl_val)

                w_df = aggregate_to_weekly(df)
                self._universe_weekly[sym] = w_df
                self._w_dates_list[sym] = w_df['date'].astype(str).str[:10].tolist() if w_df is not None else []
            except Exception as e:
                logger.debug(f"Error loading candles for {sym}: {e}")

        self._data_loaded = True
        logger.info(f"Loaded {len(self._universe_daily)} symbols into QuantSync-V3 universe.")

    def get_trading_dates(self, start_date: str, end_date: str, include_settlement: bool = True) -> List[str]:
        if not self._data_loaded:
            self.load_universe()
        all_dates = set()
        for df in self._universe_daily.values():
            dates = df['date'].astype(str).str[:10].values
            for d in dates:
                if start_date <= d <= end_date:
                    all_dates.add(d)
        return sorted(list(all_dates))

    def get_active_symbols(self) -> List[str]:
        return list(self.active_positions.keys())

    def scan_date(self, target_date: str) -> List[Dict[str, Any]]:
        """
        Scans universe for candidate setups triggering on or just before target_date.
        Enforces 2026-09-18 Frozen Quality Guards:
          - Daily Turnover: ₹1.5 Cr to ₹250 Cr
          - Bar Return Expansion: >= 4.0%
          - Volume Surge Ratio: >= 2.50x
          - Close-in-Range (CIR): >= 0.85
          - Upper Wick: <= 0.10
          - Prior 60-day Runup: >= 15.0%
          - Max ATH Distance: <= 20.0%
          - Distance from SMA200: <= 30.0%
          - Min Stop Distance: >= 8.0%
          - Selection: TOP_2_CONVICTION_RANKED (Max 2 takes per day)
        """
        if not self._data_loaded:
            self.load_universe()

        qg = self.quality_guards
        min_turnover = float(qg.get("min_daily_turnover", 15000000.0))
        max_turnover = float(qg.get("max_daily_turnover", 2500000000.0))
        min_bar_ret = float(qg.get("min_bar_return_pct", 4.0))
        min_vol_ratio = float(qg.get("min_volume_ratio", 2.50))
        min_cir = float(qg.get("min_close_in_range", 0.85))
        max_upper_wick = float(qg.get("max_upper_wick_pct", 0.10))
        min_runup_60 = float(qg.get("min_prior_runup_60_pct", 15.0))
        max_ath_dist = float(qg.get("max_ath_dist_pct", 20.0))
        max_dist_sma200 = float(qg.get("max_dist_sma200_pct", 30.0))
        max_chase = float(qg.get("max_chase_pct", 8.0))
        min_stop_buf = float(qg.get("min_stop_distance_pct", 8.0)) / 100.0

        candidates = []
        for sym, daily_df in self._universe_daily.items():
            if sym in self.active_positions:
                continue

            d_list = self._dates_list.get(sym)
            if not d_list or len(d_list) < 60:
                continue
            i = bisect_right(d_list, target_date) - 1
            if i < 60:
                continue
            cur_date = d_list[i]
            # Only evaluate setups triggering on target_date
            if cur_date != target_date:
                continue

            cur_c = float(daily_df['close'].iloc[i])
            cur_o = float(daily_df['open'].iloc[i])
            cur_h = float(daily_df['high'].iloc[i])
            cur_l = float(daily_df['low'].iloc[i])
            cur_v = float(daily_df['volume'].iloc[i])

            # 1. Bullish candle required
            if cur_c <= cur_o:
                continue

            # 2. Liquidity Turnover Guard (₹1.5 Cr to ₹250 Cr)
            turnover = cur_c * cur_v
            if turnover < min_turnover or turnover > max_turnover:
                continue

            # 3. Bar Return Expansion Guard (>= 4.0%)
            prev_c = float(daily_df['close'].iloc[i - 1]) if i > 0 else cur_c
            if prev_c <= 0:
                continue
            bar_ret = ((cur_c / prev_c) - 1.0) * 100.0
            if bar_ret < min_bar_ret:
                continue

            # 4. Parabolic Exhaustion Guard: Reject if extended >30% above SMA200 with selling wick
            bar_range = max(cur_h - cur_l, 0.01)
            u_wick_pre = (cur_h - max(cur_c, cur_o)) / bar_range
            if i >= 200:
                sma200 = float(daily_df['close'].iloc[max(0, i - 200):i].mean())
                if sma200 > 0:
                    dist_sma = ((cur_c / sma200) - 1.0) * 100.0
                    if dist_sma > max_dist_sma200 and u_wick_pre > max_upper_wick:
                        continue

            # 5. Prior 60-Day Runup Guard (>= 15.0%)
            c_60ago = float(daily_df['close'].iloc[i - 60])
            if c_60ago <= 0:
                continue
            stock_ret60 = ((cur_c / c_60ago) - 1.0) * 100.0
            if stock_ret60 < min_runup_60:
                continue

            # 6. Champion Pattern Detection (HTF, BASE, 52W_HIGH, DARVAS)
            champ = detect_all_champion_patterns(
                daily_df, i, symbol=sym,
                min_cir=min_cir,
                max_upper_wick=max_upper_wick,
                min_vol_ratio=min_vol_ratio,
                max_chase_pct=max_chase,
                max_ath_dist_pct=max_ath_dist
            )

            # 7. Check RS Leader Refined Strategy if Champion not matched
            rs_setup = None
            if rs_leader_refined_live is not None and not champ:
                try:
                    sub_candles = [
                        {"t": str(daily_df['date'].iloc[k]), "o": float(daily_df['open'].iloc[k]),
                         "h": float(daily_df['high'].iloc[k]), "l": float(daily_df['low'].iloc[k]),
                         "c": float(daily_df['close'].iloc[k]), "v": float(daily_df['volume'].iloc[k])}
                        for k in range(max(0, i - 65), i + 1)
                    ]
                    sub_idx = len(sub_candles) - 1
                    ema50_val = float(daily_df['close'].iloc[max(0, i - 50):i + 1].ewm(span=50, adjust=False).mean().iloc[-1])
                    ema200_val = float(daily_df['close'].iloc[max(0, i - 200):i + 1].ewm(span=200, adjust=False).mean().iloc[-1])
                    ind_dict = {
                        "ema50": [ema50_val] * len(sub_candles),
                        "ema200": [ema200_val] * len(sub_candles)
                    }
                    rs_setup = rs_leader_refined_live.evaluate(sym, sub_candles, ind_dict, sub_idx)
                except Exception as ex:
                    logger.debug(f"RS leader evaluation fallback for {sym}: {ex}")

            if not champ and not rs_setup:
                continue

            # Geometry & Setup extraction
            if champ:
                pattern_id = champ.pattern
                pattern_name = champ.pattern_label
                entry_p = champ.entry
                stop_p = champ.stop
                target_p = champ.target_price
                be_lock_p = champ.be_lock_price
                vol_ratio = champ.volume_ratio
                cir_val = champ.cir
                u_wick = champ.upper_wick
                geom = dict(champ.geometry)
            else:
                pattern_id = rs_setup["strategy_id"]
                pattern_name = "RS Leader Refined"
                entry_p = float(rs_setup["entry_price"])
                stop_p = float(rs_setup["structural_stop"])
                target_p = float(rs_setup["target_price"])
                be_lock_p = round(entry_p + 0.80 * (entry_p - stop_p), 2)
                vol_ratio = float(rs_setup.get("raw_measurements", {}).get("vol_ratio", min_vol_ratio))
                cir_val = float(rs_setup.get("raw_measurements", {}).get("cir", min_cir))
                u_wick = float(rs_setup.get("raw_measurements", {}).get("upper_wick_pct", 0.05))
                geom = {
                    "pattern": "RS_LEADER",
                    "pivot": round(entry_p, 2),
                    "stop": stop_p,
                    "target": target_p,
                    "be_lock": be_lock_p
                }

            # Enforce Minimum Stop Distance (8.0%)
            if stop_p > entry_p * (1.0 - min_stop_buf):
                stop_p = round(entry_p * (1.0 - min_stop_buf), 2)

            risk_p = max(entry_p - stop_p, 0.01)
            risk_pct = round((risk_p / entry_p) * 100.0, 2)

            # Weekly Structural Fibonacci Confluence
            w_df = self._universe_weekly.get(sym)
            fib_struct = None
            w_list = self._w_dates_list.get(sym)
            if w_df is not None and w_list and len(w_df) >= 15:
                w_idx = bisect_right(w_list, target_date) - 1
                if w_idx >= 5:
                    fib_struct = self.swing_detector.build_active_structure(sym, w_df, w_idx)

            if fib_struct is not None:
                geom['fib_structure_id'] = fib_struct.structure_id
                geom['fib_a_price'] = fib_struct.a_price
                geom['fib_b_price'] = fib_struct.b_price
                geom['fib_c_price'] = fib_struct.c_price
                geom['fib_levels'] = {
                    "1.000": fib_struct.projection.level_1_000,
                    "1.272": fib_struct.projection.level_1_272,
                    "1.618": fib_struct.projection.level_1_618,
                    "2.000": fib_struct.projection.level_2_000
                }

            # Banana ML Engine v2 Scoring & Analog Agreement
            ml_score = 0.50
            ml_size_mult = 1.00
            analog_wr = 0.50
            if self.ml_engine is not None and len(daily_df) > 40:
                try:
                    closes = daily_df['close'].values.astype(float)
                    shape = self.ml_engine.shape_embedding(closes, i)
                    analogs = self.ml_engine.analog_features(shape)
                    analog_wr = float(analogs.get("analog_win_rate", 0.50))
                    vol_boost = min(max(vol_ratio / 3.0 - 0.5, 0.0), 1.0)
                    ml_score = round(0.50 + 0.30 * vol_boost + 0.20 * (analog_wr - 0.50), 3)
                    ml_size_mult = self.ml_engine.compute_size(ml_score, analogs.get("analog_std_r", 2.0))
                except Exception as ex:
                    logger.debug(f"ML scoring fallback for {sym}: {ex}")

            # Institutional Quality Conviction Score (0-100)
            start_250 = max(0, i - 250)
            high_250 = float(daily_df['high'].iloc[start_250:i].max()) if i > start_250 else cur_h
            ath_dist = max(0.0, (high_250 - cur_c) / high_250 * 100.0) if high_250 > 0 else 0.0

            score_candle = (cir_val * 20.0) + ((1.0 - min(u_wick, 1.0)) * 20.0)
            score_vol = min(vol_ratio / 5.0, 1.0) * 25.0
            score_ath = max(0.0, (20.0 - ath_dist) / 20.0) * 20.0
            score_trend = max(0.0, min((stock_ret60 - 15.0) / 30.0, 1.0)) * 15.0
            conviction_score = round(score_candle + score_vol + score_ath + score_trend, 1)

            cand = {
                "symbol": sym,
                "engine_version": "v3",
                "strategy_id": pattern_id,
                "strategy_name": pattern_name,
                "entry_date": cur_date,
                "entry_price": entry_p,
                "stop_price": stop_p,
                "target_price": target_p,
                "be_lock_price": be_lock_p,
                "risk_pct": risk_pct,
                "volume_ratio": round(vol_ratio, 2),
                "cir": round(cir_val, 2),
                "upper_wick": round(u_wick, 2),
                "conviction_score": conviction_score,
                "geometry": geom,
                "ml_score": ml_score,
                "ml_size_multiplier": ml_size_mult,
                "analog_win_rate": analog_wr,
                "action": "BUY_NOW" if risk_pct <= 5.0 else "BUY"
            }
            candidates.append(cand)

        # Sort by Conviction Score & ML Score descending
        candidates.sort(
            key=lambda c: (c.get("conviction_score", 0.0), c.get("ml_score", 0.5), c["volume_ratio"], -c["risk_pct"]),
            reverse=True
        )

        min_score = float(self.daily_selection.get("min_score", 0.0))
        candidates = [c for c in candidates if c.get("conviction_score", 0.0) >= min_score]

        # Enforce Frozen Selection Mode: TOP_2_CONVICTION_RANKED
        if self.quality_guards.get("daily_selection_mode") == "TOP_2_CONVICTION_RANKED":
            max_takes = int(self.daily_selection.get("max_takes", 2))
            candidates = candidates[:max_takes]

        return candidates

    def get_candidates(self, date_str: str) -> List[Dict[str, Any]]:
        return self.scan_date(date_str)

    def execute_buy(self, cand: Dict[str, Any], date_str: str) -> Optional[Dict[str, Any]]:
        """Allocates capital and opens position."""
        if len(self.active_positions) >= self.max_slots:
            return None

        sym = cand["symbol"]
        if sym in self.active_positions:
            return None

        alloc = min(self.capital_per_position, self.cash)
        if alloc < 2000.0:
            return None

        ep = cand["entry_price"]
        shares = int(alloc // ep)
        if shares <= 0:
            return None

        leg = TransactionCostModel.calculate_leg_friction(ep, shares, is_buy=True, scenario=FrictionScenario.BASE)
        cost_total = round(ep * shares + leg.total_cost_cash, 2)
        if cost_total > self.cash:
            return None

        self.cash -= cost_total
        trade_id = f"V3_ACT_{len(self.active_positions) + len(self.closed_trades) + 1:05d}"

        pos = {
            "trade_id": trade_id,
            "engine_version": "v3",
            "symbol": sym,
            "strategy_id": cand.get("strategy_id", "HTF"),
            "strategy_name": cand.get("strategy_name", "High-Tight Flag"),
            "entry_date": date_str,
            "entry_price": ep,
            "initial_stop": cand["stop_price"],
            "current_stop": cand["stop_price"],
            "target_price": cand["target_price"],
            "be_lock_price": cand["be_lock_price"],
            "shares": shares,
            "invested_capital": round(ep * shares, 2),
            "current_ltp": ep,
            "unrealized_pnl": 0.0,
            "roi_pct": 0.0,
            "entry_friction": round(leg.total_cost_cash, 2),
            "status": "OPEN",
            "geometry": cand.get("geometry", {})
        }

        self.active_positions[sym] = pos
        return pos

    def check_exits(self, ltp_map: Dict[str, float], current_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Evaluates live trailing stops and targets."""
        closed_now = []
        for sym, pos in list(self.active_positions.items()):
            ltp = ltp_map.get(sym)
            if not ltp or ltp <= 0:
                continue

            pos["current_ltp"] = ltp
            ep = pos["entry_price"]
            cur_roi = ((ltp - ep) / ep) * 100.0
            pos["roi_pct"] = round(cur_roi, 2)
            pos["unrealized_pnl"] = round((ltp - ep) * pos["shares"], 2)

            # Breakeven lock check
            be_lock = pos.get("be_lock_price")
            if be_lock and ltp >= be_lock and pos["current_stop"] < ep:
                pos["current_stop"] = ep  # Trail stop to breakeven!

            # Exit triggers: Stop hit or Target hit
            exit_reason = None
            if ltp <= pos["current_stop"]:
                exit_reason = "STOP_LOSS"
            elif ltp >= pos["target_price"]:
                exit_reason = "TARGET_HIT"

            if exit_reason:
                pos_closed = self.active_positions.pop(sym)
                shares = pos_closed["shares"]
                leg_out = TransactionCostModel.calculate_leg_friction(ltp, shares, is_buy=False, scenario=FrictionScenario.BASE)
                proceeds = round(ltp * shares - leg_out.total_cost_cash, 2)
                self.cash += proceeds

                gross_pnl = round((ltp - ep) * shares, 2)
                tot_friction = round(pos_closed["entry_friction"] + leg_out.total_cost_cash, 2)
                net_pnl = round(gross_pnl - tot_friction, 2)

                exit_dt = current_date or datetime.date.today().strftime("%Y-%m-%d")
                try:
                    d_entry = datetime.datetime.strptime(str(pos_closed["entry_date"])[:10], "%Y-%m-%d").date()
                    d_exit = datetime.datetime.strptime(exit_dt[:10], "%Y-%m-%d").date()
                    h_days = max(1, (d_exit - d_entry).days)
                except Exception:
                    h_days = 1

                init_stop = float(pos_closed.get("initial_stop") or pos_closed.get("current_stop") or (ep * 0.92))
                risk_per_share = abs(ep - init_stop)
                if risk_per_share < 0.01 * ep:
                    risk_per_share = 0.08 * ep
                r_val = round((ltp - ep) / max(0.01, risk_per_share), 2)
                r_val = max(-2.5, min(8.0, r_val))

                tr = {
                    "trade_id": pos_closed["trade_id"],
                    "engine_version": "v3",
                    "symbol": sym,
                    "strategy_id": pos_closed.get("strategy_id", "HTF"),
                    "entry_date": pos_closed["entry_date"],
                    "exit_date": exit_dt,
                    "entry_price": ep,
                    "exit_price": ltp,
                    "shares": shares,
                    "invested_capital": pos_closed["invested_capital"],
                    "gross_pnl": gross_pnl,
                    "total_friction": tot_friction,
                    "net_pnl": net_pnl,
                    "roi_pct": round(((ltp - ep) / ep) * 100.0, 2),
                    "risknorm_r": r_val,
                    "capped_r": r_val,
                    "exit_reason": exit_reason,
                    "holding_days": h_days,
                    "cash_after": round(self.cash, 2)
                }
                self.closed_trades.append(tr)
                closed_now.append(tr)

        return closed_now

    def step(self, current_date: str, is_live: bool = False) -> Dict[str, Any]:
        """Executes one single market session."""
        # Update daily closes from universe if available
        ltp_today = self._date_to_close.get(current_date, {})

        # 1. Check exits
        closed_today = self.check_exits(ltp_today, current_date=current_date)

        # 2. Check entries
        candidates = self.scan_date(current_date)
        opened_today = []
        for cand in candidates:
            pos = self.execute_buy(cand, current_date)
            if pos:
                opened_today.append(pos)
                if len(self.active_positions) >= self.max_slots:
                    break

        # 3. Calculate NAV
        equity_holdings = sum(p.get("current_ltp", p["entry_price"]) * p["shares"] for p in self.active_positions.values())
        total_nav = round(self.cash + equity_holdings, 2)
        nav_entry = {
            "engine_version": "v3",
            "date": current_date,
            "total_nav": total_nav,
            "cash": round(self.cash, 2),
            "invested_capital": round(equity_holdings, 2),
            "open_positions": len(self.active_positions)
        }
        self.daily_nav_history.append(nav_entry)

        # 4. If live or persisted, sync to MasterStorage
        if is_live:
            for p in opened_today:
                try:
                    self.storage.create_active_trade(p)
                except Exception:
                    pass
            for tr in closed_today:
                try:
                    self.storage.close_trade(tr)
                except Exception:
                    pass
            try:
                self.storage.record_daily_nav(nav_entry)
            except Exception:
                pass

        return {
            "date": current_date,
            "opened": opened_today,
            "closed": closed_today,
            "active_count": len(self.active_positions),
            "total_nav": total_nav
        }

    def run(self, start_date: str, end_date: str, is_live: bool = False, **kwargs) -> Dict[str, Any]:
        """Chronological multi-session execution."""
        dates = self.get_trading_dates(start_date, end_date)
        logger.info(f"Running QuantSync-V3 across {len(dates)} trading sessions ({start_date} -> {end_date})...")

        for d in dates:
            self.step(d, is_live=is_live)

        n_closed = len(self.closed_trades)
        wins = [t for t in self.closed_trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in self.closed_trades if t.get("net_pnl", 0) <= 0]
        wr = round(len(wins) / n_closed * 100.0, 1) if n_closed else 0.0
        gross_w = sum(t.get("net_pnl", 0) for t in wins)
        gross_l = abs(sum(t.get("net_pnl", 0) for t in losses))
        pf = round(gross_w / max(1e-4, gross_l), 2) if gross_l else 2.0

        final_nav = self.daily_nav_history[-1]["total_nav"] if self.daily_nav_history else self.initial_capital
        tot_ret = round((final_nav - self.initial_capital) / self.initial_capital * 100.0, 2)

        summary = {
            "engine": "QuantSync-V3",
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": self.initial_capital,
            "final_nav": final_nav,
            "total_return_pct": tot_ret,
            "trades_closed": n_closed,
            "active_open": len(self.active_positions),
            "win_rate": wr,
            "profit_factor": pf
        }
        return summary

    def export_reports(self, output_dir: str, prefix: str):
        os.makedirs(output_dir, exist_ok=True)
        if self.closed_trades:
            df = pd.DataFrame(self.closed_trades)
            df.to_csv(os.path.join(output_dir, f"{prefix}_v3_closed_trades.csv"), index=False)

    def print_summary(self):
        n_closed = len(self.closed_trades)
        wins = [t for t in self.closed_trades if t.get("net_pnl", 0) > 0]
        wr = round(len(wins) / n_closed * 100.0, 1) if n_closed else 0.0
        print(f"QuantSync-V3: Closed={n_closed}, Active={len(self.active_positions)}, WR={wr}%, Cash=₹{self.cash:,.2f}")


def get_engine(config: Optional[Dict[str, Any]] = None) -> BaseTradingEngine:
    """Factory hook for EngineRegistry auto-discovery."""
    cfg = config or {}
    if not cfg:
        cfg_file = os.path.join(_PROD_ROOT, "config.json")
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file) as f:
                    master_cfg = json.load(f)
                    cfg = master_cfg.get("engines", {}).get("v3", {})
            except Exception:
                pass
    return V3TradingEngine(
        engine_id="v3",
        engine_name=cfg.get("name", "QuantSync-V3"),
        initial_capital=float(cfg.get("capital", 300000.0)),
        capital_per_position=float(cfg.get("capital_per_pos", 12000.0)),
        max_slots=int(cfg.get("max_slots", 25)),
        config=cfg
    )


TradingEnginePlugin = V3TradingEngine
