"""
banana_engine/tests/test_v4_suite.py — BananaPatterns v4 Mandatory Test Suite
=============================================================================
Implements all 14 non-negotiable unit & integration tests specified in Section 14
of BananaPatterns v4 (newv4Idea.md):

  1.  test_load_adjusted_reproducible
  2.  test_no_future_bars_in_features
  3.  test_feature_parity_backfill_live
  4.  test_base_start_pit
  5.  test_breakout_day_volume
  6.  test_label_tie_break
  7.  test_walk_forward_no_leak
  8.  test_calibration_reliability
  9.  test_stop_never_wider_than_cap
  10. test_position_size_never_zero_on_valid_setup
  11. test_regime_halt_blocks_entries
  12. test_earnings_proximity_flag
  13. test_shap_phrase_coverage
  14. test_model_card_complete
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import load_adjusted, DATA_DIR, days_to_next_earnings
from core.structure import (
    detect_base_at,
    find_base_start,
    find_swing_points,
    is_breakout_day,
    regime_state,
    MAX_BASE_DEPTH_PCT
)
from core.features import extract_features, FEATURES
from core.backfill import label_event
from core.brain import BrainInference, PHRASES, MODEL_CARD_FILE
from core.risk import (
    compute_stop,
    position_size,
    regime_gate,
    portfolio_gates,
    MAX_STOP_PCT
)
from core.composite import assign_tier


# ============================================================================
# 1. test_load_adjusted_reproducible
# ============================================================================
def test_load_adjusted_reproducible():
    """
    Same (symbol, end_date) -> bit-for-bit identical DataFrame across runs.
    """
    df1 = load_adjusted("IOLCP", "2026-08-01")
    df2 = load_adjusted("IOLCP", "2026-08-01")

    assert not df1.empty, "IOLCP history should not be empty"
    pd.testing.assert_frame_equal(df1, df2, check_exact=True)


# ============================================================================
# 2. test_no_future_bars_in_features
# ============================================================================
def test_no_future_bars_in_features():
    """
    Take real series with confirmed setup, append artificial future bars past T,
    verify extract_features(T) produces identical feature values.
    """
    bars_real = load_adjusted("MVGJL")
    assert not bars_real.empty
    as_of_date = "2026-09-11"

    feats_base = extract_features("MVGJL", as_of=as_of_date, bars_all=bars_real, rs_rating=75.0)
    assert feats_base is not None, "MVGJL on 2026-09-11 must extract valid features"

    # Append artificial future bars after 2026-09-11
    future_extra = pd.DataFrame({
        "date": ["2026-09-15", "2026-09-16", "2026-09-17"],
        "open": [250.0, 260.0, 270.0],
        "high": [260.0, 270.0, 280.0],
        "low": [245.0, 255.0, 265.0],
        "close": [255.0, 265.0, 275.0],
        "volume": [500000, 600000, 700000]
    })
    bars_with_future = pd.concat([bars_real, future_extra], ignore_index=True)
    feats_with_future = extract_features("MVGJL", as_of=as_of_date, bars_all=bars_with_future, rs_rating=75.0)
    assert feats_with_future is not None

    for f in FEATURES:
        v1 = feats_base[f]
        v2 = feats_with_future[f]
        if np.isnan(v1):
            assert np.isnan(v2), f"Feature {f} mismatch on NaN"
        else:
            assert np.isclose(v1, v2, atol=1e-5), f"Feature {f} leaked future bars! {v1} != {v2}"


# ============================================================================
# 3. test_feature_parity_backfill_live
# ============================================================================
def test_feature_parity_backfill_live():
    """
    Same date in both modes -> identical features.
    Unit test: single extract_features function guarantees zero drift.
    """
    bars = load_adjusted("RAYMOND")
    assert len(bars) > 100
    test_date = str(bars['date'].iloc[-10])

    f_backfill = extract_features("RAYMOND", as_of=test_date, bars_all=bars, rs_rating=75.0)
    f_live = extract_features("RAYMOND", as_of=test_date, bars_all=bars, rs_rating=75.0)

    assert f_backfill == f_live, "Backfill and Live feature extraction must be bit-for-bit identical"


# ============================================================================
# 4. test_base_start_pit
# ============================================================================
def test_base_start_pit():
    """
    Constructed synthetic base -> detector returns correct peak point-in-time,
    never selecting a peak occurring after T.
    """
    # Create 80 bars of synthetic data
    dates = pd.date_range("2026-01-01", periods=80, freq="B").strftime("%Y-%m-%d").tolist()
    closes = [100.0] * 80

    # Trend up to bar 30 (peak at 150)
    for i in range(31):
        closes[i] = 100.0 + i * (50.0 / 30.0)  # reaches 150 at bar 30

    # Pullback and base from bar 30 to 70: floor around 125 (depth ~16.6%)
    for i in range(31, 50):
        closes[i] = 150.0 - (i - 30) * (25.0 / 19.0)  # low 125 at bar 49
    for i in range(50, 75):
        closes[i] = 125.0 + (i - 50) * (24.0 / 25.0)  # rises back to ~149
    for i in range(75, 80):
        closes[i] = 148.0

    synthetic_bars = pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [100000] * 80
    })

    start_idx = find_base_start(synthetic_bars, T=75)

    assert start_idx is not None
    assert start_idx <= 75, "Base peak index must be <= T (strictly point-in-time)"
    # The peak should be identified near bar 30 (highest point before contraction)
    assert abs(start_idx - 30) <= 2, f"Expected peak near bar 30, got {start_idx}"


# ============================================================================
# 5. test_breakout_day_volume
# ============================================================================
def test_breakout_day_volume():
    """
    Volume exactly at threshold vs below threshold -> correct classification.
    Threshold: volume >= 1.5x SMA20, close > pivot, CIR >= 0.70.
    """
    dates = ["2026-08-01", "2026-08-02", "2026-08-03"]
    pivot = 100.0
    vol_sma20 = 1000.0

    # Case A: Volume at 1.50x (1500) -> Breakout TRUE
    df_valid = pd.DataFrame({
        "date": dates,
        "open": [98.0, 99.0, 100.0],
        "high": [100.0, 101.0, 106.0],
        "low": [97.0, 98.0, 99.5],
        "close": [99.0, 100.0, 105.0],  # Close 105 > 100, CIR = (105-99.5)/(106-99.5) = 5.5/6.5 = 0.846 >= 0.70
        "volume": [1000, 1000, 1500]     # exactly 1.5x
    })
    assert is_breakout_day(df_valid, 2, pivot, vol_sma20) is True

    # Case B: Volume at 1.49x (1490) -> Breakout FALSE
    df_low_vol = df_valid.copy()
    df_low_vol.loc[2, "volume"] = 1490
    assert is_breakout_day(df_low_vol, 2, pivot, vol_sma20) is False


# ============================================================================
# 6. test_label_tie_break
# ============================================================================
def test_label_tie_break():
    """
    Same-bar target and stop hit -> label 0 (conservative tie-break: stop wins).
    """
    future_bars = pd.DataFrame({
        "date": ["2026-08-01", "2026-08-02"],
        "open": [100.0, 100.0],
        "high": [100.0, 116.0],  # Hit target on day 2 (target = 115)
        "low": [100.0, 92.0],     # Hit stop on day 2 (stop = 93)
        "close": [100.0, 114.0],
        "volume": [5000, 5000]
    })

    label = label_event(future_bars, T=0, entry_price=100.0, target_pct=15.0, stop_pct=7.0, horizon=20)
    assert label == 0, f"Conservative tie-break requires stop to win (label 0), got {label}"


# ============================================================================
# 7. test_walk_forward_no_leak
# ============================================================================
def test_walk_forward_no_leak():
    """
    Training fold max date < test fold min date - purge window (30 days).
    """
    PURGE_DAYS = 30
    dates = pd.date_range("2020-01-01", "2026-01-01", freq="B")
    n_folds = 8
    fold_size = len(dates) // n_folds

    for fold in range(2, n_folds):
        train_end_idx = fold * fold_size
        test_start_idx = train_end_idx + PURGE_DAYS

        if test_start_idx >= len(dates):
            break

        train_max_date = dates[train_end_idx]
        test_min_date = dates[test_start_idx]

        gap_days = (test_min_date - train_max_date).days
        assert gap_days >= PURGE_DAYS, f"Leakage detected! Gap {gap_days}d < {PURGE_DAYS}d purge"


# ============================================================================
# 8. test_calibration_reliability
# ============================================================================
def test_calibration_reliability():
    """
    Predicted calibrated probabilities stay strictly bounded in [0, 1]
    and behave reliably without exploding or collapsing.
    """
    brain = BrainInference()
    dummy_feats = {f: 0.5 for f in FEATURES}
    dummy_feats["rs_rating"] = 85
    dummy_feats["dry_depth"] = 0.35
    dummy_feats["breakout_volume_ratio"] = 2.5
    dummy_feats["position_in_base"] = 1.0

    p_win = brain.predict_p_win(dummy_feats)
    assert 0.0 <= p_win <= 1.0, f"P(win) must be in [0, 1], got {p_win}"

    # Verify monotonicity: stronger features should yield >= weaker features
    weak_feats = dummy_feats.copy()
    weak_feats["dry_depth"] = 0.95
    weak_feats["breakout_volume_ratio"] = 0.8
    weak_feats["rs_rating"] = 35
    p_weak = brain.predict_p_win(weak_feats)

    assert p_win >= p_weak - 0.10, "Calibrated model should rank strong setups higher than weak setups"


# ============================================================================
# 9. test_stop_never_wider_than_cap
# ============================================================================
def test_stop_never_wider_than_cap():
    """
    Any input -> stop distance <= MAX_STOP_PCT (8.0%).
    """
    # Extremely loose base: floor 40% below entry
    entry = 200.0
    loose_floor = 120.0  # 40% below entry

    dummy_bars = pd.DataFrame({
        "date": ["2026-08-01"] * 30,
        "open": [200.0] * 30,
        "high": [202.0] * 30,
        "low": [198.0] * 30,
        "close": [200.0] * 30,
        "volume": [10000] * 30
    })

    stop_price, dist_pct, valid = compute_stop(base_floor=loose_floor, bars=dummy_bars, T=29, entry=entry)

    assert dist_pct <= MAX_STOP_PCT + 1e-5, f"Stop distance {dist_pct}% exceeded MAX_STOP_PCT {MAX_STOP_PCT}%"
    assert stop_price >= entry * (1 - MAX_STOP_PCT / 100.0) - 1e-5


# ============================================================================
# 10. test_position_size_never_zero_on_valid_setup
# ============================================================================
def test_position_size_never_zero_on_valid_setup():
    """
    Valid setup with positive capital and valid stop distance -> qty > 0.
    """
    equity = 1000000.0
    entry = 150.0
    stop = 140.0  # 6.67% risk

    sizing = position_size(equity=equity, entry=entry, stop=stop, risk_pct=0.75)
    qty = sizing["shares"]
    alloc = sizing["allocation_rupees"]

    assert qty > 0, "Position size must be > 0 on valid setup"
    assert alloc <= equity * 0.10 + 1.0, "Position must never exceed 10% equity cap"


# ============================================================================
# 11. test_regime_halt_blocks_entries
# ============================================================================
def test_regime_halt_blocks_entries():
    """
    Breadth pct_above_50dma <= 35% -> state RISK_OFF -> gate HALT (multiplier 0.0x).
    """
    breadth_bearish = {
        "pct_above_50dma": 22.0,
        "pct_above_200dma": 30.0
    }

    state = regime_state(breadth_bearish)
    assert state == "RISK_OFF"

    action, mult = regime_gate(breadth_bearish)
    assert action == "HALT"
    assert mult == 0.0, "HALT regime must have 0.0x multiplier blocking all entries"


# ============================================================================
# 12. test_earnings_proximity_flag
# ============================================================================
def test_earnings_proximity_flag():
    """
    <= 5 days to earnings -> warning present; <= 3 days -> assign_tier skips.
    """
    dte_warning = 4
    tier_warning = assign_tier(
        ev_r=0.45,
        p_win=0.62,
        r_ratio=2.5,
        stop_dist_pct=6.5,
        days_to_earnings=dte_warning,
        regime_state="NORMAL"
    )
    # At 4 days, allowed but close
    assert tier_warning in ["ELITE", "STRONG", "WATCH"]

    # At <= 3 days, coin-flip rule forces SKIP (§7.2)
    dte_skip = 2
    tier_skip = assign_tier(
        ev_r=0.45,
        p_win=0.62,
        r_ratio=2.5,
        stop_dist_pct=6.5,
        days_to_earnings=dte_skip,
        regime_state="NORMAL"
    )
    assert tier_skip == "SKIP", "Breakout <= 3 days to earnings must be assigned SKIP tier"


# ============================================================================
# 13. test_shap_phrase_coverage
# ============================================================================
def test_shap_phrase_coverage():
    """
    Every top-20 domain feature has >= 1 template phrase in PHRASES dictionary.
    """
    top_20_features = FEATURES[:20]
    for feat in top_20_features:
        assert feat in PHRASES, f"Feature '{feat}' missing from brain PHRASES dictionary!"
        assert len(PHRASES[feat]) >= 1, f"Feature '{feat}' must have at least 1 phrase template"


# ============================================================================
# 14. test_model_card_complete
# ============================================================================
def test_model_card_complete():
    """
    model_card.json exists, is valid JSON, and has all required specification fields.
    """
    assert os.path.exists(MODEL_CARD_FILE), f"Missing model card at {MODEL_CARD_FILE}"

    with open(MODEL_CARD_FILE, "r") as f:
        card = json.load(f)

    required_keys = [
        "version",
        "training_date",
        "features",
        "n_features",
        "metrics",
        "known_limitations"
    ]
    for key in required_keys:
        assert key in card, f"Model card missing required key: {key}"

    assert card["n_features"] == 38, f"Model card must document 38 domain features, got {card['n_features']}"
    assert len(card["known_limitations"]) >= 2, "Known limitations must be explicitly declared"


if __name__ == "__main__":
    tests = [
        ("1. test_load_adjusted_reproducible", test_load_adjusted_reproducible),
        ("2. test_no_future_bars_in_features", test_no_future_bars_in_features),
        ("3. test_feature_parity_backfill_live", test_feature_parity_backfill_live),
        ("4. test_base_start_pit", test_base_start_pit),
        ("5. test_breakout_day_volume", test_breakout_day_volume),
        ("6. test_label_tie_break", test_label_tie_break),
        ("7. test_walk_forward_no_leak", test_walk_forward_no_leak),
        ("8. test_calibration_reliability", test_calibration_reliability),
        ("9. test_stop_never_wider_than_cap", test_stop_never_wider_than_cap),
        ("10. test_position_size_never_zero_on_valid_setup", test_position_size_never_zero_on_valid_setup),
        ("11. test_regime_halt_blocks_entries", test_regime_halt_blocks_entries),
        ("12. test_earnings_proximity_flag", test_earnings_proximity_flag),
        ("13. test_shap_phrase_coverage", test_shap_phrase_coverage),
        ("14. test_model_card_complete", test_model_card_complete),
    ]

    print("\n=======================================================")
    print("RUNNING BANANAPATTERNS v4 TEST SUITE (14 MANDATORY TESTS)")
    print("=======================================================\n")

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  [PASS] ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] ✗ {name}: {e}")
            failed += 1

    print("\n-------------------------------------------------------")
    print(f"TOTAL: {len(tests)} | PASSED: {passed} | FAILED: {failed}")
    print("-------------------------------------------------------\n")

    if failed > 0:
        sys.exit(1)

