"""
engines/v3/tests/test_quality_guards_freeze.py — Integrity Guard for September 18th Freeze
==========================================================================================
Baseline: state_20260918_v6_quality_guards
Verifies:
  1. All 7 banana_engine core modules match locked SHA-256 checksums bit-for-bit.
  2. RS Leader Refined strategy matches locked SHA-256 checksum.
  3. vps_data_live/config.json contains the exact frozen quality guard thresholds.
  4. V3 engine correctly enforces 0.85 CIR, 2.50x Volume, and TOP_2_CONVICTION_RANKED selection.
"""

import os
import sys
import json
import hashlib
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_V3_DIR = os.path.dirname(_TESTS_DIR)
_PROD_ROOT = os.path.dirname(os.path.dirname(_V3_DIR))

if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)
if _V3_DIR not in sys.path:
    sys.path.insert(0, _V3_DIR)


LOCKED_HASHES = {
    "banana_engine/core/champion_patterns.py": "9c9847485ddda50ef71e7ed6cfd9bce07b5a63d3199a51476d1d525a5e05312e",
    "banana_engine/core/adaptive_exits.py": "e74453db1b0e23591985886c56f43d886a757372f183dbc70ca9d5b29175d756",
    "banana_engine/core/smart_money.py": "a37a7d33002b502fe243a091a2669be4fe6dfabf51c3c06b3f0c28e3fe801f8f",
    "banana_engine/core/cascade.py": "3bedf590e487b5120b06f2ac6ca56145c0aaa65b86e1016fdf0e1536abeb0519",
    "banana_engine/core/failure_anatomy.py": "b1a4f797adb2947603f220eb2f9311f6241ffb0cafd8dc979e5daf988f120616",
    "banana_engine/core/enhanced_features.py": "ebfffd8cc2741cdac8761a4bbf0eec1bd12651663574ec2ea88215dddf2c7ae3",
    "banana_engine/core/enhanced_brain.py": "bdec87ec0945d37e2f3c52b644486804a16c2dfc0be7e3ecb4fac83002374cf7",
    "strategies/rs_leader_refined_live.py": "33e36ba33772cc652979cb72bdf89b54382974fbe0b59de2d800d20429c8a3af",
}

EXPECTED_QUALITY_GUARDS = {
    "min_volume_ratio": 2.50,
    "min_close_in_range": 0.85,
    "max_upper_wick_pct": 0.10,
    "min_bar_return_pct": 4.0,
    "min_chase_pct": 1.5,
    "max_chase_pct": 8.0,
    "min_prior_runup_60_pct": 15.0,
    "max_ath_dist_pct": 20.0,
    "max_dist_sma200_pct": 30.0,
    "min_stop_distance_pct": 8.0,
    "daily_selection_mode": "TOP_2_CONVICTION_RANKED",
}


class TestQualityGuardsFreeze(unittest.TestCase):

    def test_bit_for_bit_file_hashes(self):
        """Verifies that all 8 snapshot files match their locked SHA-256 hashes."""
        for rel_path, exp_hash in LOCKED_HASHES.items():
            abs_path = os.path.join(_V3_DIR, rel_path)
            self.assertTrue(os.path.exists(abs_path), f"Missing frozen file: {abs_path}")
            with open(abs_path, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(
                actual_hash, exp_hash,
                f"INTEGRITY BREACH in {rel_path}!\nExpected: {exp_hash}\nActual:   {actual_hash}"
            )
            print(f"  [Freeze Match Verified] {rel_path} -> {actual_hash[:16]}...")

    def test_config_quality_guards(self):
        """Verifies that vps_data_live/config.json has the exact frozen quality guards."""
        cfg_path = os.path.join(_PROD_ROOT, "config.json")
        self.assertTrue(os.path.exists(cfg_path), f"Missing config.json at {cfg_path}")

        with open(cfg_path, "r") as f:
            cfg = json.load(f)

        v3_cfg = cfg.get("engines", {}).get("v3", {})
        self.assertTrue(v3_cfg.get("enabled"), "V3 engine must be enabled")

        qg = v3_cfg.get("quality_guards", {})
        for k, expected_val in EXPECTED_QUALITY_GUARDS.items():
            self.assertIn(k, qg, f"Missing quality guard: {k}")
            self.assertEqual(
                qg[k], expected_val,
                f"Mismatch in quality guard {k}: expected {expected_val}, got {qg[k]}"
            )

        ds = v3_cfg.get("daily_selection", {})
        self.assertEqual(ds.get("max_takes"), 2, "Daily selection max_takes must be 2")
        print("  [Config Verified] Quality guards match frozen baseline specification")

    def test_v3_engine_instantiation(self):
        """Verifies that V3TradingEngine loads quality guards and ML brain correctly."""
        from engines.v3.engine import V3TradingEngine
        engine = V3TradingEngine()
        self.assertEqual(engine.quality_guards.get("min_close_in_range"), 0.85)
        self.assertEqual(engine.quality_guards.get("min_volume_ratio"), 2.50)
        self.assertEqual(engine.quality_guards.get("max_upper_wick_pct"), 0.10)
        self.assertEqual(engine.quality_guards.get("daily_selection_mode"), "TOP_2_CONVICTION_RANKED")
        self.assertEqual(engine.daily_selection.get("max_takes"), 2)
        print("  [Engine Verified] V3TradingEngine instantiated with frozen Quality Guards")


if __name__ == "__main__":
    unittest.main()
