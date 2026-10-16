"""
scripts/recompute_divergence_threshold.py — Recompute Online Calibrator Divergence Threshold
=============================================================================================
Runs walk_forward_three_way() with up to 30 folds, fits fast vs. slow isotonic calibration
windows to compute |P_fast - P_slow| across all out-of-sample predictions, aggregates the
empirical distribution, and saves percentiles to data/divergence_threshold.json.
"""

import os
import sys
import json
import pandas as pd
import numpy as np
from sklearn.isotonic import IsotonicRegression

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR
from core.features import FEATURES
from core.brain import walk_forward_three_way


def main():
    # Prefer full dataset if available, otherwise fix-pass dataset
    full_path = os.path.join(DATA_DIR, "backfill_dataset_full.parquet")
    fix_path = os.path.join(DATA_DIR, "backfill_dataset.parquet")

    if os.path.exists(full_path):
        data_path = full_path
    elif os.path.exists(fix_path):
        data_path = fix_path
    else:
        print(f"Error: No dataset found at {full_path} or {fix_path}")
        return

    df = pd.read_parquet(data_path)
    print(f"Loaded dataset from {data_path}: {len(df)} rows ({df['date'].min()} to {df['date'].max()})")

    divergences = []
    fold_details = []

    print(f"\n--- Running Walk-Forward Folds (n_folds=30) ---")
    folds = list(walk_forward_three_way(df, features=FEATURES, label="label", n_folds=30))
    print(f"Data supported {len(folds)} valid walk-forward folds.")

    for i, (model, test, raw_test, auc) in enumerate(folds, 1):
        # We need fast and slow calibrator curves for this fold
        # Train on pre-test window:
        # Slow window: Long-term anchor (all prior train)
        # Fast window: Recent drift detector (validation set or recent segment)
        dataset_sorted = df.sort_values('date').reset_index(drop=True)
        test_start_date = test['date'].min()

        # Re-identify train and validation sets for this fold
        pre_test = dataset_sorted[dataset_sorted['date'] < test_start_date]
        if len(pre_test) < 20:
            continue

        val_size = max(5, len(test) // 2)
        train_sub = pre_test.iloc[:-val_size]
        val_sub = pre_test.iloc[-val_size:]

        if len(train_sub) < 10 or len(val_sub) < 5:
            continue

        raw_train = model.predict_proba(train_sub[FEATURES])[:, 1]
        raw_val = model.predict_proba(val_sub[FEATURES])[:, 1]

        # Fit Slow Isotonic (Long-term anchor)
        iso_slow = None
        if len(np.unique(train_sub['label'])) > 1:
            iso_slow = IsotonicRegression(out_of_bounds='clip')
            iso_slow.fit(raw_train, train_sub['label'])

        # Fit Fast Isotonic (Recent drift sensor)
        iso_fast = None
        if len(np.unique(val_sub['label'])) > 1:
            iso_fast = IsotonicRegression(out_of_bounds='clip')
            iso_fast.fit(raw_val, val_sub['label'])

        fold_divs = []
        if iso_slow is not None and iso_fast is not None:
            p_slow_arr = iso_slow.predict(raw_test)
            p_fast_arr = iso_fast.predict(raw_test)
            fold_divs = list(np.abs(p_fast_arr - p_slow_arr))
            divergences.extend(fold_divs)

        mean_fold_div = np.mean(fold_divs) if fold_divs else 0.0
        fold_details.append({
            "fold": i,
            "test_start": str(test['date'].min()),
            "test_end": str(test['date'].max()),
            "test_size": len(test),
            "auc": round(float(auc), 4),
            "divergences_count": len(fold_divs),
            "mean_divergence": round(float(mean_fold_div), 4)
        })
        print(f"  Fold {i:2d}: {test['date'].min()} to {test['date'].max()} | N={len(test):3d} | AUC={auc:.4f} | Mean |P_fast - P_slow|={mean_fold_div:.4f}")

    if not divergences:
        print("Warning: Insufficient class variation to fit fast/slow windows across folds.")
        return

    div_arr = np.array(divergences)
    p50 = float(np.percentile(div_arr, 50))
    p75 = float(np.percentile(div_arr, 75))
    p90 = float(np.percentile(div_arr, 90))
    p95 = float(np.percentile(div_arr, 95))
    p99 = float(np.percentile(div_arr, 99))
    max_div = float(np.max(div_arr))

    print("\n" + "=" * 60)
    print("=== EMPIRICAL DIVERGENCE DISTRIBUTION ===")
    print("=" * 60)
    print(f"Total Prediction Evaluations: {len(div_arr):,}")
    print(f"  50th Percentile (Median) : {p50:.4f}")
    print(f"  75th Percentile          : {p75:.4f}")
    print(f"  90th Percentile          : {p90:.4f}")
    print(f"  95th Percentile          : {p95:.4f}")
    print(f"  99th Percentile          : {p99:.4f}")
    print(f"  Maximum Divergence       : {max_div:.4f}")
    print(f"Current System Threshold   : 0.0800")
    print(f"Difference (P95 - 0.08)    : {p95 - 0.08:+.4f}")
    print("=" * 60)

    result_payload = {
        "calculated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset_used": os.path.basename(data_path),
        "total_samples": len(div_arr),
        "total_folds": len(fold_details),
        "current_in_use_threshold": 0.08,
        "recommended_recomputed_threshold_p95": round(p95, 4),
        "percentiles": {
            "p50": round(p50, 4),
            "p75": round(p75, 4),
            "p90": round(p90, 4),
            "p95": round(p95, 4),
            "p99": round(p99, 4),
            "max": round(max_div, 4)
        },
        "fold_details": fold_details
    }

    out_file = os.path.join(DATA_DIR, "divergence_threshold.json")
    with open(out_file, "w") as f:
        json.dump(result_payload, f, indent=2)

    print(f"\nSaved recomputed threshold distribution to {out_file}")


if __name__ == "__main__":
    main()
