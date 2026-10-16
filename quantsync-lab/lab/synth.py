"""Deterministic synthetic candles for tests and local demos (never used in production)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_candles(seed: int, start="2018-01-01", end="2026-09-30", drift=0.0003) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, end)
    r = rng.normal(drift, 0.017, len(dates))
    close = 100 * np.exp(np.cumsum(r))
    op = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.004, len(dates)))
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.006, len(dates))))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.006, len(dates))))
    vol = rng.integers(2e5, 2e6, len(dates)).astype(float)
    return pd.DataFrame({"date": dates, "open": op, "high": hi, "low": lo, "close": close, "volume": vol})


def write_universe(path, n=25, **kw):
    path.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        make_candles(i, **kw).to_parquet(path / f"SYM{i:02d}.parquet")
