"""
vps_data/engines/v2/tests/test_v2_live_functionality.py
========================================================
Tests the 5 core fixes implemented in QuantSync-V2:
1. Zero external dependencies (No banana_engine, no QuantSync_Lines)
2. Live scanner candidate generation from stock_history.db with Zero Lookahead
3. Strict 1-position-per-stock deduplication (No overwriting)
4. Dynamic target_price & stop_loss checking in check_exits()
5. Dynamic market regime detection & strategy-calibrated Arbiter inputs
"""

import os
import sys
import unittest

V2_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VPS_DATA = os.path.abspath(os.path.join(V2_ROOT, "..", ".."))

if VPS_DATA not in sys.path:
    sys.path.insert(0, VPS_DATA)
if V2_ROOT not in sys.path:
    sys.path.insert(0, V2_ROOT)

from engines.v2.engine import QuantSyncV2Engine
from engines.v2.live_scanner import V11LiveScanner


class TestV2LiveFunctionality(unittest.TestCase):

    def setUp(self):
        self.engine = QuantSyncV2Engine()

    def test_zero_external_dependencies(self):
        """Verify no files in v2 reference banana_engine or QuantSync_Lines."""
        for root, _, files in os.walk(V2_ROOT):
            for f in files:
                if f.endswith(".py") and f != "test_v2_live_functionality.py":
                    p = os.path.join(root, f)
                    with open(p, "r", encoding="utf-8") as py_file:
                        content = py_file.read()
                        self.assertNotIn("banana_engine", content, f"Found banana_engine reference in {p}")
                        self.assertNotIn("QuantSync_Lines", content, f"Found QuantSync_Lines reference in {p}")

    def test_dynamic_market_regime(self):
        """Verify market regime is dynamically classified from NIFTY candles."""
        scanner = self.engine.live_scanner
        regime_info = scanner.get_market_regime("2026-09-16")
        self.assertIn("regime", regime_info)
        self.assertIn(regime_info["regime"], ["BULL_CALM", "BULL_VOLATILE", "BEAR_CALM", "BEAR_VOLATILE"])
        self.assertGreater(regime_info["close"], 0.0)
        self.assertGreater(regime_info["ema200"], 0.0)

    def test_live_candidate_discovery(self):
        """Verify live scanner discovers valid breakout setups with target prices."""
        scanner = self.engine.live_scanner
        cands = scanner.scan(target_date="2026-09-08", min_probability=0.50)
        self.assertGreater(len(cands), 0, "Expected at least 1 candidate on 2026-09-08")

        first = cands[0]
        self.assertIn("symbol", first)
        self.assertIn("strategy_id", first)
        self.assertIn("entry_price", first)
        self.assertIn("stop_price", first)
        self.assertIn("target_price", first)
        self.assertGreater(first["target_price"], first["entry_price"])
        self.assertLess(first["stop_price"], first["entry_price"])
        self.assertGreaterEqual(first["score"], 0.50)

    def test_strict_deduplication(self):
        """Verify that an active position is never overwritten by a duplicate candidate."""
        sym = "TEST_STOCK"
        fake_pos = {
            "trade_id": "V11_TRD_TEST_01",
            "engine_version": "v11",
            "symbol": sym,
            "strategy_id": "APEX_BASE",
            "sector": "Industrial Manufacturing",
            "entry_date": "2026-09-08",
            "exit_date": "",
            "entry_price": 100.0,
            "exit_price": 112.0,
            "current_stop": 95.0,
            "target_price": 112.0,
            "shares": 100,
            "invested_capital": 10000.0,
            "entry_friction": 15.0,
        }
        self.engine.active_positions[sym] = fake_pos

        # Inject fake candidate for the same symbol
        duplicate_cand = {
            "symbol": sym,
            "strategy_id": "STAGE2_BASE",
            "entry_price": 105.0,
            "stop_price": 98.0,
            "target_price": 120.0,
            "score": 0.85,
            "sector": "Industrial Manufacturing",
        }
        self.engine._scored_events_by_date["2026-09-09"] = [duplicate_cand]

        # Step through 2026-09-09
        self.engine.step("2026-09-09", is_live=False)

        # Verify active position was NOT overwritten
        self.assertEqual(self.engine.active_positions[sym]["entry_price"], 100.0)
        self.assertEqual(self.engine.active_positions[sym]["trade_id"], "V11_TRD_TEST_01")
        self.assertEqual(self.engine.active_positions[sym]["strategy_id"], "APEX_BASE")

    def test_check_exits_stop_and_target(self):
        """Verify check_exits triggers STOP_HIT when LTP <= stop, and TARGET_HIT when LTP >= target."""
        self.engine.active_positions.clear()

        pos_stop = {
            "trade_id": "TRD_STOP",
            "engine_version": "v11",
            "symbol": "STOCK_A",
            "strategy_id": "RS_LEADER",
            "sector": "Information Technology",
            "entry_date": "2026-09-01",
            "entry_price": 200.0,
            "current_stop": 188.0,
            "target_price": 230.0,
            "shares": 50,
            "entry_friction": 15.0,
        }
        pos_target = {
            "trade_id": "TRD_TARGET",
            "engine_version": "v11",
            "symbol": "STOCK_B",
            "strategy_id": "APEX_BASE",
            "sector": "Financial Services",
            "entry_date": "2026-09-01",
            "entry_price": 100.0,
            "current_stop": 95.0,
            "target_price": 115.0,
            "shares": 100,
            "entry_friction": 15.0,
        }
        self.engine.active_positions["STOCK_A"] = pos_stop
        self.engine.active_positions["STOCK_B"] = pos_target

        ltp_map = {
            "STOCK_A": 187.50,  # Below stop 188.0
            "STOCK_B": 116.00,  # Above target 115.0
        }

        exits = self.engine.check_exits(ltp_map)
        self.assertEqual(len(exits), 2)

        exit_reasons = {e["symbol"]: e["exit_reason"] for e in exits}
        self.assertEqual(exit_reasons["STOCK_A"], "STOP_HIT")
        self.assertEqual(exit_reasons["STOCK_B"], "TARGET_HIT")

        # Verify active positions were removed
        self.assertNotIn("STOCK_A", self.engine.active_positions)
        self.assertNotIn("STOCK_B", self.engine.active_positions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
