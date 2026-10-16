from __future__ import annotations

import numpy as np
import pandas as pd

MIN_TRADES_SHOWN = 30
METRICS_V = 3  # bump when a summary definition changes; cached past tests from older versions are recomputed  # win rate is not published below this many closed trades


def _clean_num(v, ndigits=2):
    if v is None or not np.isfinite(v):
        return None
    return round(float(v), ndigits)


def summarize(t: pd.DataFrame) -> dict:
    c = t[~t["is_open"]].copy() if len(t) else t
    if len(c):
        c = c[np.isfinite(c["ret_pct"])]
    n = len(c)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_ret": None, "median_ret": None,
                "profit_factor": None, "max_dd": None, "avg_bars": None, "best": None, "worst": None}
    r = c["ret_pct"].to_numpy()
    w, ls = r[r > 0], r[r <= 0]
    # Equal-weight curve: trades closing on the same day are averaged, then compounded.
    # (A plain running sum of per-trade % gave dips beyond -100%, which is meaningless.)
    daily = c.groupby("exit_date")["ret_pct"].mean().sort_index().to_numpy() / 100.0
    eq = np.cumprod(1.0 + np.maximum(daily, -0.999))
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    dd = float(((eq / peak - 1.0) * 100).min()) if len(eq) else None
    dd = dd if dd is not None and np.isfinite(dd) else None
    pf = float(w.sum() / -ls.sum()) if ls.sum() < 0 and np.isfinite(w.sum() / -ls.sum()) else None
    return {"trades": n, "wins": int(len(w)), "losses": int(len(ls)),
            "win_rate": _clean_num(len(w) / n * 100, 1),
            "avg_ret": _clean_num(r.mean(), 2),
            "median_ret": _clean_num(np.median(r), 2),
            "profit_factor": _clean_num(pf, 2),
            "max_dd": _clean_num(dd, 2),
            "avg_bars": _clean_num(c["bars"].mean(), 1),
            "best": _clean_num(r.max(), 2),
            "worst": _clean_num(r.min(), 2)}


def by_year(t: pd.DataFrame, years: list[int]) -> dict:
    out = {"All": summarize(t[t["entry_date"] >= pd.Timestamp(f"{min(years)}-01-01")] if len(t) else t)}
    for y in years:
        m = t["entry_date"].dt.year == y if len(t) else []
        out[str(y)] = summarize(t[m] if len(t) else t)
    return out


def monthly_counts(t: pd.DataFrame) -> list[dict]:
    if not len(t):
        return []
    g = t.groupby(t["entry_date"].dt.strftime("%Y-%m"))["ret_pct"]
    return [{"month": k, "trades": int(len(v)), "win_rate": round(float((v > 0).mean() * 100), 1)} for k, v in g]


def _downsample(pts: list, n: int) -> list:
    if len(pts) <= n:
        return pts
    idx = np.linspace(0, len(pts) - 1, n).round().astype(int)
    return [pts[i] for i in sorted(set(idx.tolist()))]


EXIT_GROUP = {"stop": "Stop hit", "stop_pct": "Stop hit", "atr_stop": "Stop hit", "STOP_HIT": "Stop hit", "STOP_LOSS": "Stop hit",
              "target": "Target hit", "target_pct": "Target hit", "atr_target": "Target hit", "TARGET_HIT": "Target hit",
              "trail": "Trailing stop", "trail_pct": "Trailing stop", "trail_atr": "Trailing stop", "SYSTEM_TARGET_OR_TRAIL": "Trailing stop",
              "time": "Time limit", "TIME_EXIT": "Time limit", "rule": "Exit rule", "max_hold": "Time limit"}


def exit_label(r) -> str:
    if r is None or (isinstance(r, float) and not np.isfinite(r)):
        return "Other"
    s = str(r)
    return EXIT_GROUP.get(s) or EXIT_GROUP.get(s.lower()) or s.replace("_", " ").capitalize()


def analytics(t: pd.DataFrame) -> dict:
    """Chart data for a strategy page, computed from closed trades only (same equal-weight convention as summarize).

    equity:  value of 100 compounded through each exit day (trades closing the same day are averaged)
    monthly: compounded return of the trades that closed in each month
    dist:    how many trades ended in each 2%-wide return bucket (tails clipped at +/-14%)
    exits:   share of trades by how they closed
    """
    empty = {"equity": [], "spark": [], "monthly": [], "dist": [], "exits": [], "avg_win": None, "avg_loss": None,
             "max_losing_streak": 0, "first_date": None, "last_date": None}
    if not len(t):
        return empty
    c = t[~t["is_open"].astype(bool)].copy()
    c = c[np.isfinite(c["ret_pct"])] if len(c) else c
    c = c[c["exit_date"].notna()] if len(c) else c
    if not len(c):
        return empty
    c["exit_date"] = pd.to_datetime(c["exit_date"])
    daily = c.groupby("exit_date")["ret_pct"].mean().sort_index()
    eq = 100 * np.cumprod(1.0 + np.maximum(daily.to_numpy() / 100.0, -0.999))
    pts = [{"d": d.strftime("%Y-%m-%d"), "v": round(float(v), 2)} for d, v in zip(daily.index, eq)]
    start = (c["entry_date"].min() if "entry_date" in c else daily.index[0])
    pts = [{"d": pd.Timestamp(start).strftime("%Y-%m-%d"), "v": 100.0}] + pts
    m = c.groupby(c["exit_date"].dt.strftime("%Y-%m"))
    monthly = []
    for k, g in m:
        dd = g.groupby("exit_date")["ret_pct"].mean().to_numpy() / 100.0
        monthly.append({"month": k, "ret": round(float((np.prod(1 + np.maximum(dd, -0.999)) - 1) * 100), 2),
                        "trades": int(len(g)), "win_rate": round(float((g["ret_pct"] > 0).mean() * 100), 1)})
    r = c["ret_pct"].to_numpy()
    edges = list(range(-14, 16, 2))
    clipped = np.clip(r, -14.999, 15.999)
    dist = []
    for lo in edges[:-1]:
        n = int(((clipped >= lo) & (clipped < lo + 2)).sum())
        dist.append({"lo": lo, "hi": lo + 2, "n": n})
    reasons = c["exit_reason"].map(exit_label) if "exit_reason" in c else pd.Series(["Other"] * len(c))
    vc = reasons.value_counts()
    exits = [{"label": k, "n": int(v), "pct": round(float(v / len(c) * 100), 1)} for k, v in vc.items()]
    streak = best = 0
    for x in c.sort_values("exit_date")["ret_pct"].to_numpy():
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    w, ls = r[r > 0], r[r <= 0]
    return {"equity": _downsample(pts, 400), "spark": [p["v"] for p in _downsample(pts, 32)], "monthly": monthly, "dist": dist,
            "exits": exits, "avg_win": _clean_num(w.mean()) if len(w) else None, "avg_loss": _clean_num(ls.mean()) if len(ls) else None,
            "max_losing_streak": int(best), "first_date": pts[0]["d"], "last_date": pts[-1]["d"]}
