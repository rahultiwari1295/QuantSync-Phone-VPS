"""
banana_engine/core/enhanced_backfill.py — Enhanced Institutional Backfill Pipeline
==================================================================================
Enriches the baseline backfill dataset with institutional features:
  - Smart Money Accumulation (6 features)
  - Cross-Sectional Cascade Momentum (5 features)
  - Post-Hoc Failure Anatomy Classification (labels for negative filter)

Outputs:
  data/enhanced_train_v5.parquet
  data/enhanced_test_v5.parquet
"""

import os
import sys
import time
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, load_adjusted
from core.smart_money import compute_smart_money_features
from core.cascade import CrossSectionalBreakoutIndex, build_cascade_index_from_df
from core.failure_anatomy import classify_trade_failure, FAILURE_MODES
from core.enhanced_features import SMART_MONEY_FEATURES, CASCADE_FEATURES, ENHANCED_FEATURES


def enrich_dataset_with_v5(
    input_parquet_path: str,
    output_parquet_path: str,
    cascade_index: Optional[CrossSectionalBreakoutIndex] = None
) -> pd.DataFrame:
    """
    Takes an existing base backfill parquet dataset, computes smart money features,
    cascade momentum features, and failure anatomy labels, saving the enhanced dataset.
    """
    print(f"[Enhanced Backfill] Loading base dataset from {input_parquet_path}...")
    df = pd.read_parquet(input_parquet_path).copy()
    print(f"  Loaded {len(df)} records.")

    unique_symbols = df['symbol'].unique()
    print(f"  Pre-caching OHLCV bars for {len(unique_symbols)} unique symbols...")
    bars_cache: Dict[str, pd.DataFrame] = {}
    t0 = time.time()
    for s in unique_symbols:
        try:
            b = load_adjusted(s)
            if not b.empty:
                # Index by date for fast lookup
                bars_cache[s] = b
        except Exception:
            continue
    print(f"  Cached {len(bars_cache)} symbols in {time.time() - t0:.1f}s.")

    # 1. Build or use CrossSectionalBreakoutIndex for Cascade Features
    if cascade_index is None:
        print("[Enhanced Backfill] Building Cross-Sectional Cascade Index...")
        cascade_index = build_cascade_index_from_df(df)

    # 2. Compute features row-by-row with cached bars
    print("[Enhanced Backfill] Computing Smart Money, Cascade & Failure features...")
    sm_rows = []
    cas_rows = []
    fail_rows = []

    # Map date strings to row indices per symbol
    date_to_idx: Dict[str, Dict[str, int]] = {}
    for s, b in bars_cache.items():
        date_to_idx[s] = {d: i for i, d in enumerate(b['date'].values)}

    for row in df.itertuples():
        sym = getattr(row, "symbol", "")
        dt = str(getattr(row, "date", ""))[:10]
        entry = float(getattr(row, "entry", 0.0))
        bars = bars_cache.get(sym)

        # Smart Money Features
        if bars is not None and sym in date_to_idx and dt in date_to_idx[sym]:
            T = date_to_idx[sym][dt]
            sm = compute_smart_money_features(bars, T)
            fail = classify_trade_failure(bars, T, entry_price=entry)
        else:
            sm = {k: 0.0 for k in SMART_MONEY_FEATURES}
            sm["accumulation_score"] = 50.0
            fail = {m: 0 for m in FAILURE_MODES} | {"is_failure": 0}

        # Cascade Features
        cas = cascade_index.compute_features(symbol=sym, current_date=dt)

        sm_rows.append(sm)
        cas_rows.append(cas)
        fail_rows.append(fail)

    # Attach all new columns
    sm_df = pd.DataFrame(sm_rows, index=df.index)
    cas_df = pd.DataFrame(cas_rows, index=df.index)
    fail_df = pd.DataFrame(fail_rows, index=df.index)

    enhanced_df = pd.concat([df, sm_df, cas_df, fail_df], axis=1)

    os.makedirs(os.path.dirname(output_parquet_path), exist_ok=True)
    enhanced_df.to_parquet(output_parquet_path, index=False)
    print(f"[Enhanced Backfill] Saved {len(enhanced_df)} enhanced rows to {output_parquet_path}")

    return enhanced_df


def run_full_v5_enrichment():
    """
    Enriches both train (2020-2024) and test (2025-2026) datasets.
    """
    train_in = os.path.join(DATA_DIR, "backfill_train_v51.parquet")
    test_in = os.path.join(DATA_DIR, "backfill_test_v51.parquet")
    
    train_out = os.path.join(DATA_DIR, "enhanced_train_v5.parquet")
    test_out = os.path.join(DATA_DIR, "enhanced_test_v5.parquet")

    # Build shared cross-sectional index across both train and test to capture continuous cascade momentum
    print("[Enrichment] Loading full event set for global cascade indexing...")
    df_tr = pd.read_parquet(train_in)
    df_te = pd.read_parquet(test_in)
    global_df = pd.concat([df_tr, df_te], axis=0).reset_index(drop=True)
    global_cascade_idx = build_cascade_index_from_df(global_df)

    print("\n--- Processing Train Set (2020 - 2024) ---")
    enrich_dataset_with_v5(train_in, train_out, cascade_index=global_cascade_idx)

    print("\n--- Processing Test Set (2025 - 2026) ---")
    enrich_dataset_with_v5(test_in, test_out, cascade_index=global_cascade_idx)
    print("\n[Enrichment] Full v5 dataset generation complete.")


if __name__ == "__main__":
    run_full_v5_enrichment()
