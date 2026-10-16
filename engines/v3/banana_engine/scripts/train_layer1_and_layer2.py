#!/usr/bin/env python3
"""
scripts/train_layer1_and_layer2.py — Layer 1 & Layer 2 Training Pipeline
=======================================================================
Implements Layer 1 (Pattern-Specific Models + Meta-Classifier) and
Layer 2 (Multi-Timeframe Weekly Features + Alignment Filter):
1. Loads candidate dataset (data/ml_engine_v2_candidates.parquet).
2. Trains 4 LightGBM pattern classifiers (HTF, BASE, 52W_HIGH, DARVAS) on 2020-2024.
3. Generates pattern scores and trains Meta-Classifier.
4. Evaluates Layer 1 OOS (2025-2026) AUC and logs layer1_metrics.json.
5. Ingests 8 weekly features + mtf_alignment score.
6. Tests mtf_alignment filter reject rate.
7. Retrains all 4 pattern models + meta-classifier with weekly features.
8. Logs layer2_metrics.json.
"""

import sys
import os
import time
import json
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, accuracy_score
from typing import Dict, List, Any

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_PATH = os.path.join(_PKG_ROOT, "data/ml_engine_v2_candidates.parquet")
OUT_DIR = os.path.join(_PKG_ROOT, "data/ml_engine_v2")

BASE_FEATURES = [
    # Candle anatomy
    "chase_pct", "upper_wick_pct", "lower_wick_pct", "body_pct",
    "cir", "is_bull_body", "bar_ret", "gap_pct",
    # Volume
    "vol_ratio_20", "vol_ratio_50", "volume_slope_20",
    "ud_ratio_20", "dry_up_ratio",
    # Trend alignment
    "dist_sma20", "dist_sma50", "dist_sma200",
    "sma50_slope_20", "sma200_slope_20",
    "ema_stack_score",
    # Supply structure
    "ath_dist_pct", "dist_from_52w_high", "dist_from_52w_low",
    # Pattern geometry
    "is_htf", "is_base", "is_52w", "is_darvas",
    "base_depth_pct", "base_duration_bars",
    "flag_pole_height_pct", "flag_duration_bars",
    # Execution risk
    "risk_pct", "r_ratio_available"
]

WEEKLY_FEATURES = [
    "w_uptrend", "w_sma_slope", "w_rs_20", "w_atr_pct",
    "w_vol_ratio", "w_base_depth", "w_base_tightness", "w_position_52w"
]

PATTERNS = ['HTF', 'BASE', '52W_HIGH', 'DARVAS']

def get_lgb_params(seed=42):
    return {
        'objective': 'binary',
        'metric': 'auc',
        'boosting_type': 'gbdt',
        'n_estimators': 1000,
        'learning_rate': 0.03,
        'num_leaves': 15,
        'max_depth': 4,
        'min_child_samples': 30,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'random_state': seed,
        'verbose': -1,
        'n_jobs': -1
    }

def run_layer1(df):
    print("\n=======================================================")
    print("[LAYER 1] Training Pattern-Specific Classifiers & Meta-Ensemble")
    print("=======================================================")
    
    os.makedirs(OUT_DIR, exist_ok=True)
    train_df = df[df['year'] <= 2024].copy()
    test_df = df[df['year'] >= 2025].copy()
    
    pattern_models = {}
    pattern_metrics = {}
    
    # In-Sample Train/Val split: 2020-2023 train, 2024 val
    is_tr = train_df['year'] <= 2023
    is_val = train_df['year'] == 2024
    
    for pat in PATTERNS:
        tr_pat = train_df[train_df['pattern'] == pat]
        te_pat = test_df[test_df['pattern'] == pat]
        print(f"  Training {pat:<10} | Train N: {len(tr_pat):,} | Test N: {len(te_pat):,}")
        
        tr_sub = tr_pat[tr_pat['year'] <= 2023]
        val_sub = tr_pat[tr_pat['year'] == 2024]
        
        clf = lgb.LGBMClassifier(**get_lgb_params())
        if len(tr_sub) > 50 and len(val_sub) > 20:
            clf.fit(
                tr_sub[BASE_FEATURES], tr_sub['label'],
                eval_set=[(val_sub[BASE_FEATURES], val_sub['label'])],
                callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)]
            )
            n_trees = max(clf.best_iteration_, 50)
            clf_full = lgb.LGBMClassifier(**get_lgb_params())
            clf_full.set_params(n_estimators=n_trees)
            clf_full.fit(tr_pat[BASE_FEATURES], tr_pat['label'])
            clf = clf_full
        else:
            clf.set_params(n_estimators=100)
            clf.fit(tr_pat[BASE_FEATURES], tr_pat['label'])
            
        tr_pred = clf.predict_proba(tr_pat[BASE_FEATURES])[:, 1]
        te_pred = clf.predict_proba(te_pat[BASE_FEATURES])[:, 1]
        tr_auc = roc_auc_score(tr_pat['label'], tr_pred)
        te_auc = roc_auc_score(te_pat['label'], te_pred)
        
        model_path = os.path.join(OUT_DIR, f"pattern_{pat}.lgb")
        clf.booster_.save_model(model_path)
        pattern_models[pat] = clf
        
        pattern_metrics[pat] = {
            'train_n': len(tr_pat),
            'test_n': len(te_pat),
            'train_auc': round(float(tr_auc), 4),
            'oos_auc': round(float(te_auc), 4),
            'model_path': model_path
        }
        print(f"    -> In-Sample AUC: {tr_auc:.4f} | Out-of-Sample AUC: {te_auc:.4f}")
        
    # Generate Pattern Scores across all records
    for pat in PATTERNS:
        clf = pattern_models[pat]
        mask_tr = train_df['pattern'] == pat
        mask_te = test_df['pattern'] == pat
        
        train_df.loc[mask_tr, f'score_{pat}'] = clf.predict_proba(train_df.loc[mask_tr, BASE_FEATURES])[:, 1]
        test_df.loc[mask_te, f'score_{pat}'] = clf.predict_proba(test_df.loc[mask_te, BASE_FEATURES])[:, 1]
        
        # Fill non-matching patterns with universe mean
        mean_tr = train_df.loc[mask_tr, f'score_{pat}'].mean()
        train_df[f'score_{pat}'] = train_df[f'score_{pat}'].fillna(mean_tr)
        test_df[f'score_{pat}'] = test_df[f'score_{pat}'].fillna(mean_tr)
        
    pat_map = {'HTF': 0, 'BASE': 1, '52W_HIGH': 2, 'DARVAS': 3}
    train_df['pattern_ordinal'] = train_df['pattern'].map(pat_map).fillna(0).astype(int)
    test_df['pattern_ordinal'] = test_df['pattern'].map(pat_map).fillna(0).astype(int)
    
    META_FEATURES = BASE_FEATURES + [f'score_{p}' for p in PATTERNS] + ['pattern_ordinal']
    
    print("\n  Training Meta-Classifier on Pattern Scores + Base Features...")
    meta_clf = lgb.LGBMClassifier(**get_lgb_params())
    meta_clf.fit(
        train_df.loc[is_tr, META_FEATURES], train_df.loc[is_tr, 'label'],
        eval_set=[(train_df.loc[is_val, META_FEATURES], train_df.loc[is_val, 'label'])],
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)]
    )
    n_trees = max(meta_clf.best_iteration_, 80)
    meta_full = lgb.LGBMClassifier(**get_lgb_params())
    meta_full.set_params(n_estimators=n_trees)
    meta_full.fit(train_df[META_FEATURES], train_df['label'])
    meta_clf = meta_full
    
    meta_tr_pred = meta_clf.predict_proba(train_df[META_FEATURES])[:, 1]
    meta_te_pred = meta_clf.predict_proba(test_df[META_FEATURES])[:, 1]
    meta_tr_auc = roc_auc_score(train_df['label'], meta_tr_pred)
    meta_te_auc = roc_auc_score(test_df['label'], meta_te_pred)
    
    meta_path = os.path.join(OUT_DIR, "meta_model.lgb")
    meta_clf.booster_.save_model(meta_path)
    print(f"  -> Meta-Classifier In-Sample AUC: {meta_tr_auc:.4f} | Out-of-Sample AUC: {meta_te_auc:.4f}")
    
    layer1_res = {
        'pattern_models': pattern_metrics,
        'meta_model': {
            'train_n': len(train_df),
            'test_n': len(test_df),
            'train_auc': round(float(meta_tr_auc), 4),
            'oos_auc': round(float(meta_te_auc), 4),
            'model_path': meta_path,
            'features': META_FEATURES
        },
        'acceptance_criteria': {
            'meta_oos_auc_ge_055': bool(meta_te_auc >= 0.55),
            'all_patterns_ge_052': bool(all(m['oos_auc'] >= 0.52 for m in pattern_metrics.values()))
        }
    }
    
    l1_json = os.path.join(OUT_DIR, "layer1_metrics.json")
    with open(l1_json, "w") as f:
        json.dump(layer1_res, f, indent=2)
    print(f"  Saved Layer 1 metrics to {l1_json}")
    
    return layer1_res, train_df, test_df

def run_layer2(train_df, test_df):
    print("\n=======================================================")
    print("[LAYER 2] Incorporating Multi-Timeframe Weekly Features")
    print("=======================================================")
    
    # Analyze MTF Alignment Filter
    print("  Evaluating mtf_alignment distribution & reject performance...")
    for split_name, df_sub in [('In-Sample (2020-2024)', train_df), ('Out-of-Sample (2025-2026)', test_df)]:
        low_mtf = df_sub[df_sub['mtf_alignment'] < 3]
        high_mtf = df_sub[df_sub['mtf_alignment'] >= 3]
        low_loss_rate = np.mean(low_mtf['label'] == 0) * 100.0 if len(low_mtf) else 0.0
        high_loss_rate = np.mean(high_mtf['label'] == 0) * 100.0 if len(high_mtf) else 0.0
        print(f"    {split_name}:")
        print(f"      mtf_alignment < 3  (Reject Bucket): N = {len(low_mtf):,} | Loss Rate = {low_loss_rate:.1f}%")
        print(f"      mtf_alignment >= 3 (Keep Bucket):   N = {len(high_mtf):,} | Loss Rate = {high_loss_rate:.1f}%")
        
    L2_BASE_FEATURES = BASE_FEATURES + WEEKLY_FEATURES + ['mtf_alignment']
    pattern_models_l2 = {}
    pattern_metrics_l2 = {}
    
    is_tr = train_df['year'] <= 2023
    is_val = train_df['year'] == 2024
    
    for pat in PATTERNS:
        tr_pat = train_df[train_df['pattern'] == pat]
        te_pat = test_df[test_df['pattern'] == pat]
        
        tr_sub = tr_pat[tr_pat['year'] <= 2023]
        val_sub = tr_pat[tr_pat['year'] == 2024]
        
        clf = lgb.LGBMClassifier(**get_lgb_params(seed=101))
        if len(tr_sub) > 50 and len(val_sub) > 20:
            clf.fit(
                tr_sub[L2_BASE_FEATURES], tr_sub['label'],
                eval_set=[(val_sub[L2_BASE_FEATURES], val_sub['label'])],
                callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)]
            )
            n_trees = max(clf.best_iteration_, 50)
            clf_full = lgb.LGBMClassifier(**get_lgb_params(seed=101))
            clf_full.set_params(n_estimators=n_trees)
            clf_full.fit(tr_pat[L2_BASE_FEATURES], tr_pat['label'])
            clf = clf_full
        else:
            clf.set_params(n_estimators=100)
            clf.fit(tr_pat[L2_BASE_FEATURES], tr_pat['label'])
            
        tr_pred = clf.predict_proba(tr_pat[L2_BASE_FEATURES])[:, 1]
        te_pred = clf.predict_proba(te_pat[L2_BASE_FEATURES])[:, 1]
        tr_auc = roc_auc_score(tr_pat['label'], tr_pred)
        te_auc = roc_auc_score(te_pat['label'], te_pred)
        
        model_path = os.path.join(OUT_DIR, f"pattern_{pat}.lgb")
        clf.booster_.save_model(model_path)
        pattern_models_l2[pat] = clf
        
        pattern_metrics_l2[pat] = {
            'train_auc': round(float(tr_auc), 4),
            'oos_auc': round(float(te_auc), 4),
            'model_path': model_path
        }
        print(f"  Pattern {pat:<10} + Weekly | IS AUC: {tr_auc:.4f} | OOS AUC: {te_auc:.4f}")
        
    for pat in PATTERNS:
        clf = pattern_models_l2[pat]
        mask_tr = train_df['pattern'] == pat
        mask_te = test_df['pattern'] == pat
        
        train_df.loc[mask_tr, f'score_{pat}'] = clf.predict_proba(train_df.loc[mask_tr, L2_BASE_FEATURES])[:, 1]
        test_df.loc[mask_te, f'score_{pat}'] = clf.predict_proba(test_df.loc[mask_te, L2_BASE_FEATURES])[:, 1]
        
        mean_tr = train_df.loc[mask_tr, f'score_{pat}'].mean()
        train_df[f'score_{pat}'] = train_df[f'score_{pat}'].fillna(mean_tr)
        test_df[f'score_{pat}'] = test_df[f'score_{pat}'].fillna(mean_tr)
        
    L2_META_FEATURES = L2_BASE_FEATURES + [f'score_{p}' for p in PATTERNS] + ['pattern_ordinal']
    
    print("\n  Retraining Meta-Classifier with Weekly MTF Features...")
    meta_clf_l2 = lgb.LGBMClassifier(**get_lgb_params(seed=101))
    meta_clf_l2.fit(
        train_df.loc[is_tr, L2_META_FEATURES], train_df.loc[is_tr, 'label'],
        eval_set=[(train_df.loc[is_val, L2_META_FEATURES], train_df.loc[is_val, 'label'])],
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)]
    )
    n_trees = max(meta_clf_l2.best_iteration_, 80)
    meta_full_l2 = lgb.LGBMClassifier(**get_lgb_params(seed=101))
    meta_full_l2.set_params(n_estimators=n_trees)
    meta_full_l2.fit(train_df[L2_META_FEATURES], train_df['label'])
    meta_clf_l2 = meta_full_l2
    
    meta_tr_pred = meta_clf_l2.predict_proba(train_df[L2_META_FEATURES])[:, 1]
    meta_te_pred = meta_clf_l2.predict_proba(test_df[L2_META_FEATURES])[:, 1]
    meta_tr_auc = roc_auc_score(train_df['label'], meta_tr_pred)
    meta_te_auc = roc_auc_score(test_df['label'], meta_te_pred)
    
    meta_path = os.path.join(OUT_DIR, "meta_model.lgb")
    meta_clf_l2.booster_.save_model(meta_path)
    print(f"  -> Layer 2 Meta-Classifier IS AUC: {meta_tr_auc:.4f} | OOS AUC: {meta_te_auc:.4f}")
    
    low_mtf_oos = test_df[test_df['mtf_alignment'] < 3]
    oos_reject_loss_rate = np.mean(low_mtf_oos['label'] == 0) * 100.0 if len(low_mtf_oos) else 0.0
    
    layer2_res = {
        'pattern_models': pattern_metrics_l2,
        'meta_model': {
            'train_auc': round(float(meta_tr_auc), 4),
            'oos_auc': round(float(meta_te_auc), 4),
            'features': L2_META_FEATURES
        },
        'mtf_filter': {
            'low_mtf_oos_candidates': len(low_mtf_oos),
            'oos_reject_loss_rate': round(float(oos_reject_loss_rate), 1),
            'criterion_ge_60pct_met': bool(oos_reject_loss_rate >= 60.0)
        }
    }
    
    l2_json = os.path.join(OUT_DIR, "layer2_metrics.json")
    with open(l2_json, "w") as f:
        json.dump(layer2_res, f, indent=2)
    print(f"  Saved Layer 2 metrics to {l2_json}")
    
    return layer2_res, train_df, test_df

def main():
    if not os.path.exists(DATA_PATH):
        print(f"Error: {DATA_PATH} not found. Please wait for dataset build to complete.")
        sys.exit(1)
        
    print(f"Loading candidate dataset from {DATA_PATH}...")
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} candidates.")
    
    l1_res, train_df, test_df = run_layer1(df)
    l2_res, train_df, test_df = run_layer2(train_df, test_df)

if __name__ == "__main__":
    main()
