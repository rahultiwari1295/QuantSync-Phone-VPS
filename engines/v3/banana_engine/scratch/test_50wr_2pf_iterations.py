#!/usr/bin/env python3
"""
scratch/test_50wr_2pf_iterations.py
===================================
Rigorous exploration strictly on 2020-2024 In-Sample data.
Goal: Discover iterations that produce:
  - ~200 - 250 trades / year (1,000 - 1,250 trades over 5 years)
  - Win Rate >= 50%
  - Profit Factor >= 2.0
"""
import os
import sys
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import KFold

DATA_PATH = "QuantSync_Lines/banana_engine/data/ml_engine_v2_candidates.parquet"
df = pd.read_parquet(DATA_PATH)
train = df[df['year'] <= 2024].copy().reset_index(drop=True)
print(f"Loaded 2020-2024 In-Sample candidates: {len(train)} rows (~{len(train)/5:.0f}/yr)")

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

# Label 1: reached 2R (label)
# Let's train a 5-fold cross-validated model to get out-of-fold probability predictions on 2020-2024
oof_preds = np.zeros(len(train))
kf = KFold(n_splits=5, shuffle=False)

for fold, (trn_idx, val_idx) in enumerate(kf.split(train)):
    X_tr, y_tr = train.loc[trn_idx, feature_cols], train.loc[trn_idx, 'label']
    X_va, y_va = train.loc[val_idx, feature_cols], train.loc[val_idx, 'label']
    
    clf = lgb.LGBMClassifier(
        n_estimators=150, learning_rate=0.03, num_leaves=31,
        subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1
    )
    clf.fit(X_tr, y_tr)
    oof_preds[val_idx] = clf.predict_proba(X_va)[:, 1]

train['pred_prob'] = oof_preds

print("\n--- ITERATION 1: Pure ML Probability Deciles (Fixed 3R Target, Stop at Low) ---")
for pct in [5, 8, 10, 12, 15, 20]:
    cutoff = np.percentile(train['pred_prob'], 100 - pct)
    subset = train[train['pred_prob'] >= cutoff].copy()
    n_yr = len(subset) / 5.0
    wins = subset[subset['pnl_r'] > 0]
    losses = subset[subset['pnl_r'] <= 0]
    wr = len(wins) / len(subset) * 100.0
    tot_win = wins['pnl_r'].sum()
    tot_loss = abs(losses['pnl_r'].sum())
    pf = tot_win / tot_loss if tot_loss > 0 else 999.0
    avg_w = wins['pnl_r'].mean() if len(wins) > 0 else 0.0
    avg_l = abs(losses['pnl_r'].mean()) if len(losses) > 0 else 1.0
    print(f"Top {pct:2d}%: {len(subset):4d} trades ({n_yr:5.1f}/yr) | WR: {wr:5.1f}% | AvgW: {avg_w:4.2f}R | AvgL: {avg_l:4.2f}R | PF: {pf:4.2f}")

print("\n--- ITERATION 2: Testing Target Optimization (What if target is 2.0R vs 2.5R vs 3.0R?) ---")
# If trade takes target at 2.0R, wins are when mfe_r >= 2.0 before stop (i.e. label == 1)
# When target is 2.0R: win gives +2.0R, loss gives -1.0R
# If WR is p, PF = (p * 2.0) / ((1-p) * 1.0). To get PF >= 2.0 at 2.0R, we need WR >= 50%!
# If target is 2.5R: win gives +2.5R, loss gives -1.0R. PF = (p * 2.5) / ((1-p) * 1.0). For PF >= 2.0, need WR >= 44.4%!
for pct in [8, 10, 12, 15]:
    cutoff = np.percentile(train['pred_prob'], 100 - pct)
    subset = train[train['pred_prob'] >= cutoff].copy()
    n_yr = len(subset) / 5.0
    
    # 2.0R Target
    wr_2r = (subset['label'] == 1).mean() * 100.0
    pf_2r = (wr_2r / 100.0 * 2.0) / ((1 - wr_2r / 100.0) * 1.0)
    
    # 2.5R Target (approx mfe_r >= 2.5)
    w_25 = (subset['mfe_r'] >= 2.5) & (subset['label'] == 1) # reached 2R first and exceeded 2.5
    wr_25 = w_25.mean() * 100.0
    pf_25 = (wr_25 / 100.0 * 2.5) / ((1 - wr_25 / 100.0) * 1.0)
    
    print(f"Top {pct:2d}% ({n_yr:4.0f}/yr) -> 2.0R Target: WR={wr_2r:5.1f}%, PF={pf_2r:4.2f} | 2.5R Target: WR={wr_25:5.1f}%, PF={pf_25:4.2f}")
