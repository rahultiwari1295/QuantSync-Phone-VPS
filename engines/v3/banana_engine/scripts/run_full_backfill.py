"""
scripts/run_full_backfill.py — Full Universe Backfill & Coverage Audit
=====================================================================
Executes backfill_universe() on the complete non-index universe without modifying
backfill logic, saving output to data/backfill_dataset_full.parquet and auditing metrics.
"""

import os
import sys
import sqlite3
import pandas as pd
import numpy as np

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB
from core.backfill import backfill_universe


def main():
    output_path = os.path.join(DATA_DIR, "backfill_dataset_full.parquet")
    print(f"=== Starting Full-Universe Backfill ===")
    print(f"Target Output: {output_path}")
    print(f"Date Range: 2015-01-01 to 2024-12-31")

    # Execute backfill on the complete non-index universe as defined in backfill.py
    df = backfill_universe(
        symbols=None,
        start="2015-01-01",
        end="2024-12-31",
        output_path=output_path
    )

    print("\n" + "=" * 60)
    print("=== FULL BACKFILL COVERAGE AUDIT REPORT ===")
    print("=" * 60)

    if df.empty:
        print("ERROR: Backfill produced an empty DataFrame!")
        return

    total_rows = len(df)
    min_date = df['date'].min()
    max_date = df['date'].max()
    distinct_symbols = df['symbol'].nunique()
    pos_rate = df['label'].mean()

    print(f"Total Rows: {total_rows:,}")
    print(f"Date Range: {min_date} to {max_date}")
    print(f"Distinct Symbols Covered: {distinct_symbols}")
    print(f"Label Positive Rate: {pos_rate:.4f} ({pos_rate * 100:.2f}%)")

    # Distribution of rows per year
    df['year'] = pd.to_datetime(df['date']).dt.year
    year_counts = df['year'].value_counts().sort_index()
    print("\n--- Distribution of Rows per Year ---")
    for yr, count in year_counts.items():
        bar = "#" * int(count / max(1, (total_rows / 40)))
        print(f"  {yr}: {count:6,d} ({count / total_rows * 100:5.1f}%) | {bar}")

    # Tier breakdown if available
    print("\n--- Tier Breakdown ---")
    if 'tier' in df.columns:
        tier_counts = df['tier'].value_counts()
        for tier, count in tier_counts.items():
            print(f"  {tier}: {count:,} ({count / total_rows * 100:.1f}%)")
    else:
        print("  N/A (Tier labels are generated dynamically at inference time by grading logic)")

    print("=" * 60)


if __name__ == "__main__":
    main()
