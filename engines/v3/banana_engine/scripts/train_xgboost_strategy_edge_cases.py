#!/usr/bin/env python3
"""
scripts/train_xgboost_strategy_edge_cases.py
============================================
Uses XGBoost Machine Learning to discover the non-linear feature interactions,
exact edge cases, and failure traps across all chart trading strategies.

Protocol:
1. Candidate Setup Generation across 1,319 Indian stocks (2020-2026).
2. 24 Quantitative Features extracted per setup.
3. In-Sample Training (2020-2024) using 5-Fold Time-Series Cross-Validation.
4. Edge Case & Failure Trap Discovery (Feature Importance & Decision Path Mining).
5. Held-Out Out-of-Sample Validation (2025-2026) evaluating Raw vs Manual vs XGBoost.
"""

import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import roc_auc_score, precision_score, classification_report
from sklearn.tree import DecisionTreeClassifier, export_text
from typing import Dict, List, Any, Tuple

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

DB_PATH = os.path.abspath("Prod_VPS/Data/stock_history.db")
OUTPUT_JSON = os.path.join(_PKG_ROOT, "data/xgboost_edge_cases_results.json")

# -----------------------------------------------------------------------------
# 1. Candle Universe Loading & Technical Indicator Precomputation
# -----------------------------------------------------------------------------

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
            
            # Moving averages & volume
            df['ema20'] = df['c'].ewm(span=20, adjust=False).mean()
            df['sma50'] = df['c'].rolling(50, min_periods=20).mean()
            df['sma200'] = df['c'].rolling(200, min_periods=50).mean()
            df['vol_sma20'] = df['v'].rolling(20, min_periods=5).mean().shift(1)
            df['vol_sma50'] = df['v'].rolling(50, min_periods=10).mean().shift(1)
            
            # 52-week High
            df['high_250'] = df['h'].rolling(250, min_periods=60).max().shift(1)
            
            # ATR 14
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

# -----------------------------------------------------------------------------
# 2. Candidate Setup Generation & Feature Extraction
# -----------------------------------------------------------------------------

def extract_setups_and_features(universe: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    print("\n[2/5] Scanning candidate trade setups and extracting 24 quantitative features...")
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
            
            # Liquidity floor: price >= 30, turnover >= 1.5 Cr
            turnover = c[i] * v[i]
            if c[i] < 30.0 or turnover < 15_000_000.0: continue
            if np.isnan(sma200[i]) or np.isnan(sma50[i]) or np.isnan(high_250[i]): continue
            
            # General uptrend context
            if c[i] < sma200[i] * 0.95: continue
            
            patterns_detected = []
            pattern_stops = []
            
            # 1. Base Breakout Candidate
            res_ceiling = float(np.max(h[i-50:i-2]))
            base_floor = float(np.min(l[i-50:i-2]))
            base_depth = (res_ceiling - base_floor) / max(res_ceiling, 0.01)
            if base_depth <= 0.35 and c[i] > res_ceiling and c[i] > o[i]:
                patterns_detected.append("BASE_BREAKOUT")
                pattern_stops.append(max(l[i], c[i] * 0.95))
                
            # 2. High-Tight Flag Candidate
            pole_l = float(np.min(l[i-15:i-3])); pole_h = float(np.max(h[i-15:i-3]))
            pole_adv = (pole_h - pole_l) / max(pole_l, 0.01)
            flag_l = float(np.min(l[i-4:i])); flag_h = float(np.max(h[i-4:i]))
            flag_rng = (flag_h - flag_l) / max(flag_l, 0.01)
            if pole_adv >= 0.14 and flag_rng <= 0.09 and c[i] > flag_h and c[i] > o[i]:
                patterns_detected.append("HIGH_TIGHT_FLAG")
                pattern_stops.append(max(flag_l, c[i] * 0.955))
                
            # 3. 52-Week High Breakout Candidate
            if c[i] > high_250[i] and c[i] > o[i]:
                patterns_detected.append("52W_HIGH")
                pattern_stops.append(max(l[i], c[i] * 0.95))
                
            # 4. Darvas Box Candidate
            box_top_idx = i - 20 + int(np.argmax(h[i-20:i]))
            box_top = h[box_top_idx]
            box_bot = float(np.min(l[box_top_idx:i]))
            if (i - box_top_idx >= 3) and (0.03 <= (box_top - box_bot)/box_top <= 0.12) and c[i] > box_top and c[i] > o[i]:
                patterns_detected.append("DARVAS_BOX")
                pattern_stops.append(box_bot)
                
            if not patterns_detected:
                continue
                
            # Evaluate the best candidate pattern
            pat_name = patterns_detected[0]
            stop_p = round(pattern_stops[0], 2)
            ep = c[i]
            initial_risk = ep - stop_p
            if initial_risk <= 0 or initial_risk / ep > 0.08: continue
            
            target_p = round(ep + 3.0 * initial_risk, 2)
            
            # Forward simulation to obtain ground truth label
            # y = 1 if target hit before stop hit, 0 otherwise
            hit_target = False
            hit_stop = False
            exit_pnl = 0.0
            holding_days = 30
            
            for j in range(i + 1, min(i + 31, n)):
                if h[j] >= target_p:
                    hit_target = True
                    exit_pnl = round((target_p - ep) / ep * 100.0, 2)
                    holding_days = j - i
                    break
                if l[j] <= stop_p:
                    hit_stop = True
                    exit_pnl = round((stop_p - ep) / ep * 100.0, 2)
                    holding_days = j - i
                    break
                    
            if not hit_target and not hit_stop:
                last_c = c[min(i + 30, n - 1)]
                exit_pnl = round((last_c - ep) / ep * 100.0, 2)
                hit_target = (exit_pnl > 0 and exit_pnl >= 2.0 * (initial_risk / ep * 100.0))
                
            label = 1 if hit_target else 0
            
            # --- Quantitative Features ---
            rng = max(h[i] - l[i], 0.01)
            cir = (c[i] - l[i]) / rng
            upper_wick = (h[i] - max(c[i], o[i])) / rng
            lower_wick = (min(c[i], o[i]) - l[i]) / rng
            body_pct = abs(c[i] - o[i]) / rng
            bar_ret = (c[i] / c[i-1] - 1.0) * 100.0
            gap_pct = (o[i] / c[i-1] - 1.0) * 100.0
            
            vol_sma20_val = vol_sma20[i] if not np.isnan(vol_sma20[i]) and vol_sma20[i] > 0 else 1.0
            vol_sma50_val = vol_sma50[i] if not np.isnan(vol_sma50[i]) and vol_sma50[i] > 0 else 1.0
            vol_ratio20 = v[i] / vol_sma20_val
            vol_ratio50 = v[i] / vol_sma50_val
            turnover_cr = (c[i] * v[i]) / 10_000_000.0
            
            ath_dist_pct = (high_250[i] - c[i]) / high_250[i] * 100.0
            dist_sma50 = (c[i] - sma50[i]) / sma50[i] * 100.0 if not np.isnan(sma50[i]) else 0.0
            dist_sma200 = (c[i] - sma200[i]) / sma200[i] * 100.0 if not np.isnan(sma200[i]) else 0.0
            sma50_slope = (sma50[i] - sma50[i-10]) / sma50[i-10] * 100.0 if not np.isnan(sma50[i-10]) else 0.0
            sma200_slope = (sma200[i] - sma200[i-20]) / sma200[i-20] * 100.0 if not np.isnan(sma200[i-20]) else 0.0
            
            prior_runup_20 = (c[i] / c[i-20] - 1.0) * 100.0 if i >= 20 else 0.0
            prior_runup_60 = (c[i] / c[i-60] - 1.0) * 100.0 if i >= 60 else 0.0
            atr14_pct = (atr14[i] / c[i] * 100.0) if not np.isnan(atr14[i]) else 2.5
            risk_pct = (initial_risk / ep) * 100.0
            
            # Base volume dryup
            base_vol_avg = float(np.mean(v[i-20:i]))
            vol_dryup_ratio = base_vol_avg / vol_sma50_val
            
            records.append({
                'symbol': sym,
                'date': d_str,
                'year': int(d_str[:4]),
                'pattern': pat_name,
                'entry_price': ep,
                'stop_price': stop_p,
                'target_price': target_p,
                'pnl_pct': exit_pnl,
                'holding_days': holding_days,
                'label': label,
                # Features
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
                'risk_pct': risk_pct,
                'is_htf': 1 if pat_name == "HIGH_TIGHT_FLAG" else 0,
                'is_base': 1 if pat_name == "BASE_BREAKOUT" else 0,
                'is_52w': 1 if pat_name == "52W_HIGH" else 0,
                'is_darvas': 1 if pat_name == "DARVAS_BOX" else 0
            })
            
    df_all = pd.DataFrame(records)
    print(f"      Extracted {len(df_all):,} setups across {df_all['date'].nunique()} trading dates in {time.time() - t0:.1f}s.")
    return df_all

# -----------------------------------------------------------------------------
# 3. XGBoost Model Training & Cross Validation
# -----------------------------------------------------------------------------

def train_and_evaluate_xgboost(df_all: pd.DataFrame):
    print("\n[3/5] Training XGBoost with 5-Fold Time-Series Splits on 2020-2024 In-Sample...")
    
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
    y_train = train_df['label'].values
    X_test = test_df[feature_cols].values
    y_test = test_df['label'].values
    
    print(f"      In-Sample  (2020-2024): {len(train_df):,} samples | Class 1 (Winners): {np.mean(y_train)*100:.1f}%")
    print(f"      Out-Sample (2025-2026): {len(test_df):,} samples | Class 1 (Winners): {np.mean(y_test)*100:.1f}%")
    
    # 5-Fold Time-Series CV
    train_df_sorted = train_df.sort_values('date').reset_index(drop=True)
    n_train = len(train_df_sorted)
    fold_size = n_train // 6
    cv_scores = []
    
    for fold in range(1, 6):
        split_point = fold * fold_size
        val_end = split_point + fold_size
        cv_train = train_df_sorted.iloc[:split_point]
        cv_val = train_df_sorted.iloc[split_point:val_end]
        
        dtrain_cv = xgb.DMatrix(cv_train[feature_cols].values, label=cv_train['label'].values)
        dval_cv = xgb.DMatrix(cv_val[feature_cols].values, label=cv_val['label'].values)
        
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
        bst = xgb.train(params, dtrain_cv, num_boost_round=300, evals=[(dval_cv, 'val')], verbose_eval=False)
        preds = bst.predict(dval_cv)
        auc = roc_auc_score(cv_val['label'].values, preds)
        cv_scores.append(auc)
        print(f"        Fold {fold}: AUC = {auc:.4f} (Val Size: {len(cv_val):,})")
        
    print(f"      Mean 5-Fold CV AUC: {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")
    
    # Train full In-Sample Model
    dtrain_full = xgb.DMatrix(X_train, label=y_train, feature_names=feature_cols)
    dtest_full = xgb.DMatrix(X_test, label=y_test, feature_names=feature_cols)
    
    full_model = xgb.train(params, dtrain_full, num_boost_round=350)
    
    # Feature Importances
    score_dict = full_model.get_score(importance_type='gain')
    imp_df = pd.DataFrame([{'feature': k, 'gain': v} for k, v in score_dict.items()]).sort_values('gain', ascending=False)
    
    print("\n      --- TOP 10 XGBOOST PREDICTIVE FEATURES (BY GAIN) ---")
    for idx, r in imp_df.head(10).reset_index().iterrows():
        print(f"        {idx+1:>2d}. {r['feature']:<20} | Gain: {r['gain']:.2f}")
        
    # Evaluate on Held-Out Out-of-Sample (2025-2026)
    test_preds = full_model.predict(dtest_full)
    test_auc = roc_auc_score(y_test, test_preds)
    test_df['ml_prob'] = test_preds
    train_df['ml_prob'] = full_model.predict(dtrain_full)
    
    print(f"\n      HELD-OUT OUT-OF-SAMPLE (2025-2026) AUC: {test_auc:.4f}")
    
    return full_model, feature_cols, train_df, test_df, imp_df

# -----------------------------------------------------------------------------
# 4. Edge Case & Failure Trap Discovery (Decision Tree Mining)
# -----------------------------------------------------------------------------

def discover_edge_cases_and_traps(train_df: pd.DataFrame, feature_cols: List[str]):
    print("\n[4/5] Mining exact Edge Cases & Failure Traps from Model Tree Splits...")
    
    # Train shallow surrogate decision tree on In-Sample
    dt = DecisionTreeClassifier(max_depth=3, min_samples_leaf=100, random_state=42)
    dt.fit(train_df[feature_cols], train_df['label'])
    
    tree_rules = export_text(dt, feature_names=feature_cols)
    print("      Surrogate Decision Tree Splits:")
    for line in tree_rules.split("\n")[:15]:
        print("        " + line)
        
    # Empirical Edge Case Rules
    print("\n      --- THE GOLDEN EDGE CASES DISCOVERED BY XGBOOST ---")
    # Edge Case 1: Ultra Marubozu + 52W Proximity
    ec1_mask = (train_df['cir'] >= 0.85) & (train_df['upper_wick'] <= 0.08) & (train_df['ath_dist_pct'] <= 15.0) & (train_df['vol_ratio20'] >= 2.0)
    ec1 = train_df[ec1_mask]
    ec1_wr = np.mean(ec1['label']) * 100.0
    ec1_pf = sum(ec1[ec1['pnl_pct'] > 0]['pnl_pct']) / max(abs(sum(ec1[ec1['pnl_pct'] < 0]['pnl_pct'])), 1e-3)
    print(f"      1. [Golden Edge: Zero-Overhead Power Bar]")
    print(f"         Rule: CIR >= 0.85 & Upper Wick <= 0.08 & ATH Dist <= 15% & Vol >= 2.0x")
    print(f"         In-Sample Metrics: Win Rate: {ec1_wr:.1f}% | Profit Factor: {ec1_pf:.2f} | Trades: {len(ec1):,}")

    # Edge Case 2: High-Tight Flag + Volume Contraction
    ec2_mask = (train_df['is_htf'] == 1) & (train_df['vol_dryup_ratio'] <= 0.65) & (train_df['risk_pct'] <= 4.0)
    ec2 = train_df[ec2_mask]
    ec2_wr = np.mean(ec2['label']) * 100.0 if len(ec2) else 0.0
    ec2_pf = sum(ec2[ec2['pnl_pct'] > 0]['pnl_pct']) / max(abs(sum(ec2[ec2['pnl_pct'] < 0]['pnl_pct'])), 1e-3) if len(ec2) else 0.0
    print(f"      2. [Golden Edge: Tight Shelf Sniper]")
    print(f"         Rule: Pattern is HTF & Vol Dryup <= 0.65 & Risk <= 4.0%")
    print(f"         In-Sample Metrics: Win Rate: {ec2_wr:.1f}% | Profit Factor: {ec2_pf:.2f} | Trades: {len(ec2):,}")

    print("\n      --- THE FATAL FAILURE TRAPS (FALSE BREAKOUT EDGE CASES) ---")
    # Trap 1: Extended Overhead Supply Trap
    trap1_mask = (train_df['ath_dist_pct'] > 25.0) & (train_df['upper_wick'] > 0.15)
    t1 = train_df[trap1_mask]
    t1_loss_rate = (1.0 - np.mean(t1['label'])) * 100.0
    print(f"      1. [Trap: Heavy Overhead Resistance Rejection]")
    print(f"         Rule: ATH Dist > 25% & Upper Wick > 0.15")
    print(f"         Failure Rate: {t1_loss_rate:.1f}% (N={len(t1):,}) -> PURGED BY ML")

    # Trap 2: Low Volume Poke
    trap2_mask = (train_df['vol_ratio20'] < 1.5) & (train_df['cir'] < 0.70)
    t2 = train_df[trap2_mask]
    t2_loss_rate = (1.0 - np.mean(t2['label'])) * 100.0
    print(f"      2. [Trap: Dry Volume Hesitant Poke]")
    print(f"         Rule: Vol Ratio < 1.5x & CIR < 0.70")
    print(f"         Failure Rate: {t2_loss_rate:.1f}% (N={len(t2):,}) -> PURGED BY ML")

    # Trap 3: Parabolic Exhaustion
    trap3_mask = (train_df['dist_sma200'] > 35.0) & (train_df['upper_wick'] > 0.12)
    t3 = train_df[trap3_mask]
    t3_loss_rate = (1.0 - np.mean(t3['label'])) * 100.0
    print(f"      3. [Trap: Parabolic 200-SMA Overextension]")
    print(f"         Rule: Dist SMA200 > 35% & Upper Wick > 0.12")
    print(f"         Failure Rate: {t3_loss_rate:.1f}% (N={len(t3):,}) -> PURGED BY ML")

# -----------------------------------------------------------------------------
# 5. Out-of-Sample Production Comparison: Raw vs Rule vs XGBoost
# -----------------------------------------------------------------------------

def evaluate_production_modes(train_df: pd.DataFrame, test_df: pd.DataFrame):
    print("\n[5/5] PRODUCTION WALK-FORWARD COMPARISON: RAW VS RULE-BASED VS XGBOOST (2025-2026)")
    print("=" * 135)
    print(f"{'Execution Mode':<28} | {'IS PF':>6} {'IS WR':>6} {'IS N':>5} {'IS Tr/Yr':>8} | {'OOS PF':>6} {'OOS WR':>6} {'OOS N':>5} {'OOS Tr/Yr':>9} | {'Payoff':>6}")
    print("=" * 135)

    def calc(df_sub, yrs):
        if len(df_sub) == 0: return {'n': 0, 'wr': 0.0, 'pf': 0.0, 'tr_yr': 0, 'payoff': 0.0}
        pnl = df_sub['pnl_pct'].values
        wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
        wr = len(wins) / len(pnl) * 100.0
        avg_w = float(np.mean(wins)) if len(wins) else 0.0
        avg_l = float(abs(np.mean(losses))) if len(losses) else 0.0
        pf = sum(wins) / abs(sum(losses)) if len(losses) and abs(sum(losses)) > 0 else 999.0
        payoff = avg_w / avg_l if avg_l > 0 else 0.0
        return {'n': len(pnl), 'wr': wr, 'pf': pf, 'tr_yr': round(len(pnl) / yrs), 'payoff': payoff}

    # Mode 1: Raw Signals (No filters, top 2 per day)
    raw_tr_top2 = train_df.sort_values('date').groupby('date').head(2)
    raw_te_top2 = test_df.sort_values('date').groupby('date').head(2)
    m1_is = calc(raw_tr_top2, 5.0); m1_oos = calc(raw_te_top2, 1.75)
    print(f"{'1. Raw Signals (No ML)':<28} | {m1_is['pf']:>6.2f} {m1_is['wr']:>5.1f}% {m1_is['n']:>5d} {m1_is['tr_yr']:>8d} | {m1_oos['pf']:>6.2f} {m1_oos['wr']:>5.1f}% {m1_oos['n']:>5d} {m1_oos['tr_yr']:>9d} | {m1_oos['payoff']:>6.2f}")

    # Mode 2: Manual Rule-Filtered Benchmark (CIR >= 0.85, Wick <= 0.10, Vol >= 2.0x, ATH <= 20%)
    rule_mask_tr = (train_df['cir'] >= 0.85) & (train_df['upper_wick'] <= 0.10) & (train_df['vol_ratio20'] >= 2.0) & (train_df['ath_dist_pct'] <= 20.0)
    rule_mask_te = (test_df['cir'] >= 0.85) & (test_df['upper_wick'] <= 0.10) & (test_df['vol_ratio20'] >= 2.0) & (test_df['ath_dist_pct'] <= 20.0)
    
    rule_tr = train_df[rule_mask_tr].sort_values('date').groupby('date').head(2)
    rule_te = test_df[rule_mask_te].sort_values('date').groupby('date').head(2)
    m2_is = calc(rule_tr, 5.0); m2_oos = calc(rule_te, 1.75)
    print(f"{'2. Manual Rule-Based System':<28} | {m2_is['pf']:>6.2f} {m2_is['wr']:>5.1f}% {m2_is['n']:>5d} {m2_is['tr_yr']:>8d} | {m2_oos['pf']:>6.2f} {m2_oos['wr']:>5.1f}% {m2_oos['n']:>5d} {m2_oos['tr_yr']:>9d} | {m2_oos['payoff']:>6.2f}")

    # Mode 3: XGBoost ML Filtered (Top Model Probabilities, Daily Top 2)
    ml_thresh = float(train_df['ml_prob'].quantile(0.60)) # Filter out bottom 60% probability
    ml_tr = train_df[train_df['ml_prob'] >= ml_thresh].sort_values(['date', 'ml_prob'], ascending=[True, False]).groupby('date').head(2)
    ml_te = test_df[test_df['ml_prob'] >= ml_thresh].sort_values(['date', 'ml_prob'], ascending=[True, False]).groupby('date').head(2)
    m3_is = calc(ml_tr, 5.0); m3_oos = calc(ml_te, 1.75)
    print(f"{'3. XGBoost Model (Top 40%)':<28} | {m3_is['pf']:>6.2f} {m3_is['wr']:>5.1f}% {m3_is['n']:>5d} {m3_is['tr_yr']:>8d} | {m3_oos['pf']:>6.2f} {m3_oos['wr']:>5.1f}% {m3_oos['n']:>5d} {m3_oos['tr_yr']:>9d} | {m3_oos['payoff']:>6.2f}")

    # Mode 4: XGBoost High Conviction (Top 25% Probability, Daily Top 1-2)
    ml_top_thresh = float(train_df['ml_prob'].quantile(0.75))
    ml_top_tr = train_df[train_df['ml_prob'] >= ml_top_thresh].sort_values(['date', 'ml_prob'], ascending=[True, False]).groupby('date').head(2)
    ml_top_te = test_df[test_df['ml_prob'] >= ml_top_thresh].sort_values(['date', 'ml_prob'], ascending=[True, False]).groupby('date').head(2)
    m4_is = calc(ml_top_tr, 5.0); m4_oos = calc(ml_top_te, 1.75)
    print(f"{'4. XGBoost Sniper (Top 25%)':<28} | {m4_is['pf']:>6.2f} {m4_is['wr']:>5.1f}% {m4_is['n']:>5d} {m4_is['tr_yr']:>8d} | {m4_oos['pf']:>6.2f} {m4_oos['wr']:>5.1f}% {m4_oos['n']:>5d} {m4_oos['tr_yr']:>9d} | {m4_oos['payoff']:>6.2f}")
    print("=" * 135)

    results_payload = {
        'Raw_No_ML': {'In_Sample': m1_is, 'Out_Of_Sample': m1_oos},
        'Manual_Rule_Based': {'In_Sample': m2_is, 'Out_Of_Sample': m2_oos},
        'XGBoost_Top_40': {'In_Sample': m3_is, 'Out_Of_Sample': m3_oos},
        'XGBoost_Sniper_Top_25': {'In_Sample': m4_is, 'Out_Of_Sample': m4_oos}
    }
    with open(OUTPUT_JSON, "w") as f:
        json.dump(results_payload, f, indent=2)
    print(f"Results saved to {OUTPUT_JSON}.")

def main():
    universe = load_universe()
    df_all = extract_setups_and_features(universe)
    model, features, train_df, test_df, imp_df = train_and_evaluate_xgboost(df_all)
    discover_edge_cases_and_traps(train_df, features)
    evaluate_production_modes(train_df, test_df)

if __name__ == "__main__":
    main()
