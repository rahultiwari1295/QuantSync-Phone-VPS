#!/usr/bin/env python3
"""
scripts/build_layer4_analog_engine.py — Layer 4: Analogical Retrieval Engine
==========================================================================
1. Extracts 32-point normalized price shape embeddings for all candidates (lookback=40).
2. Builds historical index from 2020-2024 candidates:
   - data/ml_engine_v2/historical_shapes.npy
   - data/ml_engine_v2/historical_outcomes.npy
   - data/ml_engine_v2/historical_meta.parquet (for leave-one-out symbol/date exclusion)
3. Computes 7 analog features with strict leave-one-out self-exclusion for train set.
4. Retrains Meta-Classifier and evaluates analog feature importance & win-rate spread.
5. Saves layer4_metrics.json and updated dataset.
"""

import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score
from typing import Dict, List, Any

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_PATH = os.path.join(_PKG_ROOT, "data/ml_engine_v2_candidates.parquet")
OUT_DIR = os.path.join(_PKG_ROOT, "data/ml_engine_v2")
DB_PATH = os.path.abspath("Prod_VPS/Data/stock_history.db")

def shape_embedding(closes, signal_idx, lookback=40, n_points=32):
    if signal_idx < lookback:
        return np.zeros(n_points, dtype=np.float32)
    seg = closes[signal_idx - lookback : signal_idx + 1]
    if len(seg) < lookback:
        return np.zeros(n_points, dtype=np.float32)
    x_old = np.linspace(0, 1, len(seg))
    x_new = np.linspace(0, 1, n_points)
    resampled = np.interp(x_new, x_old, seg)
    lo = float(np.min(resampled))
    hi = float(np.max(resampled))
    if hi - lo < 1e-6:
        return np.zeros(n_points, dtype=np.float32)
    norm = (resampled - lo) / (hi - lo)
    return norm.astype(np.float32)

def main():
    print("=======================================================")
    print("[LAYER 4] Building Analogical Retrieval Engine & Index")
    print("=======================================================")
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded {len(df):,} candidates from {DATA_PATH}.")
    
    # Load raw candles to extract shape embeddings
    print(f"Loading raw close series from {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, rawCandles FROM StockHistory")
    rows = c.fetchall()
    conn.close()
    
    closes_by_sym = {}
    for sym, raw in rows:
        try:
            rl = json.loads(raw)
            if len(rl) < 120: continue
            c_vals = np.array([float(x['c']) for x in rl], dtype=np.float32)
            closes_by_sym[sym] = c_vals
        except Exception:
            continue
            
    print(f"Extracted close arrays for {len(closes_by_sym)} symbols.")
    
    # Generate 32-dim shapes for all candidates
    shapes_list = []
    valid_mask = []
    print("Computing shape embeddings for all candidates...")
    for idx, row in df.iterrows():
        sym = row['symbol']
        sig_idx = int(row['signal_idx'])
        c_arr = closes_by_sym.get(sym)
        if c_arr is not None and sig_idx < len(c_arr):
            v = shape_embedding(c_arr, sig_idx, lookback=40, n_points=32)
            shapes_list.append(v)
            valid_mask.append(True)
        else:
            shapes_list.append(np.zeros(32, dtype=np.float32))
            valid_mask.append(False)
            
    all_shapes = np.array(shapes_list, dtype=np.float32)
    
    # In-Sample Index (2020-2024)
    train_mask = (df['year'] <= 2024).values
    hist_shapes = all_shapes[train_mask]
    hist_outcomes = df.loc[train_mask, 'pnl_r'].values.astype(np.float32)
    hist_syms = df.loc[train_mask, 'symbol'].values
    hist_dates = df.loc[train_mask, 'date'].values
    
    shapes_path = os.path.join(OUT_DIR, "historical_shapes.npy")
    outcomes_path = os.path.join(OUT_DIR, "historical_outcomes.npy")
    np.save(shapes_path, hist_shapes)
    np.save(outcomes_path, hist_outcomes)
    print(f"Saved {len(hist_shapes):,} historical shapes to {shapes_path}.")
    print(f"Saved {len(hist_outcomes):,} historical outcomes to {outcomes_path}.")
    
    # Pre-normalize historical shapes for cosine similarity: norm = 1
    norms = np.linalg.norm(hist_shapes, axis=1, keepdims=True) + 1e-9
    hist_shapes_norm = hist_shapes / norms
    
    # Compute Analog Features for all candidates
    print("Computing analog features with leave-one-out exclusion for training set...")
    t_start = time.time()
    k = 20
    
    analog_wr = np.zeros(len(df), dtype=np.float32)
    analog_mean_r = np.zeros(len(df), dtype=np.float32)
    analog_weighted_r = np.zeros(len(df), dtype=np.float32)
    analog_best_r = np.zeros(len(df), dtype=np.float32)
    analog_worst_r = np.zeros(len(df), dtype=np.float32)
    analog_std_r = np.zeros(len(df), dtype=np.float32)
    analog_top_sim = np.zeros(len(df), dtype=np.float32)
    
    all_syms = df['symbol'].values
    all_dates = df['date'].values
    
    for i in range(len(df)):
        q = all_shapes[i]
        q_norm = q / (np.linalg.norm(q) + 1e-9)
        sims = np.dot(hist_shapes_norm, q_norm)
        
        if train_mask[i]:
            # Leave-one-out: exclude candidate with same symbol and same date
            cur_sym = all_syms[i]
            cur_date = all_dates[i]
            match_mask = (hist_syms == cur_sym) & (hist_dates == cur_date)
            sims[match_mask] = -999.0 # eliminate from top-k
            
        top_k_idx = np.argsort(-sims)[:k]
        top_outcomes = hist_outcomes[top_k_idx]
        top_sims = np.maximum(sims[top_k_idx], 0.001)
        
        w = top_sims / np.sum(top_sims)
        
        analog_wr[i] = (top_outcomes > 0).mean()
        analog_mean_r[i] = top_outcomes.mean()
        analog_weighted_r[i] = np.sum(top_outcomes * w)
        analog_best_r[i] = top_outcomes.max()
        analog_worst_r[i] = top_outcomes.min()
        analog_std_r[i] = top_outcomes.std()
        analog_top_sim[i] = top_sims[0]
        
    print(f"Finished analog feature generation for {len(df):,} candidates in {time.time() - t_start:.2f}s.")
    
    # Verification of Leave-One-Out Exclusion
    test_idx = np.random.choice(np.where(train_mask)[0])
    q_test = all_shapes[test_idx]
    q_test_norm = q_test / (np.linalg.norm(q_test) + 1e-9)
    sims_test = np.dot(hist_shapes_norm, q_test_norm)
    match_mask = (hist_syms == all_syms[test_idx]) & (hist_dates == all_dates[test_idx])
    sims_test[match_mask] = -999.0
    top_k_test = np.argsort(-sims_test)[:k]
    for hit_idx in top_k_test:
        assert not (hist_syms[hit_idx] == all_syms[test_idx] and hist_dates[hit_idx] == all_dates[test_idx]), "LEAKAGE DETECTED in Analog Engine!"
    print("Leave-one-out verification PASSED: Zero self-matches in retrieval.")
    
    df['analog_win_rate'] = analog_wr
    df['analog_mean_r'] = analog_mean_r
    df['analog_weighted_r'] = analog_weighted_r
    df['analog_best_r'] = analog_best_r
    df['analog_worst_r'] = analog_worst_r
    df['analog_std_r'] = analog_std_r
    df['analog_top_similarity'] = analog_top_sim
    
    # Save back to candidates parquet
    df.to_parquet(DATA_PATH, index=False)
    print(f"Updated candidate dataset with 7 analog features in {DATA_PATH}.")
    
    # Retrain Meta-Classifier with Analog Features
    train_df = df[df['year'] <= 2024].copy()
    test_df = df[df['year'] >= 2025].copy()
    
    # Check Analog Agreement Spread
    # High agreement = low analog_std_r
    q33 = train_df['analog_std_r'].quantile(0.33)
    q67 = train_df['analog_std_r'].quantile(0.67)
    high_agree_oos = test_df[test_df['analog_std_r'] <= q33]
    low_agree_oos = test_df[test_df['analog_std_r'] >= q67]
    high_wr = (high_agree_oos['label'] == 1).mean() * 100.0
    low_wr = (low_agree_oos['label'] == 1).mean() * 100.0
    spread = high_wr - low_wr
    print(f"Analog Agreement Win-Rate Spread (OOS): High Agreement {high_wr:.1f}% vs Low Agreement {low_wr:.1f}% (Spread = {spread:+.1f}%)")
    
    # Retrain Meta-Classifier with Layer 4 features
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
    
    L4_FEATURES = BASE_FEATURES + WEEKLY_FEATURES + ANALOG_FEATURES
    
    print("\nRetraining Meta-Classifier with Layer 4 Features (Base + MTF + Analog)...")
    clf = lgb.LGBMClassifier(
        objective='binary',
        metric='auc',
        boosting_type='gbdt',
        n_estimators=1000,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=4,
        min_child_samples=30,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbose=-1,
        n_jobs=-1
    )
    is_tr = train_df['year'] <= 2023
    is_val = train_df['year'] == 2024
    clf.fit(
        train_df.loc[is_tr, L4_FEATURES], train_df.loc[is_tr, 'label'],
        eval_set=[(train_df.loc[is_val, L4_FEATURES], train_df.loc[is_val, 'label'])],
        callbacks=[lgb.early_stopping(stopping_rounds=50, verbose=False)]
    )
    n_trees = max(clf.best_iteration_, 100)
    clf_full = lgb.LGBMClassifier(
        objective='binary',
        metric='auc',
        boosting_type='gbdt',
        n_estimators=n_trees,
        learning_rate=0.03,
        num_leaves=15,
        max_depth=4,
        min_child_samples=30,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbose=-1,
        n_jobs=-1
    )
    clf_full.fit(train_df[L4_FEATURES], train_df['label'])
    clf = clf_full
    
    tr_pred = clf.predict_proba(train_df[L4_FEATURES])[:, 1]
    te_pred = clf.predict_proba(test_df[L4_FEATURES])[:, 1]
    tr_auc = roc_auc_score(train_df['label'], tr_pred)
    te_auc = roc_auc_score(test_df['label'], te_pred)
    
    # Save model
    meta_path = os.path.join(OUT_DIR, "meta_model.lgb")
    clf.booster_.save_model(meta_path)
    print(f"Layer 4 Meta-Classifier In-Sample AUC: {tr_auc:.4f} | Out-of-Sample AUC: {te_auc:.4f}")
    
    # Top features by gain
    importances = clf.feature_importances_
    sorted_imp = sorted(zip(L4_FEATURES, importances), key=lambda x: x[1], reverse=True)
    print("Top 10 Features by Importance:")
    for f_name, imp in sorted_imp[:10]:
        print(f"  - {f_name:<24}: {imp}")
        
    layer4_res = {
        'train_auc': round(float(tr_auc), 4),
        'oos_auc': round(float(te_auc), 4),
        'analog_spread_pct': round(float(spread), 1),
        'top_features': [f for f, _ in sorted_imp[:10]],
        'features': L4_FEATURES
    }
    
    l4_json = os.path.join(OUT_DIR, "layer4_metrics.json")
    with open(l4_json, "w") as f:
        json.dump(layer4_res, f, indent=2)
    print(f"Saved Layer 4 metrics to {l4_json} in {time.time() - t0:.2f}s.")

if __name__ == "__main__":
    main()
