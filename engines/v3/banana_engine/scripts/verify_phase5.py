"""
scripts/verify_phase5.py — Verification for Phase 5 Buy/Sell Engine Correctness
"""
import os
import sys
import sqlite3
import pandas as pd
import numpy as np

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.trade_lifecycle import evaluate_trade_lifecycle
from core.regret import RegretLedger, REGRET_DB
from core.data_layer import STOCK_HISTORY_DB

def test_fix_5_1_same_bar_tie_break():
    print("\n--- Testing Fix 5.1: Same-bar tie break ---")
    bars = pd.DataFrame({
        'date': ['2024-01-01', '2024-01-02'],
        'open': [100.0, 100.0],
        'high': [100.0, 120.0],   # touches target (118)
        'low':  [100.0, 90.0],    # touches stop (92)
        'close': [100.0, 95.0],
        'volume': [1000, 1000],
    })
    outcome = evaluate_trade_lifecycle(
        bars, entry_idx=0, entry_price=100.0, stop=92.0, target=118.0
    )
    print(f"Outcome for ambiguous bar: {outcome}")
    assert outcome == 'STOP_HIT', f"Expected STOP_HIT, got {outcome}"
    print("Fix 5.1 Verified: Ambiguous same-bar exit favors STOP_HIT.")

def test_fix_5_2_trailing_stop():
    print("\n--- Testing Fix 5.2: Trailing stop logic ---")
    # Synthetic trade: entry at 100 on day 0, stop at 93, target at 130
    # Day 1: 102
    # Day 2: 105
    # Day 3: high 115 (+15%), closes at 114 -> moves stop to breakeven (100)
    # Day 4-9: hovers around 110-114
    # Day 10: low drops to 98 (-10% from peak, below breakeven 100, but above initial stop 93)
    dates = [f"2024-01-{i:02d}" for i in range(1, 15)]
    bars = pd.DataFrame({
        'date': dates,
        'open': [100.0] * 14,
        'high': [100.0, 102.0, 105.0, 115.0, 114.0, 113.0, 112.0, 111.0, 110.0, 109.0, 108.0, 108.0, 108.0, 108.0],
        'low':  [100.0, 99.0, 101.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 98.0, 97.0, 97.0, 97.0, 97.0],
        'close':[100.0, 101.0, 104.0, 114.0, 113.0, 112.0, 111.0, 110.0, 109.0, 99.0, 98.0, 98.0, 98.0, 98.0],
        'volume':[1000] * 14
    })
    outcome = evaluate_trade_lifecycle(
        bars, entry_idx=0, entry_price=100.0, stop=93.0, target=130.0
    )
    print(f"Outcome for +15% then retrace: {outcome}")
    assert outcome == 'TRAILING_STOP', f"Expected TRAILING_STOP, got {outcome}"
    print("Fix 5.2 Verified: Trailing stop triggered after +15% advance and retrace.")

def test_fix_5_3_delisted_symbol():
    print("\n--- Testing Fix 5.3: Delisted symbol in regret ledger ---")
    ledger = RegretLedger()
    test_sym = "TEST_DELISTED_SYM"
    
    # Clean up any previous test entries
    with sqlite3.connect(REGRET_DB) as conn:
        conn.execute("DELETE FROM regret_entries WHERE symbol = ?", (test_sym,))
    with sqlite3.connect(STOCK_HISTORY_DB) as conn:
        conn.execute("DELETE FROM StockHistory WHERE symbol = ?", (test_sym,))

    # Insert 10 bars ended > 30 days ago (e.g. in 2024) into StockHistory
    import json
    fake_candles = [
        {'t': f"2024-01-{i:02d}", 'o': 100.0, 'h': 105.0, 'l': 95.0, 'c': 90.0, 'v': 1000}
        for i in range(1, 11)
    ]
    with sqlite3.connect(STOCK_HISTORY_DB) as conn:
        conn.execute(
            "INSERT INTO StockHistory (token, symbol, rawCandles, candleCount, isV3Eligible) VALUES (?, ?, ?, ?, ?)",
            ("999999", test_sym, json.dumps(fake_candles), len(fake_candles), 1)
        )
    
    # Record a setup in regret ledger at bar 0
    setup_id = ledger.log_setup(
        symbol=test_sym,
        breakout_date="2024-01-01",
        decision="TAKEN",
        score=0.75,
        entry_price=100.0,
        stop_price=92.0,
        target_price=116.0
    )
    
    # Run resolution
    resolved_count = ledger.resolve_pending_outcomes(horizon=20)
    print(f"Resolved count: {resolved_count}")
    
    with sqlite3.connect(REGRET_DB) as conn:
        row = conn.execute("SELECT resolved, outcome, realized_r FROM regret_entries WHERE setup_id = ?", (setup_id,)).fetchone()
    
    print(f"Entry status after resolution: resolved={row[0]}, outcome={row[1]}, realized_r={row[2]}")
    assert row[0] == 1, "Expected entry to be resolved"
    assert row[1] == 'DELISTED', f"Expected outcome DELISTED, got {row[1]}"
    print("Fix 5.3 Verified: Delisted symbol resolved as DELISTED terminal loss.")
    
    # Clean up
    with sqlite3.connect(REGRET_DB) as conn:
        conn.execute("DELETE FROM regret_entries WHERE symbol = ?", (test_sym,))
    with sqlite3.connect(STOCK_HISTORY_DB) as conn:
        conn.execute("DELETE FROM StockHistory WHERE symbol = ?", (test_sym,))

if __name__ == '__main__':
    test_fix_5_1_same_bar_tie_break()
    test_fix_5_2_trailing_stop()
    test_fix_5_3_delisted_symbol()
    print("\nALL PHASE 5 VERIFICATIONS PASSED!")
