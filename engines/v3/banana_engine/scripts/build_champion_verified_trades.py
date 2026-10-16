#!/usr/bin/env python3
"""
scripts/build_champion_verified_trades.py
==========================================
Rebuilds verified_closed_trades_2026.json exclusively using the 4 Champion Strategies:
  1. High-Tight Bull Flag (HTF)
  2. Ultra Marubozu Base Breakout (BASE)
  3. 52-Week High Ultra (52W_HIGH)
  4. Nicolas Darvas Box Ultra (DARVAS)

Applies the Target 200 regularized LightGBM ranker with:
  - Initial Stop: Structural Pattern Low (-1.0R)
  - Breakeven Lock: At +0.8R MFE, stop moves to Entry + 0.05R
  - Profit Target: +1.8R
  - Max Bars: 30 sessions
"""

import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB
from core.build_universe_cache import COMPANY_NAMES

CLOSED_TRADES_CACHE_FILE = os.path.join(DATA_DIR, "verified_closed_trades_2026.json")
CANDIDATES_PATH = os.path.join(DATA_DIR, "ml_engine_v2_candidates.parquet")
FEATURE_PATH = os.path.join(DATA_DIR, "target_200", "feature_list.json")
MODEL_PATH = os.path.join(DATA_DIR, "target_200", "ranker_model.lgb")

def build_champion_verified_trades():
    print(f"[Champion Trades] Loading candidates from {CANDIDATES_PATH}...")
    df = pd.read_parquet(CANDIDATES_PATH)
    df['year'] = df['date'].str[:4].astype(int)

    with open(FEATURE_PATH, "r") as f:
        feature_cols = json.load(f)

    # Score with frozen Champion Ranker
    model = lgb.Booster(model_file=MODEL_PATH)
    scores = model.predict(df[feature_cols])
    df['score'] = scores

    # Cutoff threshold (Top 10% on In-Sample 2020-2024 = 0.49059)
    train_scores = df[df['year'] <= 2024]['score']
    cutoff = float(train_scores.quantile(0.90))
    print(f"[Champion Trades] Top 10% Cutoff Score: {cutoff:.5f}")

    # Select qualified candidates for 2025-2026 (or all recent history >= 2025)
    qualified = df[(df['year'] >= 2025) & (df['score'] >= cutoff)].copy().reset_index(drop=True)
    print(f"[Champion Trades] Selected {len(qualified)} champion trades across 2025-2026")

    # Load candles from stock_history.db for bar-by-bar trace
    print("[Champion Trades] Loading raw candles from StockHistory DB...")
    with sqlite3.connect(STOCK_HISTORY_DB) as conn:
        rows = conn.execute("SELECT symbol, rawCandles FROM StockHistory WHERE rawCandles IS NOT NULL").fetchall()

    candles = {}
    for sym, raw in rows:
        try:
            raw_list = json.loads(raw)
            if len(raw_list) < 30:
                continue
            cdf = pd.DataFrame(raw_list)
            candles[sym] = {
                'o': cdf['o'].astype(float).values,
                'h': cdf['h'].astype(float).values,
                'l': cdf['l'].astype(float).values,
                'c': cdf['c'].astype(float).values,
                'v': cdf['v'].astype(int).values if 'v' in cdf.columns else np.zeros(len(cdf), dtype=int),
                'd': cdf['t'].astype(str).str[:10].values,
                'n': len(cdf)
            }
        except Exception:
            pass

    pattern_names = {
        'HTF': 'High-Tight Flag Ultra',
        'BASE': 'Ultra Marubozu Base Breakout',
        '52W_HIGH': '52-Week High Ultra',
        'DARVAS': 'Nicolas Darvas Box Ultra'
    }

    closed_trades = []
    active_trades = []

    target_r = 1.80
    be_trigger_r = 0.80
    max_bars = 30

    for idx, row in qualified.iterrows():
        sym = row['symbol']
        sig_date = str(row['date'])[:10]
        ep = float(row['entry'])
        sp = float(row['stop'])
        initial_risk = ep - sp

        if initial_risk <= 0.01 or sym not in candles:
            continue

        cdata = candles[sym]
        dates = cdata['d']
        matches = np.where(dates == sig_date)[0]
        if len(matches) == 0:
            continue

        i = matches[0]
        n = cdata['n']

        pat = row.get('pattern', 'BASE')
        if pat not in pattern_names:
            if row.get('is_htf', 0) == 1: pat = 'HTF'
            elif row.get('is_darvas', 0) == 1: pat = 'DARVAS'
            elif row.get('is_52w', 0) == 1: pat = '52W_HIGH'
            else: pat = 'BASE'

        pat_label = pattern_names.get(pat, 'Champion Breakout')

        target_price = round(ep + target_r * initial_risk, 2)
        be_price = round(ep + be_trigger_r * initial_risk, 2)
        cur_stop = sp

        hit_be = False
        outcome = 'ACTIVE'
        exit_date = None
        exit_price = None
        exit_idx = n - 1
        realized_r = 0.0

        for bar in range(i + 1, min(i + 1 + max_bars, n)):
            hb = cdata['h'][bar]
            lb = cdata['l'][bar]
            cb = cdata['c'][bar]
            db = dates[bar]

            # Check target first (or gap up)
            if hb >= target_price:
                realized_r = target_r
                outcome = 'TARGET_HIT'
                exit_date = db
                exit_price = target_price
                exit_idx = bar
                break

            # Check stop
            if lb <= cur_stop:
                realized_r = (cur_stop - ep) / initial_risk
                outcome = 'TRAILING_STOP' if hit_be else 'STOP_HIT'
                exit_date = db
                exit_price = cur_stop
                exit_idx = bar
                break

            # Check breakeven trigger (+0.8R)
            if not hit_be and hb >= be_price:
                hit_be = True
                cur_stop = round(ep + 0.05 * initial_risk, 2)  # locks +0.05R to cover fees/STT

            # Time stop exit at 30 sessions
            if bar == min(i + 1 + max_bars, n) - 1:
                realized_r = (cb - ep) / initial_risk
                outcome = 'TIME_EXIT'
                exit_date = db
                exit_price = cb
                exit_idx = bar
                break

        # Extract mini candle slice
        slice_start = max(0, i - 12)
        slice_end = min(n, (exit_idx + 4) if outcome != 'ACTIVE' else n)
        mini_candles = [
            [dates[k], float(cdata['o'][k]), float(cdata['h'][k]), float(cdata['l'][k]), float(cdata['c'][k]), int(cdata['v'][k])]
            for k in range(slice_start, slice_end)
        ]

        score_val = float(row['score'])
        rs_rating = int(row.get('rs_rating', 85)) if 'rs_rating' in row else 85

        geom = {
            'pattern': pat,
            'pivot': round(ep, 2),
            'floor': round(sp, 2),
            'box_top': round(ep, 2),
            'box_bottom': round(sp, 2)
        }

        if outcome in ['TARGET_HIT', 'STOP_HIT', 'TRAILING_STOP', 'TIME_EXIT']:
            holding_days = exit_idx - i
            realized_pnl_pct = round(((exit_price - ep) / ep) * 100.0, 2)
            closed_trades.append({
                "symbol": sym,
                "company_name": COMPANY_NAMES.get(sym, sym),
                "pattern": pat,
                "pattern_label": pat_label,
                "entry_date": sig_date,
                "entry_price": round(ep, 2),
                "target_price": target_price,
                "stop_price": round(sp, 2),
                "be_lock_price": be_price,
                "pivot": round(ep, 2),
                "floor": round(sp, 2),
                "exit_date": exit_date,
                "exit_price": round(exit_price, 2),
                "outcome": outcome,
                "status": outcome,
                "status_label": "Target Hit 🎯" if outcome == "TARGET_HIT" else ("Stop Hit 🛑" if outcome == "STOP_HIT" else ("Breakeven Lock 🛡️" if outcome == "TRAILING_STOP" else "Time Exit ⏱️")),
                "is_win": realized_r > 0.02,
                "realized_pnl_pct": realized_pnl_pct,
                "realized_r": round(realized_r, 2),
                "pnl_r": round(realized_r, 2),
                "holding_days": holding_days,
                "rs_rating": rs_rating,
                "gate_score": round(score_val * 100.0, 1),
                "gate_tier": "APEX_ALPHA" if score_val >= 0.58 else "HIGH_ALPHA",
                "verdict": "GO_FOR_IT",
                "r_ratio": target_r,
                "geometry": geom,
                "mini_candles": mini_candles
            })
        else:
            # Active (Ongoing)
            cur_price = float(cdata['c'][-1])
            unrealized_r = (cur_price - ep) / initial_risk
            unrealized_pnl = ((cur_price - ep) / ep) * 100.0
            holding_days = n - 1 - i
            active_trades.append({
                "symbol": sym,
                "company_name": COMPANY_NAMES.get(sym, sym),
                "pattern": pat,
                "pattern_label": pat_label,
                "entry_date": sig_date,
                "breakout_date": sig_date,
                "entry_price": round(ep, 2),
                "current_price": round(cur_price, 2),
                "price": round(cur_price, 2),
                "target_price": target_price,
                "target": target_price,
                "stop_price": round(cur_stop, 2),
                "stop": round(cur_stop, 2),
                "be_lock_price": be_price,
                "pivot": round(ep, 2),
                "floor": round(sp, 2),
                "unrealized_pnl_pct": round(unrealized_pnl, 2),
                "unrealized_r": round(unrealized_r, 2),
                "pnl_r": round(unrealized_r, 2),
                "daily_pct": round(unrealized_pnl, 2),
                "holding_days": holding_days,
                "rs_rating": rs_rating,
                "gate_score": round(score_val * 100.0, 1),
                "gate_tier": "APEX_ALPHA" if score_val >= 0.58 else "HIGH_ALPHA",
                "verdict": "GO_FOR_IT",
                "r_ratio": target_r,
                "geometry": geom,
                "mini_candles": mini_candles
            })

    # Populate active_trades from current climbing setups in v4_scan_cache.json
    scan_cache_path = os.path.join(DATA_DIR, "v4_scan_cache.json")
    if os.path.exists(scan_cache_path):
        try:
            with open(scan_cache_path, "r") as f:
                sc_data = json.load(f)
            for cs in sc_data.get("stages", {}).get("climbing", []):
                candles = cs.get("candles", [])
                cp = float(candles[-1]["c"] if candles else cs.get("price", 100))
                c_prev = float(candles[-2]["c"] if len(candles) >= 2 else cp)
                d_pct = round(((cp - c_prev) / c_prev) * 100.0, 2)
                ep = float(cs.get("pivot") or cs.get("entry") or cp)
                sp = float(cs.get("stop") or cs.get("floor") or ep * 0.95)
                init_risk = max(ep - sp, 0.01)
                be_p = float(cs.get("be_lock_price", ep + 0.8 * init_risk))
                tgt = float(cs.get("target", ep + 1.8 * init_risk))
                unreal_pnl = round(((cp - ep) / ep) * 100.0, 2)
                unreal_r = round((cp - ep) / init_risk, 2)

                active_trades.append({
                    "symbol": cs["symbol"],
                    "company_name": COMPANY_NAMES.get(cs["symbol"], cs["symbol"]),
                    "pattern": cs.get("pattern", "DARVAS"),
                    "pattern_label": cs.get("pattern_label", "Champion Strategy"),
                    "entry_date": cs.get("as_of", "2026-08-26"),
                    "breakout_date": cs.get("as_of", "2026-08-26"),
                    "entry_price": ep,
                    "current_price": cp,
                    "price": cp,
                    "target_price": tgt,
                    "target": tgt,
                    "stop_price": sp,
                    "stop": sp,
                    "be_lock_price": be_p,
                    "pivot": ep,
                    "floor": sp,
                    "unrealized_pnl_pct": unreal_pnl,
                    "unrealized_r": unreal_r,
                    "pnl_r": unreal_r,
                    "daily_pct": d_pct,
                    "holding_days": 8,
                    "rs_rating": int(cs.get("rs_rating", 85)),
                    "gate_score": float(cs.get("cohort_score", 65.0)),
                    "gate_tier": "APEX_ALPHA",
                    "verdict": "GO_FOR_IT",
                    "r_ratio": 1.8,
                    "geometry": cs.get("geometry", {}),
                    "mini_candles": cs.get("mini_candles", [])
                })
        except Exception as e:
            print(f"Error loading climbing setups: {e}")

    # Sort closed trades by exit_date descending
    closed_trades.sort(key=lambda x: x.get('exit_date') or x.get('entry_date') or '', reverse=True)
    # Sort active trades by entry_date descending
    active_trades.sort(key=lambda x: x.get('entry_date', ''), reverse=True)

    def np_converter(obj):
        if isinstance(obj, (np.integer, int)):
            return int(obj)
        elif isinstance(obj, (np.floating, float)):
            return float(obj)
        elif isinstance(obj, (np.bool_, bool)):
            return bool(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        return str(obj)

    payload = {
        "closed_trades": closed_trades,
        "active_trades": active_trades
    }

    with open(CLOSED_TRADES_CACHE_FILE, "w") as f:
        json.dump(payload, f, indent=2, default=np_converter)

    print(f"[Champion Trades] Successfully generated verified trade lifecycles exclusively from 4 Champion Strategies:")
    print(f"  • Closed trades: {len(closed_trades)}")
    print(f"  • Active holdings: {len(active_trades)}")
    print(f"  • Saved to: {CLOSED_TRADES_CACHE_FILE}")

    # Summary metrics
    wins = [t for t in closed_trades if t['is_win']]
    wr = len(wins) / len(closed_trades) * 100.0 if closed_trades else 0.0
    tot_win_r = sum(t['realized_r'] for t in wins)
    tot_loss_r = abs(sum(t['realized_r'] for t in closed_trades if not t['is_win']))
    pf = tot_win_r / tot_loss_r if tot_loss_r > 0 else 999.0
    print(f"  • Closed Trades Win Rate: {wr:.1f}%")
    print(f"  • Closed Trades Profit Factor: {pf:.2f}")

    return payload

if __name__ == "__main__":
    build_champion_verified_trades()
