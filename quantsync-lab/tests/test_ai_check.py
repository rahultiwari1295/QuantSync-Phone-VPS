"""Unit tests for Phase 3: 3-stage AI check protocol (tests/test_ai_check.py)."""
from __future__ import annotations

import sys
import numpy as np
import pandas as pd
import pytest

from lab import ai
from lab.config import Config


def _make_ai_ds(n_per_year=120, planted=True, seed=42):
    """Generate trade rows across 2018-2026."""
    rng = np.random.default_rng(seed)
    years = list(range(2018, 2027))
    frames = []

    for yr in years:
        dates = pd.date_range(f"{yr}-01-15", f"{yr}-11-30", periods=n_per_year)
        df = pd.DataFrame({
            "symbol": "SYM",
            "signal_date": dates - pd.Timedelta(days=1),
            "entry_date": dates,
            "exit_date": dates + pd.Timedelta(days=7),
            "close": 100.0,
        })
        # Generate 25 features
        for f in ai.AI_FEATURES_25:
            df[f] = rng.normal(0, 1, n_per_year)

        if planted:
            # Planted edge: when rsi_14 > 0, high win rate & strong positive return
            edge = df["rsi_14"] > 0
            p = np.where(edge, 0.85, 0.25)
            y = (rng.random(n_per_year) < p).astype(int)
            ret = np.where(y == 1, rng.uniform(2.0, 6.0, n_per_year), rng.uniform(-4.0, -1.0, n_per_year))
        else:
            # Pure noise: returns centered around zero, consistent y = (ret > 0)
            ret = rng.normal(0.0, 2.0, n_per_year)
            y = (ret > 0).astype(int)

        df["y"] = y
        df["ret_pct"] = ret
        frames.append(df)

    res = pd.concat(frames, ignore_index=True)
    return res.sort_values("entry_date").reset_index(drop=True)


def test_planted_edge():
    """A planted feature that predicts y in every year produces verdict='improved'."""
    ds = _make_ai_ds(n_per_year=120, planted=True, seed=7)
    cfg = Config()
    res = ai.check(ds, cfg)

    assert res["protocol"] == "v2"
    assert res["verdict"] == "improved"
    assert res["gate"] in (50, 60, 70)
    assert res["model"] in ("lgbm_default", "lgbm_shallow", "lgbm_compact", "xgboost", "logreg", "ensemble")
    assert res["leak"]["passed"] is True
    assert res["valid"]["ai"]["win_rate"] >= res["valid"]["plain"]["win_rate"]
    assert res["valid"]["ai"]["avg_ret"] > res["valid"]["plain"]["avg_ret"]


def test_random_noise_no_edge():
    """Random labels yield verdict='no_edge' and pass the leak test."""
    ds = _make_ai_ds(n_per_year=200, planted=False, seed=99)
    cfg = Config()
    res = ai.check(ds, cfg)

    assert res["protocol"] == "v2"
    assert res["verdict"] in ("no_edge", "insufficient")
    if res["leak"]:
        assert res["leak"]["passed"] is True


def test_spy_proves_no_2025_in_tuning():
    """A spy proves no row with entry_date >= 2025-01-01 reaches Stage A tuning."""
    ds = _make_ai_ds(n_per_year=120, planted=True, seed=12)
    cfg = Config()

    original_oos = ai.oos_scores_candidate
    max_dates_seen = []

    def spy_oos(cand_name, ds_arg, years, **kw):
        max_dates_seen.append(ds_arg["entry_date"].max())
        assert ds_arg["entry_date"].max() < pd.Timestamp("2025-01-01"), "Row >= 2025 reached tuning loop!"
        return original_oos(cand_name, ds_arg, years, **kw)

    try:
        ai.oos_scores_candidate = spy_oos
        res = ai.check(ds, cfg)
        assert len(max_dates_seen) > 0
        assert all(d < pd.Timestamp("2025-01-01") for d in max_dates_seen)
    finally:
        ai.oos_scores_candidate = original_oos


def test_determinism():
    """Same input twice produces identical verdict, model, gate and numbers."""
    ds = _make_ai_ds(n_per_year=120, planted=True, seed=42)
    cfg = Config()

    r1 = ai.check(ds, cfg)
    r2 = ai.check(ds, cfg)

    assert r1["protocol"] == r2["protocol"]
    assert r1["verdict"] == r2["verdict"]
    assert r1["model"] == r2["model"]
    assert r1["gate"] == r2["gate"]
    assert r1["valid"] == r2["valid"]
    assert r1["blind"] == r2["blind"]
    assert r1["leak"] == r2["leak"]


def test_missing_xgboost_handled_gracefully(monkeypatch):
    """If xgboost is not installed or unimportable, tuning still completes without error."""
    ds = _make_ai_ds(n_per_year=120, planted=True, seed=10)
    cfg = Config()

    # Hide xgboost
    monkeypatch.setitem(sys.modules, "xgboost", None)

    res = ai.check(ds, cfg)
    assert res["protocol"] == "v2"
    assert res["verdict"] in ("improved", "no_edge")
    # Confirmed xgboost was not chosen or failed cleanly
    if res["tune"] and "candidates" in res["tune"]:
        xgb_entries = [c for c in res["tune"]["candidates"] if c["name"] == "xgboost"]
        assert len(xgb_entries) == 0
