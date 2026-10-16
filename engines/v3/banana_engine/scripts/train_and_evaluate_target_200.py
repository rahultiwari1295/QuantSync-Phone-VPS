#!/usr/bin/env python3
"""
scripts/train_and_evaluate_target_200.py
=======================================
Phase 1 & Phase 2 Execution:
Goal:
  - ~200 - 250 trades / year
  - Win Rate >= 50%
  - Profit Factor >= 2.0

Protocol:
  1. Train solely on In-Sample (2020-2024).
  2. Optimize model & thresholds across In-Sample folds.
  3. Freeze model and evaluate blindly on 2025-2026 Out-of-Sample.
"""
import os
import sys
import json
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import KFold

DATA_PATH = "QuantSync_Lines/banana_engine/data/ml_engine_v2_candidates.parquet"
OUTPUT_DIR = "QuantSync_Lines/banana_engine/data/target_200"
os.makedirs(OUTPUT_DIR, exist_ok=True)

df = pd.read_parquet(DATA_PATH)
df['year'] = df['date'].str[:4].astype(int)

train = df[df['year'] <= 2024].copy().reset_index(drop=True)
test = df[df['year'] >= 2025].copy().reset_index(drop=True)

print(f"Dataset Loaded:")
print(f"  In-Sample  (2020-2024): {len(train)} candidates (~{len(train)/5:.0f}/yr)")
print(f"  Out-Sample (2025-2026): {len(test)} candidates (~{len(test)/1.52:.0f}/yr)")

feature_cols = [
    'chase_pct', 'upper_wick_pct', 'lower_wick_pct', 'body_pct', 'cir', 'is_bull_body',
    'bar_ret', 'gap_pct', 'vol_ratio_20', 'vol_ratio_50', 'volume_slope_20',
    'ud_ratio_20', 'dry_up_ratio', 'dist_sma20', 'dist_sma50', 'dist_sma200',
    'sma50_slope_20', 'sma200_slope_20', 'ema_stack_score', 'ath_dist_pct',
    'dist_from_52w_high', 'dist_from_52w_low', 'is_htf', 'is_base', 'is_52w',
    'is_darvas', 'base_depth_pct', 'base_duration_bars', 'flag_pole_height_pct',
    'flag_duration_bars', 'risk_pct', 'r_ratio_available', 'w_uptrend',
    'w_sma_slope', 'w_rs_20', 'w_atr_pct', 'w_vol_ratio', 'w_base_depth',
    'w_base_tightness', 'w_position_52w', 'mtf_alignment', 'analog_win_rate',
    'analog_mean_r', 'analog_weighted_r', 'analog_best_r', 'analog_worst_r',
    'analog_std_r', 'analog_top_similarity'
]

# 1. Train 5-fold CV on Train to get Out-of-Fold In-Sample predictions
oof_preds = np.zeros(len(train))
kf = KFold(n_splits=5, shuffle=False)

for trn_idx, val_idx in kf.split(train):
    X_tr, y_tr = train.loc[trn_idx, feature_cols], train.loc[trn_idx, 'label']
    X_va = train.loc[val_idx, feature_cols]
    
    clf = lgb.LGBMClassifier(
        n_estimators=180, learning_rate=0.03, num_leaves=31,
        subsample=0.8, colsample_bytree=0.8, min_child_samples=30,
        random_state=42, verbose=-1
    )
    clf.fit(X_tr, y_tr)
    oof_preds[val_idx] = clf.predict_proba(X_va)[:, 1]

train['score'] = oof_preds

# 2. Train final production model on ALL 2020-2024 data
final_model = lgb.LGBMClassifier(
    n_estimators=180, learning_rate=0.03, num_leaves=31,
    subsample=0.8, colsample_bytree=0.8, min_child_samples=30,
    random_state=42, verbose=-1
)
final_model.fit(train[feature_cols], train['label'])

# Save model and artifacts
final_model.booster_.save_model(os.path.join(OUTPUT_DIR, "ranker_model.lgb"))
with open(os.path.join(OUTPUT_DIR, "feature_list.json"), "w") as f:
    json.dump(feature_cols, f, indent=2)

# 3. Score Out-of-Sample test set with frozen model
test['score'] = final_model.predict_proba(test[feature_cols])[:, 1]

# Function to simulate metrics for a given subset and exit configuration
def evaluate_strategy(subset: pd.DataFrame, option_name: str, years: float):
    # Option A: Pure Fixed 2.0R Target
    # Win if reached 2R (label == 1)
    if "Option A" in option_name:
        is_win = (subset['label'] == 1)
        r_stream = np.where(is_win, 2.0, -1.0)
    elif "Option B" in option_name:
        # Option B: 50/50 Tranche (2.0R / 3.0R)
        # Half exits at 2.0R (+1.0R contribution)
        # Half exits at 3.0R if mfe_r >= 3.0 (+1.5R contribution), else stopped out (-0.5R)
        hit_2r = (subset['label'] == 1)
        hit_3r = hit_2r & (subset['mfe_r'] >= 3.0)
        # If hit_3r: +1.0 + 1.5 = +2.5R
        # If hit_2r but not 3r: +1.0 - 0.5 = +0.5R
        # If neither: -0.5 - 0.5 = -1.0R
        r_stream = np.where(hit_3r, 2.5, np.where(hit_2r, 0.5, -1.0))
        is_win = (r_stream > 0)
    else: # Fixed 3.0R
        hit_3r = (subset['label'] == 1) & (subset['mfe_r'] >= 3.0)
        r_stream = np.where(hit_3r, 3.0, -1.0)
        is_win = hit_3r
        
    n_trades = len(subset)
    trades_yr = n_trades / years
    wr = is_win.mean() * 100.0
    wins = r_stream[r_stream > 0]
    losses = abs(r_stream[r_stream < 0])
    
    tot_win = wins.sum()
    tot_loss = losses.sum()
    pf = tot_win / tot_loss if tot_loss > 0 else 999.0
    ann_r = r_stream.sum() / years
    
    # Max Drawdown in R
    cum_r = np.cumsum(r_stream)
    peaks = np.maximum.accumulate(cum_r)
    dd = peaks - cum_r
    max_dd = np.max(dd) if len(dd) > 0 else 0.0
    
    # Sharpe
    sharpe = (np.mean(r_stream) / np.std(r_stream)) * np.sqrt(trades_yr) if np.std(r_stream) > 0 else 0.0
    
    return {
        'trades': n_trades,
        'trades_yr': round(trades_yr, 1),
        'wr': round(wr, 1),
        'pf': round(pf, 2),
        'ann_r': round(ann_r, 1),
        'max_dd_r': round(max_dd, 1),
        'sharpe': round(sharpe, 2)
    }

print("\n" + "="*80)
print("PHASE 1: IN-SAMPLE BENCHMARK (2020 - 2024, 5 YEARS)")
print("="*80)

# Calculate threshold for Top 10% on In-Sample
cutoff_p90 = np.percentile(train['score'], 90.0) # Top 10%
cutoff_p89 = np.percentile(train['score'], 89.0) # Top 11%

sub_train_optA_10 = train[train['score'] >= cutoff_p90]
sub_train_optA_11 = train[train['score'] >= cutoff_p89]

res_train_A10 = evaluate_strategy(sub_train_optA_10, "Option A (2.0R Target)", years=5.0)
res_train_B10 = evaluate_strategy(sub_train_optA_10, "Option B (50/50 2R/3R)", years=5.0)
res_train_A11 = evaluate_strategy(sub_train_optA_11, "Option A (2.0R Target)", years=5.0)
res_train_B11 = evaluate_strategy(sub_train_optA_11, "Option B (50/50 2R/3R)", years=5.0)

print("\n--- IN-SAMPLE OVERALL (2020 - 2024) ---")
print(f"Option A (Top 10% ML, 2.0R Fixed) : {res_train_A10['trades_yr']} tr/yr | WR: {res_train_A10['wr']}% | PF: {res_train_A10['pf']} | Ann R: +{res_train_A10['ann_r']}R | Max DD: {res_train_A10['max_dd_r']}R")
print(f"Option B (Top 10% ML, 50/50 2R/3R): {res_train_B10['trades_yr']} tr/yr | WR: {res_train_B10['wr']}% | PF: {res_train_B10['pf']} | Ann R: +{res_train_B10['ann_r']}R | Max DD: {res_train_B10['max_dd_r']}R")
print(f"Option A (Top 11% ML, 2.0R Fixed) : {res_train_A11['trades_yr']} tr/yr | WR: {res_train_A11['wr']}% | PF: {res_train_A11['pf']} | Ann R: +{res_train_A11['ann_r']}R | Max DD: {res_train_A11['max_dd_r']}R")
print(f"Option B (Top 11% ML, 50/50 2R/3R): {res_train_B11['trades_yr']} tr/yr | WR: {res_train_B11['wr']}% | PF: {res_train_B11['pf']} | Ann R: +{res_train_B11['ann_r']}R | Max DD: {res_train_B11['max_dd_r']}R")

print("\n--- IN-SAMPLE YEAR-BY-YEAR (Option A - Top 10%) ---")
for y in range(2020, 2025):
    y_sub = sub_train_optA_10[sub_train_optA_10['year'] == y]
    y_res = evaluate_strategy(y_sub, "Option A (2.0R Target)", years=1.0)
    print(f"Year {y}: N={y_res['trades']:3d} | WR: {y_res['wr']:5.1f}% | PF: {y_res['pf']:4.2f} | Realized R: +{y_res['ann_r']:5.1f}R | Max DD: {y_res['max_dd_r']:4.1f}R")

print("\n" + "="*80)
print("PHASE 2: BLIND OUT-OF-SAMPLE EVALUATION (2025 - 2026, 1.52 YEARS)")
print("="*80)

# Apply identical In-Sample cutoff threshold to test set
sub_test_optA_10 = test[test['score'] >= cutoff_p90]
sub_test_optA_11 = test[test['score'] >= cutoff_p89]

res_test_A10 = evaluate_strategy(sub_test_optA_10, "Option A (2.0R Target)", years=1.52)
res_test_B10 = evaluate_strategy(sub_test_optA_10, "Option B (50/50 2R/3R)", years=1.52)
res_test_A11 = evaluate_strategy(sub_test_optA_11, "Option A (2.0R Target)", years=1.52)
res_test_B11 = evaluate_strategy(sub_test_optA_11, "Option B (50/50 2R/3R)", years=1.52)

print("\n--- OUT-OF-SAMPLE OVERALL (2025 - 2026) ---")
print(f"Option A (Top 10% ML, 2.0R Fixed) : {res_test_A10['trades_yr']} tr/yr | WR: {res_test_A10['wr']}% | PF: {res_test_A10['pf']} | Ann R: +{res_test_A10['ann_r']}R | Max DD: {res_test_A10['max_dd_r']}R")
print(f"Option B (Top 10% ML, 50/50 2R/3R): {res_test_B10['trades_yr']} tr/yr | WR: {res_test_B10['wr']}% | PF: {res_test_B10['pf']} | Ann R: +{res_test_B10['ann_r']}R | Max DD: {res_test_B10['max_dd_r']}R")
print(f"Option A (Top 11% ML, 2.0R Fixed) : {res_test_A11['trades_yr']} tr/yr | WR: {res_test_A11['wr']}% | PF: {res_test_A11['pf']} | Ann R: +{res_test_A11['ann_r']}R | Max DD: {res_test_A11['max_dd_r']}R")
print(f"Option B (Top 11% ML, 50/50 2R/3R): {res_test_B11['trades_yr']} tr/yr | WR: {res_test_B11['wr']}% | PF: {res_test_B11['pf']} | Ann R: +{res_test_B11['ann_r']}R | Max DD: {res_test_B11['max_dd_r']}R")

print("\n--- OUT-OF-SAMPLE YEAR-BY-YEAR (Option A - Top 10%) ---")
for y in [2025, 2026]:
    y_sub = sub_test_optA_10[sub_test_optA_10['year'] == y]
    y_len = 1.0 if y == 2025 else 0.52
    y_res = evaluate_strategy(y_sub, "Option A (2.0R Target)", years=y_len)
    print(f"Year {y}: N={y_res['trades']:3d} ({y_res['trades_yr']:.0f}/yr) | WR: {y_res['wr']:5.1f}% | PF: {y_res['pf']:4.2f} | Realized R: +{y_res['ann_r']:5.1f}R | Max DD: {y_res['max_dd_r']:4.1f}R")

# Save complete metrics artifact
results_payload = {
    'threshold_p90': float(cutoff_p90),
    'threshold_p89': float(cutoff_p89),
    'in_sample_2020_2024': {
        'Option_A_Top10': res_train_A10,
        'Option_B_Top10': res_train_B10,
        'Option_A_Top11': res_train_A11,
        'Option_B_Top11': res_train_B11,
    },
    'out_sample_2025_2026': {
        'Option_A_Top10': res_test_A10,
        'Option_B_Top10': res_test_B10,
        'Option_A_Top11': res_test_A11,
        'Option_B_Top11': res_test_B11,
    }
}
with open("deploy/target_200_oos_results.json", "w") as f:
    json.dump(results_payload, f, indent=2)

print("\n[COMPLETE] Results saved to deploy/target_200_oos_results.json")
