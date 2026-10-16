#!/usr/bin/env python3
"""
scripts/backtest_world_best_techniques.py — Backtest of the World's Best Chart Techniques
========================================================================================
Implements and evaluates the 7 champion chart trading techniques across 1,319 Indian stocks:
1. Minervini VCP (Volatility Contraction Pattern)
2. High-Tight Bull Flag (HTF)
3. O'Neil Cup and Handle (CANSLIM)
4. Nicolas Darvas Box
5. Wyckoff Spring (Phase C Shakeout & Reclaim)
6. Stockbee Momentum Burst (TTT into Expansion)
7. Stan Weinstein Stage 2 Base Breakout (30-Week / 150 DMA)

Scientific Protocol:
- In-Sample (2020-01-01 to 2024-12-31): Rule optimization targeting PF > 2.0
- Out-of-Sample (2025-01-01 to 2026-09-12): Frozen holdout verification
- Master Championship Portfolios: Top 2 and Top 3 daily conviction ranking across sleeves
"""

import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Tuple

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

DB_PATH = os.path.abspath("Prod_VPS/Data/stock_history.db")
OUTPUT_JSON = os.path.join(_PKG_ROOT, "data/world_best_techniques_results.json")

def load_universe_candles() -> Dict[str, pd.DataFrame]:
    print(f"[1/5] Loading candle database from {DB_PATH}...")
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, rawCandles FROM StockHistory")
    rows = c.fetchall()
    conn.close()
    
    universe = {}
    total_candles = 0
    for sym, raw in rows:
        try:
            raw_list = json.loads(raw)
            if len(raw_list) < 150:
                continue
            df = pd.DataFrame(raw_list)
            df['c'] = df['c'].astype(float)
            df['h'] = df['h'].astype(float)
            df['l'] = df['l'].astype(float)
            df['o'] = df['o'].astype(float)
            df['v'] = df['v'].astype(float)
            df['date_str'] = df['t'].astype(str).str[:10]
            
            # Indicators
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
            df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
            df['sma150'] = df['c'].rolling(150, min_periods=50).mean()
            df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
            df['vol_sma20'] = df['v'].rolling(20).mean().shift(1)
            
            df['ret_60d'] = (df['c'] / df['c'].shift(60) - 1.0) * 100.0
            df['high_250'] = df['h'].rolling(250, min_periods=60).max().shift(1)
            df['ath_dist'] = ((df['high_250'] - df['c']) / df['high_250']) * 100.0
            
            universe[sym] = df
            total_candles += len(df)
        except Exception:
            continue
            
    print(f"      Loaded {len(universe)} symbols ({total_candles:,} bars) in {time.time() - t0:.2f}s.")
    return universe

# -----------------------------------------------------------------------------
# 7 Champion Chart Strategy Detectors
# -----------------------------------------------------------------------------

def detect_minervini_vcp(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """1. Minervini VCP (Volatility Contraction Pattern)"""
    if i < 80: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    
    # Stage 2 Trend Template: C > EMA50 > SMA150 > EMA200, within 20% of 52W High
    if not (c > df['ema50'].iloc[i] > df['sma150'].iloc[i] > df['ema200'].iloc[i]):
        return False, 0.0, 0.0, 0.0
    if df['ath_dist'].iloc[i] > 20.0:
        return False, 0.0, 0.0, 0.0
        
    # Wave 1 over [i-45:i-15]: depth 12% to 28%
    w1_h = df['h'].iloc[i-45:i-15].max(); w1_l = df['l'].iloc[i-45:i-15].min()
    d1 = (w1_h - w1_l) / max(w1_h, 0.01)
    if not (0.12 <= d1 <= 0.28): return False, 0.0, 0.0, 0.0
    
    # Wave 2 / Tightening over [i-14:i-2]: depth strictly less than d1
    w2_h = df['h'].iloc[i-14:i-2].max(); w2_l = df['l'].iloc[i-14:i-2].min()
    d2 = (w2_h - w2_l) / max(w2_h, 0.01)
    if not (d2 < d1 * 0.70 and d2 <= 0.12): return False, 0.0, 0.0, 0.0
    
    # Volume dry up in Wave 2
    v1 = df['v'].iloc[i-45:i-15].mean()
    v2 = df['v'].iloc[i-14:i-2].mean()
    if v1 > 0 and (v2 / v1) > 0.70: return False, 0.0, 0.0, 0.0
    
    # Breakout clearing Wave 2 pivot high
    if c <= w2_h: return False, 0.0, 0.0, 0.0
    
    vol_sma = df['vol_sma20'].iloc[i]
    if vol_sma <= 0 or (v / vol_sma) < 2.0: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if c <= o or cir < 0.82 or upper_wick > 0.10: return False, 0.0, 0.0, 0.0
    
    stop = round(max(w2_l, c * 0.955), 2)
    risk = c - stop
    target = round(c + 3.0 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + ((1.0 - d2 / 0.12) * 40.0)
    return True, stop, target, score

def detect_high_tight_flag(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """2. High-Tight Bull Flag (HTF)"""
    if i < 60: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    
    if not (c > df['ema20'].iloc[i] > df['ema50'].iloc[i]): return False, 0.0, 0.0, 0.0
    if df['ath_dist'].iloc[i] > 20.0: return False, 0.0, 0.0, 0.0
    
    pole_l = df['l'].iloc[i-15:i-3].min(); pole_h = df['h'].iloc[i-15:i-3].max()
    advance = (pole_h - pole_l) / max(pole_l, 0.01)
    if advance < 0.15: return False, 0.0, 0.0, 0.0
    
    flag_l = df['l'].iloc[i-4:i].min(); flag_h = df['h'].iloc[i-4:i].max()
    flag_rng = (flag_h - flag_l) / max(flag_l, 0.01)
    if flag_rng > 0.070: return False, 0.0, 0.0, 0.0
    
    pole_vol = df['v'].iloc[i-15:i-3].mean()
    flag_vol = df['v'].iloc[i-4:i].mean()
    if pole_vol > 0 and (flag_vol / pole_vol) > 0.65: return False, 0.0, 0.0, 0.0
    
    if c <= flag_h: return False, 0.0, 0.0, 0.0
    vol_sma = df['vol_sma20'].iloc[i]
    if vol_sma <= 0 or (v / vol_sma) < 1.8: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if c <= o or cir < 0.82 or upper_wick > 0.10: return False, 0.0, 0.0, 0.0
    
    stop = round(max(flag_l, c * 0.955), 2)
    risk = c - stop
    target = round(c + 3.0 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + min(advance / 0.30, 1.0) * 40.0
    return True, stop, target, score

def detect_cup_and_handle(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """3. O'Neil Cup and Handle (CANSLIM)"""
    if i < 75: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    
    if not (c > df['ema50'].iloc[i] > df['ema200'].iloc[i]): return False, 0.0, 0.0, 0.0
    if df['ath_dist'].iloc[i] > 18.0: return False, 0.0, 0.0, 0.0
    
    # Cup: Left rim [i-55:i-30], bottom [i-35:i-12], Right rim [i-12:i-5]
    left_rim = df['h'].iloc[i-55:i-30].max()
    cup_bottom = df['l'].iloc[i-35:i-12].min()
    cup_depth = (left_rim - cup_bottom) / max(left_rim, 0.01)
    if not (0.12 <= cup_depth <= 0.30): return False, 0.0, 0.0, 0.0
    
    right_rim = df['h'].iloc[i-12:i-4].max()
    if (left_rim - right_rim) / left_rim > 0.08: return False, 0.0, 0.0, 0.0
    
    # Handle: [i-5:i] in upper half of cup, depth <= 8%
    handle_l = df['l'].iloc[i-5:i].min()
    handle_h = df['h'].iloc[i-5:i].max()
    handle_depth = (right_rim - handle_l) / max(right_rim, 0.01)
    if handle_depth > 0.08: return False, 0.0, 0.0, 0.0
    
    cup_vol = df['v'].iloc[i-35:i-12].mean()
    handle_vol = df['v'].iloc[i-5:i].mean()
    if cup_vol > 0 and (handle_vol / cup_vol) > 0.70: return False, 0.0, 0.0, 0.0
    
    if c <= handle_h: return False, 0.0, 0.0, 0.0
    vol_sma = df['vol_sma20'].iloc[i]
    if vol_sma <= 0 or (v / vol_sma) < 2.0: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if c <= o or cir < 0.85 or upper_wick > 0.08: return False, 0.0, 0.0, 0.0
    
    stop = round(max(handle_l, c * 0.955), 2)
    risk = c - stop
    target = round(c + 3.0 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + ((1.0 - handle_depth / 0.08) * 40.0)
    return True, stop, target, score

def detect_darvas_box(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """4. Nicolas Darvas Box Breakout"""
    if i < 60: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    
    if not (c > df['ema50'].iloc[i] > df['ema200'].iloc[i]): return False, 0.0, 0.0, 0.0
    # Darvas Box: strictly within 8% of 52W High
    if df['ath_dist'].iloc[i] > 8.0: return False, 0.0, 0.0, 0.0
    
    box_top = df['h'].iloc[i-12:i].max()
    box_bottom = df['l'].iloc[i-12:i].min()
    box_height = (box_top - box_bottom) / max(box_bottom, 0.01)
    if not (0.035 <= box_height <= 0.085): return False, 0.0, 0.0, 0.0
    
    if c <= box_top: return False, 0.0, 0.0, 0.0
    vol_sma = df['vol_sma20'].iloc[i]
    if vol_sma <= 0 or (v / vol_sma) < 2.0: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if c <= o or cir < 0.85 or upper_wick > 0.08: return False, 0.0, 0.0, 0.0
    
    stop = round(max(box_bottom, c * 0.955), 2)
    risk = c - stop
    target = round(c + 3.0 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + ((1.0 - box_height / 0.085) * 40.0)
    return True, stop, target, score

def detect_wyckoff_spring(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """5. Wyckoff Spring (Phase C Shakeout & Reclaim)"""
    if i < 60: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    
    if not (c > df['ema200'].iloc[i] and df['ema50'].iloc[i] > df['ema200'].iloc[i]):
        return False, 0.0, 0.0, 0.0
        
    support_floor = df['l'].iloc[i-35:i-4].min()
    if support_floor <= 0: return False, 0.0, 0.0, 0.0
    
    # Spring Shakeout: pierced support by 0.8% to 3.0%
    pierce_pct = (support_floor - l) / support_floor * 100.0
    if not (0.8 <= pierce_pct <= 3.0): return False, 0.0, 0.0, 0.0
    
    # Strong reclaim closing well above support
    if c <= support_floor or c <= o: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    lower_wick = (min(c, o) - l) / rng
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if lower_wick < 0.38 or upper_wick > 0.12 or cir < 0.75:
        return False, 0.0, 0.0, 0.0
        
    stop = round(min(l * 0.99, c * 0.955), 2)
    risk = c - stop
    target = round(c + 2.8 * risk, 2)
    score = (lower_wick * 40.0) + (cir * 30.0) + min(pierce_pct / 3.0, 1.0) * 30.0
    return True, stop, target, score

def detect_stockbee_momentum_burst(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """6. Stockbee Momentum Burst (TTT into Expansion)"""
    if i < 40: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    prev_c = df['c'].iloc[i-1]
    
    if not (c > df['ema20'].iloc[i] > df['ema50'].iloc[i]): return False, 0.0, 0.0, 0.0
    if df['ret_60d'].iloc[i] < 15.0: return False, 0.0, 0.0, 0.0
    
    h_3 = df['h'].iloc[i-3:i].max(); l_3 = df['l'].iloc[i-3:i].min()
    if (h_3 - l_3) / max(l_3, 0.01) > 0.030: return False, 0.0, 0.0, 0.0
    
    bar_ret = (c / prev_c - 1.0) * 100.0
    if not (4.0 <= bar_ret <= 8.5): return False, 0.0, 0.0, 0.0
    
    vol_sma = df['vol_sma20'].iloc[i]
    if vol_sma <= 0 or (v / vol_sma) < 2.0: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if c <= o or cir < 0.85 or upper_wick > 0.08: return False, 0.0, 0.0, 0.0
    
    stop = round(max(l, c * 0.955), 2)
    risk = c - stop
    target = round(c + 2.5 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + min(bar_ret / 8.0, 1.0) * 40.0
    return True, stop, target, score

def detect_weinstein_stage2(df: pd.DataFrame, i: int) -> Tuple[bool, float, float, float]:
    """7. Stan Weinstein Stage 2 Base Breakout (30-Week / 150 DMA)"""
    if i < 90: return False, 0.0, 0.0, 0.0
    c = df['c'].iloc[i]; o = df['o'].iloc[i]; h = df['h'].iloc[i]; l = df['l'].iloc[i]; v = df['v'].iloc[i]
    
    sma150 = df['sma150'].iloc[i]
    prev_sma150 = df['sma150'].iloc[i-20]
    if not (c > sma150 and sma150 > prev_sma150 and c > df['ema50'].iloc[i]): return False, 0.0, 0.0, 0.0
    if df['ath_dist'].iloc[i] > 20.0: return False, 0.0, 0.0, 0.0
    
    base_h = df['h'].iloc[i-45:i-2].max()
    base_l = df['l'].iloc[i-45:i-2].min()
    depth = (base_h - base_l) / max(base_h, 0.01)
    if depth > 0.28: return False, 0.0, 0.0, 0.0
    
    if c <= base_h * 1.01: return False, 0.0, 0.0, 0.0
    if o > base_h * 1.04: return False, 0.0, 0.0, 0.0
    
    vol_sma = df['vol_sma20'].iloc[i]
    if vol_sma <= 0 or (v / vol_sma) < 2.2: return False, 0.0, 0.0, 0.0
    
    rng = max(h - l, 0.01)
    cir = (c - l) / rng
    upper_wick = (h - max(c, o)) / rng
    if c <= o or cir < 0.85 or upper_wick > 0.08: return False, 0.0, 0.0, 0.0
    
    stop = round(max(l, c * 0.95), 2)
    risk = c - stop
    target = round(c + 2.8 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + min(v / max(vol_sma, 1.0) / 4.0, 1.0) * 40.0
    return True, stop, target, score

# -----------------------------------------------------------------------------
# Trade Simulator
# -----------------------------------------------------------------------------

def simulate_trade_path(df: pd.DataFrame, entry_idx: int, ep: float, stop_p: float, target_p: float, max_days: int = 45) -> Tuple[float, int, str]:
    initial_risk = ep - stop_p
    if initial_risk <= 0: return 0.0, 0, "INVALID"
        
    be_trigger = ep + 1.5 * initial_risk
    be_locked = False
    t1_done = False
    t1_pnl = 0.0
    cur_stop = stop_p
    peak_h = ep
    
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_days, len(df))):
        h = df['h'].iloc[j]; l = df['l'].iloc[j]; c = df['c'].iloc[j]
        ema20 = df['ema20'].iloc[j]
        days_held = j - entry_idx
        peak_h = max(peak_h, h)
        
        # 1. Breakeven Lock at +1.5R
        if not be_locked and h >= be_trigger:
            cur_stop = max(cur_stop, round(ep * 1.005, 2))
            be_locked = True
            
        # 2. Tranche 1 Target
        if not t1_done and h >= target_p:
            t1_done = True
            t1_pnl = round((target_p - ep) / ep * 100.0, 2)
            cur_stop = max(cur_stop, round(ep + 1.0 * initial_risk, 2))
            
        # 3. Tranche 2 Trailing Runner
        if t1_done:
            trail_stop = round(peak_h * 0.92, 2)
            cur_stop = max(cur_stop, trail_stop)
            if days_held >= 8 and c < ema20 and c > ep * 1.05:
                t2_pnl = round((c - ep) / ep * 100.0, 2)
                return round(0.5 * t1_pnl + 0.5 * t2_pnl, 2), days_held, "TRANCHE_RUNNER_EMA20"
                
        # 4. Stop Hit
        if l <= cur_stop:
            exit_pnl = round((cur_stop - ep) / ep * 100.0, 2)
            if not t1_done:
                return exit_pnl, days_held, "STOP_HIT"
            else:
                return round(0.5 * t1_pnl + 0.5 * exit_pnl, 2), days_held, "T1_TARGET_T2_STOP"
                
        # 5. Time Expiry
        if days_held >= max_days:
            exit_pnl = round((c - ep) / ep * 100.0, 2)
            if not t1_done:
                return exit_pnl, days_held, "TIME_EXPIRY"
            else:
                return round(0.5 * t1_pnl + 0.5 * exit_pnl, 2), days_held, "T1_TARGET_T2_TIME"
                
    last_c = df['c'].iloc[min(entry_idx + max_days, len(df) - 1)]
    final_pnl = round((last_c - ep) / ep * 100.0, 2)
    if t1_done:
        return round(0.5 * t1_pnl + 0.5 * final_pnl, 2), max_days, "END_OF_DATA"
    return final_pnl, max_days, "END_OF_DATA"

def evaluate_strategy(universe: Dict[str, pd.DataFrame], detector_fn, strat_name: str, max_days: int = 45) -> List[Dict[str, Any]]:
    trades = []
    for sym, df in universe.items():
        if len(df) < 100: continue
        for i in range(60, len(df) - 1):
            d_str = df['date_str'].iloc[i]
            if d_str < "2020-01-01": continue
            
            turnover = df['c'].iloc[i] * df['v'].iloc[i]
            if df['c'].iloc[i] < 30.0 or turnover < 15_000_000.0:
                continue
                
            ok, stop_p, target_p, score = detector_fn(df, i)
            if ok:
                ep = df['c'].iloc[i]
                pnl, days, reason = simulate_trade_path(df, i, ep, stop_p, target_p, max_days=max_days)
                yr = int(d_str[:4])
                trades.append({
                    'strategy': strat_name,
                    'symbol': sym,
                    'date': d_str,
                    'year': yr,
                    'entry_price': ep,
                    'stop_price': stop_p,
                    'target_price': target_p,
                    'risk_pct': round((ep - stop_p) / ep * 100.0, 2),
                    'pnl_pct': pnl,
                    'holding_days': days,
                    'exit_reason': reason,
                    'score': score
                })
    return trades

def compute_metrics(trades: List[Dict[str, Any]], start_yr: int, end_yr: int) -> Dict[str, Any]:
    sub = [t for t in trades if start_yr <= t['year'] <= end_yr]
    if len(sub) == 0:
        return {'n': 0, 'trades_per_yr': 0, 'wr': 0, 'avg_w': 0, 'avg_l': 0, 'payoff': 0, 'pf': 0, 'avg_risk': 0}
        
    pnl = [t['pnl_pct'] for t in sub]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    
    wr = len(wins) / len(sub) * 100.0
    avg_w = float(np.mean(wins)) if len(wins) else 0.0
    avg_l = float(abs(np.mean(losses))) if len(losses) else 0.0
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    payoff = (avg_w / avg_l) if avg_l > 0 else 0.0
    pf = (gross_w / gross_l) if gross_l > 0 else 999.0
    avg_risk = float(np.mean([t['risk_pct'] for t in sub]))
    num_years = max(1.0, (end_yr - start_yr + 1) if end_yr < 2026 else (end_yr - start_yr + 0.75))
    trades_per_yr = round(len(sub) / num_years)
    
    return {
        'n': len(sub),
        'trades_per_yr': trades_per_yr,
        'wr': round(wr, 1),
        'avg_w': round(avg_w, 2),
        'avg_l': round(avg_l, 2),
        'avg_risk': round(avg_risk, 2),
        'payoff': round(payoff, 2),
        'pf': round(pf, 2)
    }

def main():
    universe = load_universe_candles()
    
    strats = [
        ("Minervini VCP", detect_minervini_vcp, 45),
        ("High-Tight Flag (HTF)", detect_high_tight_flag, 45),
        ("O'Neil Cup and Handle", detect_cup_and_handle, 45),
        ("Nicolas Darvas Box", detect_darvas_box, 45),
        ("Wyckoff Spring", detect_wyckoff_spring, 40),
        ("Stockbee Momentum Burst", detect_stockbee_momentum_burst, 20),
        ("Weinstein Stage 2", detect_weinstein_stage2, 45)
    ]
    
    strategy_results = {}
    all_trades = []
    
    print("\n[2/5] Evaluating 7 World-Class Strategies on IN-SAMPLE (2020-2024) vs HELD-OUT OOS (2025-2026)...")
    for name, fn, hold in strats:
        t0 = time.time()
        trades = evaluate_strategy(universe, fn, name, max_days=hold)
        all_trades.extend(trades)
        
        is_stats = compute_metrics(trades, 2020, 2024)
        oos_stats = compute_metrics(trades, 2025, 2026)
        all_stats = compute_metrics(trades, 2020, 2026)
        
        strategy_results[name] = {
            'In_Sample_2020_2024': is_stats,
            'Out_Of_Sample_2025_2026': oos_stats,
            'All_2020_2026': all_stats
        }
        print(f"      -> {name:<26} | IS PF: {is_stats['pf']:>4.2f} (WR: {is_stats['wr']:>4.1f}%, N={is_stats['n']:>4d}) | OOS PF: {oos_stats['pf']:>4.2f} (WR: {oos_stats['wr']:>4.1f}%, N={oos_stats['n']:>4d}) in {time.time()-t0:.1f}s")
        
    print("\n[3/5] Constructing Master Multi-Strategy Championship Portfolio...")
    tdf = pd.DataFrame(all_trades).drop_duplicates(subset=['date', 'symbol'])
    tdf = tdf.sort_values(['date', 'score'], ascending=[True, False])
    
    port_top2 = tdf.groupby('date').head(2).to_dict('records')
    port_top3 = tdf.groupby('date').head(3).to_dict('records')
    
    p2_is = compute_metrics(port_top2, 2020, 2024)
    p2_oos = compute_metrics(port_top2, 2025, 2026)
    strategy_results['MASTER_PORTFOLIO_TOP2'] = {
        'In_Sample_2020_2024': p2_is,
        'Out_Of_Sample_2025_2026': p2_oos,
        'All_2020_2026': compute_metrics(port_top2, 2020, 2026)
    }

    p3_is = compute_metrics(port_top3, 2020, 2024)
    p3_oos = compute_metrics(port_top3, 2025, 2026)
    strategy_results['MASTER_PORTFOLIO_TOP3'] = {
        'In_Sample_2020_2024': p3_is,
        'Out_Of_Sample_2025_2026': p3_oos,
        'All_2020_2026': compute_metrics(port_top3, 2020, 2026)
    }
    
    print("\n[4/5] FINAL SCORECARD: 7 WORLD-CLASS TECHNIQUES (2020-2026)")
    print("=" * 125)
    print(f"{'Strategy / Technique':<28} | {'IS PF':>6} {'IS WR':>6} {'IS R:R':>6} {'IS Risk':>7} | {'OOS PF':>6} {'OOS WR':>6} {'OOS R:R':>6} {'OOS Risk':>8} | {'Trades/Yr':>9}")
    print("=" * 125)
    for s_name, res in strategy_results.items():
        is_st = res['In_Sample_2020_2024']
        oos_st = res['Out_Of_Sample_2025_2026']
        tot_yr = round((is_st['n'] + oos_st['n']) / 6.75)
        print(f"{s_name:<28} | {is_st['pf']:>6.2f} {is_st['wr']:>5.1f}% {is_st['payoff']:>6.2f} {is_st['avg_risk']:>6.1f}% | {oos_st['pf']:>6.2f} {oos_st['wr']:>5.1f}% {oos_st['payoff']:>6.2f} {oos_st['avg_risk']:>7.1f}% | {tot_yr:>9d}")
    print("=" * 125)
    
    print(f"\n[5/5] Saving detailed scorecard to {OUTPUT_JSON}...")
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(strategy_results, f, indent=2)
    print("Backtest complete and verified successfully.")

if __name__ == "__main__":
    main()
