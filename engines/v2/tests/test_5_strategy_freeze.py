"""
vps_data/engines/v2/tests/test_5_strategy_freeze.py
==================================================
PRODUCTION FREEZE TEST: 5 STRATEGIES + PORTFOLIO ARBITER (V2 ENGINE)

Enforces the zero-tolerance Production Hash Lock across migrated V2 components:
  1. Strategy 1: config/strategy_1_rs_leader_best_config.json
  2. Strategy 2: config/strategy_2_apex_base_best_config.json
  3. Strategy 3: config/strategy_3_power_earnings_gap_best_config.json
  4. Strategy 4: config/strategy_4_high_tight_flag_best_config.json
  5. Strategy 5: config/strategy_5_stage2_base_best_config.json
  6. Master:     config/master_portfolio_strategies_config.json
  7. Arbiter:    decision/arbiter.py

FAILS WITH HARD EXCEPTION IF ANY BYTE IN ANY LOCKED FILE CHANGES.
NO EXCEPTIONS.
"""

import os
import sys
import json
import hashlib
import unittest

V2_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
V11_ROOT = V2_ROOT  # Alias
if V2_ROOT not in sys.path:
    sys.path.insert(0, V2_ROOT)

LOCK_FILE = os.path.join(V2_ROOT, "config", "production_hash_lock.json")



class FreezeViolationError(Exception):
    """Raised when any frozen strategy config or the arbiter is modified."""
    pass


def test_production_hash_lock_file_exists():
    """Verify that the production lock file exists and is valid JSON."""
    assert os.path.exists(LOCK_FILE), f"Missing production lock file at {LOCK_FILE}"
    with open(LOCK_FILE, "r") as f:
        data = json.load(f)
    assert data.get("lock_name") == "QuantSync V11 Production 5-Strategy + Arbiter Hash Lock"
    assert data.get("hash_algorithm") == "SHA-256"
    assert "locked_files" in data
    assert len(data["locked_files"]) == 7, f"Expected 7 locked files, found {len(data['locked_files'])}"


def test_zero_tolerance_config_and_arbiter_freeze():
    """
    CRITICAL FREEZE INVARIANT:
    Computes SHA-256 of all 5 strategy configs, the master portfolio config,
    and the portfolio arbiter. Fails if any file has been modified by even 1 bit.
    """
    with open(LOCK_FILE, "r") as f:
        lock_data = json.load(f)

    locked_files = lock_data["locked_files"]
    violations = []

    for rel_path, meta in locked_files.items():
        # Map original rel_path 'quantsync_v11/<subpath>' to '<subpath>' in V11_ROOT
        subpath = rel_path.split("/", 1)[1] if rel_path.startswith("quantsync_v11/") else rel_path
        abs_path = os.path.join(V11_ROOT, subpath)

        if not os.path.exists(abs_path):
            violations.append(f"MISSING FILE: {subpath} does not exist in {V11_ROOT}!")
            continue

        with open(abs_path, "rb") as f:
            content = f.read()

        actual_sha256 = hashlib.sha256(content).hexdigest()
        expected_sha256 = meta["sha256"]

        if actual_sha256 != expected_sha256:
            violations.append(
                f"FREEZE BREACH in {subpath}!\n"
                f"  Expected SHA-256: {expected_sha256}\n"
                f"  Actual SHA-256:   {actual_sha256}\n"
                f"  File was tampered with or modified after freeze."
            )

    if violations:
        msg = "\n" + "=" * 80 + "\n"
        msg += "PRODUCTION FREEZE VIOLATION DETECTED IN V11! NO MODIFICATIONS ALLOWED!\n"
        msg += "=" * 80 + "\n"
        msg += "\n".join(violations) + "\n"
        msg += "=" * 80
        raise FreezeViolationError(msg)


def test_strategy_configs_valid_schema_and_integrity():
    """Verify all 5 strategy config files parse correctly and contain required production keys."""
    strategy_files = [
        "config/strategy_1_rs_leader_best_config.json",
        "config/strategy_2_apex_base_best_config.json",
        "config/strategy_3_power_earnings_gap_best_config.json",
        "config/strategy_4_high_tight_flag_best_config.json",
        "config/strategy_5_stage2_base_best_config.json",
    ]

    expected_ids = [
        "RS_LEADER_REFINED",
        "APEX_BASE_BREAKOUT",
        "POWER_EARNINGS_GAP",
        "HIGH_TIGHT_FLAG",
        "STAGE2_BASE_BREAKOUT",
    ]

    for subpath, exp_id in zip(strategy_files, expected_ids):
        abs_path = os.path.join(V11_ROOT, subpath)
        with open(abs_path, "r") as f:
            cfg = json.load(f)

        assert cfg.get("strategy_id") == exp_id, f"Invalid strategy_id in {subpath}"
        assert "exit_architecture" in cfg, f"Missing exit_architecture in {subpath}"
        assert "core_screening" in cfg, f"Missing core_screening in {subpath}"
        assert "empirical_benchmarks" in cfg, f"Missing empirical_benchmarks in {subpath}"


def test_master_portfolio_config_schema():
    """Verify the master portfolio config references all 5 strategies correctly."""
    master_path = os.path.join(V11_ROOT, "config", "master_portfolio_strategies_config.json")
    with open(master_path, "r") as f:
        m_cfg = json.load(f)

    assert m_cfg["status"] == "FROZEN_AND_LOCKED"
    assert len(m_cfg["strategies"]) == 5
    assert len(m_cfg["strategy_priority_order"]) == 5
    assert m_cfg["portfolio_benchmarks"]["profit_factor"] == 1.91
    assert m_cfg["portfolio_benchmarks"]["max_drawdown_pct"] == 11.8
    assert m_cfg["portfolio_benchmarks"]["total_deduplicated_trades"] == 410


def test_arbiter_execution_integrity():
    """Verify the frozen arbiter executes deterministic risk gating without regression."""
    from decision.arbiter import PortfolioState, TradeCandidate, arbiter_decide

    port = PortfolioState(open_positions=5, nav=300000.0, current_drawdown_pct=0.0)
    cand = TradeCandidate(
        symbol="RELIANCE",
        sector="ENERGY",
        trap_score=0.20,
        est_mfe_r=2.5,
        max_corr_to_book=0.30,
        pattern_type="APEX_BASE",
    )

    # 1. Standard approval
    dec, size, reason = arbiter_decide(cand, "BULL_CALM", port, max_positions=25)
    assert dec == "TAKE"
    assert size > 0.0
    assert reason == "ev_approved"

    # 2. Capacity block
    port_full = PortfolioState(open_positions=25, nav=300000.0)
    dec_cap, size_cap, reason_cap = arbiter_decide(cand, "BULL_CALM", port_full, max_positions=25)
    assert dec_cap == "SKIP"
    assert size_cap == 0.0
    assert reason_cap == "capacity"

    # 3. Drawdown circuit breaker
    port_halted = PortfolioState(open_positions=2, nav=300000.0, current_drawdown_pct=0.09)
    dec_halt, size_halt, reason_halt = arbiter_decide(cand, "BULL_CALM", port_halted, dd_halt_at=0.08)
    assert dec_halt == "SKIP"
    assert size_halt == 0.0
    assert reason_halt == "drawdown_halt"


class TestProduction5StrategyFreeze(unittest.TestCase):
    def test_production_hash_lock_file_exists(self):
        test_production_hash_lock_file_exists()

    def test_zero_tolerance_config_and_arbiter_freeze(self):
        test_zero_tolerance_config_and_arbiter_freeze()

    def test_strategy_configs_valid_schema_and_integrity(self):
        test_strategy_configs_valid_schema_and_integrity()

    def test_master_portfolio_config_schema(self):
        test_master_portfolio_config_schema()

    def test_arbiter_execution_integrity(self):
        test_arbiter_execution_integrity()


if __name__ == "__main__":
    print("=" * 80)
    print("RUNNING V11 ENGINE ZERO-TOLERANCE FREEZE TEST...")
    print("=" * 80)

    test_production_hash_lock_file_exists()
    print("✓ [PASS] Production Hash Lock file exists and is valid JSON")

    test_zero_tolerance_config_and_arbiter_freeze()
    print("✓ [PASS] All 5 Strategy Configs + Master Config + Arbiter MATCH EXACT SHA-256 HASHES")

    test_strategy_configs_valid_schema_and_integrity()
    print("✓ [PASS] All 5 Strategy Config schemas and production parameters verified")

    test_master_portfolio_config_schema()
    print("✓ [PASS] Master 5-Strategy Portfolio config schema verified")

    test_arbiter_execution_integrity()
    print("✓ [PASS] Arbiter deterministic execution, capacity gating, and circuit breakers verified")

    print("=" * 80)
    print("ALL V11 PRODUCTION FREEZE TESTS PASSED. 5 STRATEGIES + ARBITER ARE FULLY LOCKED.")
    print("=" * 80)
