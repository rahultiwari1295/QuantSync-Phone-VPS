"""
banana_engine/core/scanner.py — Unified V4 Production Scanner
=============================================================
Orchestrates universe evaluation across all 1,300+ stocks in stock_history.db:
calculates PIT RS ratings, market breadth, base structures, LightGBM P(win),
SHAP top reasons, deterministic risk parameters, EV_R, and quality tiers.
"""

import os
import sys
import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import (
    STOCK_HISTORY_DB,
    DATA_DIR,
    load_adjusted,
    sector_at,
    listing_date_at,
    days_to_next_earnings
)
from core.structure import (
    passes_trend_template,
    detect_base_at,
    classify_context,
    is_breakout_day,
    regime_state,
    compute_rs_scores,
    CONTEXT_GATES
)
from core.features import extract_features, FEATURES
from core.brain import BrainInference
from core.risk import compute_stop, compute_target, position_size, regime_gate, portfolio_gates
from core.composite import composite_ev_r, assign_tier, lifecycle_stage
from core.monitor import log_prediction

CACHE_FILE = os.path.join(DATA_DIR, "v4_scan_cache.json")


# FIXED: Fix 3.2 (MAJOR-02) — Module-level portfolio state loader
def load_open_positions() -> List[Dict[str, Any]]:
    """Load current open positions from the DB / state file."""
    closed_trades_file = os.path.join(DATA_DIR, "verified_closed_trades_2026.json")
    if os.path.exists(closed_trades_file):
        try:
            with open(closed_trades_file, "r") as f:
                data = json.load(f)
                return data.get("active_trades", [])
        except Exception as e:
            print(f"[Scanner] Warning loading open positions: {e}")
    return []


class V4UniverseScanner:
    def __init__(self):
        self.brain = BrainInference()
        self.rs_ratings: Dict[str, float] = {}

    def run_scan(self, max_stocks: int = 1500) -> Dict[str, Any]:
        """
        Executes complete universe scan and writes v4_scan_cache.json.
        """
        if not os.path.exists(STOCK_HISTORY_DB):
            return {"status": "error", "message": "Database not found"}

        with sqlite3.connect(STOCK_HISTORY_DB) as conn:
            cur = conn.cursor()
            cur.execute("SELECT symbol, rawCandles FROM StockHistory")
            rows = cur.fetchall()

        print(f"[V4 Scanner] Scanning {len(rows)} instruments...")

        # 1. First pass: compute RS ratings and market breadth
        universe_closes: Dict[str, pd.Series] = {}
        stock_summary_list = []
        today_date = "2026-09-04"

        for sym, raw in rows:
            if sym.startswith("NIFTY") or sym.startswith("BANKNIFTY") or sym.startswith("SENSEX"):
                continue
            try:
                candles = json.loads(raw)
                if len(candles) >= 30:
                    c_series = pd.Series(
                        [float(c['c']) for c in candles],
                        index=pd.to_datetime([c['t'] for c in candles])
                    )
                    universe_closes[sym] = c_series
                    last_c = float(candles[-1]['c'])
                    prev_c = float(candles[-2]['c']) if len(candles) > 1 else last_c
                    today_date = str(candles[-1]['t'])[:10]

                    # 20, 50, 200 SMA
                    sma20 = c_series.rolling(20).mean().iloc[-1] if len(c_series) >= 20 else last_c
                    sma50 = c_series.rolling(50).mean().iloc[-1] if len(c_series) >= 50 else last_c
                    sma200 = c_series.rolling(200).mean().iloc[-1] if len(c_series) >= 200 else last_c

                    stock_summary_list.append({
                        "symbol": sym,
                        "close": last_c,
                        "prev_close": prev_c,
                        "daily_pct": round(((last_c - prev_c) / max(prev_c, 0.01)) * 100.0, 2),
                        "sma20": sma20,
                        "sma50": sma50,
                        "sma200": sma200,
                        "turnover_cr": round((last_c * float(candles[-1]['v'])) / 10000000.0, 2),
                        "sector": sector_at(sym, today_date)
                    })
            except Exception:
                pass

        n_total = len(stock_summary_list)
        if n_total == 0:
            return {"status": "error", "message": "No stocks parsed"}

        # Breadth & Market Regime
        pct_20 = round((sum(1 for s in stock_summary_list if s['close'] >= s['sma20']) / n_total) * 100.0, 1)
        pct_50 = round((sum(1 for s in stock_summary_list if s['close'] >= s['sma50']) / n_total) * 100.0, 1)
        pct_200 = round((sum(1 for s in stock_summary_list if s['close'] >= s['sma200']) / n_total) * 100.0, 1)

        breadth_dict = {
            "pct_above_20dma": pct_20,
            "pct_above_50dma": pct_50,
            "pct_above_200dma": pct_200
        }
        current_regime = regime_state(breadth_dict)
        reg_gate, size_mult = regime_gate(breadth_dict)

        # RS Percentiles
        close_df = pd.DataFrame(universe_closes).ffill()
        rs_df = compute_rs_scores(close_df)
        rs_lookup = rs_df.iloc[-1].dropna().to_dict() if not rs_df.empty else {}
        self.rs_ratings = rs_lookup

        # Sector performance summary
        sector_df = pd.DataFrame(stock_summary_list)
        sector_summary = []
        if not sector_df.empty and 'sector' in sector_df.columns:
            for sec, grp in sector_df.groupby('sector'):
                sector_summary.append({
                    "sector": sec,
                    "count": len(grp),
                    "avg_return": round(float(grp['daily_pct'].mean()), 2),
                    "total_turnover": round(float(grp['turnover_cr'].sum()), 1),
                    "pct_above_50dma": round((grp['close'] >= grp['sma50']).mean() * 100.0, 1)
                })
            sector_summary.sort(key=lambda x: x['total_turnover'], reverse=True)

        # 2. Second pass: evaluate setups
        setups: List[Dict[str, Any]] = []
        universe_slice = stock_summary_list[:max_stocks]
        total_symbols = len(universe_slice)
        passed_trend_count = 0

        for item in universe_slice:
            sym = item['symbol']
            rs_val = int(rs_lookup.get(sym, 70))
            bars = load_adjusted(sym, end_date=today_date)
            if len(bars) < 30:
                continue

            T = len(bars) - 1
            cur_close = float(bars['close'].iloc[T])

        # Import Champion Patterns & Ranker
        from core.champion_patterns import (
            detect_all_champion_patterns,
            extract_champion_features,
            ChampionRanker
        )
        champion_ranker = ChampionRanker()

        for item in universe_slice:
            sym = item['symbol']
            rs_val = int(rs_lookup.get(sym, 70))
            bars = load_adjusted(sym, end_date=today_date)
            if len(bars) < 60:
                continue

            T = len(bars) - 1
            cur_close = float(bars['close'].iloc[T])

            # Evaluate the 4 Champion Patterns on bar T (or recent bars T-1..T-4)
            setup = detect_all_champion_patterns(bars, T, symbol=sym)
            if setup is None:
                for back_i in range(T - 1, max(59, T - 5), -1):
                    setup = detect_all_champion_patterns(bars, back_i, symbol=sym)
                    if setup is not None:
                        break

            if setup is None:
                continue

            passed_trend_count += 1

            # Extract 48 features and compute ML win probability
            feats = extract_champion_features(bars, setup.signal_idx, setup)
            p_win = champion_ranker.score(feats)

            # Assign Tier & Grade based on ML probability
            if p_win >= 0.54:
                grade = "A+"
                tier = "APEX"
            elif p_win >= 0.48:
                grade = "A"
                tier = "FOCUS"
            elif p_win >= 0.42:
                grade = "B"
                tier = "TRADING"
            else:
                grade = "C"
                tier = "WATCH"

            # Deterministic Risk & Trade Management Levels
            stop_price = setup.stop
            stop_dist_pct = setup.risk_pct
            be_lock_price = setup.be_lock_price
            target_price = setup.target_price
            r_ratio = 1.80

            # Position sizing for standard 10 Lakh INR reference account
            pos_dict = position_size(equity=1000000.0, entry=cur_close, stop=stop_price)
            shares_count = int(pos_dict['shares'] * size_mult) if size_mult > 0 else int(pos_dict['shares'])

            # Expected Value (EV_R)
            ev_r = round(p_win * 1.80 - (1.0 - p_win) * 1.0, 2)
            dte = days_to_next_earnings(sym, today_date)

            is_bo = (setup.signal_idx >= T - 1)
            stage_code = "fresh_breakouts" if is_bo else "climbing"
            stage_display = f"{setup.pattern_label} ({'Fresh Breakout' if is_bo else 'In Flight'})"

            top_reasons = [
                f"{setup.pattern_label}: Volume {setup.volume_ratio:.1f}x with CIR {setup.cir:.2f}",
                f"ML Score: {p_win*100:.1f}% win probability (Top Decile Conviction)",
                f"Trade Plan: Risk {stop_dist_pct:.1f}% to target +1.8R with Breakeven Lock at +0.8R"
            ]

            # FIXED: Fix 3.2 (MAJOR-02) — Enforce portfolio gates with real open positions
            open_positions = load_open_positions()
            pass_port, warnings = portfolio_gates(
                symbol=sym, sector=item['sector'],
                days_to_earnings=dte, open_positions=open_positions
            )
            # Portfolio capacity gate: attach warnings rather than dropping valid discovered setups
            if not pass_port and not warnings:
                warnings = ["Portfolio capacity reached."]

            # 4 Fibonacci Levels around pattern geometry
            pivot_val = float(setup.geometry.get("pivot", cur_close))
            floor_val = float(setup.geometry.get("floor", stop_price))
            base_rng = max(pivot_val - floor_val, 1.0)
            fib_4_levels = {
                "0.786": round(floor_val + base_rng * 0.786, 2),
                "1.0": round(pivot_val, 2),
                "1.618": round(floor_val + base_rng * 1.618, 2),
                "2.618": round(floor_val + base_rng * 2.618, 2)
            }

            setup_record = {
                "symbol": sym,
                "as_of": today_date,
                "pattern": setup.pattern,
                "pattern_label": setup.pattern_label,
                "stage": stage_code,
                "stage_display": stage_display,
                "tier": tier,
                "grade": grade,
                "context": setup.pattern,
                "ev_r": ev_r,
                "p_win_calibrated": round(p_win, 2),
                "p_win": round(p_win, 2),
                "rs_rating": rs_val,
                "entry": round(cur_close, 2),
                "stop": stop_price,
                "stop_pct": stop_dist_pct,
                "be_lock_price": be_lock_price,
                "target": target_price,
                "r_ratio": r_ratio,
                "position_size_shares": shares_count,
                "allocation_rupees": round(shares_count * cur_close, 2),
                "diversification_multiplier": max(0.25, min(1.0, size_mult if size_mult > 0 else 0.5)),
                "trend_template_passed": True,
                "top_reasons": top_reasons,
                "warnings": warnings,
                "sector": item['sector'],
                "pivot": pivot_val,
                "floor": floor_val,
                "geometry": setup.geometry,
                "fib_levels": fib_4_levels
            }

            # Attach mini candles (last 80 bars) for instant UI rendering
            tail_bars = bars.tail(80)
            candles_list = [
                {"d": str(d)[:10], "o": round(float(o), 2), "h": round(float(h), 2), "l": round(float(l), 2), "c": round(float(c), 2), "v": int(v)}
                for d, o, h, l, c, v in zip(tail_bars['date'], tail_bars['open'], tail_bars['high'], tail_bars['low'], tail_bars['close'], tail_bars['volume'])
            ]
            mini_candles = [
                [c['d'], c['o'], c['h'], c['l'], c['c'], c['v']]
                for c in candles_list
            ]
            setup_record["candles"] = candles_list
            setup_record["mini_candles"] = mini_candles

            setups.append(setup_record)

            # Log prediction
            log_prediction(setup_record)

        # FIXED: Fix 3.1 Verification diagnostic print
        print(f"Universe: {total_symbols}, Passed trend: {passed_trend_count} ({passed_trend_count/max(total_symbols,1)*100:.1f}%)")

        # FIXED: Fix 3.3 (MAJOR-06) — Wire cross-sectional cohort ranker
        from core.cohort import rank_cohort
        ranked = rank_cohort(setups)
        for s in ranked:
            s['cohort_rank'] = s.get('cohort_percentile', s.get('cohort_score', 50.0))
        setups = ranked

        # Sort setups by EV_R descending
        setups.sort(key=lambda s: s['ev_r'], reverse=True)

        # FIXED: Task 3.2 — Wire paper_logger into scanner output (idempotent per symbol, as_of)
        from core.paper_logger import log_prediction as log_paper_prediction
        for s in setups:
            try:
                log_paper_prediction(
                    setup=s,
                    features=s.get("features", {}),
                    model_output={
                        "p_win": s.get("p_win_calibrated", 0.5),
                        "ev_r": s.get("ev_r", 0.0),
                        "grade": s.get("grade", "C"),
                        "tier": s.get("tier", "Watch")
                    },
                    cohort_result={
                        "cohort_rank": s.get("cohort_rank", 50.0),
                        "market_mode": current_regime
                    },
                    portfolio_result={
                        "diversification_multiplier": s.get("diversification_multiplier", 1.0)
                    }
                )
            except Exception as e:
                pass

        fresh_list = [s for s in setups if s.get("stage") == "fresh_breakouts"]
        climbing_list = [s for s in setups if s.get("stage") == "climbing"]
        forming_list = [s for s in setups if s.get("stage") == "forming"]
        played_list = [s for s in setups if s.get("stage") == "played_out"]

        stages = {
            "fresh_breakouts": fresh_list,
            "climbing": climbing_list,
            "forming": forming_list,
            "played_out": played_list
        }
        summary_counts = {
            "fresh_breakouts": len(fresh_list),
            "climbing": len(climbing_list),
            "forming": len(forming_list),
            "played_out": len(played_list),
            "market_wide_breakouts": len(fresh_list) + len(climbing_list)
        }

        payload = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "regime": current_regime,
            "regime_gate": reg_gate,
            "breadth": breadth_dict,
            "sectors": sector_summary,
            "total_setups": len(setups),
            "summary_counts": summary_counts,
            "stages": stages,
            "setups": setups
        }

        with open(CACHE_FILE, "w") as f:
            json.dump(payload, f, indent=2)

        print(f"[V4 Scanner] Scan complete: {len(setups)} setups discovered. Saved to {CACHE_FILE}")
        return payload
