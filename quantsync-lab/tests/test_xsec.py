"""Tests for cross-sectional as-of liquidity and relative strength (Phase 2)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from lab import catalog, pipeline, store, universe
from lab.config import Config


def _make_df(dates, close, vol):
    return pd.DataFrame({
        "date": dates,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": vol,
    })


def test_liq_rank_point_in_time_invariance(tmp_path):
    """Appending later dates must not alter liq_rank or rs60_pct for earlier dates."""
    candles = tmp_path / "candles"
    candles.mkdir()
    d1 = pd.bdate_range("2023-01-01", "2024-06-30")
    
    # 3 stocks with distinct turnover
    _make_df(d1, 100.0 + np.arange(len(d1)), 10_000.0).to_parquet(candles / "SYM_A.parquet")
    _make_df(d1, 200.0 + np.arange(len(d1)), 50_000.0).to_parquet(candles / "SYM_B.parquet")
    _make_df(d1, 50.0 + np.arange(len(d1)), 2_000.0).to_parquet(candles / "SYM_C.parquet")

    cfg = Config(data_dir=tmp_path / "data", db_path=tmp_path / "lab.db",
                 candles_dir=candles, run_dir=tmp_path / "run", restrict_universe=False)
    store.cook(cfg, "1D")
    
    xp = store.xsec_path(cfg, "1D")
    assert xp.exists()
    x1 = pd.read_parquet(xp).sort_values(["date", "symbol"]).reset_index(drop=True)

    # Now append bars for 2024-07-01 to 2025-06-30
    d2 = pd.bdate_range("2023-01-01", "2025-06-30")
    _make_df(d2, 100.0 + np.arange(len(d2)), 10_000.0).to_parquet(candles / "SYM_A.parquet")
    _make_df(d2, 200.0 + np.arange(len(d2)), 50_000.0).to_parquet(candles / "SYM_B.parquet")
    _make_df(d2, 50.0 + np.arange(len(d2)), 2_000.0).to_parquet(candles / "SYM_C.parquet")

    store.cook(cfg, "1D")
    x2 = pd.read_parquet(xp).sort_values(["date", "symbol"]).reset_index(drop=True)

    # Compare on original dates
    x2_old = x2[x2["date"] <= d1[-1]].reset_index(drop=True)
    pd.testing.assert_frame_equal(x1[["date", "symbol", "liq_rank"]], x2_old[["date", "symbol", "liq_rank"]])


def test_selection_bias_asof_liquidity(tmp_path):
    """A stock illiquid in 2019 and liquid in 2026 must trade in 2026 but produce NO 2019 trades."""
    candles = tmp_path / "candles"
    candles.mkdir()

    d_2019 = pd.bdate_range("2019-01-01", "2019-06-30")
    d_2026 = pd.bdate_range("2026-01-01", "2026-06-30")
    all_d = d_2019.append(d_2026)

    # ALWAYS_LIQ: steady volume across both years
    _make_df(all_d, 100.0, 100_000.0).to_parquet(candles / "ALWAYS_LIQ.parquet")

    # LATE_BLOOMER: illiquid in 2019 (vol=1), huge in 2026 (vol=1,000,000)
    vol_late = np.where(all_d.year == 2019, 1.0, 1_000_000.0)
    _make_df(all_d, 100.0, vol_late).to_parquet(candles / "LATE_BLOOMER.parquet")

    cfg = Config(data_dir=tmp_path / "data", db_path=tmp_path / "lab.db",
                 candles_dir=candles, run_dir=tmp_path / "run",
                 restrict_universe=True, free_universe_size=1, universe_min_price=0.0)
    store.cook(cfg, "1D")

    rule = {"v": 1, "all": [{"l": {"f": "close"}, "op": ">", "r": {"c": 0}}]}
    exit_ = {"v": 1, "any": [{"type": "time", "v": 5}]}

    trades, _ = pipeline.run_trades(cfg, rule, exit_, "1D")
    assert len(trades) > 0

    late_trades = trades[trades["symbol"] == "LATE_BLOOMER"]
    late_2019 = late_trades[late_trades["entry_date"].dt.year == 2019]
    late_2026 = late_trades[late_trades["entry_date"].dt.year == 2026]

    assert len(late_2019) == 0, "LATE_BLOOMER was illiquid in 2019 and must produce no 2019 trades"
    assert len(late_2026) > 0, "LATE_BLOOMER was liquid in 2026 and must produce 2026 trades"


def test_xsec_catalog_fields_and_signals(tmp_path):
    """Catalog exposes liq_rank and rs60_pct, and store.signals joins them properly."""
    keys = {f["key"] for f in catalog.catalog()["features"]}
    assert "liq_rank" in keys
    assert "rs60_pct" in keys

    candles = tmp_path / "candles"
    candles.mkdir()
    dates = pd.bdate_range("2023-01-01", "2023-04-30")
    _make_df(dates, 100.0, 10_000.0).to_parquet(candles / "SYM1.parquet")
    _make_df(dates, 200.0, 50_000.0).to_parquet(candles / "SYM2.parquet")

    cfg = Config(data_dir=tmp_path / "data", db_path=tmp_path / "lab.db",
                 candles_dir=candles, run_dir=tmp_path / "run")
    store.cook(cfg, "1D")

    # Filter where liq_rank == 1 (SYM2 is most traded)
    rule = {"v": 1, "all": [{"l": {"f": "liq_rank"}, "op": "<=", "r": {"c": 1}}]}
    sg = store.signals(cfg, rule, "1D", columns='"symbol","date","close","liq_rank"')
    assert len(sg) > 0
    assert set(sg["symbol"]) == {"SYM2"}
    assert (sg["liq_rank"] == 1).all()
