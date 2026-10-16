"""Trade simulator. One function is used for both history and live tracking (parity by construction).

Rules: signal at bar t close -> entry at next bar open (skipped if it gaps > 8 % above the signal close);
one open trade per symbol; stop assumed first when stop and target are both inside a bar; gap-through
fills at the open; rule exits fill at the next open; costs 0.10 % per side; time cap 60 bars (1D) / 26 (1W).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

COST = 0.001
GAP_SKIP = 0.08
CAP = {"1D": 60, "1W": 26}
TRADE_COLS = ["symbol", "signal_date", "entry_date", "entry_price", "exit_date", "exit_price",
              "exit_reason", "ret_pct", "bars", "is_open", "mfe_pct", "mae_pct"]


def _spec(exit_: dict | None, tf: str):
    s = {"stop_pct": None, "target_pct": None, "atr_stop": None, "atr_target": None,
         "trail_atr": None, "trail_pct": None, "time": CAP[tf], "rule": None}
    for it in (exit_ or {}).get("any", []):
        if it["type"] != "rule":
            s[it["type"]] = it["v"]
    s["time"] = min(int(s["time"]), CAP[tf])
    return s


def simulate_symbol(symbol: str, b: pd.DataFrame, sig: np.ndarray, exit_: dict | None, tf: str,
                    rule_exit: np.ndarray | None = None) -> list[dict]:
    """b: date/open/high/low/close/atr_14 sorted. sig/rule_exit: bool arrays aligned to b."""
    d = b["date"].to_numpy()
    o, h, l, c = (b[k].to_numpy("float64") for k in ("open", "high", "low", "close"))
    atr = b["atr_14"].to_numpy("float64")
    sp, n, trades, free_from = _spec(exit_, tf), len(b), [], 0
    for t in np.flatnonzero(sig):
        e = t + 1
        if e >= n or e < free_from:
            continue
        if o[e] > c[t] * (1 + GAP_SKIP):
            continue
        ep, a = o[e], atr[t]
        if ep <= 0 or not np.isfinite(ep):
            continue
        stop = target = None
        fixed_stop = []
        if sp["stop_pct"]: fixed_stop.append(ep * (1 - sp["stop_pct"] / 100))
        if sp["atr_stop"] and a == a: fixed_stop.append(ep - sp["atr_stop"] * a)
        if sp["target_pct"]: target = ep * (1 + sp["target_pct"] / 100)
        if sp["atr_target"] and a == a:
            tt = ep + sp["atr_target"] * a
            target = tt if target is None else min(target, tt)
        hi, lo = ep, ep
        xp = xr = None; xi = n - 1
        for i in range(e, n):
            if i > e and rule_exit is not None and rule_exit[i - 1]:
                xp, xr, xi = o[i], "rule", i; break
            lvl = list(fixed_stop)
            if i > e:  # trailing levels use only prior bars' highs
                if sp["trail_atr"] and a == a: lvl.append(hi - sp["trail_atr"] * a)
                if sp["trail_pct"]: lvl.append(hi * (1 - sp["trail_pct"] / 100))
            stop = max(lvl) if lvl else None
            if stop is not None and (o[i] <= stop or l[i] <= stop):
                xp, xr, xi = (o[i] if o[i] <= stop else stop), "stop", i; break
            if target is not None and (o[i] >= target or h[i] >= target):
                xp, xr, xi = (o[i] if o[i] >= target else target), "target", i; break
            hi, lo = max(hi, h[i]), min(lo, l[i])
            if i - e + 1 >= sp["time"]:
                xp, xr, xi = c[i], "time", i; break
        is_open = xp is None
        if is_open:
            xp, xr = c[n - 1], "open"
        ret = (xp * (1 - COST)) / (ep * (1 + COST)) - 1
        trades.append({"symbol": symbol, "signal_date": d[t], "entry_date": d[e], "entry_price": ep,
                       "exit_date": d[xi], "exit_price": xp, "exit_reason": xr, "ret_pct": ret * 100,
                       "bars": int(xi - e + 1), "is_open": is_open,
                       "mfe_pct": (hi / ep - 1) * 100, "mae_pct": (lo / ep - 1) * 100})
        if is_open:
            break
        free_from = xi + 1
    return trades


def simulate(bars: dict[str, pd.DataFrame], sig: dict[str, np.ndarray], exit_: dict | None, tf: str,
             rule_exit: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
    rows = []
    for s, b in bars.items():
        if s in sig and sig[s].any():
            rows += simulate_symbol(s, b, sig[s], exit_, tf, (rule_exit or {}).get(s))
    df = pd.DataFrame(rows, columns=TRADE_COLS)
    return df.sort_values(["entry_date", "symbol"]).reset_index(drop=True)


def sig_arrays(signal_df: pd.DataFrame, bars: dict[str, pd.DataFrame]) -> dict[str, np.ndarray]:
    out = {}
    for s, g in signal_df.groupby("symbol"):
        if s in bars:
            out[s] = np.isin(bars[s]["date"].to_numpy(), g["date"].to_numpy())
    return out
