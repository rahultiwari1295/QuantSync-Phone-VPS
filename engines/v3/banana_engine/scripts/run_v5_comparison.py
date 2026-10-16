"""
banana_engine/scripts/run_v5_comparison.py — Institutional Head-to-Head Comparison Backtest
==========================================================================================
Compares Baseline v4 vs Enhanced Institutional v5 on held-out 2025-2026 daily OHLC data.

Systems Compared:
  [A] Baseline v4: 38 Base Features -> Baseline LightGBM -> Fixed Exits
  [B] Exit Ablation: 38 Base Features -> Baseline LightGBM -> Adaptive Exits
  [C] Signal Ablation: 49 Enhanced Features -> Enhanced LightGBM -> Fixed Exits
  [D] Full Enhanced v5: 49 Enhanced Features -> Enhanced LightGBM + Failure Trap Filter -> Adaptive Exits
"""

import os
import sys
import json
import time
from typing import Dict, List, Any, Tuple
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, brier_score_loss

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, load_adjusted
from core.features import FEATURES
from core.enhanced_features import ENHANCED_FEATURES
from core.adaptive_exits import simulate_adaptive_exit, simulate_baseline_fixed_exit

RESULTS_FILE = os.path.join(DATA_DIR, "v5_head_to_head_comparison_results.json")


def load_datasets() -> Tuple[pd.DataFrame, pd.DataFrame]:
    tr_path = os.path.join(DATA_DIR, "enhanced_train_v5.parquet")
    te_path = os.path.join(DATA_DIR, "enhanced_test_v5.parquet")
    
    tr = pd.read_parquet(tr_path)
    te = pd.read_parquet(te_path)
    return tr, te


def compute_performance_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Computes institutional quantitative metrics from trade list."""
    if not trades:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_r": 0.0,
            "avg_r": 0.0,
            "expectancy_r": 0.0,
            "max_drawdown_r": 0.0,
            "sharpe_ratio": 0.0,
            "avg_winner_r": 0.0,
            "avg_loser_r": 0.0,
            "runners_count": 0,
        }

    r_multiples = np.array([t["r_multiple"] for t in trades], dtype=float)
    pnl_pcts = np.array([t["pnl_pct"] for t in trades], dtype=float)
    
    n_trades = len(r_multiples)
    winners = r_multiples[r_multiples > 0]
    losers = r_multiples[r_multiples < 0]
    runners = r_multiples[r_multiples >= 2.0]

    win_rate = float(len(winners) / n_trades) if n_trades > 0 else 0.0
    sum_win = float(np.sum(winners)) if len(winners) > 0 else 0.0
    sum_loss = abs(float(np.sum(losers))) if len(losers) > 0 else 1e-6
    profit_factor = float(sum_win / sum_loss) if sum_loss > 0 else 999.0

    total_r = float(np.sum(r_multiples))
    avg_r = float(np.mean(r_multiples))
    avg_winner_r = float(np.mean(winners)) if len(winners) > 0 else 0.0
    avg_loser_r = float(np.mean(losers)) if len(losers) > 0 else 0.0

    # Expectancy in R
    loss_rate = 1.0 - win_rate
    expectancy_r = (win_rate * avg_winner_r) - (loss_rate * abs(avg_loser_r))

    # Max drawdown in R
    cum_r = np.cumsum(r_multiples)
    running_max = np.maximum.accumulate(cum_r)
    dd_r = running_max - cum_r
    max_dd_r = float(np.max(dd_r)) if len(dd_r) > 0 else 0.0

    # Sharpe on trade returns
    std_r = float(np.std(r_multiples)) if len(r_multiples) > 1 else 1.0
    # Annualized assuming ~100 trades/year
    sharpe = float((avg_r / (std_r + 1e-6)) * np.sqrt(min(n_trades, 150)))

    return {
        "trade_count": int(n_trades),
        "win_rate": round(win_rate * 100.0, 2),
        "profit_factor": round(profit_factor, 2),
        "total_r": round(total_r, 2),
        "avg_r": round(avg_r, 3),
        "expectancy_r": round(expectancy_r, 3),
        "max_drawdown_r": round(max_dd_r, 2),
        "sharpe_ratio": round(sharpe, 2),
        "avg_winner_r": round(avg_winner_r, 3),
        "avg_loser_r": round(avg_loser_r, 3),
        "runners_count": int(len(runners)),
    }


def run_comparison():
    print("=" * 80)
    print("INSTITUTIONAL QUANTUM ENGINE: HEAD-TO-HEAD COMPARISON BACKTEST")
    print("Baseline v4 vs Enhanced v5 (Daily OHLCV Only)")
    print("=" * 80)

    train_df, test_df = load_datasets()
    print(f"Train samples (2020-2024): {len(train_df)}")
    print(f"Test samples  (2025-2026): {len(test_df)}")

    # Pre-cache bars for test symbols to make bar-by-bar evaluation ultra-fast
    test_symbols = test_df['symbol'].unique()
    print(f"\nCaching daily candlestick bars for {len(test_symbols)} test symbols...")
    t0 = time.time()
    test_bars_cache = {}
    for s in test_symbols:
        try:
            b = load_adjusted(s)
            if not b.empty:
                test_bars_cache[s] = b
        except Exception:
            continue
    print(f"Cached in {time.time() - t0:.1f}s.")

    # Map dates to bar index for each symbol
    symbol_date_idx = {}
    for s, b in test_bars_cache.items():
        symbol_date_idx[s] = {d: i for i, d in enumerate(b['date'].values)}

    # -------------------------------------------------------------
    # 1. Train Models
    # -------------------------------------------------------------
    print("\n--- Training Models on 2020-2024 Window ---")
    
    # [Model 1] Baseline Model (38 Features)
    lgb_baseline_params = dict(
        objective="binary", metric="auc",
        n_estimators=300, learning_rate=0.03,
        num_leaves=20, max_depth=5, min_child_samples=25,
        subsample=0.85, subsample_freq=1, colsample_bytree=0.85,
        reg_alpha=0.2, reg_lambda=1.0, random_state=42, n_jobs=-1, verbose=-1
    )
    
    tr_clean_base = train_df.dropna(subset=FEATURES + ["label"]).copy()
    te_clean_base = test_df.dropna(subset=FEATURES + ["label"]).copy()
    
    model_baseline = lgb.LGBMClassifier(**lgb_baseline_params)
    model_baseline.fit(
        tr_clean_base[FEATURES], tr_clean_base["label"]
    )
    preds_baseline = model_baseline.predict_proba(test_df[FEATURES])[:, 1]
    
    # [Model 2] Enhanced Model (49 Features: 38 Base + 6 Smart Money + 5 Cascade)
    tr_clean_enh = train_df.dropna(subset=ENHANCED_FEATURES + ["label"]).copy()
    te_clean_enh = test_df.dropna(subset=ENHANCED_FEATURES + ["label"]).copy()
    
    model_enhanced = lgb.LGBMClassifier(**lgb_baseline_params)
    model_enhanced.fit(
        tr_clean_enh[ENHANCED_FEATURES], tr_clean_enh["label"]
    )
    preds_enhanced = model_enhanced.predict_proba(test_df[ENHANCED_FEATURES])[:, 1]

    # [Model 3] Failure Anatomy Negative Filter
    fail_col = "fail_immediate_rejection" if "fail_immediate_rejection" in train_df.columns else "is_failure"
    fail_model = lgb.LGBMClassifier(
        objective="binary", metric="auc",
        n_estimators=300, learning_rate=0.03,
        num_leaves=16, max_depth=4, min_child_samples=30,
        subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=2.0, random_state=42, n_jobs=-1, verbose=-1
    )
    fail_model.fit(
        tr_clean_enh[ENHANCED_FEATURES], tr_clean_enh[fail_col]
    )
    preds_failure = fail_model.predict_proba(test_df[ENHANCED_FEATURES])[:, 1]

    print("Model Training Complete.")

    # -------------------------------------------------------------
    # 2. Simulate Bar-by-Bar Trades on Held-Out Test Period (2025-2026)
    # -------------------------------------------------------------
    print("\n--- Executing Bar-by-Bar Backtests on Test Period ---")

    # High-confidence threshold (Top 30% of each model)
    base_cutoff = float(np.quantile(preds_baseline, 0.70))
    enh_cutoff = float(np.quantile(preds_enhanced, 0.70))
    fail_threshold = 0.28  # Reject if failure probability >= 28%

    trades_A: List[Dict[str, Any]] = []  # Baseline: Base Model + Fixed Exits
    trades_B: List[Dict[str, Any]] = []  # Exit Ablation: Base Model + Adaptive Exits
    trades_C: List[Dict[str, Any]] = []  # Signal Ablation: Enhanced Model + Fixed Exits
    trades_D: List[Dict[str, Any]] = []  # Full v5: Enhanced Model + Failure Filter + Adaptive Exits

    rejected_by_failure_filter = 0

    for idx, row in enumerate(test_df.itertuples()):
        sym = getattr(row, "symbol", "")
        dt = str(getattr(row, "date", ""))[:10]
        entry = float(getattr(row, "entry", 0.0))
        stop_p = float(getattr(row, "floor", entry * 0.93))
        target_p = entry * 1.15
        regime = getattr(row, "regime_state_ord", 1)
        regime_str = "BULL" if regime == 2 else ("BEAR" if regime == 0 else "NEUTRAL")

        bars = test_bars_cache.get(sym)
        if bars is None or entry <= 0:
            continue

        if sym not in symbol_date_idx or dt not in symbol_date_idx[sym]:
            continue

        T = symbol_date_idx[sym][dt]
        if T >= len(bars) - 2:
            continue

        p_base = preds_baseline[idx]
        p_enh = preds_enhanced[idx]
        p_fail = preds_failure[idx]

        # [A] Baseline: Base Model qualifies (Top 30%) -> Fixed Exits
        if p_base >= base_cutoff:
            res_a = simulate_baseline_fixed_exit(bars, T, entry, stop_p, target_p)
            trades_A.append(res_a)

            # [B] Exit Ablation: Base Model qualifies -> Adaptive Exits
            res_b = simulate_adaptive_exit(bars, T, entry, stop_p, target_p, regime=regime_str)
            trades_B.append(res_b)

        # [C] Signal Ablation: Enhanced Model qualifies -> Fixed Exits
        if p_enh >= enh_cutoff:
            res_c = simulate_baseline_fixed_exit(bars, T, entry, stop_p, target_p)
            trades_C.append(res_c)

            # [D] Full v5: Enhanced Model + Negative Failure Filter -> Adaptive Exits
            if p_fail < fail_threshold:
                res_d = simulate_adaptive_exit(bars, T, entry, stop_p, target_p, regime=regime_str)
                trades_D.append(res_d)
            else:
                rejected_by_failure_filter += 1

    # -------------------------------------------------------------
    # 3. Calculate and Print Metrics
    # -------------------------------------------------------------
    metrics_A = compute_performance_metrics(trades_A)
    metrics_B = compute_performance_metrics(trades_B)
    metrics_C = compute_performance_metrics(trades_C)
    metrics_D = compute_performance_metrics(trades_D)

    print("\n" + "=" * 90)
    print("HEAD-TO-HEAD BACKTEST RESULTS (2025 - 2026 HELD-OUT OOS DATA)")
    print("=" * 90)
    header = f"{'Metric':<25} | {'[A] Baseline v4':>15} | {'[B] Exit Ablation':>16} | {'[C] Signal Ablation':>17} | {'[D] Enhanced v5':>16}"
    print(header)
    print("-" * 90)

    rows = [
        ("Trade Count", "trade_count"),
        ("Win Rate (%)", "win_rate"),
        ("Profit Factor", "profit_factor"),
        ("Total Realized R", "total_r"),
        ("Avg R / Trade", "avg_r"),
        ("Expectancy (R)", "expectancy_r"),
        ("Max Drawdown (R)", "max_drawdown_r"),
        ("Sharpe Ratio", "sharpe_ratio"),
        ("Avg Winner (R)", "avg_winner_r"),
        ("Avg Loser (R)", "avg_loser_r"),
        ("Runners (>=2R)", "runners_count"),
    ]

    for label, key in rows:
        val_a = str(metrics_A[key])
        val_b = str(metrics_B[key])
        val_c = str(metrics_C[key])
        val_d = str(metrics_D[key])
        print(f"{label:<25} | {val_a:>15} | {val_b:>16} | {val_c:>17} | {val_d:>16}")

    print("=" * 90)
    print(f"Negative Failure Filter Action: Filtered out {rejected_by_failure_filter} toxic setups.")

    # Save to JSON
    output_payload = {
        "test_period": "2025-01-01 to 2026-09-07",
        "total_test_candidates": len(test_df),
        "systems": {
            "baseline_v4": metrics_A,
            "exit_ablation": metrics_B,
            "signal_ablation": metrics_C,
            "enhanced_v5": metrics_D
        },
        "negative_filter": {
            "setups_filtered": rejected_by_failure_filter,
            "threshold": fail_threshold
        }
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(output_payload, f, indent=2)

    print(f"\nSaved results artifact to {RESULTS_FILE}")


if __name__ == "__main__":
    run_comparison()
