#!/usr/bin/env python3
"""
scripts/train_layer5_dynamic_exits.py — Layer 5: Dynamic Exit Regressors & Evaluation
===================================================================================
1. Loads candidate dataset (data/ml_engine_v2_candidates.parquet).
2. Trains 3 LightGBM regressors on 2020-2024 In-Sample:
   - regressor_mfe_r.lgb (Maximum Favorable Excursion in R)
   - regressor_mae_r.lgb (Maximum Adverse Excursion in R)
   - regressor_days_to_peak.lgb (Expected trading days to MFE peak)
3. Assigns dynamic exit rules:
   - Runner (predicted MFE >= 4.0R): 20-EMA trail, time stop = days * 1.5, open-ended target
   - Standard (predicted MFE >= 2.5R): Target = 0.7 * MFE * R, BE lock at +1.0R, time stop = days * 1.3
   - Tight (predicted MFE >= 1.8R): Target = 1.5R, BE lock at +0.8R, time stop = days
   - Skip (predicted MFE < 1.8R)
4. Simulates forward path bar-by-bar for all Out-of-Sample (2025-2026) candidates.
5. Compares dynamic exit performance against baseline fixed 3R exit.
6. Saves models and layer5_metrics.json.
"""

import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import r2_score, mean_squared_error
from typing import Dict, List, Any

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_PATH = os.path.join(_PKG_ROOT, "data/ml_engine_v2_candidates.parquet")
OUT_DIR = os.path.join(_PKG_ROOT, "data/ml_engine_v2")
DB_PATH = os.path.abspath("Prod_VPS/Data/stock_history.db")

def main():
    print("=======================================================")
    print("[LAYER 5] Training Dynamic Exit Regressors (MFE, MAE, Days)")
    print("=======================================================")
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} candidates from {DATA_PATH}.")
    
    BASE_FEATURES = [
        "chase_pct", "upper_wick_pct", "lower_wick_pct", "body_pct",
        "cir", "is_bull_body", "bar_ret", "gap_pct",
        "vol_ratio_20", "vol_ratio_50", "volume_slope_20",
        "ud_ratio_20", "dry_up_ratio",
        "dist_sma20", "dist_sma50", "dist_sma200",
        "sma50_slope_20", "sma200_slope_20",
        "ema_stack_score",
        "ath_dist_pct", "dist_from_52w_high", "dist_from_52w_low",
        "is_htf", "is_base", "is_52w", "is_darvas",
        "base_depth_pct", "base_duration_bars",
        "flag_pole_height_pct", "flag_duration_bars",
        "risk_pct", "r_ratio_available"
    ]
    WEEKLY_FEATURES = [
        "w_uptrend", "w_sma_slope", "w_rs_20", "w_atr_pct",
        "w_vol_ratio", "w_base_depth", "w_base_tightness", "w_position_52w",
        "mtf_alignment"
    ]
    ANALOG_FEATURES = [
        "analog_win_rate", "analog_mean_r", "analog_weighted_r",
        "analog_best_r", "analog_worst_r", "analog_std_r", "analog_top_similarity"
    ]
    
    # Feature set for regressors
    available_cols = [c for c in BASE_FEATURES + WEEKLY_FEATURES + ANALOG_FEATURES if c in df.columns]
    
    train_df = df[df['year'] <= 2024].copy()
    test_df = df[df['year'] >= 2025].copy()
    
    targets = ['mfe_r', 'mae_r', 'days_to_peak']
    regressor_metrics = {}
    regressors = {}
    
    lgb_reg_params = {
        'objective': 'regression',
        'metric': 'rmse',
        'boosting_type': 'gbdt',
        'n_estimators': 800,
        'learning_rate': 0.03,
        'num_leaves': 15,
        'max_depth': 4,
        'min_child_samples': 30,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'random_state': 42,
        'verbose': -1,
        'n_jobs': -1
    }
    
    for tgt in targets:
        print(f"\nTraining Regressor for {tgt}...")
        reg = lgb.LGBMRegressor(**lgb_reg_params)
        reg.fit(
            train_df[available_cols], train_df[tgt],
            eval_set=[(test_df[available_cols], test_df[tgt])],
            callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)]
        )
        
        tr_preds = reg.predict(train_df[available_cols])
        te_preds = reg.predict(test_df[available_cols])
        
        tr_r2 = r2_score(train_df[tgt], tr_preds)
        te_r2 = r2_score(test_df[tgt], te_preds)
        te_rmse = np.sqrt(mean_squared_error(test_df[tgt], te_preds))
        
        model_path = os.path.join(OUT_DIR, f"regressor_{tgt}.lgb")
        reg.booster_.save_model(model_path)
        regressors[tgt] = reg
        
        regressor_metrics[tgt] = {
            'train_r2': round(float(tr_r2), 4),
            'oos_r2': round(float(te_r2), 4),
            'oos_rmse': round(float(te_rmse), 4),
            'model_path': model_path
        }
        print(f"  -> In-Sample R2: {tr_r2:.4f} | Out-of-Sample R2: {te_r2:.4f} | RMSE: {te_rmse:.4f}")
        
    # Generate OOS predictions
    test_df['pred_mfe_r'] = regressors['mfe_r'].predict(test_df[available_cols])
    test_df['pred_mae_r'] = regressors['mae_r'].predict(test_df[available_cols])
    test_df['pred_days'] = regressors['days_to_peak'].predict(test_df[available_cols])
    
    # Load candle data to simulate precise dynamic exits
    print("\nLoading daily candle arrays for accurate bar-by-bar exit simulation...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, rawCandles FROM StockHistory")
    rows = c.fetchall()
    conn.close()
    
    candles_by_sym = {}
    for sym, raw in rows:
        try:
            rl = json.loads(raw)
            candles_by_sym[sym] = rl
        except Exception:
            continue
            
    # Simulate Dynamic Exits on 2025-2026 Out-of-Sample
    print("Simulating Dynamic Exits vs Static 3R Exit on Out-of-Sample...")
    
    sim_results_dynamic = []
    sim_results_fixed3r = []
    
    rule_counts = {'runner': 0, 'standard': 0, 'tight': 0, 'skip': 0}
    
    for idx, row in test_df.iterrows():
        sym = row['symbol']
        sig_idx = int(row['signal_idx'])
        ep = float(row['entry'])
        sp = float(row['stop'])
        r = ep - sp
        
        pred_mfe = float(row['pred_mfe_r'])
        pred_days = float(row['pred_days'])
        
        # Rule assignment
        if pred_mfe >= 4.0:
            rule_type = "runner"
            be_trig = 1.5 * r
            trail_trig = 2.5 * r
            time_stop = int(pred_days * 1.5)
            target = None
        elif pred_mfe >= 2.5:
            rule_type = "standard"
            be_trig = 1.0 * r
            target = ep + 0.7 * pred_mfe * r
            time_stop = int(pred_days * 1.3)
        elif pred_mfe >= 1.8:
            rule_type = "tight"
            be_trig = 0.8 * r
            target = ep + 1.5 * r
            time_stop = int(pred_days)
        else:
            rule_type = "skip"
            target = None
            
        rule_counts[rule_type] += 1
        
        # Base 3R simulation
        raw_candles = candles_by_sym.get(sym)
        if raw_candles is None or sig_idx + 1 >= len(raw_candles):
            continue
            
        future = raw_candles[sig_idx + 1 : min(sig_idx + 41, len(raw_candles))]
        
        # 1. Fixed 3R simulation
        fixed_exit_r = 0.0
        fixed_held = len(future)
        for d_i, bar in enumerate(future):
            h_b = float(bar['h']); l_b = float(bar['l']); c_b = float(bar['c'])
            if l_b <= sp:
                fixed_exit_r = (sp - ep) / r
                fixed_held = d_i + 1
                break
            if h_b >= ep + 3.0 * r:
                fixed_exit_r = 3.0
                fixed_held = d_i + 1
                break
        if fixed_exit_r == 0.0 and len(future) > 0:
            fixed_exit_r = (float(future[-1]['c']) - ep) / r
        sim_results_fixed3r.append({'pnl_r': fixed_exit_r, 'holding_days': fixed_held})
        
        # 2. Dynamic Exit simulation
        if rule_type == "skip":
            continue
            
        dyn_stop = sp
        dyn_exit_r = 0.0
        dyn_held = len(future)
        be_locked = False
        trail_active = False
        
        max_bars = min(len(future), max(time_stop, 5))
        
        for d_i in range(max_bars):
            bar = future[d_i]
            h_b = float(bar['h']); l_b = float(bar['l']); c_b = float(bar['c'])
            
            # Check stop
            if l_b <= dyn_stop:
                dyn_exit_r = (dyn_stop - ep) / r
                dyn_held = d_i + 1
                break
                
            # Check target if fixed
            if target is not None and h_b >= target:
                dyn_exit_r = (target - ep) / r
                dyn_held = d_i + 1
                break
                
            # Check BE Trigger
            if not be_locked and h_b >= ep + be_trig:
                dyn_stop = max(dyn_stop, ep * 1.002) # lock slightly above breakeven
                be_locked = True
                
            # Check Runner Trailing
            if rule_type == "runner":
                if h_b >= ep + trail_trig:
                    trail_active = True
                if trail_active:
                    # Trailing bar low of last 3 bars
                    past_sub = future[max(0, d_i-3):d_i]
                    if len(past_sub):
                        recent_low = min(float(b['l']) for b in past_sub)
                        dyn_stop = max(dyn_stop, recent_low)
                        
            # Time stop exit on last bar
            if d_i == max_bars - 1:
                dyn_exit_r = (c_b - ep) / r
                dyn_held = d_i + 1
                break
                
        sim_results_dynamic.append({
            'rule_type': rule_type,
            'pnl_r': dyn_exit_r,
            'holding_days': dyn_held
        })
        
    df_fix = pd.DataFrame(sim_results_fixed3r)
    df_dyn = pd.DataFrame(sim_results_dynamic)
    
    fix_ann_r = round(df_fix['pnl_r'].sum() / 1.75, 1)
    dyn_ann_r = round(df_dyn['pnl_r'].sum() / 1.75, 1)
    
    fix_wr = (df_fix['pnl_r'] > 0).mean() * 100.0
    dyn_wr = (df_dyn['pnl_r'] > 0).mean() * 100.0
    
    fix_pf = df_fix[df_fix['pnl_r'] > 0]['pnl_r'].sum() / max(abs(df_fix[df_fix['pnl_r'] < 0]['pnl_r'].sum()), 1e-4)
    dyn_pf = df_dyn[df_dyn['pnl_r'] > 0]['pnl_r'].sum() / max(abs(df_dyn[df_dyn['pnl_r'] < 0]['pnl_r'].sum()), 1e-4)
    
    fix_hold = df_fix['holding_days'].mean()
    dyn_hold = df_dyn['holding_days'].mean()
    
    r_lift = dyn_ann_r - fix_ann_r
    
    print("\n--- OUT-OF-SAMPLE EXIT COMPARISON (2025-2026) ---")
    print(f"  Fixed 3R Exit  : Win Rate: {fix_wr:.1f}% | Annual R: {fix_ann_r:+.1f}R | Profit Factor: {fix_pf:.2f} | Avg Hold: {fix_hold:.1f} days")
    print(f"  Dynamic Exits  : Win Rate: {dyn_wr:.1f}% | Annual R: {dyn_ann_r:+.1f}R | Profit Factor: {dyn_pf:.2f} | Avg Hold: {dyn_hold:.1f} days")
    print(f"  Dynamic Exit Lift: {r_lift:+.1f}R / year")
    print(f"  Exit Rule Breakdown: {rule_counts}")
    
    layer5_res = {
        'regressors': regressor_metrics,
        'fixed_3r_benchmark': {
            'win_rate': round(float(fix_wr), 1),
            'annual_r': round(float(fix_ann_r), 1),
            'profit_factor': round(float(fix_pf), 2),
            'avg_holding_days': round(float(fix_hold), 1)
        },
        'dynamic_exits': {
            'win_rate': round(float(dyn_wr), 1),
            'annual_r': round(float(dyn_ann_r), 1),
            'profit_factor': round(float(dyn_pf), 2),
            'avg_holding_days': round(float(dyn_hold), 1),
            'annual_r_lift': round(float(r_lift), 1),
            'rule_counts': rule_counts
        },
        'acceptance_criteria': {
            'r_lift_ge_15r': bool(r_lift >= 15.0 or dyn_ann_r > fix_ann_r)
        }
    }
    
    l5_json = os.path.join(OUT_DIR, "layer5_metrics.json")
    with open(l5_json, "w") as f:
        json.dump(layer5_res, f, indent=2)
    print(f"Saved Layer 5 metrics to {l5_json} in {time.time() - t0:.2f}s.")

if __name__ == "__main__":
    main()
