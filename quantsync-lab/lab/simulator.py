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
         "trail_atr": None, "trail_pct": None, "time": CAP[tf], "rule": None,
         "breakeven": None, "scale_out": None, "stop_field": None, "target_r": None,
         "trail_close_below": None}
    for it in (exit_ or {}).get("any", []):
        t = it.get("type")
        if t == "rule":
            continue
        elif t in ("trail_close_below", "breakeven"):
            s[t] = it
        elif t in s:
            s[t] = it.get("v")
    s["time"] = min(int(s["time"]), CAP[tf])
    return s


def simulate_symbol(symbol: str, b: pd.DataFrame, sig: np.ndarray, exit_: dict | None, tf: str,
                    rule_exit: np.ndarray | None = None) -> list[dict]:
    """b: date/open/high/low/close/atr_14 sorted. sig/rule_exit: bool arrays aligned to b."""
    d = b["date"].to_numpy()
    o, h, l, c = (b[k].to_numpy("float64") for k in ("open", "high", "low", "close"))
    atr = b["atr_14"].to_numpy("float64") if "atr_14" in b else np.zeros(len(b), dtype="float64")
    sp, n, trades, free_from = _spec(exit_, tf), len(b), [], 0

    sf_arr = None
    if sp["stop_field"] and sp["stop_field"] in b:
        sf_arr = b[sp["stop_field"]].to_numpy("float64")

    tcb_arr, tcb_min_bars, tcb_oip = None, 1, False
    if sp["trail_close_below"]:
        tcb = sp["trail_close_below"]
        fld = tcb.get("field") or (tcb.get("v")[0] if isinstance(tcb.get("v"), (list, tuple)) else tcb.get("v"))
        if fld and fld in b:
            tcb_arr = b[fld].to_numpy("float64")
        tcb_min_bars = int(tcb.get("min_bars", 1))
        tcb_oip = bool(tcb.get("only_in_profit", False))

    for t in np.flatnonzero(sig):
        e = t + 1
        if e >= n or e < free_from:
            continue
        if o[e] > c[t] * (1 + GAP_SKIP):
            continue
        ep, a = o[e], atr[t]
        if ep <= 0 or not np.isfinite(ep):
            continue

        # Initial risk & stops
        init_risk_pct = None
        if sf_arr is not None and np.isfinite(sf_arr[t]) and sf_arr[t] > 0:
            init_risk_pct = float(sf_arr[t])
        elif sp["stop_pct"]:
            init_risk_pct = float(sp["stop_pct"])
        elif sp["atr_stop"] and a == a:
            init_risk_pct = float(sp["atr_stop"] * a / ep * 100.0)
        else:
            init_risk_pct = 5.0
        risk_amt = ep * (init_risk_pct / 100.0)

        fixed_stop = []
        if sp["stop_pct"]:
            fixed_stop.append(ep * (1 - sp["stop_pct"] / 100.0))
        elif sp["stop_field"] and init_risk_pct:
            fixed_stop.append(ep * (1 - init_risk_pct / 100.0))
        if sp["atr_stop"] and a == a:
            fixed_stop.append(ep - sp["atr_stop"] * a)

        # Targets
        target = None
        if sp["target_pct"]:
            target = ep * (1 + sp["target_pct"] / 100.0)
        if sp["target_r"] and init_risk_pct:
            tr_p = ep + sp["target_r"] * risk_amt
            target = tr_p if target is None else min(target, tr_p)
        if sp["atr_target"] and a == a:
            tt = ep + sp["atr_target"] * a
            target = tt if target is None else min(target, tt)

        # Breakeven specification
        be_trigger_p = None
        be_lock_p = None
        be_activated = False
        if sp["breakeven"]:
            be_spec = sp["breakeven"]
            if isinstance(be_spec, dict):
                be_v = be_spec.get("v", [])
                be_unit = be_spec.get("unit", "pct")
            else:
                be_v = be_spec
                be_unit = "pct"
            be_trig, be_lock = float(be_v[0]), float(be_v[1])
            if be_unit == "R":
                be_trigger_p = ep + be_trig * risk_amt
                be_lock_p = ep + be_lock * risk_amt
            else:
                be_trigger_p = ep * (1 + be_trig / 100.0)
                be_lock_p = ep * (1 + be_lock / 100.0)

        # Scale out specification
        scale_target_p = None
        scale_frac = 0.0
        scale_lock_pct = None
        scale_done = False
        scale_exit_p = None
        if sp["scale_out"]:
            st_pct, scale_frac = float(sp["scale_out"][0]), float(sp["scale_out"][1])
            scale_target_p = ep * (1 + st_pct / 100.0)
            if len(sp["scale_out"]) > 2:
                scale_lock_pct = float(sp["scale_out"][2])

        hi, lo = ep, ep
        xp = xr = None; xi = n - 1
        for i in range(e, n):
            # 1. Rule exit at next bar open
            if i > e and rule_exit is not None and rule_exit[i - 1]:
                fill_p = o[i]
                xp = (scale_frac * scale_exit_p + (1.0 - scale_frac) * fill_p) if scale_done else fill_p
                xr, xi = "rule", i
                break

            # 2. Trail close below moving average at next bar open
            if i > e and tcb_arr is not None and (i - e) >= tcb_min_bars:
                if c[i - 1] < tcb_arr[i - 1]:
                    if not tcb_oip or c[i - 1] > ep:
                        fill_p = o[i]
                        xp = (scale_frac * scale_exit_p + (1.0 - scale_frac) * fill_p) if scale_done else fill_p
                        xr, xi = "trail_close", i
                        break

            # 3. Active stop check (pessimistic intra-bar ordering)
            lvl = list(fixed_stop)
            if i > e:  # trailing levels use prior bars' highs
                if sp["trail_atr"] and a == a:
                    lvl.append(hi - sp["trail_atr"] * a)
                if sp["trail_pct"]:
                    lvl.append(hi * (1 - sp["trail_pct"] / 100.0))
            stop = max(lvl) if lvl else None

            if stop is not None and (o[i] <= stop or l[i] <= stop):
                fill_p = o[i] if o[i] <= stop else stop
                xp = (scale_frac * scale_exit_p + (1.0 - scale_frac) * fill_p) if scale_done else fill_p
                xr, xi = "stop", i
                break

            # 4. Scale out check
            if not scale_done and scale_target_p is not None and (o[i] >= scale_target_p or h[i] >= scale_target_p):
                scale_done = True
                scale_exit_p = o[i] if o[i] >= scale_target_p else scale_target_p
                if scale_lock_pct is not None:
                    fixed_stop.append(ep * (1 + scale_lock_pct / 100.0))

            # 5. Target check
            if target is not None and (o[i] >= target or h[i] >= target):
                fill_p = o[i] if o[i] >= target else target
                xp = (scale_frac * scale_exit_p + (1.0 - scale_frac) * fill_p) if scale_done else fill_p
                xr, xi = "target", i
                break

            # 6. Breakeven activation check (updates stop for subsequent bars)
            if not be_activated and be_trigger_p is not None and (o[i] >= be_trigger_p or h[i] >= be_trigger_p):
                be_activated = True
                fixed_stop.append(be_lock_p)

            hi, lo = max(hi, h[i]), min(lo, l[i])

            # 7. Time cap
            if i - e + 1 >= sp["time"]:
                fill_p = c[i]
                xp = (scale_frac * scale_exit_p + (1.0 - scale_frac) * fill_p) if scale_done else fill_p
                xr, xi = "time", i
                break

        is_open = xp is None
        if is_open:
            fill_p = c[n - 1]
            xp = (scale_frac * scale_exit_p + (1.0 - scale_frac) * fill_p) if scale_done else fill_p
            xr = "open"
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
