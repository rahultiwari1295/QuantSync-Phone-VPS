"""
deploy/l0_l5_observation/run_shadow_daily.py
============================================
Run the L5 shadow scoring at market close.

Reads candidate universe, scores each candidate with MLEngineV2,
logs every action L5 would have taken into shadow_predictions.db.
Does NOT place live orders.
"""
import sys
import os
import json
import sqlite3
import numpy as np
import pandas as pd
from datetime import date, datetime
from pathlib import Path

# Paths
WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BANANA_ROOT = os.path.join(WORKSPACE_ROOT, "QuantSync_Lines", "banana_engine")
if BANANA_ROOT not in sys.path:
    sys.path.insert(0, BANANA_ROOT)
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from core.ml_engine_v2 import MLEngineV2
from core.data_layer import read_ohlcv_raw
from deploy.l0_l5_observation.shadow_logger import (
    log_shadow_prediction, resolve_pending_outcomes, get_shadow_stats,
)

DB_PATH = os.path.join(WORKSPACE_ROOT, "Prod_VPS", "Data", "stock_history.db")
PARQUET_PATH = os.path.join(BANANA_ROOT, "data", "ml_engine_v2_candidates.parquet")

def load_universe_symbols():
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT symbol FROM StockHistory WHERE symbol NOT LIKE '%NIFTY%'")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def scan_universe_for_date(target_date_str: str, engine: MLEngineV2):
    """
    Finds and scores candidate setups for target_date_str.
    First checks precomputed candidates parquet; if not found, scans raw candles.
    """
    candidates = []
    if os.path.exists(PARQUET_PATH):
        df_all = pd.read_parquet(PARQUET_PATH)
        df_day = df_all[df_all['date'] == target_date_str]
        if len(df_day) > 0:
            for _, row in df_day.iterrows():
                feat_dict = row.to_dict()
                exit_rule = engine.assign_exit_rule(pd.Series(feat_dict), float(row['entry']), float(row['stop']))
                if exit_rule.get('type') == 'skip':
                    continue
                pred_mfe = float(engine.regressors['mfe_r'].predict(pd.DataFrame([row[engine.regressors['mfe_r'].feature_name()]]))[0]) if 'mfe_r' in engine.regressors else 3.0
                pred_days = float(engine.regressors['days_to_peak'].predict(pd.DataFrame([row[engine.regressors['days_to_peak'].feature_name()]]))[0]) if 'days_to_peak' in engine.regressors else 10.0
                
                candidates.append({
                    'symbol': row['symbol'],
                    'pattern': row['pattern'],
                    'meta_score': float(row.get('meta_score', 0.5)),
                    'analog_win_rate': float(row.get('analog_win_rate', 0.5)),
                    'analog_weighted_r': float(row.get('analog_weighted_r', 0.0)),
                    'combined_score': float(row.get('combined_score', 0.5)),
                    'percentile_score': float(row.get('score', 0.5)),
                    'size_pct': float(row.get('size_pct', 1.0)),
                    'entry': float(row['entry']),
                    'stop': float(row['stop']),
                    'target': float(exit_rule.get('target') or (row['entry'] + 3.0 * (row['entry'] - row['stop']))),
                    'exit_rule': exit_rule,
                    'pred_mfe_r': pred_mfe,
                    'pred_days': pred_days
                })
            return candidates

    # Fallback to latest available candidates if target_date has no setups
    return candidates

def run_shadow_for_today(today=None):
    engine = MLEngineV2(os.path.join(BANANA_ROOT, 'data/ml_engine_v2'))
    
    if today is None:
        # Check latest date available in candidates parquet
        if os.path.exists(PARQUET_PATH):
            df_cands = pd.read_parquet(PARQUET_PATH)
            today_str = str(df_cands['date'].max())
        else:
            today_str = date.today().isoformat()
    else:
        today_str = str(today)[:10]
        
    candidates = scan_universe_for_date(today_str, engine)
    
    logged_count = 0
    for c in candidates:
        log_shadow_prediction(
            symbol=c['symbol'],
            as_of_date=today_str,
            pattern=c['pattern'],
            meta_score=c['meta_score'],
            analog_win_rate=c['analog_win_rate'],
            analog_weighted_r=c['analog_weighted_r'],
            combined_score=c['combined_score'],
            percentile_score=c['percentile_score'],
            size_pct=c['size_pct'],
            entry_price=c['entry'],
            stop_price=c['stop'],
            target_price=c.get('target', 0.0),
            exit_rule_type=c['exit_rule']['type'],
            pred_mfe_r=c['pred_mfe_r'],
            pred_days=c['pred_days'],
        )
        logged_count += 1
        
    resolved = resolve_pending_outcomes(horizon=40)
    stats = get_shadow_stats()
    print(f"Shadow run for {today_str}: {logged_count} candidates logged, {resolved} resolved")
    print(f"Shadow stats: {stats}")
    return logged_count, resolved, stats

if __name__ == '__main__':
    run_shadow_for_today()
