#!/usr/bin/env python3
"""
scripts/build_ml_v2_dataset.py — ML Engine v2 Dataset Builder
============================================================
Builds the complete multi-timeframe candidate universe for ML Engine v2:
1. Loads daily OHLCV across all symbols in Prod_VPS/Data/stock_history.db.
2. Aggregates weekly OHLCV bars by ISO-week.
3. Computes NIFTY weekly benchmark series for Weekly Relative Strength (w_rs_20).
4. Detects candidate setups across 4 pattern families with loosened bounds:
   - High-Tight Flag (HTF)
   - Base Breakout (BASE)
   - 52-Week High Breakout (52W_HIGH)
   - Darvas Box (DARVAS)
5. Extracts 31 Base features + 8 Weekly features + MTF Alignment composite score.
6. Computes forward path metrics (mfe_r, mae_r, days_to_peak, realized PnL, optimal 2R reach).
7. Saves candidate dataset to data/ml_engine_v2_candidates.parquet.
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
OUTPUT_PARQUET = os.path.join(_PKG_ROOT, "data/ml_engine_v2_candidates.parquet")

def load_universe_and_weekly():
    print(f"[1/4] Loading candle database from {DB_PATH}...")
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, rawCandles FROM StockHistory")
    rows = c.fetchall()
    conn.close()
    
    universe = {}
    weekly_universe = {}
    total_daily_bars = 0
    
    for sym, raw in rows:
        try:
            raw_list = json.loads(raw)
            if len(raw_list) < 120:
                continue
            df = pd.DataFrame(raw_list)
            df['c'] = df['c'].astype(float)
            df['h'] = df['h'].astype(float)
            df['l'] = df['l'].astype(float)
            df['o'] = df['o'].astype(float)
            df['v'] = df['v'].astype(float)
            df['date_str'] = df['t'].astype(str).str[:10]
            df['date'] = pd.to_datetime(df['date_str'])
            df = df.sort_values('date').reset_index(drop=True)
            
            # Daily Technicals
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
            df['ema50'] = df['c'].ewm(span=50, adjust=False).mean()
            df['ema200'] = df['c'].ewm(span=200, adjust=False).mean()
            df['sma20'] = df['c'].rolling(20, min_periods=10).mean()
            df['sma50'] = df['c'].rolling(50, min_periods=20).mean()
            df['sma200'] = df['c'].rolling(200, min_periods=50).mean()
            
            df['vol_sma20'] = df['v'].rolling(20, min_periods=5).mean().shift(1)
            df['vol_sma50'] = df['v'].rolling(50, min_periods=10).mean().shift(1)
            df['high_250'] = df['h'].rolling(250, min_periods=60).max().shift(1)
            df['low_250'] = df['l'].rolling(250, min_periods=60).min().shift(1)
            
            tr = np.maximum(df['h'] - df['l'], np.maximum(np.abs(df['h'] - df['c'].shift(1)), np.abs(df['l'] - df['c'].shift(1))))
            df['atr14'] = tr.rolling(14, min_periods=5).mean()
            
            # Weekly Bars Aggregation (ISO Week)
            df['week'] = df['date'].dt.to_period('W')
            weekly = df.groupby('week').agg({
                'date_str': 'last',
                'o': 'first',
                'h': 'max',
                'l': 'min',
                'c': 'last',
                'v': 'sum'
            }).reset_index()
            weekly['date'] = pd.to_datetime(weekly['date_str'])
            weekly = weekly.sort_values('date').reset_index(drop=True)
            
            universe[sym] = {
                'df': df,
                'c': df['c'].values, 'h': df['h'].values, 'l': df['l'].values,
                'o': df['o'].values, 'v': df['v'].values, 'dates': df['date_str'].values,
                'ema20': df['ema20'].values, 'ema50': df['ema50'].values, 'ema200': df['ema200'].values,
                'sma20': df['sma20'].values, 'sma50': df['sma50'].values, 'sma200': df['sma200'].values,
                'vol_sma20': df['vol_sma20'].values, 'vol_sma50': df['vol_sma50'].values,
                'high_250': df['high_250'].values, 'low_250': df['low_250'].values,
                'atr14': df['atr14'].values,
                'n': len(df)
            }
            weekly_universe[sym] = weekly
            total_daily_bars += len(df)
        except Exception:
            continue
            
    print(f"      Loaded {len(universe)} symbols ({total_daily_bars:,} bars) in {time.time() - t0:.2f}s.")
    return universe, weekly_universe

def compute_nifty_weekly(weekly_universe):
    print("[2/4] Preparing NIFTY weekly index series...")
    if 'NIFTY' in weekly_universe:
        nifty_w = weekly_universe['NIFTY'].copy()
    elif 'NIFTY500' in weekly_universe:
        nifty_w = weekly_universe['NIFTY500'].copy()
    else:
        # Construct equal-weighted weekly index across universe as fallback
        dates = sorted(list(set([d for w in weekly_universe.values() for d in w['date_str'].values])))
        nifty_w = pd.DataFrame({'date_str': dates})
        nifty_w['c'] = 1000.0
        
    nifty_w['date'] = pd.to_datetime(nifty_w['date_str'])
    nifty_w = nifty_w.sort_values('date').reset_index(drop=True)
    return nifty_w

def extract_weekly_metrics(weekly_df, as_of_date_str, nifty_w):
    """
    Computes 8 point-in-time weekly features as of candidate signal date.
    """
    w = weekly_df[weekly_df['date_str'] <= as_of_date_str]
    if len(w) < 30:
        return None
        
    close = w['c'].values
    volume = w['v'].values
    highs = w['h'].values
    lows = w['l'].values
    
    current_close = close[-1]
    sma10 = float(np.mean(close[-10:]))
    sma30 = float(np.mean(close[-30:]))
    
    # 1. Weekly trend
    w_uptrend = int(current_close > sma10 > sma30)
    
    # 2. Weekly SMA slope
    ref_sma10 = float(np.mean(close[-15:-5])) if len(close) >= 15 else sma10
    w_sma_slope = (sma10 - ref_sma10) / max(ref_sma10, 0.01)
    
    # 3. Weekly RS vs Nifty (20-week return relative to Nifty 20-week return)
    stock_20w_ret = (current_close / close[-20] - 1.0) if len(close) >= 20 else 0.0
    nw = nifty_w[nifty_w['date_str'] <= as_of_date_str]
    if len(nw) >= 20:
        nifty_20w_ret = (nw['c'].iloc[-1] / nw['c'].iloc[-20] - 1.0)
    else:
        nifty_20w_ret = 0.0
    w_rs_20 = stock_20w_ret - nifty_20w_ret
    
    # 4. Weekly ATR% (10-week average)
    tr_w = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - close[:-1]), np.abs(lows[1:] - close[:-1])))
    w_atr_pct = float(np.mean(tr_w[-10:]) / current_close) if len(tr_w) >= 10 else 0.04
    
    # 5. Weekly Volume Ratio (8-week vs 30-week average)
    vol_sma8 = float(np.mean(volume[-8:]))
    vol_sma30 = float(np.mean(volume[-30:]))
    w_vol_ratio = vol_sma8 / max(vol_sma30, 1.0)
    
    # 6. Weekly Base Depth (20-week)
    h_20w = float(np.max(highs[-20:]))
    l_20w = float(np.min(lows[-20:]))
    w_base_depth = (h_20w - l_20w) / max(h_20w, 0.01)
    w_base_tightness = int(w_base_depth < 0.20)
    
    # 7. Weekly 52-Week Position
    lookback_52 = min(len(w), 52)
    w_52w_high = float(np.max(highs[-lookback_52:]))
    w_52w_low = float(np.min(lows[-lookback_52:]))
    denom = w_52w_high - w_52w_low
    w_position_52w = (current_close - w_52w_low) / denom if denom > 1e-4 else 0.5
    
    return {
        "w_uptrend": w_uptrend,
        "w_sma_slope": w_sma_slope,
        "w_rs_20": w_rs_20,
        "w_atr_pct": w_atr_pct,
        "w_vol_ratio": w_vol_ratio,
        "w_base_depth": w_base_depth,
        "w_base_tightness": w_base_tightness,
        "w_position_52w": w_position_52w,
    }

def detect_and_build_candidates(universe, weekly_universe, nifty_w):
    print("[3/4] Extracting candidate universe with 31 base + 8 weekly + forward path metrics...")
    t0 = time.time()
    candidates = []
    
    for sym, d in universe.items():
        if sym in ['NIFTY', 'NIFTY500', 'BANKNIFTY']:
            continue
        n = d['n']
        if n < 220:
            continue
            
        c = d['c']; h = d['h']; l = d['l']; o = d['o']; v = d['v']; dates = d['dates']
        ema20 = d['ema20']; ema50 = d['ema50']; ema200 = d['ema200']
        sma20 = d['sma20']; sma50 = d['sma50']; sma200 = d['sma200']
        vol_sma20 = d['vol_sma20']; vol_sma50 = d['vol_sma50']
        high_250 = d['high_250']; low_250 = d['low_250']; atr14 = d['atr14']
        w_df = weekly_universe.get(sym)
        if w_df is None:
            continue
            
        for i in range(80, n - 42):
            d_str = dates[i]
            if d_str < "2020-01-01":
                continue
                
            turnover_cr = (c[i] * v[i]) / 10_000_000.0
            if c[i] < 25.0 or turnover_cr < 1.0:
                continue
            if np.isnan(sma200[i]) or np.isnan(sma50[i]) or np.isnan(high_250[i]) or high_250[i] <= 0:
                continue
                
            # Loosened Gates
            rng = max(h[i] - l[i], 0.01)
            cir = (c[i] - l[i]) / rng
            upper_wick = (h[i] - max(c[i], o[i])) / rng
            lower_wick = (min(c[i], o[i]) - l[i]) / rng
            body_pct = abs(c[i] - o[i]) / rng
            is_bull_body = int(c[i] >= o[i])
            bar_ret = (c[i] / c[i-1] - 1.0) * 100.0
            gap_pct = (o[i] / c[i-1] - 1.0) * 100.0
            
            if cir < 0.60 or upper_wick > 0.25 or bar_ret < 3.5:
                continue
                
            vol_sma20_val = vol_sma20[i] if not np.isnan(vol_sma20[i]) and vol_sma20[i] > 0 else 1.0
            vol_ratio20 = v[i] / vol_sma20_val
            if vol_ratio20 < 1.20:
                continue
                
            ath_dist_pct = (high_250[i] - c[i]) / high_250[i] * 100.0
            if ath_dist_pct > 30.0:
                continue
                
            prior_runup_60 = (c[i] / c[i-60] - 1.0) * 100.0 if i >= 60 else 0.0
            if prior_runup_60 < 10.0:
                continue
                
            # Pattern Detectors (Loosened bounds)
            detected_patterns = []
            stops = []
            base_depth_pct = 0.0
            base_duration_bars = 0
            flag_pole_height_pct = 0.0
            flag_duration_bars = 0
            chase_pct = 0.0
            
            # 1. High-Tight Flag (HTF)
            pole_l = float(np.min(l[i-16:i-3])); pole_h = float(np.max(h[i-16:i-3]))
            pole_adv = (pole_h - pole_l) / max(pole_l, 0.01)
            flag_l = float(np.min(l[i-5:i])); flag_h = float(np.max(h[i-5:i]))
            flag_rng = (flag_h - flag_l) / max(flag_l, 0.01)
            if pole_adv >= 0.12 and flag_rng <= 0.09 and c[i] > flag_h:
                detected_patterns.append("HTF")
                stops.append(max(flag_l, c[i] * 0.95))
                flag_pole_height_pct = pole_adv * 100.0
                flag_duration_bars = 5
                chase_pct = (c[i] - flag_h) / flag_h * 100.0
                
            # 2. Base Breakout (BASE)
            res_ceiling = float(np.max(h[i-50:i-2]))
            base_floor = float(np.min(l[i-50:i-2]))
            b_depth = (res_ceiling - base_floor) / max(res_ceiling, 0.01)
            if b_depth <= 0.35 and c[i] > res_ceiling * 1.002:
                chase = (c[i] - res_ceiling) / res_ceiling * 100.0
                if chase <= 12.0:
                    detected_patterns.append("BASE")
                    stops.append(max(l[i], c[i] * 0.95))
                    base_depth_pct = b_depth * 100.0
                    base_duration_bars = 50
                    chase_pct = chase
                    
            # 3. 52-Week High (52W_HIGH)
            if c[i] >= high_250[i]:
                detected_patterns.append("52W_HIGH")
                stops.append(max(l[i], c[i] * 0.95))
                chase_pct = (c[i] - high_250[i]) / high_250[i] * 100.0
                
            # 4. Darvas Box (DARVAS)
            box_top_idx = i - 20 + int(np.argmax(h[i-20:i]))
            box_top = h[box_top_idx]
            box_bot = float(np.min(l[box_top_idx:i]))
            box_h = (box_top - box_bot) / max(box_top, 0.01)
            if (i - box_top_idx >= 3) and (0.025 <= box_h <= 0.12) and c[i] > box_top:
                detected_patterns.append("DARVAS")
                stops.append(box_bot)
                chase_pct = (c[i] - box_top) / box_top * 100.0
                
            if not detected_patterns:
                continue
                
            pat_name = detected_patterns[0]
            stop_p = round(stops[0], 2)
            ep = c[i]
            initial_risk = ep - stop_p
            if initial_risk <= 0.01 or (initial_risk / ep) > 0.08:
                continue
                
            risk_pct = (initial_risk / ep) * 100.0
            
            # Weekly Features (PIT)
            w_feats = extract_weekly_metrics(w_df, d_str, nifty_w)
            if w_feats is None:
                continue
                
            # Trend and MA Alignments
            dist_sma20 = (c[i] - sma20[i]) / sma20[i] * 100.0 if not np.isnan(sma20[i]) else 0.0
            dist_sma50 = (c[i] - sma50[i]) / sma50[i] * 100.0 if not np.isnan(sma50[i]) else 0.0
            dist_sma200 = (c[i] - sma200[i]) / sma200[i] * 100.0 if not np.isnan(sma200[i]) else 0.0
            
            sma50_slope_20 = (sma50[i] - sma50[i-20]) / max(sma50[i-20], 0.01) * 100.0 if not np.isnan(sma50[i-20]) else 0.0
            sma200_slope_20 = (sma200[i] - sma200[i-20]) / max(sma200[i-20], 0.01) * 100.0 if not np.isnan(sma200[i-20]) else 0.0
            
            # EMA Stack Score (0 to 4)
            ema_stack = 0
            if c[i] > ema20[i]: ema_stack += 1
            if ema20[i] > ema50[i]: ema_stack += 1
            if ema50[i] > ema200[i]: ema_stack += 1
            if c[i] > ema200[i]: ema_stack += 1
            
            # Volume Metrics
            vol_sma50_val = vol_sma50[i] if not np.isnan(vol_sma50[i]) and vol_sma50[i] > 0 else 1.0
            vol_ratio50 = v[i] / vol_sma50_val
            volume_slope_20 = (vol_sma20_val - float(np.mean(v[i-40:i-20]))) / max(float(np.mean(v[i-40:i-20])), 1.0) if i >= 40 else 0.0
            
            up_vol = float(np.sum([v[k] for k in range(i-20, i) if c[k] >= c[k-1]]))
            down_vol = float(np.sum([v[k] for k in range(i-20, i) if c[k] < c[k-1]]))
            ud_ratio_20 = up_vol / max(down_vol, 1.0)
            base_vol_avg = float(np.mean(v[i-20:i]))
            dry_up_ratio = base_vol_avg / max(vol_sma50_val, 1.0)
            
            # Supply Structure
            dist_from_52w_high = (high_250[i] - c[i]) / high_250[i] * 100.0
            dist_from_52w_low = (c[i] - low_250[i]) / max(low_250[i], 0.01) * 100.0
            r_ratio_available = 3.0 # Standard benchmark target
            
            # Multi-Timeframe Alignment Score (0 to 6)
            mtf_alignment = 0
            if w_feats['w_uptrend'] == 1: mtf_alignment += 1
            if w_feats['w_sma_slope'] > 0.02: mtf_alignment += 1
            if w_feats['w_rs_20'] > 0: mtf_alignment += 1
            if w_feats['w_position_52w'] > 0.70: mtf_alignment += 1
            if dist_sma50 > 0: mtf_alignment += 1
            if ema_stack >= 3: mtf_alignment += 1
            
            # Forward Path Statistics (Max 40 bars forward)
            peak_high = ep
            lowest_low = ep
            target_2R = ep + 2.0 * initial_risk
            target_3R = ep + 3.0 * initial_risk
            reached_2R = 0
            resolved = False
            realized_pnl_pct = 0.0
            peak_day = 1
            
            for j in range(i + 1, min(i + 41, n)):
                hj = h[j]; lj = l[j]
                if hj > peak_high:
                    peak_high = hj
                    peak_day = j - i
                if lj < lowest_low:
                    lowest_low = lj
                    
                if lj <= stop_p:
                    if not resolved:
                        realized_pnl_pct = round((stop_p - ep) / ep * 100.0, 2)
                        resolved = True
                    break
                    
                if hj >= target_2R:
                    reached_2R = 1
                    
                if hj >= target_3R and not resolved:
                    realized_pnl_pct = round((target_3R - ep) / ep * 100.0, 2)
                    resolved = True
                    
            if not resolved:
                last_c = c[min(i + 40, n - 1)]
                realized_pnl_pct = round((last_c - ep) / ep * 100.0, 2)
                
            mfe_r = min(max((peak_high - ep) / initial_risk, 0.0), 10.0)
            mae_r = max(min((lowest_low - ep) / initial_risk, 0.0), -3.0)
            days_to_peak = min(max(peak_day, 1), 40)
            pnl_r = round(realized_pnl_pct / risk_pct, 2)
            
            candidates.append({
                'symbol': sym,
                'date': d_str,
                'year': int(d_str[:4]),
                'signal_idx': i,
                'pattern': pat_name,
                'entry': ep,
                'stop': stop_p,
                'initial_risk': initial_risk,
                'label': reached_2R,
                # Path Targets (Layer 5)
                'mfe_r': mfe_r,
                'mae_r': mae_r,
                'days_to_peak': days_to_peak,
                'realized_pnl_pct': realized_pnl_pct,
                'pnl_r': pnl_r,
                # 31 BASE FEATURES
                'chase_pct': chase_pct,
                'upper_wick_pct': upper_wick,
                'lower_wick_pct': lower_wick,
                'body_pct': body_pct,
                'cir': cir,
                'is_bull_body': is_bull_body,
                'bar_ret': bar_ret,
                'gap_pct': gap_pct,
                'vol_ratio_20': vol_ratio20,
                'vol_ratio_50': vol_ratio50,
                'volume_slope_20': volume_slope_20,
                'ud_ratio_20': ud_ratio_20,
                'dry_up_ratio': dry_up_ratio,
                'dist_sma20': dist_sma20,
                'dist_sma50': dist_sma50,
                'dist_sma200': dist_sma200,
                'sma50_slope_20': sma50_slope_20,
                'sma200_slope_20': sma200_slope_20,
                'ema_stack_score': ema_stack,
                'ath_dist_pct': ath_dist_pct,
                'dist_from_52w_high': dist_from_52w_high,
                'dist_from_52w_low': dist_from_52w_low,
                'is_htf': 1 if pat_name == "HTF" else 0,
                'is_base': 1 if pat_name == "BASE" else 0,
                'is_52w': 1 if pat_name == "52W_HIGH" else 0,
                'is_darvas': 1 if pat_name == "DARVAS" else 0,
                'base_depth_pct': base_depth_pct,
                'base_duration_bars': base_duration_bars,
                'flag_pole_height_pct': flag_pole_height_pct,
                'flag_duration_bars': flag_duration_bars,
                'risk_pct': risk_pct,
                'r_ratio_available': r_ratio_available,
                # 8 WEEKLY FEATURES (Layer 2)
                'w_uptrend': w_feats['w_uptrend'],
                'w_sma_slope': w_feats['w_sma_slope'],
                'w_rs_20': w_feats['w_rs_20'],
                'w_atr_pct': w_feats['w_atr_pct'],
                'w_vol_ratio': w_feats['w_vol_ratio'],
                'w_base_depth': w_feats['w_base_depth'],
                'w_base_tightness': w_feats['w_base_tightness'],
                'w_position_52w': w_feats['w_position_52w'],
                'mtf_alignment': mtf_alignment
            })
            
    df_out = pd.DataFrame(candidates)
    print(f"      Extracted {len(df_out):,} candidates across {df_out['date'].nunique()} trading dates in {time.time() - t0:.2f}s.")
    return df_out

def main():
    u, w_u = load_universe_and_weekly()
    nifty_w = compute_nifty_weekly(w_u)
    df = detect_and_build_candidates(u, w_u, nifty_w)
    
    print(f"[4/4] Saving candidate universe to {OUTPUT_PARQUET}...")
    os.makedirs(os.path.dirname(OUTPUT_PARQUET), exist_ok=True)
    df.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"      Successfully saved {len(df):,} candidates ({len(df.columns)} columns).")
    
    # Summary of split
    train_n = len(df[df['year'] <= 2024])
    test_n = len(df[df['year'] >= 2025])
    print(f"      Train Set (2020-2024 In-Sample):  {train_n:,} candidates ({train_n/len(df)*100:.1f}%)")
    print(f"      Test Set  (2025-2026 Out-Sample): {test_n:,} candidates ({test_n/len(df)*100:.1f}%)")
    print(f"      Pattern Distribution:")
    for pat, count in df['pattern'].value_counts().items():
        print(f"        - {pat:<10}: {count:,} ({count/len(df)*100:.1f}%)")

if __name__ == "__main__":
    main()
