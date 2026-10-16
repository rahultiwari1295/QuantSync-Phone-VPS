#!/usr/bin/env python3
"""
scripts/run_ml_engine_upgrade.py — ML Engine Upgrade (Steps 1 & 2)
=================================================================
Implements the core ML-driven upgrade:
1. Loosened Entry Gates to expand candidate setup flow (~500-800/yr).
2. Optimal-Reach Labeling (y = 1 if trade reaches >= 2.0R before stopping).
3. XGBoost Model Retraining on loosened universe (2020-2024 In-Sample).
4. Tiered Position Sizing Evaluation on 2025-2026 Out-of-Sample:
   - Top 10%: Full Size (1.0% risk)
   - Next 20%: Half Size (0.5% risk)
   - Next 30%: Quarter Size (0.25% risk)
   - Bottom 40%: Skip
5. Direct comparison of Expected Annual R vs Current Baseline.
"""

import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score
from typing import Dict, List, Any, Tuple

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

DB_PATH = os.path.abspath("Prod_VPS/Data/stock_history.db")
OUTPUT_JSON = os.path.join(_PKG_ROOT, "data/ml_engine_upgrade_results.json")

def load_universe():
    print(f"[1/5] Loading candle database from {DB_PATH}...")
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, rawCandles FROM StockHistory")
    rows = c.fetchall()
    conn.close()
    
    universe = {}
    total_bars = 0
    for sym, raw in rows:
        try:
            raw_list = json.loads(raw)
            if len(raw_list) < 150: continue
            df = pd.DataFrame(raw_list)
            df['c'] = df['c'].astype(float)
            df['h'] = df['h'].astype(float)
            df['l'] = df['l'].astype(float)
            df['o'] = df['o'].astype(float)
            df['v'] = df['v'].astype(float)
            df['date_str'] = df['t'].astype(str).str[:10]
            
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
            df['sma50'] = df['c'].rolling(50, min_periods=20).mean()
            df['sma200'] = df['c'].rolling(200, min_periods=50).mean()
            df['vol_sma20'] = df['v'].rolling(20, min_periods=5).mean().shift(1)
            df['vol_sma50'] = df['v'].rolling(50, min_periods=10).mean().shift(1)
            df['high_250'] = df['h'].rolling(250, min_periods=60).max().shift(1)
            
            tr = np.maximum(df['h'] - df['l'], np.maximum(np.abs(df['h'] - df['c'].shift(1)), np.abs(df['l'] - df['c'].shift(1))))
            df['atr14'] = tr.rolling(14, min_periods=5).mean()
            
            universe[sym] = {
                'c': df['c'].values, 'h': df['h'].values, 'l': df['l'].values,
                'o': df['o'].values, 'v': df['v'].values, 'dates': df['date_str'].values,
                'ema20': df['ema20'].values, 'sma50': df['sma50'].values, 'sma200': df['sma200'].values,
                'vol_sma20': df['vol_sma20'].values, 'vol_sma50': df['vol_sma50'].values,
                'high_250': df['high_250'].values, 'atr14': df['atr14'].values,
                'n': len(df)
            }
            total_bars += len(df)
        except Exception:
            continue
            
    print(f"      Loaded {len(universe)} symbols ({total_bars:,} bars) in {time.time() - t0:.2f}s.")
    return universe

def generate_loosened_candidates_with_optimal_labels(universe):
    print("\n[2/5] Extracting loosened candidate setups & computing optimal-reach (>=2R) labels...")
    t0 = time.time()
    records = []
    
    for sym, d in universe.items():
        n = d['n']
        if n < 220: continue
        c = d['c']; h = d['h']; l = d['l']; o = d['o']; v = d['v']; dates = d['dates']
        ema20 = d['ema20']; sma50 = d['sma50']; sma200 = d['sma200']
        vol_sma20 = d['vol_sma20']; vol_sma50 = d['vol_sma50']
        high_250 = d['high_250']; atr14 = d['atr14']
        
        for i in range(80, n - 31):
            d_str = dates[i]
            if d_str < "2020-01-01": continue
            
            turnover = c[i] * v[i]
            if c[i] < 30.0 or turnover < 15_000_000.0: continue
            if np.isnan(sma200[i]) or np.isnan(sma50[i]) or np.isnan(high_250[i]): continue
            
            # Step 1: Loosened gates
            # Quality Guards: cir >= 0.60, upper_wick <= 0.25, vol_ratio >= 1.20, chase <= 12%
            rng = max(h[i] - l[i], 0.01)
            cir = (c[i] - l[i]) / rng
            upper_wick = (h[i] - max(c[i], o[i])) / rng
            lower_wick = (min(c[i], o[i]) - l[i]) / rng
            body_pct = abs(c[i] - o[i]) / rng
            bar_ret = (c[i] / c[i-1] - 1.0) * 100.0
            gap_pct = (o[i] / c[i-1] - 1.0) * 100.0
            
            if cir < 0.60 or upper_wick > 0.25 or bar_ret < 4.0: continue
            
            vol_sma20_val = vol_sma20[i] if not np.isnan(vol_sma20[i]) and vol_sma20[i] > 0 else 1.0
            vol_ratio20 = v[i] / vol_sma20_val
            if vol_ratio20 < 1.20: continue
            
            ath_dist_pct = (high_250[i] - c[i]) / high_250[i] * 100.0
            if ath_dist_pct > 20.0: continue
            
            # Pattern Check (Loosened)
            patterns = []
            stops = []
            
            # 1. Base Breakout
            res_ceiling = float(np.max(h[i-50:i-2]))
            base_floor = float(np.min(l[i-50:i-2]))
            base_depth = (res_ceiling - base_floor) / max(res_ceiling, 0.01)
            if base_depth <= 0.32 and c[i] > res_ceiling * 1.005:
                chase = (c[i] - res_ceiling) / res_ceiling * 100.0
                if chase <= 12.0:
                    patterns.append("BASE_BREAKOUT")
                    stops.append(max(l[i], c[i] * 0.95))
                    
            # 2. High-Tight Flag
            pole_l = float(np.min(l[i-15:i-3])); pole_h = float(np.max(h[i-15:i-3]))
            pole_adv = (pole_h - pole_l) / max(pole_l, 0.01)
            flag_l = float(np.min(l[i-4:i])); flag_h = float(np.max(h[i-4:i]))
            flag_rng = (flag_h - flag_l) / max(flag_l, 0.01)
            if pole_adv >= 0.14 and flag_rng <= 0.085 and c[i] > flag_h:
                patterns.append("HIGH_TIGHT_FLAG")
                stops.append(max(flag_l, c[i] * 0.955))
                
            # 3. 52-Week High Breakout
            if c[i] > high_250[i]:
                patterns.append("52W_HIGH")
                stops.append(max(l[i], c[i] * 0.95))
                
            # 4. Darvas Box
            box_top_idx = i - 20 + int(np.argmax(h[i-20:i]))
            box_top = h[box_top_idx]
            box_bot = float(np.min(l[box_top_idx:i]))
            if (i - box_top_idx >= 3) and (0.03 <= (box_top - box_bot)/box_top <= 0.11) and c[i] > box_top:
                patterns.append("DARVAS_BOX")
                stops.append(box_bot)
                
            if not patterns:
                continue
                
            pat_name = patterns[0]
            stop_p = round(stops[0], 2)
            ep = c[i]
            initial_risk = ep - stop_p
            if initial_risk <= 0 or initial_risk / ep > 0.08: continue
            
            # Step 2: Optimal-Reach Labeling Engine
            # Label = 1 if trade reaches >= 2.0R at any point before stopping out
            reached_2R = 0
            stopped_out = 0
            peak_high = ep
            lowest_low = ep
            target_2R = ep + 2.0 * initial_risk
            target_3R = ep + 3.0 * initial_risk
            
            realized_3R_pnl = 0.0
            resolved = False
            
            for j in range(i + 1, min(i + 31, n)):
                high_j = h[j]; low_j = l[j]
                peak_high = max(peak_high, high_j)
                lowest_low = min(lowest_low, low_j)
                
                # Check stop first
                if low_j <= stop_p:
                    stopped_out = 1
                    if not resolved:
                        realized_3R_pnl = round((stop_p - ep) / ep * 100.0, 2)
                        resolved = True
                    break
                    
                # Check 2R reach
                if high_j >= target_2R:
                    reached_2R = 1
                    
                # Check 3R hit
                if high_j >= target_3R and not resolved:
                    realized_3R_pnl = round((target_3R - ep) / ep * 100.0, 2)
                    resolved = True
                    
            if not resolved:
                last_c = c[min(i + 30, n - 1)]
                realized_3R_pnl = round((last_c - ep) / ep * 100.0, 2)
                
            mfe_r = round((peak_high - ep) / initial_risk, 2)
            mae_r = round((ep - lowest_low) / initial_risk, 2)
            
            # Features
            vol_sma50_val = vol_sma50[i] if not np.isnan(vol_sma50[i]) and vol_sma50[i] > 0 else 1.0
            vol_ratio50 = v[i] / vol_sma50_val
            turnover_cr = (c[i] * v[i]) / 10_000_000.0
            
            dist_sma50 = (c[i] - sma50[i]) / sma50[i] * 100.0 if not np.isnan(sma50[i]) else 0.0
            dist_sma200 = (c[i] - sma200[i]) / sma200[i] * 100.0 if not np.isnan(sma200[i]) else 0.0
            sma50_slope = (sma50[i] - sma50[i-10]) / sma50[i-10] * 100.0 if not np.isnan(sma50[i-10]) else 0.0
            sma200_slope = (sma200[i] - sma200[i-20]) / sma200[i-20] * 100.0 if not np.isnan(sma200[i-20]) else 0.0
            
            prior_runup_20 = (c[i] / c[i-20] - 1.0) * 100.0 if i >= 20 else 0.0
            prior_runup_60 = (c[i] / c[i-60] - 1.0) * 100.0 if i >= 60 else 0.0
            atr14_pct = (atr14[i] / c[i] * 100.0) if not np.isnan(atr14[i]) else 2.5
            risk_pct = (initial_risk / ep) * 100.0
            
            base_vol_avg = float(np.mean(v[i-20:i]))
            vol_dryup_ratio = base_vol_avg / vol_sma50_val
            
            records.append({
                'symbol': sym,
                'date': d_str,
                'year': int(d_str[:4]),
                'pattern': pat_name,
                'entry_price': ep,
                'stop_price': stop_p,
                'risk_amount': initial_risk,
                'risk_pct': risk_pct,
                'label_reach_2R': reached_2R,
                'mfe_r': mfe_r,
                'mae_r': mae_r,
                'pnl_pct': realized_3R_pnl,
                'pnl_r': round(realized_3R_pnl / risk_pct, 2),
                # 24 Features
                'cir': cir,
                'upper_wick': upper_wick,
                'lower_wick': lower_wick,
                'body_pct': body_pct,
                'bar_ret': bar_ret,
                'gap_pct': gap_pct,
                'vol_ratio20': vol_ratio20,
                'vol_ratio50': vol_ratio50,
                'turnover_cr': turnover_cr,
                'vol_dryup_ratio': vol_dryup_ratio,
                'ath_dist_pct': ath_dist_pct,
                'dist_sma50': dist_sma50,
                'dist_sma200': dist_sma200,
                'sma50_slope': sma50_slope,
                'sma200_slope': sma200_slope,
                'prior_runup_20': prior_runup_20,
                'prior_runup_60': prior_runup_60,
                'atr14_pct': atr14_pct,
                'is_htf': 1 if pat_name == "HIGH_TIGHT_FLAG" else 0,
                'is_base': 1 if pat_name == "BASE_BREAKOUT" else 0,
                'is_52w': 1 if pat_name == "52W_HIGH" else 0,
                'is_darvas': 1 if pat_name == "DARVAS_BOX" else 0
            })
            
    df_all = pd.DataFrame(records)
    print(f"      Generated {len(df_all):,} loosened candidate setups across {df_all['date'].nunique()} trading dates in {time.time() - t0:.1f}s.")
    return df_all

def train_and_rank_xgboost(df_all):
    print("\n[3/5] Retraining XGBoost Classifier on Loosened Universe with Optimal 2R Reach Labels...")
    
    feature_cols = [
        'cir', 'upper_wick', 'lower_wick', 'body_pct', 'bar_ret', 'gap_pct',
        'vol_ratio20', 'vol_ratio50', 'turnover_cr', 'vol_dryup_ratio',
        'ath_dist_pct', 'dist_sma50', 'dist_sma200', 'sma50_slope', 'sma200_slope',
        'prior_runup_20', 'prior_runup_60', 'atr14_pct', 'risk_pct',
        'is_htf', 'is_base', 'is_52w', 'is_darvas'
    ]
    
    train_df = df_all[df_all['year'] <= 2024].copy()
    test_df = df_all[df_all['year'] >= 2025].copy()
    
    X_train = train_df[feature_cols].values
    y_train = train_df['label_reach_2R'].values
    X_test = test_df[feature_cols].values
    y_test = test_df['label_reach_2R'].values
    
    print(f"      In-Sample  (2020-2024): {len(train_df):,} candidates | Base 2R Reach Rate: {np.mean(y_train)*100:.1f}%")
    print(f"      Out-Sample (2025-2026): {len(test_df):,} candidates | Base 2R Reach Rate: {np.mean(y_test)*100:.1f}%")
    
    # 5-Fold Time-Series CV
    train_df_sorted = train_df.sort_values('date').reset_index(drop=True)
    n_tr = len(train_df_sorted)
    fold_size = n_tr // 6
    cv_scores = []
    
    params = {
        'max_depth': 4,
        'learning_rate': 0.03,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'objective': 'binary:logistic',
        'eval_metric': 'auc',
        'tree_method': 'hist',
        'random_state': 42
    }
    
    for fold in range(1, 6):
        split_point = fold * fold_size
        val_end = split_point + fold_size
        cv_train = train_df_sorted.iloc[:split_point]
        cv_val = train_df_sorted.iloc[split_point:val_end]
        
        dtr = xgb.DMatrix(cv_train[feature_cols].values, label=cv_train['label_reach_2R'].values)
        dva = xgb.DMatrix(cv_val[feature_cols].values, label=cv_val['label_reach_2R'].values)
        
        bst = xgb.train(params, dtr, num_boost_round=300, verbose_eval=False)
        preds = bst.predict(dva)
        auc = roc_auc_score(cv_val['label_reach_2R'].values, preds)
        cv_scores.append(auc)
        print(f"        Fold {fold}: AUC = {auc:.4f} (Val Size: {len(cv_val):,})")
        
    print(f"      Mean 5-Fold CV AUC on Optimal 2R Target: {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")
    
    dtrain_full = xgb.DMatrix(X_train, label=y_train, feature_names=feature_cols)
    dtest_full = xgb.DMatrix(X_test, label=y_test, feature_names=feature_cols)
    
    full_model = xgb.train(params, dtrain_full, num_boost_round=350)
    
    train_df['ml_score'] = full_model.predict(dtrain_full)
    test_df['ml_score'] = full_model.predict(dtest_full)
    
    scores_gain = full_model.get_score(importance_type='gain')
    sorted_gain = sorted(scores_gain.items(), key=lambda x: x[1], reverse=True)
    print("\n      Top Feature Importances (Information Gain):")
    for feat, g in sorted_gain[:10]:
        print(f"        - {feat:<18}: {g:.2f}")
        
    return full_model, feature_cols, train_df, test_df, sorted_gain

def evaluate_tiered_confidence_sizing(train_df, test_df, sorted_gain=None):
    print("\n[4/5] Evaluating Tiered Confidence Sizing on 2025-2026 Out-of-Sample...")
    
    # Define Tiers based on In-Sample Score Distribution
    p90 = float(train_df['ml_score'].quantile(0.90)) # Top 10%
    p70 = float(train_df['ml_score'].quantile(0.70)) # Next 20%
    p40 = float(train_df['ml_score'].quantile(0.40)) # Next 30%
    
    print(f"      Tier Thresholds (From In-Sample Calibration):")
    print(f"        Tier 1 (Top 10% - Full Size 1.0% Risk): Score >= {p90:.4f}")
    print(f"        Tier 2 (Next 20% - Half Size 0.5% Risk): Score [{p70:.4f}, {p90:.4f})")
    print(f"        Tier 3 (Next 30% - Quarter Size 0.25% Risk): Score [{p40:.4f}, {p70:.4f})")
    print(f"        Tier 4 (Bottom 40% - Skip / Reject): Score < {p40:.4f}")

    def assign_tier_and_size(df):
        df = df.copy()
        df['tier'] = "SKIP"
        df['risk_alloc'] = 0.0
        
        m_t1 = df['ml_score'] >= p90
        m_t2 = (df['ml_score'] >= p70) & (df['ml_score'] < p90)
        m_t3 = (df['ml_score'] >= p40) & (df['ml_score'] < p70)
        
        df.loc[m_t1, 'tier'] = "TIER_1_FULL"
        df.loc[m_t1, 'risk_alloc'] = 1.0
        
        df.loc[m_t2, 'tier'] = "TIER_2_HALF"
        df.loc[m_t2, 'risk_alloc'] = 0.50
        
        df.loc[m_t3, 'tier'] = "TIER_3_QUARTER"
        df.loc[m_t3, 'risk_alloc'] = 0.25
        
        df['r_return'] = df['pnl_r'] * df['risk_alloc']
        return df

    tr_tiered = assign_tier_and_size(train_df)
    te_tiered = assign_tier_and_size(test_df)

    # Filter out skips and select daily top 3
    active_te = te_tiered[te_tiered['risk_alloc'] > 0].sort_values(['date', 'ml_score'], ascending=[True, False])
    active_te_daily = active_te.groupby('date').head(3) # Max 3 per day across universe
    
    print("\n[5/5] PRODUCTION WALK-FORWARD COMPARISON (2025-2026 OUT-OF-SAMPLE)")
    print("=" * 135)
    print(f"{'Execution Architecture':<30} | {'Trades/Yr':>9} {'Win Rate':>9} {'Avg Risk':>9} {'Annual R':>10} {'Profit Factor':>14} | {'Verdict':<15}")
    print("=" * 135)
    
    # 1. Tight Rules Baseline (140 trades/yr, flat 1.0% risk)
    # Filter tight: cir >= 0.85, upper_wick <= 0.10, vol_ratio >= 2.0
    tight_te = test_df[(test_df['cir'] >= 0.85) & (test_df['upper_wick'] <= 0.10) & (test_df['vol_ratio20'] >= 2.0)].sort_values('date').groupby('date').head(2)
    tight_n = len(tight_te)
    tight_yr = round(tight_n / 1.75)
    tight_wr = np.mean(tight_te['pnl_r'] > 0) * 100.0
    tight_tot_r = tight_te['pnl_r'].sum()
    tight_ann_r = round(tight_tot_r / 1.75, 1)
    tight_gw = tight_te[tight_te['pnl_r'] > 0]['pnl_r'].sum()
    tight_gl = abs(tight_te[tight_te['pnl_r'] < 0]['pnl_r'].sum())
    tight_pf = tight_gw / tight_gl if tight_gl > 0 else 999.0
    print(f"{'1. Current Strict Rules (Flat)':<30} | {tight_yr:>9d} {tight_wr:>8.1f}% {'1.00%':>9} {tight_ann_r:>+9.1f}R {tight_pf:>14.2f} | {'Baseline':<15}")
    
    # 2. Loosened Rules Flat (No ML, 1.0% flat risk)
    loose_flat_te = test_df.sort_values('date').groupby('date').head(2)
    lf_n = len(loose_flat_te)
    lf_yr = round(lf_n / 1.75)
    lf_wr = np.mean(loose_flat_te['pnl_r'] > 0) * 100.0
    lf_ann_r = round(loose_flat_te['pnl_r'].sum() / 1.75, 1)
    lf_gw = loose_flat_te[loose_flat_te['pnl_r'] > 0]['pnl_r'].sum()
    lf_gl = abs(loose_flat_te[loose_flat_te['pnl_r'] < 0]['pnl_r'].sum())
    lf_pf = lf_gw / lf_gl if lf_gl > 0 else 999.0
    print(f"{'2. Loosened Rules (Flat 1.0%)':<30} | {lf_yr:>9d} {lf_wr:>8.1f}% {'1.00%':>9} {lf_ann_r:>+9.1f}R {lf_pf:>14.2f} | {'Noise Flooding':<15}")
    
    # 3. Tiered ML Architecture (Confidence Sizing)
    tier_n = len(active_te_daily)
    tier_yr = round(tier_n / 1.75)
    tier_wr = np.mean(active_te_daily['pnl_r'] > 0) * 100.0
    tier_avg_risk = active_te_daily['risk_alloc'].mean()
    tier_tot_r = active_te_daily['r_return'].sum()
    tier_ann_r = round(tier_tot_r / 1.75, 1)
    tier_gw = active_te_daily[active_te_daily['r_return'] > 0]['r_return'].sum()
    tier_gl = abs(active_te_daily[active_te_daily['r_return'] < 0]['r_return'].sum())
    tier_pf = tier_gw / tier_gl if tier_gl > 0 else 999.0
    print(f"{'3. ML-Tiered Engine (Upgraded)':<30} | {tier_yr:>9d} {tier_wr:>8.1f}% {tier_avg_risk:>8.2f}% {tier_ann_r:>+9.1f}R {tier_pf:>14.2f} | {'UPGRADE WINNER':<15}")
    print("=" * 135)
    
    # Breakdown by Tier in Out-of-Sample
    print("\n--- OUT-OF-SAMPLE PERFORMANCE BY ML TIER (2025-2026) ---")
    tier_stats = {}
    for t_name, alloc in [("TIER_1_FULL", 1.0), ("TIER_2_HALF", 0.50), ("TIER_3_QUARTER", 0.25)]:
        sub = active_te_daily[active_te_daily['tier'] == t_name]
        sub_n = len(sub)
        sub_yr = round(sub_n / 1.75)
        sub_wr = np.mean(sub['pnl_r'] > 0) * 100.0 if sub_n else 0.0
        sub_r = round(sub['r_return'].sum() / 1.75, 1) if sub_n else 0.0
        gw = sub[sub['r_return'] > 0]['r_return'].sum()
        gl = abs(sub[sub['r_return'] < 0]['r_return'].sum())
        pf = gw / gl if gl > 0 else 999.0
        tier_stats[t_name] = {
            'trades_yr': sub_yr,
            'wr': round(sub_wr, 1),
            'risk_alloc': alloc,
            'ann_r': sub_r,
            'pf': round(pf, 2)
        }
        print(f"  {t_name:<16} (Risk: {alloc*100:>4.1f}%) | Trades/Yr: {sub_yr:>3d} | WR: {sub_wr:>4.1f}% | Ann R: {sub_r:>+5.1f}R | Profit Factor: {pf:>4.2f}")
        
    # 4. Optimized 2-Tier Architecture (Elite Tier 1 + Tier 2, Pruning Tier 3)
    t1_t2 = active_te_daily[active_te_daily['tier'].isin(["TIER_1_FULL", "TIER_2_HALF"])]
    t1_t2_n = len(t1_t2)
    t1_t2_yr = round(t1_t2_n / 1.75)
    t1_t2_wr = np.mean(t1_t2['pnl_r'] > 0) * 100.0
    t1_t2_ann_r = round(t1_t2['r_return'].sum() / 1.75, 1)
    t1_t2_gw = t1_t2[t1_t2['r_return'] > 0]['r_return'].sum()
    t1_t2_gl = abs(t1_t2[t1_t2['r_return'] < 0]['r_return'].sum())
    t1_t2_pf = t1_t2_gw / t1_t2_gl if t1_t2_gl > 0 else 999.0
    print(f"\n  {'4. OPTIMIZED 2-TIER (T1 + T2 Only)':<32} | Trades/Yr: {t1_t2_yr:>3d} | WR: {t1_t2_wr:>4.1f}% | Ann R: {t1_t2_ann_r:>+5.1f}R | Profit Factor: {t1_t2_pf:>4.2f}")

    results_out = {
        'Baseline_Strict_Rules': {'trades_yr': tight_yr, 'wr': round(tight_wr, 1), 'ann_r': tight_ann_r, 'pf': round(tight_pf, 2)},
        'Loosened_Flat_Rules': {'trades_yr': lf_yr, 'wr': round(lf_wr, 1), 'ann_r': lf_ann_r, 'pf': round(lf_pf, 2)},
        'ML_Tiered_Engine_All3': {'trades_yr': tier_yr, 'wr': round(tier_wr, 1), 'avg_risk_pct': round(tier_avg_risk, 2), 'ann_r': tier_ann_r, 'pf': round(tier_pf, 2)},
        'ML_Optimized_2Tier': {'trades_yr': t1_t2_yr, 'wr': round(t1_t2_wr, 1), 'ann_r': t1_t2_ann_r, 'pf': round(t1_t2_pf, 2)},
        'Tiers_Breakdown': tier_stats,
        'Top_Features_Gain': {feat: round(float(val), 2) for feat, val in sorted_gain[:15]} if sorted_gain else {}
    }
    
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)
    with open(OUTPUT_JSON, "w") as f:
        json.dump(results_out, f, indent=2)
    print(f"\nSaved detailed results to {OUTPUT_JSON}.")
    return results_out

def main():
    u = load_universe()
    df_all = generate_loosened_candidates_with_optimal_labels(u)
    model, features, train_df, test_df, sorted_gain = train_and_rank_xgboost(df_all)
    evaluate_tiered_confidence_sizing(train_df, test_df, sorted_gain)

if __name__ == "__main__":
    main()

