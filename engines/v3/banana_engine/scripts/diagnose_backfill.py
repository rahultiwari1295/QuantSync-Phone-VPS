"""
scripts/diagnose_backfill.py — Funnel diagnostic for backfill candidates.
Traces:
  1. detect_base_at(bars, T) != None
  2. extract_features(...) != None
  3. label_event(...) in {0, 1}
"""

import os
import sys
import pandas as pd

# Add banana_engine root to sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.data_layer import load_adjusted
from core.structure import detect_base_at
from core.features import extract_features
from core.backfill import label_event

SYMBOLS_TO_TEST = ["TATAMOTORS", "RELIANCE", "20MICRONS", "TATASTEEL"]

def diagnose_symbol(sym: str):
    bars = load_adjusted(sym)
    total_bars = len(bars)
    if total_bars < 250:
        print(f"[{sym}] Total bars: {total_bars} (Insufficient history < 250)")
        return {
            "symbol": sym,
            "total_bars": total_bars,
            "bases_found": 0,
            "features_extracted": 0,
            "labels_produced": 0
        }

    count_base = 0
    count_features = 0
    count_labels = 0

    for T in range(250, total_bars - 1):
        cur_date = str(bars['date'].iloc[T])
        base = detect_base_at(bars, T, rs_ratings={})
        if base is not None:
            count_base += 1
            # Check feature extraction
            # Pass rs_rating=75 (current default)
            feats = extract_features(symbol=sym, as_of=cur_date, bars_all=bars, rs_rating=75.0)
            if feats is not None:
                count_features += 1
                entry_p = float(bars['close'].iloc[T])
                lbl = label_event(bars, T, entry_price=entry_p)
                if lbl in (0, 1):
                    count_labels += 1

    print(f"[{sym}] Total bars: {total_bars} | Step 1 (Bases): {count_base} | Step 2 (Features): {count_features} | Step 3 (Labels): {count_labels}")
    return {
        "symbol": sym,
        "total_bars": total_bars,
        "bases_found": count_base,
        "features_extracted": count_features,
        "labels_produced": count_labels
    }

def main():
    print("=== Phase 0 — Pre-flight diagnosis: Backfill Funnel Tracing ===")
    results = []
    for s in SYMBOLS_TO_TEST:
        res = diagnose_symbol(s)
        results.append(res)

    print("\n--- Summary Table ---")
    df_res = pd.DataFrame(results)
    print(df_res.to_string(index=False))

if __name__ == "__main__":
    main()
