import numpy as np
import pandas as pd

from lab import ai
from lab.catalog import MODEL_FEATURES


def _ds(n=1800, signal=True, seed=0):
    rng = np.random.default_rng(seed)
    d = pd.DataFrame(rng.normal(size=(n, len(MODEL_FEATURES))), columns=MODEL_FEATURES)
    d["entry_date"] = pd.to_datetime("2018-03-01") + pd.to_timedelta(np.sort(rng.integers(0, 365 * 8, n)), unit="D")
    d["exit_date"] = d["entry_date"] + pd.Timedelta(days=7)
    logit = 1.2 * d["rsi_14"] - 0.8 * d["vol_ratio"] if signal else 0 * d["rsi_14"]
    p = 1 / (1 + np.exp(-logit)) * 0.5 + 0.25
    d["y"] = (rng.random(n) < p).astype(int)
    d["ret_pct"] = np.where(d.y == 1, 3.0, -2.5) + rng.normal(0, .5, n)
    d["symbol"] = "X"
    return d.sort_values("entry_date").reset_index(drop=True)


YEARS = [2023, 2024, 2025]


def test_finds_real_edge_without_leak():
    ds = _ds(signal=True)
    r = ai.enhance_entry(ds, YEARS)
    assert r["status"] == "done" and r["edge_found"] and r["leak"]["passed"]
    assert r["gates"]["70"]["lift"] > r["gates"]["50"]["lift"] > 0
    assert r["drivers"] and "RSI" in " ".join(r["drivers"])


def test_pure_noise_has_no_edge():
    r = ai.enhance_entry(_ds(signal=False, seed=3), YEARS)
    assert not r["edge_found"]


def test_leak_gate_blocks_future_information():
    ds = _ds(signal=False, seed=5)
    assert ai.leak_test(ds, [2020, 2021, 2022] + YEARS)["passed"]


def test_too_few_trades():
    assert ai.enhance_entry(_ds(n=30), YEARS)["status"] == "insufficient"


def test_walk_forward_never_trains_on_test_year():
    ds = _ds()
    sc = ai.oos_scores(ds, [2024])
    assert sc[ds.entry_date.dt.year != 2024].isna().all() and sc[ds.entry_date.dt.year == 2024].notna().all()


def test_prepare_dataset_without_signal_date():
    """Verify prepare_dataset gracefully handles dataframes without a signal_date column."""
    ds = _ds(n=50)
    assert 'signal_date' not in ds.columns
    from lab.config import Config
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        c = Config(data_dir=Path(td), db_path=Path(td)/'lab.db', models_dir=Path(td)/'models',
                   candles_dir=Path(td)/'candles', run_dir=Path(td)/'run')
        prepared = ai.prepare_dataset(ds, c)
        assert 'y' in prepared.columns
        for col in ai.AI_FEATURES_25:
            assert col in prepared.columns
