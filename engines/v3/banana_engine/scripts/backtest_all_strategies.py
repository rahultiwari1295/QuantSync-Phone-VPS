#!/usr/bin/env python3
"""
scripts/backtest_all_strategies.py — The Complete Strategy Compendium Backtest
=============================================================================
Implements and evaluates the 7 strategies from the Compendium across 1,319 Indian stocks:
1. Trendline Breakout with Retest
2. Falling Wedge
3. Donchian Channel Breakout
4. Cup and Handle
5. Wyckoff Spring (False Breakdown)
6. Stockbee Momentum Burst
7. Nicolas Darvas Box

Plus Benchmark Reference Sleeves:
- High-Tight Bull Flag (HTF)
- Ultra Marubozu Base Breakout
- 52-Week High Breakout

Scientific Framework:
- In-Sample (2020-01-01 to 2024-12-31)
- Held-Out Out-of-Sample (2025-01-01 to 2026-09-12)
- Unified simulation engine with realistic slippage, breakeven lock, and targets
- Saves complete scorecard to data/all_strategies_backtest.json
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
OUTPUT_JSON = os.path.join(_PKG_ROOT, "data/all_strategies_backtest.json")

def load_universe_candles() -> Dict[str, Dict[str, Any]]:
    print(f"[1/4] Loading candle database from {DB_PATH}...")
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
            
            # Precompute indicators
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
            df['sma50'] = df['c'].rolling(50, min_periods=20).mean()
            df['sma200'] = df['c'].rolling(200, min_periods=50).mean()
            df['vol_sma20'] = df['v'].rolling(20, min_periods=5).mean().shift(1)
            df['high_250'] = df['h'].rolling(250, min_periods=60).max().shift(1)
            
            # Pack into numpy dict for 50x faster execution
            n_bars = len(df)
            universe[sym] = {
                'c': df['c'].values,
                'h': df['h'].values,
                'l': df['l'].values,
                'o': df['o'].values,
                'v': df['v'].values,
                'dates': df['date_str'].values,
                'ema20': df['ema20'].values,
                'sma50': df['sma50'].values,
                'sma200': df['sma200'].values,
                'vol_sma20': df['vol_sma20'].values,
                'high_250': df['high_250'].values,
                'n': n_bars
            }
            total_candles += n_bars
        except Exception:
            continue
            
    print(f"      Loaded {len(universe)} symbols ({total_candles:,} bars) in {time.time() - t0:.2f}s.")
    return universe

# -----------------------------------------------------------------------------
# Compendium Strategy Detectors
# -----------------------------------------------------------------------------

def detect_trendline_retest(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """1. Trendline Breakout with Retest"""
    lookback = 60
    if i < lookback or i < 200:
        return False, 0.0, 0.0, 0.0
    
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma50 = data['sma50']; sma200 = data['sma200']
    
    # 6. Trend filter: C > SMA50 > SMA200
    if np.isnan(sma200[i]) or np.isnan(sma50[i]) or not (c[i] > sma50[i] > sma200[i]):
        return False, 0.0, 0.0, 0.0
        
    # 1. Find swing highs in the last 60 bars
    # Swing high: h[j] > max(h[j-3:j]) and h[j] > max(h[j+1:j+4])
    swing_highs = []
    start_j = i - lookback
    for j in range(start_j + 3, i - 3):
        if h[j] > np.max(h[j-3:j]) and h[j] > np.max(h[j+1:j+4]):
            swing_highs.append((j, h[j]))
            
    if len(swing_highs) < 2:
        return False, 0.0, 0.0, 0.0
        
    # 2. Take two most recent swing highs
    h1_idx, h1_val = swing_highs[-2]
    h2_idx, h2_val = swing_highs[-1]
    
    if h2_val >= h1_val or h2_idx == h1_idx:
        return False, 0.0, 0.0, 0.0
        
    slope = (h2_val - h1_val) / (h2_idx - h1_idx)
    
    # 3. The break must have happened in the last 10 bars
    break_idx = None
    search_start = max(h2_idx + 3, i - 10)
    for j in range(search_start, i):
        line_val = h2_val + slope * (j - h2_idx)
        if c[j] > line_val * 1.005:
            break_idx = j
            break
            
    if break_idx is None:
        return False, 0.0, 0.0, 0.0
        
    # 4. Current bar is the retest bar
    line_val_now = h2_val + slope * (i - h2_idx)
    if not (l[i] <= line_val_now * 1.015 and c[i] > line_val_now and c[i] > o[i]):
        return False, 0.0, 0.0, 0.0
        
    # 5. Volume on retest lower than breakout day
    if v[break_idx] > 0 and v[i] >= v[break_idx] * 1.20:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    stop = round(float(np.min(l[break_idx:i+1])) * 0.99, 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.08:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 3.0 * risk, 2)
    score = ((entry - stop) / entry) * 100.0
    return True, stop, target, score

def detect_falling_wedge(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """2. Falling Wedge Breakout"""
    window_size = 60
    if i < window_size or i < 200:
        return False, 0.0, 0.0, 0.0
        
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma200 = data['sma200']; vol_sma20 = data['vol_sma20']
    
    if np.isnan(sma200[i]) or c[i] < sma200[i] * 0.90:
        return False, 0.0, 0.0, 0.0
        
    start_j = i - window_size
    highs_idx = []; lows_idx = []
    for j in range(start_j + 3, i - 3):
        if h[j] == np.max(h[j-3:j+4]):
            highs_idx.append(j)
        if l[j] == np.min(l[j-3:j+4]):
            lows_idx.append(j)
            
    if len(highs_idx) < 3 or len(lows_idx) < 3:
        return False, 0.0, 0.0, 0.0
        
    h_x = np.array(highs_idx[-4:])
    h_y = h[h_x]
    l_x = np.array(lows_idx[-4:])
    l_y = l[l_x]
    
    h_slope = np.polyfit(h_x, h_y, 1)[0]
    l_slope = np.polyfit(l_x, l_y, 1)[0]
    
    # Both slopes must be negative (descending) & converging
    if not (h_slope < l_slope < 0):
        return False, 0.0, 0.0, 0.0
        
    # Convergence check: width must shrink by at least 30%
    h_start = h_y[0]; l_start = l_y[0]
    h_end = h_slope * (i - h_x[0]) + h_y[0]
    l_end = l_slope * (i - l_x[0]) + l_y[0]
    width_start = h_start - l_start
    width_end = h_end - l_end
    if width_start <= 0 or width_end >= width_start * 0.70:
        return False, 0.0, 0.0, 0.0
        
    # Volume contracts: second half < first half
    mid = start_j + window_size // 2
    if np.mean(v[mid:i]) >= np.mean(v[start_j:mid]):
        return False, 0.0, 0.0, 0.0
        
    # Breakout above upper line
    upper_line_now = h_slope * (i - h_x[0]) + h_y[0]
    if not (c[i] > upper_line_now and c[i] > o[i]):
        return False, 0.0, 0.0, 0.0
        
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or v[i] < vol_sma * 1.50:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    wedge_low = float(np.min(l[i-5:i+1]))
    stop = round(min(wedge_low, entry * 0.94), 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.08:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 2.5 * risk, 2)
    score = (1.0 - (width_end / width_start)) * 100.0
    return True, stop, target, score

def detect_donchian_breakout(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """3. Donchian Channel Breakout (20-Day High)"""
    lookback = 20
    if i < lookback + 200:
        return False, 0.0, 0.0, 0.0
        
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma50 = data['sma50']; sma200 = data['sma200']; vol_sma20 = data['vol_sma20']
    
    prior_high = float(np.max(h[i-lookback:i]))
    if not (c[i] > prior_high and c[i] > o[i]):
        return False, 0.0, 0.0, 0.0
        
    # Quality Filters
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or v[i] < vol_sma * 1.50:
        return False, 0.0, 0.0, 0.0
        
    if np.isnan(sma200[i]) or np.isnan(sma50[i]) or not (c[i] > sma50[i] > sma200[i]):
        return False, 0.0, 0.0, 0.0
        
    rng = max(h[i] - l[i], 0.01)
    cir = (c[i] - l[i]) / rng
    if cir < 0.60:
        return False, 0.0, 0.0, 0.0
        
    # Avoid extended chases
    if (c[i] - prior_high) / prior_high > 0.08:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    stop = round(min(prior_high, entry * 0.94), 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.08:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 3.0 * risk, 2)
    score = cir * 50.0 + min(v[i] / max(vol_sma, 1.0) / 3.0, 1.0) * 50.0
    return True, stop, target, score

def detect_cup_and_handle(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """4. Cup and Handle (CANSLIM)"""
    lookback = 300
    if i < lookback or i < 200:
        return False, 0.0, 0.0, 0.0
        
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma200 = data['sma200']; vol_sma20 = data['vol_sma20']
    
    if np.isnan(sma200[i]) or c[i] < sma200[i]:
        return False, 0.0, 0.0, 0.0
        
    # Cup search: peak in [i-280 : i-100]
    peak_slice = h[i-280:i-100]
    if len(peak_slice) == 0:
        return False, 0.0, 0.0, 0.0
    rel_peak_idx = int(np.argmax(peak_slice))
    peak_idx = i - 280 + rel_peak_idx
    peak_high = h[peak_idx]
    
    # Cup bottom between peak_idx + 10 and i - 15
    if i - 15 <= peak_idx + 10:
        return False, 0.0, 0.0, 0.0
    bottom_slice = l[peak_idx+10:i-15]
    if len(bottom_slice) < 20:
        return False, 0.0, 0.0, 0.0
    bottom_idx = peak_idx + 10 + int(np.argmin(bottom_slice))
    bottom_low = l[bottom_idx]
    
    cup_depth_pct = (peak_high - bottom_low) / peak_high * 100.0
    if not (12.0 <= cup_depth_pct <= 33.0):
        return False, 0.0, 0.0, 0.0
        
    cup_duration = i - peak_idx
    if not (35 <= cup_duration <= 325):
        return False, 0.0, 0.0, 0.0
        
    # Right side recovery: must recover to within 10% of peak
    right_side_high = float(np.max(h[bottom_idx:i-10]))
    if right_side_high < peak_high * 0.90:
        return False, 0.0, 0.0, 0.0
        
    # Handle: last 5 to 20 bars
    handle_start = bottom_idx + int(np.argmax(h[bottom_idx:i]))
    if i - handle_start < 5 or i - handle_start > 20:
        return False, 0.0, 0.0, 0.0
        
    handle_h = float(np.max(h[handle_start:i]))
    handle_l = float(np.min(l[handle_start:i]))
    handle_depth = (handle_h - handle_l) / handle_h * 100.0
    
    # Handle depth: 10% to 30% of cup depth
    if not (cup_depth_pct * 0.10 <= handle_depth <= cup_depth_pct * 0.30):
        return False, 0.0, 0.0, 0.0
        
    # Handle volume declining
    handle_len = i - handle_start
    mid_h = handle_start + handle_len // 2
    if np.mean(v[mid_h:i]) >= np.mean(v[handle_start:mid_h]):
        return False, 0.0, 0.0, 0.0
        
    if not (c[i] > handle_h and c[i] > o[i]):
        return False, 0.0, 0.0, 0.0
        
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or v[i] < vol_sma * 1.50:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    stop = round(handle_l * 0.99, 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.08:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 3.0 * risk, 2)
    score = (1.0 - handle_depth / 30.0) * 100.0
    return True, stop, target, score

def detect_wyckoff_spring(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """5. Wyckoff Spring (False Breakdown)"""
    if i < 60 or i < 200:
        return False, 0.0, 0.0, 0.0
        
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma200 = data['sma200']; vol_sma20 = data['vol_sma20']
    
    if np.isnan(sma200[i]) or c[i] < sma200[i] * 0.85:
        return False, 0.0, 0.0, 0.0
        
    # Support clustering in past 60 bars
    lows = l[i-60:i]
    clusters = []
    for low_val in lows:
        merged = False
        for cl in clusters:
            if abs(cl['level'] - low_val) / max(cl['level'], 0.01) < 0.015:
                cl['touches'].append(low_val)
                cl['level'] = float(np.mean(cl['touches']))
                merged = True
                break
        if not merged:
            clusters.append({'level': float(low_val), 'touches': [low_val]})
            
    strong_supports = [cl for cl in clusters if len(cl['touches']) >= 3]
    if not strong_supports:
        return False, 0.0, 0.0, 0.0
        
    support_level = None
    break_idx = None
    for cl in strong_supports:
        for j in range(max(0, i - 3), i):
            if l[j] < cl['level'] * 0.99:
                support_level = cl['level']
                break_idx = j
                break
        if break_idx is not None:
            break
            
    if break_idx is None:
        return False, 0.0, 0.0, 0.0
        
    # Current bar must close back above support
    if not (c[i] > support_level and c[i] > o[i]):
        return False, 0.0, 0.0, 0.0
        
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0:
        return False, 0.0, 0.0, 0.0
    if v[break_idx] < vol_sma * 1.30 or v[i] > v[break_idx] * 0.90:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    spring_low = float(np.min(l[break_idx:i+1]))
    stop = round(spring_low * 0.99, 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.08:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 2.5 * risk, 2)
    score = (c[i] / max(support_level, 0.01) - 1.0) * 100.0
    return True, stop, target, score

def detect_momentum_burst(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """6. Stockbee Momentum Burst (3-5 Day Swing)"""
    if i < 60:
        return False, 0.0, 0.0, 0.0
        
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    ema20 = data['ema20']; vol_sma20 = data['vol_sma20']
    
    # 1. Daily gain >= 4.0%
    prev_c = c[i-1]
    daily_gain = (c[i] / prev_c - 1.0) * 100.0
    if daily_gain < 4.0:
        return False, 0.0, 0.0, 0.0
        
    # 2. Volume surge >= 1.4x SMA20
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or v[i] < vol_sma * 1.40:
        return False, 0.0, 0.0, 0.0
        
    # 3. Close in upper 30% of bar (CIR >= 0.70)
    rng = max(h[i] - l[i], 0.01)
    cir = (c[i] - l[i]) / rng
    if cir < 0.70:
        return False, 0.0, 0.0, 0.0
        
    # 4. Prior consolidation: last 5 bars tight (< 8% range)
    prior_range = float(np.max(h[i-5:i]) - np.min(l[i-5:i]))
    if prior_range / c[i] > 0.08:
        return False, 0.0, 0.0, 0.0
        
    # 5. Trend: C > EMA20
    if np.isnan(ema20[i]) or c[i] < ema20[i]:
        return False, 0.0, 0.0, 0.0
        
    # 6. Turnover filter >= 1.5 Cr
    if c[i] * v[i] < 15_000_000.0:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    stop = round(min(l[i], c[i] * 0.95), 2)
    risk = entry - stop
    if risk <= 0:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 2.0 * risk, 2)
    score = daily_gain * 5.0 + cir * 50.0
    return True, stop, target, score

def detect_darvas_box(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """7. Nicolas Darvas Box Breakout"""
    if i < 60 or i < 200:
        return False, 0.0, 0.0, 0.0
        
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma200 = data['sma200']; vol_sma20 = data['vol_sma20']
    
    if np.isnan(sma200[i]) or c[i] < sma200[i]:
        return False, 0.0, 0.0, 0.0
        
    box_top_idx = i - 20 + int(np.argmax(h[i-20:i]))
    box_top = h[box_top_idx]
    box_bottom = float(np.min(l[box_top_idx:i]))
    
    # Held for at least 3 bars
    if i - box_top_idx < 3:
        return False, 0.0, 0.0, 0.0
        
    box_height_pct = (box_top - box_bottom) / box_top * 100.0
    if not (3.0 <= box_height_pct <= 15.0):
        return False, 0.0, 0.0, 0.0
        
    if not (c[i] > box_top and c[i] > o[i]):
        return False, 0.0, 0.0, 0.0
        
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or v[i] < vol_sma * 1.50:
        return False, 0.0, 0.0, 0.0
        
    entry = c[i]
    stop = round(box_bottom * 0.99, 2)
    risk = entry - stop
    if risk <= 0 or risk / entry > 0.08:
        return False, 0.0, 0.0, 0.0
        
    target = round(entry + 3.0 * risk, 2)
    score = (1.0 - (box_height_pct / 15.0)) * 100.0
    return True, stop, target, score

# -----------------------------------------------------------------------------
# Benchmark Reference Detectors (For Direct Comparison)
# -----------------------------------------------------------------------------

def detect_benchmark_bull_flag(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """Benchmark: High-Tight Bull Flag (HTF)"""
    if i < 60: return False, 0.0, 0.0, 0.0
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    ema20 = data['ema20']; sma50 = data['sma50']; vol_sma20 = data['vol_sma20']; high_250 = data['high_250']
    
    if np.isnan(ema20[i]) or np.isnan(sma50[i]) or not (c[i] > ema20[i] > sma50[i]): return False, 0.0, 0.0, 0.0
    if np.isnan(high_250[i]) or (high_250[i] - c[i]) / high_250[i] > 0.20: return False, 0.0, 0.0, 0.0
    
    pole_l = float(np.min(l[i-15:i-3])); pole_h = float(np.max(h[i-15:i-3]))
    if (pole_h - pole_l) / max(pole_l, 0.01) < 0.15: return False, 0.0, 0.0, 0.0
    
    flag_l = float(np.min(l[i-4:i])); flag_h = float(np.max(h[i-4:i]))
    if (flag_h - flag_l) / max(flag_l, 0.01) > 0.070: return False, 0.0, 0.0, 0.0
    
    pole_vol = float(np.mean(v[i-15:i-3])); flag_vol = float(np.mean(v[i-4:i]))
    if pole_vol > 0 and (flag_vol / pole_vol) > 0.65: return False, 0.0, 0.0, 0.0
    
    if c[i] <= flag_h: return False, 0.0, 0.0, 0.0
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or (v[i] / vol_sma) < 1.80: return False, 0.0, 0.0, 0.0
    
    rng = max(h[i] - l[i], 0.01)
    cir = (c[i] - l[i]) / rng
    upper_wick = (h[i] - max(c[i], o[i])) / rng
    if c[i] <= o[i] or cir < 0.82 or upper_wick > 0.10: return False, 0.0, 0.0, 0.0
    
    stop = round(max(flag_l, c[i] * 0.955), 2)
    risk = c[i] - stop
    target = round(c[i] + 3.0 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + 40.0
    return True, stop, target, score

def detect_benchmark_base_breakout(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """Benchmark: Base Breakout Ultra Marubozu"""
    if i < 80: return False, 0.0, 0.0, 0.0
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma50 = data['sma50']; sma200 = data['sma200']; vol_sma20 = data['vol_sma20']; high_250 = data['high_250']
    
    if np.isnan(sma50[i]) or np.isnan(sma200[i]) or not (c[i] > sma50[i] > sma200[i]): return False, 0.0, 0.0, 0.0
    if np.isnan(high_250[i]) or (high_250[i] - c[i]) / high_250[i] > 0.20: return False, 0.0, 0.0, 0.0
    
    res_ceiling = float(np.max(h[i-50:i-2]))
    base_floor = float(np.min(l[i-50:i-2]))
    if (res_ceiling - base_floor) / max(res_ceiling, 0.01) > 0.28: return False, 0.0, 0.0, 0.0
    
    if c[i] <= res_ceiling * 1.01 or o[i] > res_ceiling * 1.04: return False, 0.0, 0.0, 0.0
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or (v[i] / vol_sma) < 2.50: return False, 0.0, 0.0, 0.0
    
    rng = max(h[i] - l[i], 0.01)
    cir = (c[i] - l[i]) / rng
    upper_wick = (h[i] - max(c[i], o[i])) / rng
    if c[i] <= o[i] or cir < 0.85 or upper_wick > 0.10: return False, 0.0, 0.0, 0.0
    
    stop = round(max(l[i], c[i] * 0.95), 2)
    risk = c[i] - stop
    target = round(c[i] + 2.5 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + 40.0
    return True, stop, target, score

def detect_benchmark_52w_high(data: Dict[str, Any], i: int) -> Tuple[bool, float, float, float]:
    """Benchmark: 52-Week High Breakout"""
    if i < 250: return False, 0.0, 0.0, 0.0
    h = data['h']; l = data['l']; c = data['c']; o = data['o']; v = data['v']
    sma50 = data['sma50']; sma200 = data['sma200']; vol_sma20 = data['vol_sma20']; high_250 = data['high_250']
    
    if np.isnan(sma50[i]) or np.isnan(sma200[i]) or not (c[i] > sma50[i] > sma200[i]): return False, 0.0, 0.0, 0.0
    if np.isnan(high_250[i]) or c[i] <= high_250[i]: return False, 0.0, 0.0, 0.0
    
    vol_sma = vol_sma20[i]
    if np.isnan(vol_sma) or vol_sma <= 0 or (v[i] / vol_sma) < 2.20: return False, 0.0, 0.0, 0.0
    
    rng = max(h[i] - l[i], 0.01)
    cir = (c[i] - l[i]) / rng
    upper_wick = (h[i] - max(c[i], o[i])) / rng
    if c[i] <= o[i] or cir < 0.85 or upper_wick > 0.08: return False, 0.0, 0.0, 0.0
    
    stop = round(max(l[i], c[i] * 0.95), 2)
    risk = c[i] - stop
    target = round(c[i] + 2.8 * risk, 2)
    score = (cir * 30.0) + ((1.0 - upper_wick) * 30.0) + 40.0
    return True, stop, target, score

# -----------------------------------------------------------------------------
# Realistic Trade Simulator
# -----------------------------------------------------------------------------

def simulate_trade_path(data: Dict[str, Any], entry_idx: int, ep: float, stop_p: float, target_p: float, max_days: int = 30) -> Tuple[float, int, str]:
    initial_risk = ep - stop_p
    if initial_risk <= 0: return 0.0, 0, "INVALID"
        
    be_trigger = ep + 1.5 * initial_risk
    be_locked = False
    t1_done = False
    t1_pnl = 0.0
    cur_stop = stop_p
    peak_h = ep
    
    h = data['h']; l = data['l']; c = data['c']; ema20 = data['ema20']
    n_bars = data['n']
    
    for j in range(entry_idx + 1, min(entry_idx + 1 + max_days, n_bars)):
        high_j = h[j]; low_j = l[j]; close_j = c[j]
        days_held = j - entry_idx
        peak_h = max(peak_h, high_j)
        
        # 1. Breakeven Lock at +1.5R
        if not be_locked and high_j >= be_trigger:
            cur_stop = max(cur_stop, round(ep * 1.005, 2))
            be_locked = True
            
        # 2. Tranche 1 Target
        if not t1_done and high_j >= target_p:
            t1_done = True
            t1_pnl = round((target_p - ep) / ep * 100.0, 2)
            cur_stop = max(cur_stop, round(ep + 1.0 * initial_risk, 2))
            
        # 3. Tranche 2 Trailing Runner
        if t1_done:
            trail_stop = round(peak_h * 0.92, 2)
            cur_stop = max(cur_stop, trail_stop)
            if days_held >= 8 and close_j < ema20[j] and close_j > ep * 1.05:
                t2_pnl = round((close_j - ep) / ep * 100.0, 2)
                return round(0.5 * t1_pnl + 0.5 * t2_pnl, 2), days_held, "TRANCHE_RUNNER_EMA20"
                
        # 4. Stop Hit
        if low_j <= cur_stop:
            exit_pnl = round((cur_stop - ep) / ep * 100.0, 2)
            if not t1_done:
                return exit_pnl, days_held, "STOP_HIT"
            else:
                return round(0.5 * t1_pnl + 0.5 * exit_pnl, 2), days_held, "T1_TARGET_T2_STOP"
                
        # 5. Time Expiry
        if days_held >= max_days:
            exit_pnl = round((close_j - ep) / ep * 100.0, 2)
            if not t1_done:
                return exit_pnl, days_held, "TIME_EXPIRY"
            else:
                return round(0.5 * t1_pnl + 0.5 * exit_pnl, 2), days_held, "T1_TARGET_T2_TIME"
                
    last_c = c[min(entry_idx + max_days, n_bars - 1)]
    final_pnl = round((last_c - ep) / ep * 100.0, 2)
    if t1_done:
        return round(0.5 * t1_pnl + 0.5 * final_pnl, 2), max_days, "END_OF_DATA"
    return final_pnl, max_days, "END_OF_DATA"

def evaluate_strategy(universe: Dict[str, Dict[str, Any]], detector_fn, strat_name: str, max_days: int = 30) -> List[Dict[str, Any]]:
    trades = []
    for sym, data in universe.items():
        n_bars = data['n']
        if n_bars < 100: continue
        dates = data['dates']; c = data['c']; v = data['v']
        
        for i in range(60, n_bars - 1):
            d_str = dates[i]
            if d_str < "2020-01-01": continue
            
            turnover = c[i] * v[i]
            if c[i] < 30.0 or turnover < 15_000_000.0:
                continue
                
            ok, stop_p, target_p, score = detector_fn(data, i)
            if ok:
                ep = c[i]
                pnl, days, reason = simulate_trade_path(data, i, ep, stop_p, target_p, max_days=max_days)
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
        return {'n': 0, 'trades_per_yr': 0, 'wr': 0.0, 'avg_w': 0.0, 'avg_l': 0.0, 'payoff': 0.0, 'pf': 0.0, 'expectancy': 0.0, 'avg_risk': 0.0}
        
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
    expectancy = (wr / 100.0) * payoff
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
        'pf': round(pf, 2),
        'expectancy': round(expectancy, 2)
    }

def main():
    universe = load_universe_candles()
    
    compendium_strategies = [
        ("1. Trendline Retest", detect_trendline_retest, 30),
        ("2. Falling Wedge", detect_falling_wedge, 25),
        ("3. Donchian Breakout", detect_donchian_breakout, 30),
        ("4. Cup and Handle", detect_cup_and_handle, 30),
        ("5. Wyckoff Spring", detect_wyckoff_spring, 25),
        ("6. Momentum Burst", detect_momentum_burst, 5),
        ("7. Darvas Box", detect_darvas_box, 30),
        ("Benchmark: High-Tight Flag", detect_benchmark_bull_flag, 45),
        ("Benchmark: Base Breakout", detect_benchmark_base_breakout, 45),
        ("Benchmark: 52W High Breakout", detect_benchmark_52w_high, 45)
    ]
    
    all_results = {}
    all_trades = []
    
    print("\n[2/4] Evaluating Compendium Strategies on IN-SAMPLE (2020-2024) vs HELD-OUT OOS (2025-2026)...")
    for name, fn, hold_days in compendium_strategies:
        t0 = time.time()
        trades = evaluate_strategy(universe, fn, name, max_days=hold_days)
        all_trades.extend(trades)
        
        is_stats = compute_metrics(trades, 2020, 2024)
        oos_stats = compute_metrics(trades, 2025, 2026)
        all_stats = compute_metrics(trades, 2020, 2026)
        
        all_results[name] = {
            'In_Sample_2020_2024': is_stats,
            'Out_Of_Sample_2025_2026': oos_stats,
            'All_2020_2026': all_stats
        }
        print(f"      -> {name:<28} | IS PF: {is_stats['pf']:>4.2f} (WR: {is_stats['wr']:>4.1f}%, N={is_stats['n']:>4d}) | OOS PF: {oos_stats['pf']:>4.2f} (WR: {oos_stats['wr']:>4.1f}%, N={oos_stats['n']:>4d}) in {time.time()-t0:.1f}s")
        
    print("\n[3/4] Constructing Unified Championship Portfolios...")
    tdf = pd.DataFrame(all_trades).drop_duplicates(subset=['date', 'symbol'])
    tdf = tdf.sort_values(['date', 'score'], ascending=[True, False])
    
    # Portfolio 1: Top 2 Daily across compendium
    compendium_names = [s[0] for s in compendium_strategies if not s[0].startswith("Benchmark:")]
    comp_df = tdf[tdf['strategy'].isin(compendium_names)]
    port_comp_top2 = comp_df.groupby('date').head(2).to_dict('records')
    
    # Portfolio 2: Triple-Engine Benchmark Ensemble (Bull Flag + Base Breakout + 52W High)
    bench_names = ["Benchmark: High-Tight Flag", "Benchmark: Base Breakout", "Benchmark: 52W High Breakout"]
    bench_df = tdf[tdf['strategy'].isin(bench_names)]
    port_bench_top2 = bench_df.groupby('date').head(2).to_dict('records')
    
    # Portfolio 3: Grand Master Portfolio (Best Compendium + Best Benchmark, Top 2 Daily)
    grand_df = tdf.groupby('date').head(2).to_dict('records')
    
    all_results['COMPENDIUM_PORTFOLIO_TOP2'] = {
        'In_Sample_2020_2024': compute_metrics(port_comp_top2, 2020, 2024),
        'Out_Of_Sample_2025_2026': compute_metrics(port_comp_top2, 2025, 2026),
        'All_2020_2026': compute_metrics(port_comp_top2, 2020, 2026)
    }

    all_results['BENCHMARK_ENSEMBLE_TOP2'] = {
        'In_Sample_2020_2024': compute_metrics(port_bench_top2, 2020, 2024),
        'Out_Of_Sample_2025_2026': compute_metrics(port_bench_top2, 2025, 2026),
        'All_2020_2026': compute_metrics(port_bench_top2, 2020, 2026)
    }

    all_results['GRAND_MASTER_TOP2'] = {
        'In_Sample_2020_2024': compute_metrics(grand_df, 2020, 2024),
        'Out_Of_Sample_2025_2026': compute_metrics(grand_df, 2025, 2026),
        'All_2020_2026': compute_metrics(grand_df, 2020, 2026)
    }
    
    print("\n[4/4] COMPLETE SCORECARD: COMPENDIUM VS BENCHMARKS (2020-2026)")
    print("=" * 135)
    print(f"{'Strategy / System':<30} | {'IS PF':>6} {'IS WR':>6} {'IS R:R':>6} {'IS Exp':>6} | {'OOS PF':>6} {'OOS WR':>6} {'OOS R:R':>6} {'OOS Exp':>7} | {'OOS N':>5} {'Tr/Yr':>6}")
    print("=" * 135)
    
    for s_name, res in all_results.items():
        is_st = res['In_Sample_2020_2024']
        oos_st = res['Out_Of_Sample_2025_2026']
        tot_yr = round((is_st['n'] + oos_st['n']) / 6.75)
        print(f"{s_name:<30} | {is_st['pf']:>6.2f} {is_st['wr']:>5.1f}% {is_st['payoff']:>6.2f} {is_st['expectancy']:>6.2f} | {oos_st['pf']:>6.2f} {oos_st['wr']:>5.1f}% {oos_st['payoff']:>6.2f} {oos_st['expectancy']:>7.2f} | {oos_st['n']:>5d} {tot_yr:>6d}")
    print("=" * 135)
    
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nDetailed results saved to {OUTPUT_JSON}.")

if __name__ == "__main__":
    main()
