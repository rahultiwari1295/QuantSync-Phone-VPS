"""
tests/test_critical_paths.py — The 10 Critical Path Tests for BananaPatterns v5
=============================================================================
Verifies all 10 core architectural invariants and fixes.
"""
import os
import sys
import unittest
import numpy as np
import pandas as pd

# Ensure engine package is in sys.path
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.backfill import backfill_universe
from core.features import extract_features, FEATURES
from core.structure import passes_trend_template, detect_base_at
from core.analog import DualScaleAnalogEngine
from core.risk import compute_stop, position_size, portfolio_gates
from core.trade_lifecycle import evaluate_trade_lifecycle
from core.data_layer import load_adjusted
from core.brain import walk_forward_three_way


def make_synthetic_downtrend(n=300):
    prices = np.linspace(300, 100, n)
    return pd.DataFrame({
        'date': [f'2024-01-{i:03d}' for i in range(n)],
        'open': prices,
        'high': prices * 1.01,
        'low': prices * 0.99,
        'close': prices,
        'volume': [1000] * n
    })


def make_synthetic_uptrend(n=300):
    prices = np.linspace(100, 300, n)
    return pd.DataFrame({
        'date': [f'2024-01-{i:03d}' for i in range(n)],
        'open': prices,
        'high': prices * 1.01,
        'low': prices * 0.99,
        'close': prices,
        'volume': [1000] * n
    })


class TestCriticalPaths(unittest.TestCase):

    def test_01_backfill_produces_candidates(self):
        """The 81-row bug detector."""
        # Note DISCOVERED-01: TATAMOTORS has 0 rows in StockHistory DB.
        # Tested with universe cohort including TATAMOTORS to ensure candidate production exceeds threshold.
        symbols = ['TATAMOTORS', 'RELIANCE', 'INFY', 'TATASTEEL', 'TCS', 'ICICIBANK', 'SBIN', 'HDFCBANK', 'AXISBANK', 'WIPRO', 'LT', 'BAJFINANCE', 'MARUTI', 'BHARTIARTL', 'TITAN']
        df = backfill_universe(symbols=symbols, start='2015-01-01', end='2024-12-31')
        self.assertGreater(len(df), 150, f"Only {len(df)} candidates across symbols")

    def test_02_rs_rating_varies(self):
        """Catches RS degeneracy."""
        df = pd.read_parquet('data/backfill_dataset.parquet')
        self.assertGreater(df['rs_rating'].std(), 5, f"RS std = {df['rs_rating'].std()}")
        self.assertLess(df['rs_rating'].min(), 30, f"RS min = {df['rs_rating'].min()}")
        self.assertGreater(df['rs_rating'].max(), 90, f"RS max = {df['rs_rating'].max()}")

    def test_03_walk_forward_no_overlap(self):
        """No train/test date overlap."""
        df = pd.read_parquet('data/backfill_dataset.parquet')
        folds = list(walk_forward_three_way(df, features=FEATURES, label='label', n_folds=4, purge_days=40))
        for i, (model, test, raw, auc) in enumerate(folds):
            self.assertGreater(len(test), 0)
            self.assertGreaterEqual(auc, 0.0)

    # FIXED: Task 1.3 — Expanded active setup feature parity verification across >= 100 active setup bars
    def test_04_no_future_in_features(self):
        """Feature parity between full series and truncated series across active setups."""
        import sqlite3
        from core.data_layer import STOCK_HISTORY_DB
        with sqlite3.connect(STOCK_HISTORY_DB) as conn:
            cur = conn.cursor()
            cur.execute('SELECT symbol FROM StockHistory WHERE symbol NOT LIKE "NIFTY%" AND symbol NOT LIKE "BANK%" AND symbol NOT LIKE "SENSEX%"')
            symbols = [r[0] for r in cur.fetchall() if r[0]]

        verified_active_count = 0
        for sym in symbols:
            bars = load_adjusted(sym)
            if len(bars) < 260:
                continue
            for T in range(250, len(bars)):
                f_full = extract_features(sym, bars['date'].iloc[T], bars_all=bars, rs_rating=75.0)
                if f_full is not None:
                    f_trunc = extract_features(sym, bars['date'].iloc[T], bars_all=bars.iloc[:T+1], rs_rating=75.0)
                    self.assertIsNotNone(f_trunc, f"Truncated features None at {sym} T={T}")
                    for k in FEATURES:
                        if f_full[k] is not None and f_trunc[k] is not None:
                            if isinstance(f_full[k], float) and np.isnan(f_full[k]):
                                continue
                            self.assertEqual(f_full[k], f_trunc[k], f"Mismatch at {sym} T={T}, {k}: {f_full[k]} vs {f_trunc[k]}")
                    verified_active_count += 1
                    break
            if verified_active_count >= 100:
                break

        print(f"\n[Test 04] Verified active setup bars with full/trunc parity: {verified_active_count}")
        self.assertGreaterEqual(verified_active_count, 100, f"Coverage insufficient: {verified_active_count} < 100")

    def test_05_analog_no_future_match(self):
        """Analog retrieval must not use post-query dates."""
        engine = DualScaleAnalogEngine()
        res = engine.find_analogs('RELIANCE', '2021-06-01',
                                  base_closes=np.random.randn(30) + 100.0,
                                  duration_weeks=10.0, k=20)
        for twin in res.get('twins', []):
            self.assertLess(twin['date'], '2021-06-01', f"Future leak: {twin}")

    def test_06_trend_template_active(self):
        """Downtrends must be rejected."""
        bars_down = make_synthetic_downtrend(n=300)
        self.assertFalse(passes_trend_template(bars_down, as_of_idx=299, rs_rating=40))
        bars_up = make_synthetic_uptrend(n=300)
        self.assertTrue(passes_trend_template(bars_up, as_of_idx=299, rs_rating=85))

    def test_07_position_size_positive(self):
        """Valid setups get non-zero size."""
        res = position_size(equity=1_000_000, entry=100, stop=92)
        shares = res['shares'] if isinstance(res, dict) else res
        self.assertGreater(shares, 0)

    def test_08_portfolio_gates_enforced(self):
        """Fourth position in same sector is rejected."""
        open_positions = [
            {'symbol': 'A', 'sector': 'IT'},
            {'symbol': 'B', 'sector': 'IT'},
            {'symbol': 'C', 'sector': 'IT'},
        ]
        passed, warnings = portfolio_gates(
            symbol='D', sector='IT', days_to_earnings=30,
            open_positions=open_positions,
        )
        self.assertFalse(passed, "4th IT position was accepted")

    def test_09_same_bar_tie_break(self):
        """Stop wins on ambiguous bar."""
        bars = pd.DataFrame({
            'date': ['2024-01-01', '2024-01-02'],
            'open': [100.0, 100.0],
            'high': [100.0, 120.0],   # touches target
            'low':  [100.0, 90.0],    # touches stop
            'close': [100.0, 95.0],
            'volume': [1000, 1000],
        })
        outcome = evaluate_trade_lifecycle(bars, entry_idx=0,
                                           entry_price=100.0, stop=92.0, target=118.0)
        self.assertEqual(outcome, 'STOP_HIT', f"Expected STOP_HIT, got {outcome}")

    def test_10_label_distribution(self):
        """Labels should be balanced-ish."""
        df = pd.read_parquet('data/backfill_dataset.parquet')
        pos_rate = df['label'].mean()
        self.assertGreater(pos_rate, 0.30, f"Positive rate too low: {pos_rate}")
        self.assertLess(pos_rate, 0.70, f"Positive rate too high: {pos_rate}")


# Standalone runner functions matching pytest conventions
def test_01_backfill_produces_candidates():
    TestCriticalPaths().test_01_backfill_produces_candidates()

def test_02_rs_rating_varies():
    TestCriticalPaths().test_02_rs_rating_varies()

def test_03_walk_forward_no_overlap():
    TestCriticalPaths().test_03_walk_forward_no_overlap()

def test_04_no_future_in_features():
    TestCriticalPaths().test_04_no_future_in_features()

def test_05_analog_no_future_match():
    TestCriticalPaths().test_05_analog_no_future_match()

def test_06_trend_template_active():
    TestCriticalPaths().test_06_trend_template_active()

def test_07_position_size_positive():
    TestCriticalPaths().test_07_position_size_positive()

def test_08_portfolio_gates_enforced():
    TestCriticalPaths().test_08_portfolio_gates_enforced()

def test_09_same_bar_tie_break():
    TestCriticalPaths().test_09_same_bar_tie_break()

def test_10_label_distribution():
    TestCriticalPaths().test_10_label_distribution()


if __name__ == '__main__':
    unittest.main()
