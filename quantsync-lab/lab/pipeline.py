"""Orchestration used by workers and the API: scan, backtest, enhance, enhance-exit."""
from __future__ import annotations

import hashlib
from typing import Callable

import numpy as np
import pandas as pd

from . import ai, metrics, simulator, store
from .catalog import FEATURE_KEYS, MODEL_FEATURES
from .config import Config
from .rules import canonical, normalise, normalise_exit, rule_hash

Progress = Callable[[int, str], None]
_noop: Progress = lambda p, s: None
SIM_COLS = ["date", "open", "high", "low", "close", "atr_14"]


def report_years(cfg: Config, today: pd.Timestamp | None = None) -> list[int]:
    return list(range(int(str(cfg.report_start)[:4]), (today or pd.Timestamp.today()).year + 1))


def exit_hash(exit_: dict) -> str:
    return hashlib.sha256(canonical(exit_).encode()).hexdigest()[:16]


def _fields(rule) -> list[str]:
    """Fields a rule reads, in order, so a scan can show the numbers it filtered on."""
    out = []
    def walk(x):
        if isinstance(x, dict):
            if "f" in x and isinstance(x["f"], str) and x["f"] not in out:
                out.append(x["f"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(rule)
    return out


def scan(cfg: Config, entry: dict, tf: str, limit: int = 200) -> dict:
    entry = normalise(entry)
    base = ["symbol", "date", "close", "ret_1", "rsi_14", "vol_ratio"]
    cols = base + [f for f in _fields(entry) if f not in base and f in FEATURE_KEYS][:8]
    df = store.signals(cfg, entry, tf, only_last=True, columns=",".join(f'"{c}"' for c in cols))
    m = store.meta(cfg, tf)
    records = df.sort_values("symbol").head(limit).assign(date=lambda d: d["date"].astype(str)).to_dict("records")
    clean = [{k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in r.items()} for r in records]
    return {"as_of": m["as_of"] if m else None, "count": int(len(df)), "rule_hash": rule_hash(entry, tf),
            "rows": clean}


def _signal_frame(cfg, entry, tf, extra_cols=(), start=None):
    cols = '"symbol","date","close",' + ",".join(f'"{c}"' for c in MODEL_FEATURES)
    return store.signals(cfg, entry, tf, start=start or cfg.train_start, columns=cols)


def _rule_exit_arrays(cfg, exit_, tf, bars, start=None):
    arrs = {}
    for it in exit_.get("any", []):
        if it["type"] == "rule":
            sg = store.signals(cfg, it["rule"], tf, start=start or cfg.train_start)
            arrs = simulator.sig_arrays(sg, bars)
    return arrs


def run_trades(cfg: Config, entry: dict, exit_: dict, tf: str, sg: pd.DataFrame | None = None, progress: Progress = _noop,
               start: str | None = None):
    """start: only simulate from this date (live tracking needs months, not 8 years)."""
    entry, exit_ = normalise(entry), normalise_exit(exit_)
    progress(10, "Finding entries")
    sg = _signal_frame(cfg, entry, tf, start=start) if sg is None else sg
    if not len(sg):
        return pd.DataFrame(columns=simulator.TRADE_COLS), sg
    bars = store.bars(cfg, tf, symbols=list(sg["symbol"].unique()), start=start or cfg.train_start, columns=SIM_COLS)
    progress(40, "Simulating trades")
    trades = simulator.simulate(bars, simulator.sig_arrays(sg, bars), exit_, tf, _rule_exit_arrays(cfg, exit_, tf, bars, start))
    return trades, sg


def backtest(cfg: Config, entry: dict, exit_: dict, tf: str, progress: Progress = _noop) -> dict:
    trades, _ = run_trades(cfg, entry, exit_, tf, progress=progress)
    progress(90, "Summarising")
    return summarise_trades(cfg, trades, tf)


def summarise_trades(cfg: Config, trades: pd.DataFrame, tf: str) -> dict:
    yrs = report_years(cfg)
    rep = trades[trades["entry_date"] >= pd.Timestamp(cfg.report_start)] if len(trades) else trades
    return {"tf": tf, "as_of": (store.meta(cfg, tf) or {}).get("as_of"), "years": yrs,
            "summary": metrics.by_year(trades, yrs), "months": metrics.monthly_counts(rep),
            "open_trades": int(trades["is_open"].sum()) if len(trades) else 0,
            "trades": recent_trades(rep), "analytics": metrics.analytics(rep), "metrics_v": metrics.METRICS_V}


TRADE_COLS = ["symbol", "signal_date", "entry_date", "entry_price", "exit_date", "exit_price", "exit_reason", "ret_pct", "bars", "is_open"]


def recent_trades(t: pd.DataFrame, n: int = 300) -> list[dict]:
    """Most recent past-test trades (newest first) so a strategy page can list them."""
    if not len(t):
        return []
    d = t.sort_values(["entry_date", "symbol"], ascending=[False, True]).head(n)[[c for c in TRADE_COLS if c in t.columns]].copy()
    for c in ("signal_date", "entry_date", "exit_date"):
        if c in d:
            d[c] = pd.to_datetime(d[c]).dt.strftime("%Y-%m-%d")
    out = []
    for r in d.to_dict("records"):
        out.append({k: (None if (isinstance(v, float) and not np.isfinite(v)) or (v is pd.NaT) or (isinstance(v, str) and v == "NaT")
                        else (bool(v) if k == "is_open" else (round(float(v), 2) if isinstance(v, (float, np.floating)) else (int(v) if isinstance(v, (np.integer,)) else v))))
                    for k, v in r.items()})
    return out


def _dataset(trades: pd.DataFrame, sg: pd.DataFrame) -> pd.DataFrame:
    t = trades[~trades["is_open"]].copy()
    f = sg.rename(columns={"date": "signal_date"}).drop(columns=["close"])
    ds = t.merge(f, on=["symbol", "signal_date"], how="left")
    ds["y"] = (ds["ret_pct"] > 0).astype(int)
    return ds.sort_values("entry_date").reset_index(drop=True)


def enhance(cfg: Config, entry: dict, exit_: dict, tf: str, progress: Progress = _noop, extra: pd.DataFrame | None = None) -> dict:
    progress(5, "Collecting past trades")
    trades, sg = run_trades(cfg, entry, exit_, tf, progress=lambda p, s: progress(5 + p // 5, s))
    progress(30, "Preparing features")
    ds = _dataset(trades, sg)
    progress(45, "Training and testing gates")
    res = ai.enhance_entry(ds, report_years(cfg), extra=extra)
    if res["status"] != "done":
        return res
    progress(90, "Finishing")
    res["_ds"], res["_trades_n"] = ds, len(ds)
    return res


def enhance_exit(cfg: Config, entry: dict, exit_: dict, tf: str, gate_scores: pd.Series, ds: pd.DataFrame, gate: int,
                 progress: Progress = _noop) -> dict:
    """Re-simulate gated entries under each candidate exit; choose on 2020-22, report on report years."""
    keep = ds[gate_scores >= gate]
    sg = keep[["symbol", "signal_date"]].rename(columns={"signal_date": "date"})
    sg["close"] = 0.0
    bars = store.bars(cfg, tf, symbols=list(sg["symbol"].unique()), start=cfg.train_start, columns=SIM_COLS)
    arr = simulator.sig_arrays(sg, bars)
    ry = report_years(cfg)
    cands = [normalise_exit(exit_)] + ai.exit_candidates()
    rows = []
    for i, ex in enumerate(cands):
        progress(10 + int(80 * i / len(cands)), "Testing exits")
        t = simulator.simulate(bars, arr, ex, tf, _rule_exit_arrays(cfg, ex, tf, bars))
        t = t[~t["is_open"]] if len(t) else t
        sel = t[t.entry_date.dt.year.isin([2020, 2021, 2022])] if len(t) else t
        rep = t[t.entry_date.dt.year.isin(ry)] if len(t) else t
        rows.append({"exit": ex, "select": metrics.summarize(sel), "report": metrics.summarize(rep)})
    base, rest = rows[0], rows[1:]
    ok = [r for r in rest if r["select"]["trades"] >= 20 and r["select"]["avg_ret"] is not None]
    best = max(ok, key=lambda r: r["select"]["avg_ret"], default=None)
    better = bool(best and base["select"]["avg_ret"] is not None and best["select"]["avg_ret"] > base["select"]["avg_ret"]
                  and best["report"]["avg_ret"] is not None and base["report"]["avg_ret"] is not None
                  and best["report"]["avg_ret"] >= base["report"]["avg_ret"])
    return {"yours": base["report"], "best": best["report"] if best else None, "best_exit": best["exit"] if best else None,
            "improved": better, "tested": len(rest)}
