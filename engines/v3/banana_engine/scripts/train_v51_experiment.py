"""
Train and compare four candidate systems on the v5.1 feature set.

A) baseline_38      — old feature set (control)
B) breakout_46      — 38 base + 9 raw breakout-bar features
C) v51_full_50      — 46 + 4 trap flags (full v5.1)
D) hard_filter_only — no ML, just apply the trap rules

All trained on 2020-2024. All evaluated on 2025-2026 held-out.
"""
import sys
sys.path.insert(0, '.')

import json
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, brier_score_loss

TRAIN = pd.read_parquet("data/backfill_train_v51.parquet")
TEST  = pd.read_parquet("data/backfill_test_v51.parquet")

BASE_38 = [
    "base_depth_pct", "duration_weeks", "coil_ratio", "dry_ratio",
    "num_contractions", "depth_contraction_ratio", "tightness_5d",
    "tightness_10d", "days_since_peak", "position_in_base",
    "dry_depth", "ud_ratio", "breakout_volume_ratio", "volume_slope_base",
    "max_volume_day_ratio", "volume_contraction_ratio",
    "rs_rating", "dist_from_52w_high_pct", "dist_from_52w_low_pct",
    "pct_above_50dma", "pct_above_150dma", "pct_above_200dma",
    "dma_150_above_200", "dma_200_slope_22d",
    "breadth_above_20dma", "breadth_above_50dma", "breadth_above_200dma",
    "regime_state_ord",
    "sector_return_5d", "sector_return_20d", "sector_rs_rank",
    "sector_breadth", "stock_vs_sector_rs",
    "squat_count_in_base", "failed_poke_count_in_base", "days_since_last_squat",
    "days_to_next_earnings", "weeks_since_ipo",
]
RAW_9   = ["chase_pct", "upper_wick_pct", "lower_wick_pct", "body_pct",
           "cir", "is_bull_body", "bar_ret", "dist_sma200", "prior_runup_60"]
FLAGS_4 = ["is_trap_1", "is_trap_2", "is_trap_3", "is_trap_4"]

PARAMS = dict(
    objective="binary", metric="auc",
    n_estimators=2000, learning_rate=0.03,
    num_leaves=15, max_depth=4,
    min_child_samples=20, subsample=0.8, subsample_freq=1,
    colsample_bytree=0.8, reg_alpha=0.1, reg_lambda=1.0,
    random_state=42, n_jobs=-1,
)


def clean(df, features):
    return df.dropna(subset=features + ["label"]).copy()


def train_and_eval(features, train_df, test_df):
    tr = clean(train_df, features)
    te = clean(test_df, features)
    if len(tr) < 500 or len(te) < 100:
        return {"error": "insufficient data",
                "train_n": len(tr), "test_n": len(te)}

    model = lgb.LGBMClassifier(**PARAMS)
    model.fit(
        tr[features], tr["label"],
        eval_set=[(te[features], te["label"])],
        eval_metric="auc",
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )

    p_test = model.predict_proba(te[features])[:, 1]
    auc = roc_auc_score(te["label"], p_test)
    brier = brier_score_loss(te["label"], p_test)

    q75 = np.quantile(p_test, 0.75)
    top = te[p_test >= q75]
    top_wr = top["label"].mean()
    top_n = len(top)

    # Trap breakdown
    has_flags = all(f in te.columns for f in FLAGS_4)
    if has_flags:
        te_flagged = te[te[FLAGS_4].sum(axis=1) > 0]
        te_clean_ = te[te[FLAGS_4].sum(axis=1) == 0]
        flagged_wr = float(te_flagged["label"].mean()) if len(te_flagged) else None
        clean_wr = float(te_clean_["label"].mean()) if len(te_clean_) else None
    else:
        flagged_wr = None
        clean_wr = None

    return {
        "auc": round(float(auc), 4),
        "brier": round(float(brier), 4),
        "top_quartile_win_rate": round(float(top_wr), 4),
        "top_quartile_n": int(top_n),
        "trap_flagged_win_rate": round(flagged_wr, 4) if flagged_wr is not None else None,
        "clean_win_rate": round(clean_wr, 4) if clean_wr is not None else None,
        "train_n": int(len(tr)),
        "test_n": int(len(te)),
    }


print("=" * 72)
print("v5.1 EXPERIMENT — TRAIN 2020-2024, TEST 2025-2026 (HELD OUT)")
print("=" * 72)

results = {}

print("\n[A] Baseline 38 features (control)...")
results["A_baseline_38"] = train_and_eval(BASE_38, TRAIN, TEST)

print("[B] + 9 raw breakout-bar features (46 total)...")
results["B_breakout_46"] = train_and_eval(BASE_38 + RAW_9, TRAIN, TEST)

print("[C] + 4 trap flags (50 total, full v5.1)...")
results["C_v51_full_50"] = train_and_eval(BASE_38 + RAW_9 + FLAGS_4, TRAIN, TEST)

print("[D] Hard filter only (no ML)...")
te_all = clean(TEST, BASE_38 + RAW_9 + FLAGS_4)
if all(f in te_all.columns for f in FLAGS_4):
    te_clean_only = te_all[te_all[FLAGS_4].sum(axis=1) == 0]
    te_flagged_only = te_all[te_all[FLAGS_4].sum(axis=1) > 0]
    results["D_hard_filter_only"] = {
        "auc": None,
        "brier": None,
        "top_quartile_win_rate": None,
        "top_quartile_n": None,
        "trap_flagged_win_rate": round(float(te_flagged_only["label"].mean()), 4) if len(te_flagged_only) else None,
        "clean_win_rate": round(float(te_clean_only["label"].mean()), 4) if len(te_clean_only) else None,
        "train_n": None,
        "test_n": int(len(te_all)),
        "clean_n": int(len(te_clean_only)),
    }

# Print the comparison table
print("\n" + "=" * 72)
print("RESULTS")
print("=" * 72)
print(f"{'Model':<24} {'AUC':>6} {'TopQ_WR':>8} {'Clean_WR':>9} {'Flag_WR':>8} {'N_test':>7}")
print("-" * 72)
for name, r in results.items():
    auc = f"{r['auc']:.4f}" if r.get("auc") is not None else "  —   "
    topq = f"{r['top_quartile_win_rate']:.3f}" if r.get("top_quartile_win_rate") is not None else "  —   "
    clean_wr = f"{r['clean_win_rate']:.3f}" if r.get("clean_win_rate") is not None else "  —   "
    flag_wr = f"{r['trap_flagged_win_rate']:.3f}" if r.get("trap_flagged_win_rate") is not None else "  —   "
    n = r.get("test_n", 0)
    print(f"{name:<24} {auc:>6} {topq:>8} {clean_wr:>9} {flag_wr:>8} {n:>7}")

with open("data/v51_experiment_results.json", "w") as f:
    json.dump(results, f, indent=2)

print("\nSaved to data/v51_experiment_results.json")
