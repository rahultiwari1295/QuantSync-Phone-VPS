#!/usr/bin/env python3
"""
scripts/run_ml_engine_v2_integration.py — Full Integrated ML Engine v2 Pipeline
================================================================================
Executes the end-to-end backtest on the held-out 2025-2026 Out-of-Sample universe:
1. Implements point-in-time Market Breadth (50 DMA) and Sector Concentration guards.
2. Implements Adaptive Daily Thresholds & Rolling 30-Trade Win Rate Feedback.
3. Implements Multi-Timeframe Alignment hard filter (mtf_alignment >= 3).
4. Evaluates Layer-by-Layer progression (L0 Baseline, L1, L2, L3, L4, L5).
5. Runs SHAP Explainability Audit on top 50 trades.
6. Saves all sidecar JSONs in data/ml_engine_v2/.
7. Ensures seamless symlinks between banana_engine/data/ml_engine_v2 and data/ml_engine_v2.
"""

import sys
import os
import time
import json
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
import shap
from typing import Dict, List, Any, Tuple

_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_PATH = os.path.join(_PKG_ROOT, "data/ml_engine_v2_candidates.parquet")
OUT_DIR = os.path.join(_PKG_ROOT, "data/ml_engine_v2")
DB_PATH = os.path.abspath("Prod_VPS/Data/stock_history.db")
SECTOR_DB_PATH = os.path.join(_PKG_ROOT, "data/sector_map.db")

def load_sector_map():
    if not os.path.exists(SECTOR_DB_PATH):
        return {}
    conn = sqlite3.connect(SECTOR_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol, sector FROM sector_history")
    rows = c.fetchall()
    conn.close()
    return {sym: sec for sym, sec in rows}

def compute_daily_breadth_map(universe_candles):
    """Computes PIT % of stocks above SMA50 for every trading date."""
    print("Computing daily market breadth (% of stocks > SMA50)...")
    date_above_50 = {}
    date_total = {}
    
    for sym, raw_list in universe_candles.items():
        if len(raw_list) < 60: continue
        closes = np.array([float(x['c']) for x in raw_list])
        dates = [str(x['t'])[:10] for x in raw_list]
        sma50 = pd.Series(closes).rolling(50, min_periods=20).mean().values
        
        for d, c, s in zip(dates, closes, sma50):
            if d < "2020-01-01" or np.isnan(s): continue
            if d not in date_total:
                date_total[d] = 0
                date_above_50[d] = 0
            date_total[d] += 1
            if c > s:
                date_above_50[d] += 1
                
    breadth_map = {}
    for d, tot in date_total.items():
        breadth_map[d] = (date_above_50[d] / tot * 100.0) if tot > 0 else 50.0
    return breadth_map

def regime_effect(breadth_50dma: float) -> Dict[str, Any]:
    if breadth_50dma > 65.0:
        return {"max_takes": 4, "threshold_delta": -0.05}
    elif breadth_50dma > 45.0:
        return {"max_takes": 3, "threshold_delta": 0.0}
    elif breadth_50dma > 30.0:
        return {"max_takes": 2, "threshold_delta": 0.05}
    else:
        return {"max_takes": 1, "threshold_delta": 0.10}

def rolling_adjustment(recent_win_rate: float, base_threshold: float = 0.55) -> float:
    if recent_win_rate < 0.40:
        adj = base_threshold + 0.05
    elif recent_win_rate > 0.60:
        adj = base_threshold - 0.03
    else:
        adj = base_threshold
    return float(np.clip(adj, 0.40, 0.70))

def compute_size(score: float, analog_std: float) -> float:
    if score >= 0.75: base = 1.25
    elif score >= 0.65: base = 1.00
    elif score >= 0.55: base = 0.75
    else: base = 0.50
    
    if analog_std < 1.5: multiplier = 1.20
    elif analog_std < 2.5: multiplier = 1.00
    else: multiplier = 0.70
    
    size = base * multiplier
    return float(np.clip(size, 0.25, 1.50))

def main():
    print("=========================================================================================")
    print("THE COMPLETE ML ENGINE V2 — FULL INTEGRATION & VALIDATION PROTOCOL (2025-2026)")
    print("=========================================================================================")
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    
    df = pd.read_parquet(DATA_PATH)
    print(f"Loaded candidate dataset: {len(df):,} total candidates.")
    
    # Load candle database
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
            
    breadth_map = compute_daily_breadth_map(candles_by_sym)
    sector_map = load_sector_map()
    
    # Load Models
    meta_model = lgb.Booster(model_file=os.path.join(OUT_DIR, "meta_model.lgb"))
    reg_mfe = lgb.Booster(model_file=os.path.join(OUT_DIR, "regressor_mfe_r.lgb"))
    reg_days = lgb.Booster(model_file=os.path.join(OUT_DIR, "regressor_days_to_peak.lgb"))
    
    # Feature columns used by Meta Model
    meta_features = meta_model.feature_name()
    for col in meta_features:
        if col not in df.columns:
            df[col] = 0.0
            
    # Compute Meta Scores across all candidates
    df['meta_score'] = meta_model.predict(df[meta_features])
    df['pred_mfe_r'] = reg_mfe.predict(df[reg_mfe.feature_name()])
    df['pred_days'] = reg_days.predict(df[reg_days.feature_name()])
    
    # Combined Score
    # combined_score = 0.50 * meta_score + 0.30 * analog_win_rate + 0.20 * (analog_weighted_r / 3.0)
    norm_analog_r = np.clip(df['analog_weighted_r'].values / 3.0, 0.0, 1.0)
    df['combined_score'] = (
        0.50 * df['meta_score'] +
        0.30 * df['analog_win_rate'] +
        0.20 * norm_analog_r
    )
    
    # Map combined_score to calibrated percentile score [0.0, 1.0] based on In-Sample distribution
    in_sample_raw = np.sort(df.loc[df['year'] <= 2024, 'combined_score'].values)
    df['score'] = np.searchsorted(in_sample_raw, df['combined_score'].values) / len(in_sample_raw)
    
    # Sizing
    sizes = [compute_size(s, std) for s, std in zip(df['score'], df['analog_std_r'])]
    df['size_pct'] = sizes
    
    # Split
    train_df = df[df['year'] <= 2024].copy()
    test_df = df[df['year'] >= 2025].copy()
    print(f"Test Set (2025-2026 Out-of-Sample): {len(test_df):,} candidates across {test_df['date'].nunique()} trading dates.")
    
    # -------------------------------------------------------------------------------------
    # VALIDATION PROTOCOL: LAYER-BY-LAYER PROGRESSION (2025-2026 OOS)
    # -------------------------------------------------------------------------------------
    
    def simulate_portfolio(candidates_sub, score_col='score', use_ml_filter=True, use_adaptive_selection=False, use_dynamic_exits=False, use_adaptive_sizing=False):
        """Simulates trading portfolio with daily resolution, max takes, and exit rules."""
        sorted_cands = candidates_sub.sort_values(['date', score_col], ascending=[True, False])
        trades = []
        recent_30_outcomes = []
        
        dates = sorted(sorted_cands['date'].unique())
        
        for d in dates:
            day_cands = sorted_cands[sorted_cands['date'] == d]
            if not len(day_cands):
                continue
                
            b_val = breadth_map.get(d, 50.0)
            reg = regime_effect(b_val)
            thresh_delta = reg['threshold_delta']
            
            # Rolling win rate adjustment
            if len(recent_30_outcomes) >= 15:
                recent_wr = np.mean(recent_30_outcomes[-30:])
                adj_thresh = rolling_adjustment(recent_wr, 0.50 + thresh_delta)
            else:
                adj_thresh = 0.50 + thresh_delta
                
            if not use_ml_filter:
                # Baseline: max 2 per day without score threshold
                max_takes = 2
                filtered_day = day_cands
            elif use_adaptive_selection:
                n_c = len(day_cands)
                if n_c >= 20:
                    cutoff = float(np.quantile(day_cands[score_col], 0.85))
                    day_cap = 4
                elif n_c >= 10:
                    cutoff = float(np.quantile(day_cands[score_col], 0.70))
                    day_cap = 3
                elif n_c >= 4:
                    cutoff = float(np.quantile(day_cands[score_col], 0.50))
                    day_cap = 2
                else:
                    cutoff = float(np.quantile(day_cands[score_col], 0.25))
                    day_cap = 1
                max_takes = min(reg['max_takes'], day_cap)
                filtered_day = day_cands[(day_cands[score_col] >= cutoff) & (day_cands[score_col] >= adj_thresh)]
            else:
                max_takes = 2
                filtered_day = day_cands[day_cands[score_col] >= 0.50]
                
            # Sector concentration limit (max 3/sector)
            sector_counts = {}
            day_selected = []
            for _, row in filtered_day.iterrows():
                sec = sector_map.get(row['symbol'], "Unknown")
                if sector_counts.get(sec, 0) >= 3:
                    continue
                sector_counts[sec] = sector_counts.get(sec, 0) + 1
                day_selected.append(row)
                if len(day_selected) >= max_takes:
                    break
                    
            for row in day_selected:
                sym = row['symbol']
                sig_idx = int(row['signal_idx'])
                ep = float(row['entry'])
                sp = float(row['stop'])
                r_dist = ep - sp
                raw_c = candles_by_sym.get(sym)
                if raw_c is None or sig_idx + 1 >= len(raw_c):
                    continue
                future = raw_c[sig_idx + 1 : min(sig_idx + 41, len(raw_c))]
                if not len(future):
                    continue
                    
                risk_alloc = (float(row['size_pct']) / 100.0) if use_adaptive_sizing else 0.01
                
                # Exit Simulation
                realized_r = 0.0
                holding_bars = len(future)
                
                if not use_dynamic_exits:
                    # Fixed 3R exit
                    for bi, bar in enumerate(future):
                        hb = float(bar['h']); lb = float(bar['l'])
                        if lb <= sp:
                            realized_r = (sp - ep) / r_dist
                            holding_bars = bi + 1
                            break
                        if hb >= ep + 3.0 * r_dist:
                            realized_r = 3.0
                            holding_bars = bi + 1
                            break
                    if realized_r == 0.0:
                        realized_r = (float(future[-1]['c']) - ep) / r_dist
                else:
                    # Dynamic Exit Regressor assignment
                    pred_mfe = float(row['pred_mfe_r'])
                    pred_days = float(row['pred_days'])
                    
                    if pred_mfe >= 4.0:
                        rule = "runner"
                        be_trig = 1.5 * r_dist
                        trail_trig = 2.5 * r_dist
                        time_stop = int(pred_days * 1.5)
                        target = None
                    elif pred_mfe >= 2.5:
                        rule = "standard"
                        be_trig = 1.0 * r_dist
                        target = ep + 0.7 * pred_mfe * r_dist
                        time_stop = int(pred_days * 1.3)
                    elif pred_mfe >= 1.8:
                        rule = "tight"
                        be_trig = 0.8 * r_dist
                        target = ep + 1.5 * r_dist
                        time_stop = int(pred_days)
                    else:
                        continue # skip trade
                        
                    dyn_stop = sp
                    be_locked = False
                    trail_active = False
                    max_b = min(len(future), max(time_stop, 5))
                    
                    for bi in range(max_b):
                        bar = future[bi]
                        hb = float(bar['h']); lb = float(bar['l']); cb = float(bar['c'])
                        if lb <= dyn_stop:
                            realized_r = (dyn_stop - ep) / r_dist
                            holding_bars = bi + 1
                            break
                        if target is not None and hb >= target:
                            realized_r = (target - ep) / r_dist
                            holding_bars = bi + 1
                            break
                        if not be_locked and hb >= ep + be_trig:
                            dyn_stop = max(dyn_stop, ep * 1.002)
                            be_locked = True
                        if rule == "runner":
                            if hb >= ep + trail_trig: trail_active = True
                            if trail_active:
                                past_sub = future[max(0, bi-3):bi]
                                if len(past_sub):
                                    dyn_stop = max(dyn_stop, min(float(b['l']) for b in past_sub))
                        if bi == max_b - 1:
                            realized_r = (cb - ep) / r_dist
                            holding_bars = bi + 1
                            break
                            
                pnl_r_net = round(realized_r, 2)
                pnl_dollars_r = pnl_r_net * (risk_alloc / 0.01) # scaled R
                
                recent_30_outcomes.append(1 if pnl_r_net > 0 else 0)
                trades.append({
                    'date': d,
                    'symbol': sym,
                    'pnl_r': pnl_r_net,
                    'scaled_r': pnl_dollars_r,
                    'risk_pct': risk_alloc * 100.0,
                    'holding_bars': holding_bars
                })
                
        if not trades:
            return {'trades_yr': 0, 'wr': 0, 'ann_r': 0, 'pf': 0, 'max_dd_r': 0, 'sharpe': 0}
            
        tdf = pd.DataFrame(trades)
        n = len(tdf)
        trades_yr = round(n / 1.75)
        wr = (tdf['pnl_r'] > 0).mean() * 100.0
        tot_r = tdf['scaled_r'].sum()
        ann_r = round(tot_r / 1.75, 1)
        
        gw = tdf[tdf['scaled_r'] > 0]['scaled_r'].sum()
        gl = abs(tdf[tdf['scaled_r'] < 0]['scaled_r'].sum())
        pf = round(gw / gl, 2) if gl > 0 else 999.0
        
        # Max DD in R
        cum_r = tdf['scaled_r'].cumsum()
        running_max = np.maximum.accumulate(cum_r)
        dd = running_max - cum_r
        max_dd_r = round(float(np.max(dd)), 1)
        
        # Daily Sharpe of R returns
        daily_r = tdf.groupby('date')['scaled_r'].sum()
        sharpe = round(float(daily_r.mean() / (daily_r.std() + 1e-9) * np.sqrt(252)), 2)
        
        return {
            'trades_yr': trades_yr,
            'wr': round(wr, 1),
            'ann_r': ann_r,
            'pf': pf,
            'max_dd_r': max_dd_r,
            'sharpe': sharpe,
            'avg_hold': round(float(tdf['holding_bars'].mean()), 1)
        }

    # 1. Run L0: Baseline Strict Rules (Static Filters, Flat 1.0% Risk)
    print("\n[Layer Progression Benchmark on 2025-2026 Holdout]")
    l0_mask = (test_df['cir'] >= 0.85) & (test_df['upper_wick_pct'] <= 0.10) & (test_df['vol_ratio_20'] >= 2.0)
    l0_res = simulate_portfolio(test_df[l0_mask], use_ml_filter=False, use_adaptive_selection=False, use_dynamic_exits=False, use_adaptive_sizing=False)
    print(f"  L0 Baseline Strict Rules : {l0_res['trades_yr']} trades/yr | WR: {l0_res['wr']}% | Ann R: {l0_res['ann_r']:+.1f}R | PF: {l0_res['pf']} | MaxDD: {l0_res['max_dd_r']}R | Sharpe: {l0_res['sharpe']}")

    # 2. Run L1: Pattern Models + Meta Model (Flat 1.0% Risk)
    l1_res = simulate_portfolio(test_df, score_col='meta_score', use_ml_filter=True, use_adaptive_selection=False, use_dynamic_exits=False, use_adaptive_sizing=False)
    print(f"  L1 Pattern Models + Meta : {l1_res['trades_yr']} trades/yr | WR: {l1_res['wr']}% | Ann R: {l1_res['ann_r']:+.1f}R | PF: {l1_res['pf']} | MaxDD: {l1_res['max_dd_r']}R | Sharpe: {l1_res['sharpe']}")

    # 3. Run L2: + MTF Alignment Filter (mtf_alignment >= 3)
    l2_cand = test_df[test_df['mtf_alignment'] >= 3]
    l2_res = simulate_portfolio(l2_cand, score_col='meta_score', use_ml_filter=True, use_adaptive_selection=False, use_dynamic_exits=False, use_adaptive_sizing=False)
    print(f"  L2 + MTF Alignment Filter: {l2_res['trades_yr']} trades/yr | WR: {l2_res['wr']}% | Ann R: {l2_res['ann_r']:+.1f}R | PF: {l2_res['pf']} | MaxDD: {l2_res['max_dd_r']}R | Sharpe: {l2_res['sharpe']}")

    # 4. Run L3: + Adaptive Thresholds & Regime Conditioning
    l3_res = simulate_portfolio(test_df[test_df['mtf_alignment'] >= 3], score_col='score', use_ml_filter=True, use_adaptive_selection=True, use_dynamic_exits=False, use_adaptive_sizing=False)
    print(f"  L3 + Adaptive Thresholds : {l3_res['trades_yr']} trades/yr | WR: {l3_res['wr']}% | Ann R: {l3_res['ann_r']:+.1f}R | PF: {l3_res['pf']} | MaxDD: {l3_res['max_dd_r']}R | Sharpe: {l3_res['sharpe']}")

    # 5. Run L4: + Analog Features & Adaptive Sizing (0.25% - 1.50%)
    l4_res = simulate_portfolio(test_df[test_df['mtf_alignment'] >= 3], score_col='score', use_ml_filter=True, use_adaptive_selection=True, use_dynamic_exits=False, use_adaptive_sizing=True)
    print(f"  L4 + Analog Sizing       : {l4_res['trades_yr']} trades/yr | WR: {l4_res['wr']}% | Ann R: {l4_res['ann_r']:+.1f}R | PF: {l4_res['pf']} | MaxDD: {l4_res['max_dd_r']}R | Sharpe: {l4_res['sharpe']}")

    # 6. Run L5: + Dynamic Exits (Full Integrated Engine)
    l5_res = simulate_portfolio(test_df[test_df['mtf_alignment'] >= 3], score_col='score', use_ml_filter=True, use_adaptive_selection=True, use_dynamic_exits=True, use_adaptive_sizing=True)
    print(f"  L5 + Dynamic Exits (FULL): {l5_res['trades_yr']} trades/yr | WR: {l5_res['wr']}% | Ann R: {l5_res['ann_r']:+.1f}R | PF: {l5_res['pf']} | MaxDD: {l5_res['max_dd_r']}R | Sharpe: {l5_res['sharpe']}")

    # SHAP EXPLAINABILITY AUDIT FOR TOP 50 TRADES
    print("\nGenerating SHAP explanations for top 50 trades...")
    explainer = shap.TreeExplainer(meta_model)
    top_50 = test_df[test_df['mtf_alignment'] >= 3].sort_values('combined_score', ascending=False).head(50)
    
    trade_explanations = []
    for idx, row in top_50.iterrows():
        r_df = pd.DataFrame([row[meta_features]])
        sh_vals = explainer.shap_values(r_df)
        vals = sh_vals[1][0] if isinstance(sh_vals, list) else sh_vals[0]
        top_3_idx = np.argsort(-np.abs(vals))[:3]
        
        reasons = []
        for ti in top_3_idx:
            f_name = meta_features[ti]
            v = vals[ti]
            f_val = row[f_name]
            reasons.append({
                "feature": f_name,
                "value": round(float(f_val), 2),
                "shap_impact": round(float(v), 4),
                "effect": "Bullish Catalyst" if v > 0 else "Bearish Drag"
            })
            
        trade_explanations.append({
            "symbol": row['symbol'],
            "date": row['date'],
            "pattern": row['pattern'],
            "combined_score": round(float(row['combined_score']), 4),
            "size_pct": round(float(row['size_pct']), 2),
            "top_3_shap_reasons": reasons
        })
        
    # Save Trade Explanations
    exp_json = os.path.join(OUT_DIR, "trade_explanations.json")
    with open(exp_json, "w") as f:
        json.dump(trade_explanations, f, indent=2)
    print(f"Saved SHAP explanations for {len(trade_explanations)} trades to {exp_json}.")
    
    # Save Layer 3 Metrics
    l3_json = os.path.join(OUT_DIR, "layer3_metrics.json")
    with open(l3_json, "w") as f:
        json.dump({
            "description": "Layer 3: Cross-Sectional Ranking, Regime Modulation, Rolling Feedback",
            "benchmark_results": l3_res,
            "annual_r_lift_vs_static": round(l3_res['ann_r'] - l2_res['ann_r'], 1),
            "acceptance_criteria": {
                "annual_r_improves": bool(l3_res['ann_r'] > l2_res['ann_r']),
                "max_drawdown_stable": bool(l3_res['max_dd_r'] <= 35.0)
            }
        }, f, indent=2)
    print(f"Saved Layer 3 metrics to {l3_json}.")

    # Save Feature List
    feat_json = os.path.join(OUT_DIR, "feature_list.json")
    with open(feat_json, "w") as f:
        json.dump({
            "meta_features": meta_features,
            "feature_count": len(meta_features)
        }, f, indent=2)
        
    # Save Integration Metrics
    integ_metrics = {
        'L0_Baseline_Strict': l0_res,
        'L1_Pattern_Meta': l1_res,
        'L2_MTF_Weekly': l2_res,
        'L3_Adaptive_Thresholds': l3_res,
        'L4_Analog_Sizing': l4_res,
        'L5_Dynamic_Exits_Full': l5_res
    }
    int_json = os.path.join(OUT_DIR, "integration_metrics.json")
    with open(int_json, "w") as f:
        json.dump(integ_metrics, f, indent=2)
    print(f"Saved integration metrics to {int_json}.")
    
    # Save Model Card
    model_card = {
        "model_engine": "QuantSync ML Engine v2",
        "version": "2.0.0",
        "training_window": "2020-01-01 to 2024-12-31",
        "evaluation_window": "2025-01-01 to 2026-09-12",
        "architecture": {
            "layer_1": "4 LightGBM Pattern Classifiers + Meta-Classifier",
            "layer_2": "8 Multi-Timeframe Weekly Features + MTF Alignment Filter",
            "layer_3": "Daily Cross-Sectional Ranking + Regime Adjustment + Rolling WR Feedback",
            "layer_4": "32-Point Shape Embeddings + Top-20 Cosine Similarity Analog Library",
            "layer_5": "3 LightGBM Forward Path Regressors (MFE, MAE, Days to Peak)"
        },
        "performance_summary": {
            "annual_r_multiple": l5_res['ann_r'],
            "profit_factor": l5_res['pf'],
            "win_rate": l5_res['wr'],
            "max_drawdown_r": l5_res['max_dd_r'],
            "sharpe_ratio": l5_res['sharpe'],
            "trades_per_year": l5_res['trades_yr']
        }
    }
    card_json = os.path.join(OUT_DIR, "model_card.json")
    with open(card_json, "w") as f:
        json.dump(model_card, f, indent=2)
    print(f"Saved model card to {card_json}.")
    
    # Ensure Symlink from workspace root data/ml_engine_v2 -> QuantSync_Lines/banana_engine/data/ml_engine_v2
    root_data_link = os.path.abspath("data/ml_engine_v2")
    if not os.path.exists("data"):
        os.makedirs("data", exist_ok=True)
    if not os.path.exists(root_data_link):
        try:
            os.symlink(OUT_DIR, root_data_link)
            print(f"Created symlink: {root_data_link} -> {OUT_DIR}")
        except Exception as e:
            print(f"Symlink creation notice: {e}")
            
    print(f"\nAll operations completed successfully in {time.time() - t0:.2f}s.")

if __name__ == "__main__":
    main()
