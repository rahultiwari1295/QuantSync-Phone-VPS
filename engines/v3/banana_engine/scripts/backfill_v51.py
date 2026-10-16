"""
Backfill v5.1 dataset with new breakout-bar features.
Train window: 2020-2024. Test window: 2025-2026.
"""
import sys
sys.path.insert(0, '.')

import time
import os
from core.backfill import backfill_universe

TRAIN_START = "2020-01-01"
TRAIN_END   = "2024-12-31"
TEST_START  = "2025-01-01"
TEST_END    = "2026-09-12"

TRAIN_OUT = "data/backfill_train_v51.parquet"
TEST_OUT  = "data/backfill_test_v51.parquet"

print("=== Backfilling train window (2020-2024) ===")
t0 = time.time()
train_df = backfill_universe(start=TRAIN_START, end=TRAIN_END, output_path=TRAIN_OUT)
elapsed = time.time() - t0
train_df.to_parquet(TRAIN_OUT)
print(f"Train rows: {len(train_df)}  ({elapsed:.0f}s)")

print("\n=== Backfilling test window (2025-2026) ===")
t0 = time.time()
test_df = backfill_universe(start=TEST_START, end=TEST_END, output_path=TEST_OUT)
elapsed = time.time() - t0
test_df.to_parquet(TEST_OUT)
print(f"Test rows: {len(test_df)}  ({elapsed:.0f}s)")

print("\n=== Sanity checks ===")
for name, df in [("train", train_df), ("test", test_df)]:
    print(f"\n{name}:")
    print(f"  Rows: {len(df)}")
    print(f"  RS std: {df['rs_rating'].std():.2f}")
    print(f"  Label positive rate: {df['label'].mean():.3f}")
    for trap in ["is_trap_1", "is_trap_2", "is_trap_3", "is_trap_4"]:
        if trap in df.columns:
            print(f"  {trap}: {int(df[trap].sum())} rows ({df[trap].mean()*100:.1f}%)")
