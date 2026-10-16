"""QuantSync's five price-action breakout signals as point-in-time boolean columns.

Ported from the live engine's signal modules (engines/v2/signals/p1,p2,p4,p5,p9). Every value at bar t uses only
bars <= t. Thresholds are the engine's own; where the engine picks a *stop* it is only used for its own risk filter
(the Lab simulator applies the strategy's exit rules instead).
Daily bars only (weekly bars get NaN)."""
from __future__ import annotations

import numpy as np
import pandas as pd

SIGNALS = {
    "sig_rs_leader": "RS leader breakout",
    "sig_stage2_base": "Stage-2 base breakout",
    "sig_power_gap": "Power gap",
    "sig_apex_base": "Apex base breakout",
    "sig_high_tight_flag": "High tight flag",
}


def _find_base_start(highs, lows, closes, T, min_depth=0.05, max_depth=0.35, min_bars=15, max_lookback=250):
    for peak in range(T - min_bars, max(0, T - max_lookback), -1):
        pivot = highs[peak]
        if pivot < np.max(highs[max(0, peak - 10):min(T, peak + 11)]):
            continue
        depth = (pivot - float(np.min(lows[peak:T]))) / max(pivot, 0.01)
        if not (min_depth <= depth <= max_depth):
            continue
        if np.any(closes[peak + 1:T] > pivot * 1.015):
            continue
        return peak
    return None


def add_signals(f: pd.DataFrame, tf: str) -> None:
    """Adds sig_* columns to f in place. f needs open/high/low/close/volume, sma_*, vol_sma_20, hh_20, atr_14."""
    n = len(f)
    if tf != "1D":
        for k in SIGNALS:
            f[k] = np.nan
        return
    o, h, l, c, v = (f[k].to_numpy("float64") for k in ("open", "high", "low", "close", "volume"))
    pc = np.r_[np.nan, c[:-1]]
    idx = np.arange(n)
    rng = h - l
    with np.errstate(all="ignore"):
        ok_rng = rng > 0
        cir = np.where(ok_rng, (c - l) / rng, np.nan)
        uw = np.where(ok_rng, (h - np.maximum(c, o)) / rng, np.nan)
        turnover = c * v
        vs20 = f["vol_sma_20"].to_numpy("float64")
        vr_incl = v / np.where(vs20 > 0, vs20, np.nan)
        vprior = pd.Series(v).shift(1).rolling(20, min_periods=20).mean().to_numpy()
        vr_prior = np.round(v / np.where(vprior > 0, vprior, np.nan), 2)   # the engine rounds this ratio to 2 dp before comparing
        s50, s150, s200 = (f[f"sma_{k}"].to_numpy("float64") for k in (50, 150, 200))
        s200 = np.where(np.isnan(s200), s50, s200)   # the engine falls back to the 50-bar average on young charts
        hh20 = f["hh_20"].to_numpy("float64")
        atr = f["atr_14"].to_numpy("float64")
        hs, ls = pd.Series(h), pd.Series(l)
        hh40, ll40 = hs.shift(1).rolling(40, min_periods=40).max().to_numpy(), ls.shift(1).rolling(40, min_periods=40).min().to_numpy()
        hh60, ll60 = hs.shift(1).rolling(60, min_periods=60).max().to_numpy(), ls.shift(1).rolling(60, min_periods=60).min().to_numpy()

        # P1 RS leader: 20-bar breakout on 2x volume, strong close, not stretched
        base40 = (hh40 - ll40) / hh40 * 100
        p1 = ((idx >= 60) & (c >= 20) & (turnover >= 5e6) & (c > o) & (cir >= 0.70) & (uw <= 0.12) & (c >= hh20)
              & ((c - hh20) / hh20 * 100 <= 3.5) & (vr_prior >= 2.0) & ((c / s200 - 1) * 100 <= 55) & (c > s50) & (base40 <= 28))
        # P9 Weinstein stage-2 base breakout
        s150_prev = np.r_[np.full(20, np.nan), s150[:-20]] if n > 20 else np.full(n, np.nan)
        depth60 = (hh60 - ll60) / hh60 * 100
        stop9 = np.round(np.maximum.reduce([hh60 * 0.97, c - 1.5 * atr, c * 0.93]), 2)   # the engine rounds stops to paise
        risk9 = (c - stop9) / c * 100
        p9 = ((idx >= 150) & (c >= 20) & (c >= s150) & ~(s150 < s150_prev * 0.99) & (depth60 >= 10) & (depth60 <= 35)
              & (c >= hh60) & (c > o) & (cir >= 0.65) & (uw <= 0.20) & (vr_incl >= 1.8) & (turnover >= 2e7) & (risk9 >= 1.5) & (risk9 <= 7.0))
        # P4 power earnings gap
        gap = (o - pc) / pc * 100
        gsize = o - pc
        fill = np.where(gsize > 0, (o - l) / gsize * 100, 100.0)
        stop4 = np.round(np.maximum(l * 0.99, pc + 0.35 * gsize), 2)
        risk4 = (c - stop4) / c * 100
        p4 = ((idx >= 40) & (c >= 20) & (gap >= 3.5) & (cir >= 0.70) & (uw <= 0.15) & (fill <= 40) & (vr_incl >= 3.0)
              & (turnover >= 5e7) & (risk4 >= 2.0) & (risk4 <= 8.0))

    # P2 apex base and P5 high tight flag need pattern search: run the engine's own logic on the few bars that pass cheap filters
    p2 = np.zeros(n, bool)
    p5 = np.zeros(n, bool)
    cand2 = np.flatnonzero((idx >= 40) & (c >= 20) & (c > o) & (turnover >= 1e7) & (cir >= 0.85) & (uw <= 0.10) & (vr_prior >= 2.2))
    for i in cand2:
        pk = _find_base_start(h, l, c, i)
        if pk is None:
            continue
        pivot, floor = h[pk], float(np.min(l[pk:i + 1]))
        depth, weeks = (pivot - floor) / max(pivot, 0.01) * 100, (i - pk) / 5.0
        if not (4.0 <= weeks <= 80.0 and 5.0 <= depth <= 35.0) or c[i] < pivot * 0.995 or (c[i] - pivot) / pivot * 100 > 3.0:
            continue
        s200i = float(np.mean(c[max(0, i - 199):i + 1])) if i >= 199 else float(np.mean(c[max(0, i - 49):i + 1]))
        if (c[i] - s200i) / s200i * 100 > 55:
            continue
        ath = float(np.max(h[:i + 1]))
        if (ath - c[i]) / ath * 100 > 35:
            continue
        stop = round(max(l[i], floor, round(c[i] * 0.955, 2)), 2)
        if stop >= c[i] * 0.985:
            stop = round(c[i] * 0.955, 2)
        if (c[i] - stop) / c[i] * 100 > 5.5:
            continue
        p2[i] = True
    cand5 = np.flatnonzero((idx >= 70) & (c >= 20) & (c > o) & (turnover >= 2e7) & (cir >= 0.65) & (uw <= 0.20) & (vr_incl >= 1.6))
    for i in cand5:
        s = max(0, i - 25)
        pk = s + int(np.argmax(h[s:i]))
        if not (8 <= i - pk <= 25):
            continue
        ps = max(0, pk - 40)
        if pk <= ps:
            continue
        low0 = float(np.min(l[ps:pk]))
        if low0 <= 0 or (h[pk] - low0) / low0 * 100 < 70.0:
            continue
        flag_low = float(np.min(l[pk:i]))
        if (h[pk] - flag_low) / h[pk] * 100 > 20.0 or c[i] < h[pk] * 0.99:
            continue
        risk = (c[i] - round(max(flag_low * 0.99, c[i] * 0.935), 2)) / c[i] * 100
        if 2.0 <= risk <= 7.5:
            p5[i] = True

    for k, arr in zip(SIGNALS, (p1, p9, p4, p2, p5)):
        f[k] = arr.astype("float64")
    early = np.arange(n) < 40
    for k in SIGNALS:
        f.loc[early, k] = np.nan
