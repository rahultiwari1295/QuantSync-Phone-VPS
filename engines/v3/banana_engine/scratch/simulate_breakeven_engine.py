#!/usr/bin/env python3
"""
scratch/simulate_breakeven_engine.py
===================================
Tests real bar-by-bar trade paths for Top 10% ML Candidates:
Rules:
  - Stop at Pattern Low (-1.0R)
  - At +1.0R MFE -> Move Stop to Breakeven (+0.1R to cover fees)
  - Target: 1.8R or 2.0R
  - Time stop: 20 bars
"""
import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb

# Auto-locate repo root
possible_roots = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")),
    os.path.abspath("."),
    os.path.abspath("../..")
]
REPO_ROOT = None
for r in possible_roots:
    if os.path.exists(os.path.join(r, "QuantSync_Lines/banana_engine/data/ml_engine_v2_candidates.parquet")):
        REPO_ROOT = r
        break

if not REPO_ROOT:
    raise RuntimeError("Cannot locate REPO_ROOT")

DATA_PATH = os.path.join(REPO_ROOT, "QuantSync_Lines/banana_engine/data/ml_engine_v2_candidates.parquet")
DB_PATH = os.path.join(REPO_ROOT, "Prod_VPS/Data/stock_history.db")
FEATURE_PATH = os.path.join(REPO_ROOT, "QuantSync_Lines/banana_engine/data/target_200/feature_list.json")

df = pd.read_parquet(DATA_PATH)
df['year'] = df['date'].str[:4].astype(int)

train = df[df['year'] <= 2024].copy().reset_index(drop=True)
test = df[df['year'] >= 2025].copy().reset_index(drop=True)

with open(FEATURE_PATH) as f:
    feats = json.load(f)

# Train regularized LightGBM on In-Sample
clf = lgb.LGBMClassifier(
    n_estimators=100, learning_rate=0.03, num_leaves=15, min_child_samples=50,
    subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1
)
clf.fit(train[feats], train['label'])

train['score'] = clf.predict_proba(train[feats])[:, 1]
test['score'] = clf.predict_proba(test[feats])[:, 1]

# Honest Un-Leaked Gate: Top 10% cutoff calculated strictly on In-Sample (2020-2024)
cutoff = train['score'].quantile(0.90)   # 2020-2024 threshold (0.49059)
sub_train = train[train['score'] >= cutoff].copy().reset_index(drop=True)
sub_test = test[test['score'] >= cutoff].copy().reset_index(drop=True)  # applied blindly to OOS

print(f"Candidates selected: In-Sample N={len(sub_train)} ({len(sub_train)/5:.0f}/yr), Out-of-Sample N={len(sub_test)} ({len(sub_test)/1.52:.0f}/yr)")

# Load candle universe for true bar-by-bar execution
print("Loading candles from StockHistory DB...")
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("SELECT symbol, rawCandles FROM StockHistory")
rows = c.fetchall()
conn.close()

candles = {}
for sym, raw in rows:
    try:
        raw_list = json.loads(raw)
        if len(raw_list) < 50: continue
        cdf = pd.DataFrame(raw_list)
        candles[sym] = {
            'c': cdf['c'].astype(float).values,
            'h': cdf['h'].astype(float).values,
            'l': cdf['l'].astype(float).values,
            'o': cdf['o'].astype(float).values,
            'd': cdf['t'].astype(str).str[:10].values,
            'n': len(cdf)
        }
    except Exception:
        pass

def simulate_bar_by_bar(candidates: pd.DataFrame, target_r: float, be_trigger_r: float, max_bars: int = 25):
    results = []
    
    for idx, row in candidates.iterrows():
        sym = row['symbol']
        sig_date = row['date']
        ep = float(row['entry'])
        sp = float(row['stop'])
        r_dist = ep - sp
        if r_dist <= 0.001 or sym not in candles:
            continue
            
        cdata = candles[sym]
        dates = cdata['d']
        matches = np.where(dates == sig_date)[0]
        if len(matches) == 0:
            continue
        i = matches[0]
        n = cdata['n']
        
        cur_stop = sp
        hit_be = False
        outcome = 'HOLD'
        realized_r = 0.0
        
        target_price = ep + target_r * r_dist
        be_price = ep + be_trigger_r * r_dist
        
        for bar in range(i + 1, min(i + 1 + max_bars, n)):
            hb = cdata['h'][bar]
            lb = cdata['l'][bar]
            cb = cdata['c'][bar]
            
            # Check target first if gap up, or intraday
            if hb >= target_price:
                realized_r = target_r
                outcome = 'TARGET'
                break
                
            # Check stop
            if lb <= cur_stop:
                realized_r = (cur_stop - ep) / r_dist
                outcome = 'STOP' if not hit_be else 'SCRATCH'
                break
                
            # Check breakeven trigger
            if not hit_be and hb >= be_price:
                hit_be = True
                cur_stop = ep + 0.05 * r_dist # lock +0.05R to cover slippage/STT
                
            # Time exit
            if bar == min(i + 1 + max_bars, n) - 1:
                realized_r = (cb - ep) / r_dist
                outcome = 'TIME'
                break
                
        results.append({
            'symbol': sym,
            'date': sig_date,
            'year': int(sig_date[:4]),
            'realized_r': realized_r,
            'outcome': outcome,
            'is_win': realized_r > 0.05
        })
        
    return pd.DataFrame(results)

print("\n--- SIMULATION RESULTS WITH BREAKEVEN LOCK (+1.0R LOCK -> TARGET 1.8R or 2.0R) ---")

for be_val in [0.8, 1.0]:
  for tgt in [1.8, 2.0]:
    sim_tr = simulate_bar_by_bar(sub_train, target_r=tgt, be_trigger_r=be_val)
    sim_te = simulate_bar_by_bar(sub_test, target_r=tgt, be_trigger_r=be_val)
    
    # In-Sample Metrics
    wr_tr = sim_tr['is_win'].mean() * 100.0
    wins_tr = sim_tr[sim_tr['realized_r'] > 0]['realized_r'].sum()
    loss_tr = abs(sim_tr[sim_tr['realized_r'] < 0]['realized_r'].sum())
    pf_tr = wins_tr / loss_tr if loss_tr > 0 else 999.0
    ann_r_tr = sim_tr['realized_r'].sum() / 5.0
    
    # Out-of-Sample Metrics
    wr_te = sim_te['is_win'].mean() * 100.0
    wins_te = sim_te[sim_te['realized_r'] > 0]['realized_r'].sum()
    loss_te = abs(sim_te[sim_te['realized_r'] < 0]['realized_r'].sum())
    pf_te = wins_te / loss_te if loss_te > 0 else 999.0
    ann_r_te = sim_te['realized_r'].sum() / 1.52
    
    print(f"\n[Target = {tgt:.1f}R with Breakeven Lock at +{be_val:.1f}R]:")
    print(f"  IN-SAMPLE  (2020-2024): N={len(sim_tr):4d} ({len(sim_tr)/5:.0f}/yr) | WR: {wr_tr:5.1f}% | PF: {pf_tr:4.2f} | Ann R: +{ann_r_tr:5.1f}R")
    print(f"  OUT-SAMPLE (2025-2026): N={len(sim_te):4d} ({len(sim_te)/1.52:.0f}/yr) | WR: {wr_te:5.1f}% | PF: {pf_te:4.2f} | Ann R: +{ann_r_te:5.1f}R")
    
    # Out-of-Sample year-by-year:
    for y in [2025, 2026]:
        ysim = sim_te[sim_te['year'] == y]
        y_wr = ysim['is_win'].mean() * 100.0
        y_wins = ysim[ysim['realized_r'] > 0]['realized_r'].sum()
        y_loss = abs(ysim[ysim['realized_r'] < 0]['realized_r'].sum())
        y_pf = y_wins / y_loss if y_loss > 0 else 999.0
        y_len = 1.0 if y == 2025 else 0.52
        print(f"    Year {y}: N={len(ysim):3d} ({len(ysim)/y_len:.0f}/yr) | WR: {y_wr:5.1f}% | PF: {y_pf:4.2f} | R: +{ysim['realized_r'].sum():.1f}R")
