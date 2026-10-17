"""Indicator computation for one symbol. Every value at row t uses only rows <= t (no look-ahead)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .signals import add_signals

BASE = ["open", "high", "low", "close", "volume"]
FLAGS = ["is_green", "is_inside_bar"]
YEAR_BARS = {"1D": 252, "1W": 52}


def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return out.where(dn != 0, 100.0).where(up.notna())


def compute(df: pd.DataFrame, tf: str = "1D") -> pd.DataFrame:
    """df: columns date,open,high,low,close,volume sorted ascending. Returns float32 features + date."""
    df = df.sort_values("date").reset_index(drop=True)
    o, h, l, c, v = (df[k].astype("float64") for k in BASE)
    pc = c.shift(1)
    f = pd.DataFrame({"date": df["date"]})
    for k, s in zip(BASE, (o, h, l, c, v)):
        f[k] = s
    f["ret_1"] = (c / pc - 1) * 100
    f["ret_5"] = (c / c.shift(5) - 1) * 100
    f["ret_20"] = (c / c.shift(20) - 1) * 100
    f["rsi_14"] = rsi(c, 14)
    for n in (10, 20, 50, 100, 150, 200):
        f[f"sma_{n}"] = c.rolling(n, min_periods=n).mean()
    for n in (9, 10, 20, 21, 50, 100, 200):
        f[f"ema_{n}"] = c.ewm(span=n, adjust=False, min_periods=n).mean()
    f["rsi_ema_10"] = f["rsi_14"].ewm(span=10, adjust=False, min_periods=10).mean()   # smoothed RSI: "EMA 10 of RSI 14"
    f["rsi_sma_10"] = f["rsi_14"].rolling(10, min_periods=10).mean()
    ema12, ema26 = c.ewm(span=12, adjust=False, min_periods=26).mean(), c.ewm(span=26, adjust=False, min_periods=26).mean()
    f["macd"] = ema12 - ema26
    f["macd_signal"] = f["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    f["macd_hist"] = f["macd"] - f["macd_signal"]
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = (h - l).iloc[0]
    atr = _wilder(tr, 14)
    f["atr_14"] = atr
    f["atr_pct"] = atr / c * 100
    mid, sd = c.rolling(20, min_periods=20).mean(), c.rolling(20, min_periods=20).std(ddof=0)
    f["bb_upper"], f["bb_lower"] = mid + 2 * sd, mid - 2 * sd
    f["bb_width_pct"] = (f["bb_upper"] - f["bb_lower"]) / mid.replace(0, np.nan) * 100
    f["bb_pct"] = (c - f["bb_lower"]) / (f["bb_upper"] - f["bb_lower"]).replace(0, np.nan)
    ll14, hh14 = l.rolling(14, min_periods=14).min(), h.rolling(14, min_periods=14).max()
    f["stoch_k"] = (c - ll14) / (hh14 - ll14).replace(0, np.nan) * 100
    up_m, dn_m = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up_m > dn_m) & (up_m > 0), up_m, 0.0), index=df.index)
    ndm = pd.Series(np.where((dn_m > up_m) & (dn_m > 0), dn_m, 0.0), index=df.index)
    a14 = atr.replace(0, np.nan)
    pdi, ndi = 100 * _wilder(pdm, 14) / a14, 100 * _wilder(ndm, 14) / a14
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    f["pdi_14"], f["ndi_14"] = pdi.where(atr.notna()), ndi.where(atr.notna())
    tp = (h + l + c) / 3
    md = tp.rolling(20, min_periods=20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    f["cci_20"] = (tp - tp.rolling(20, min_periods=20).mean()) / (0.015 * md.replace(0, np.nan))
    f["adx_14"] = _wilder(dx.fillna(0), 14).where(atr.notna())
    f["vol_sma_20"] = v.rolling(20, min_periods=20).mean()
    f["vol_ratio"] = v / f["vol_sma_20"].replace(0, np.nan)
    f["turnover"] = c * v
    f["hh_20"] = h.shift(1).rolling(20, min_periods=20).max()   # prior 20-bar high: close > hh_20 is a breakout
    f["ll_20"] = l.shift(1).rolling(20, min_periods=20).min()
    yb = YEAR_BARS[tf]
    f["hh_52"] = h.shift(1).rolling(yb, min_periods=yb // 2).max()
    f["ll_52"] = l.shift(1).rolling(yb, min_periods=yb // 2).min()
    f["dist_hh52_pct"] = (c / f["hh_52"] - 1) * 100
    f["dist_sma_50_pct"] = (c / f["sma_50"] - 1) * 100
    f["dist_sma_200_pct"] = (c / f["sma_200"] - 1) * 100
    f["gap_pct"] = (o / pc - 1) * 100
    f["range_pct"] = (h - l) / c * 100
    f["body_pct"] = (c - o) / o * 100
    f["is_green"] = (c > o).astype("float64")
    f["is_inside_bar"] = ((h <= h.shift(1)) & (l >= l.shift(1))).astype("float64")
    f.loc[pc.isna(), ["is_green", "is_inside_bar"]] = np.nan
    f["vol_surge"] = v / v.shift(1).rolling(20, min_periods=20).mean().replace(0, np.nan)   # vs the PRIOR 20 bars (today excluded)
    rg = (h - l).replace(0, np.nan)
    f["close_loc"] = (c - l) / rg * 100
    f["upper_wick_pct"] = (h - np.maximum(c, o)) / rg * 100
    f["dist_hh20_pct"] = (c / f["hh_20"] - 1) * 100
    # longer returns, all-time high, 52-week low distance, narrow-range day, streaks
    for n in (60, 120, 250):
        f[f"ret_{n}"] = (c / c.shift(n) - 1) * 100
    f["ath"] = h.cummax()
    f["dist_ath_pct"] = (c / f["ath"] - 1) * 100
    f["dist_ll52_pct"] = (c / f["ll_52"] - 1) * 100
    f["vol_sma_50"] = v.rolling(50, min_periods=50).mean()
    f["vol_ratio_50"] = v / f["vol_sma_50"].replace(0, np.nan)
    f["dist_ema_20_pct"] = (c / f["ema_20"] - 1) * 100
    f["sma_50_slope"] = (f["sma_50"] / f["sma_50"].shift(10) - 1) * 100
    f["sma_200_slope"] = (f["sma_200"] / f["sma_200"].shift(20) - 1) * 100
    hl = h - l
    f["is_nr7"] = (hl <= hl.rolling(7, min_periods=7).min()).astype("float64")
    up = (c > pc).astype(int)
    f["up_streak"] = up.groupby((up != up.shift()).cumsum()).cumsum() * up
    dn = (c < pc).astype(int)
    f["down_streak"] = dn.groupby((dn != dn.shift()).cumsum()).cumsum() * dn
    f.loc[pc.isna(), ["is_nr7"]] = np.nan
    add_signals(f, tf)
    cols = [k for k in f.columns if k != "date"]
    f[cols] = f[cols].replace([np.inf, -np.inf], np.nan).astype("float32")
    return f


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Daily -> weekly bars (Mon-Fri weeks). A partial current week is kept, dated by its last trading day."""
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    g = d.groupby(d["date"].dt.to_period("W-FRI"), sort=True)
    w = g.agg(date=("date", "max"), open=("open", "first"), high=("high", "max"),
              low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    return w.reset_index(drop=True)
