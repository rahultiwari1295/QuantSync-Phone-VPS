"""
scripts/investigate_fold2.py — Diagnose Fold 2 Inversion in Walk-Forward Validation
===================================================================================
Reproduces the walk-forward folds from the fix-pass dataset, calculates fold-level
statistics (AUC, base rate, feature distributions, Nifty return, breadth), and extracts
Fold 2 candidate diagnostics to data/fold2_diagnosis.parquet.
"""

import os
import sys
import pandas as pd
import numpy as np

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, load_adjusted
from core.features import FEATURES
from core.brain import walk_forward_three_way


def get_nifty_return(start_date: str, end_date: str) -> float:
    try:
        df_nifty = load_adjusted("NIFTY")
        df_sub = df_nifty[(df_nifty["date"] >= start_date) & (df_nifty["date"] <= end_date)]
        if len(df_sub) >= 2:
            return (df_sub["close"].iloc[-1] / df_sub["close"].iloc[0] - 1.0) * 100.0
    except Exception as e:
        print(f"Warning computing Nifty return: {e}")
    return 0.0


def get_market_breadth_return(start_date: str, end_date: str, sample_symbols: list) -> float:
    pos_count = 0
    total = 0
    for sym in sample_symbols:
        try:
            bars = load_adjusted(sym)
            bars_sub = bars[(bars["date"] >= start_date) & (bars["date"] <= end_date)]
            if len(bars_sub) >= 2:
                total += 1
                if bars_sub["close"].iloc[-1] > bars_sub["close"].iloc[0]:
                    pos_count += 1
        except Exception:
            continue
    return (pos_count / total * 100.0) if total > 0 else 50.0


def main():
    dataset_path = os.path.join(DATA_DIR, "backfill_dataset.parquet")
    if not os.path.exists(dataset_path):
        print(f"Error: Dataset {dataset_path} does not exist.")
        return

    df = pd.read_parquet(dataset_path)
    print(f"Loaded fix-pass dataset: {len(df)} rows from {df['date'].min()} to {df['date'].max()}")

    # Determine unique sample symbols for breadth calculation
    sample_symbols = list(df["symbol"].unique())

    features_to_track = [
        "rs_rating", "base_depth_pct", "coil_ratio",
        "dry_depth", "ud_ratio", "breakout_volume_ratio"
    ]

    print("\n" + "=" * 90)
    print(f"{'Fold':<6}{'Test Dates':<25}{'N_test':<8}{'AUC':<8}{'Pos Rate':<10}{'Nifty Ret':<12}{'Breadth (%+)':<12}")
    print("=" * 90)

    fold2_data = None
    folds = list(walk_forward_three_way(df, features=FEATURES, label="label", n_folds=8))

    for idx, (model, test, raw_test, auc) in enumerate(folds, 1):
        test_start = test["date"].min()
        test_end = test["date"].max()
        n_test = len(test)
        pos_rate = test["label"].mean() * 100.0
        nifty_ret = get_nifty_return(test_start, test_end)
        breadth = get_market_breadth_return(test_start, test_end, sample_symbols)

        print(f"{idx:<6}{test_start + ' to ' + test_end:<25}{n_test:<8}{auc:<8.4f}{pos_rate:<10.1f}%{nifty_ret:<11.2f}%{breadth:<11.1f}%")

        print("  Feature Distribution (mean ± std):")
        for f in features_to_track:
            if f in test.columns:
                m = test[f].mean()
                s = test[f].std()
                print(f"    - {f:<24}: {m:6.2f} ± {s:5.2f}")

        if idx == 2:
            test_copy = test.copy()
            test_copy["raw_score"] = raw_test
            fold2_data = test_copy

    print("=" * 90)

    if fold2_data is not None:
        sorted_fold2 = fold2_data.sort_values("raw_score", ascending=False)
        top_20 = sorted_fold2.head(min(20, len(sorted_fold2)))
        bot_20 = sorted_fold2.tail(min(20, len(sorted_fold2)))
        diagnosis_df = pd.concat([top_20, bot_20]).drop_duplicates()
        out_path = os.path.join(DATA_DIR, "fold2_diagnosis.parquet")
        diagnosis_df.to_parquet(out_path, index=False)
        print(f"\nSaved Fold 2 diagnostic candidates ({len(diagnosis_df)} rows) to {out_path}")

        print("\nFold 2 Candidate Analysis:")
        print(f"  Top 20 candidates positive label rate: {top_20['label'].mean() * 100:.1f}%")
        print(f"  Bottom 20 candidates positive label rate: {bot_20['label'].mean() * 100:.1f}%")
        print(f"  Top candidate symbols: {list(top_20['symbol'])}")
        print(f"  Bottom candidate symbols: {list(bot_20['symbol'])}")


if __name__ == "__main__":
    main()
