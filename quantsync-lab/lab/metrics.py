from __future__ import annotations

import numpy as np
import pandas as pd

MIN_TRADES_SHOWN = 30
METRICS_V = 4  # bump when a summary definition changes; cached past tests from older versions are recomputed  # win rate is not published below this many closed trades


def _clean_num(v, ndigits=2):
    if v is None or not np.isfinite(v):
        return None
    return round(float(v), ndigits)


def portfolio(trades, slots: int = 10, capital: float = 100_000.0, seed: int = 7) -> dict:
    """A slot-based portfolio simulation from the trade list.

    Rules:
    - Capital is split into `slots` equal parts. A trade takes one free slot at its `entry_date`;
      the slot's money is `equity_at_entry / slots`.
    - If more trades start on a day than there are free slots, choose which ones to take with a
      seeded random shuffle (seed fixed, so results are reproducible and no future information is
      used to pick). Count the ones not taken (`skipped`).
    - A slot is freed on the trade's `exit_date`; its money comes back as `allocated * (1 + ret_pct/100)`
      (ret_pct already includes costs).
    - Equity is marked at each exit day (realised). Optional v2: daily mark-to-market using bars;
      not required now.
    - Idle cash earns nothing.
    - Return: {"equity":[{"d","v"}...], "max_dd", "cagr", "final", "taken", "skipped",
               "avg_slots_used", "slots", "capital"}
    """
    empty = {
        "equity": [],
        "max_dd": None,
        "cagr": None,
        "final": float(capital),
        "taken": 0,
        "skipped": 0,
        "avg_slots_used": 0.0,
        "slots": slots,
        "capital": float(capital),
    }
    if trades is None:
        return empty

    import random
    from collections import defaultdict

    if isinstance(trades, pd.DataFrame):
        if len(trades) == 0:
            return empty
        c = trades[~trades["is_open"].astype(bool)].copy() if "is_open" in trades.columns else trades.copy()
        if "ret_pct" in c.columns:
            c = c[np.isfinite(c["ret_pct"])]
        if "entry_date" in c.columns:
            c = c[c["entry_date"].notna()]
        if "exit_date" in c.columns:
            c = c[c["exit_date"].notna()]
        if len(c) == 0:
            return empty
        records = []
        for r in c.to_dict("records"):
            records.append({
                "symbol": r.get("symbol", ""),
                "entry_date": pd.Timestamp(r["entry_date"]),
                "exit_date": pd.Timestamp(r["exit_date"]),
                "ret_pct": float(r["ret_pct"]),
            })
    elif isinstance(trades, list):
        records = []
        for r in trades:
            if r.get("is_open"):
                continue
            ret = r.get("ret_pct")
            if ret is None or not np.isfinite(ret):
                continue
            if not r.get("entry_date") or not r.get("exit_date"):
                continue
            records.append({
                "symbol": r.get("symbol", ""),
                "entry_date": pd.Timestamp(r["entry_date"]),
                "exit_date": pd.Timestamp(r["exit_date"]),
                "ret_pct": float(ret),
            })
        if not records:
            return empty
    else:
        return empty

    by_entry = defaultdict(list)
    for r in records:
        by_entry[r["entry_date"]].append(r)

    all_dates = sorted(set([r["entry_date"] for r in records] + [r["exit_date"] for r in records]))
    if not all_dates:
        return empty

    rng = random.Random(seed)
    cash = float(capital)
    current_equity = float(capital)
    active_slots = []  # list of {"trade": ..., "allocated": float, "exit_date": Timestamp}

    taken_trades = []
    skipped_count = 0
    raw_equity_pts = []  # (date, value)
    peak = float(capital)
    max_dd = 0.0
    first_entry_date = None

    for day in all_dates:
        # 1. Process exits on or before this day
        exiting = []
        remaining = []
        for s_item in active_slots:
            if s_item["exit_date"] <= day:
                exiting.append(s_item)
            else:
                remaining.append(s_item)
        active_slots = remaining

        if exiting:
            for s_item in exiting:
                ret = s_item["trade"]["ret_pct"]
                returned = s_item["allocated"] * (1.0 + ret / 100.0)
                cash += returned
            current_equity = cash + sum(it["allocated"] for it in active_slots)
            peak = max(peak, current_equity)
            dd = (current_equity - peak) / peak * 100.0 if peak > 0 else 0.0
            max_dd = min(max_dd, dd)
            raw_equity_pts.append((day, round(current_equity, 2)))

        # 2. Process entries on this day
        candidates = by_entry.get(day, [])
        if candidates:
            free = slots - len(active_slots)
            if free > 0:
                candidates = sorted(candidates, key=lambda x: (str(x.get("symbol", "")), x["exit_date"], x["ret_pct"]))
                rng.shuffle(candidates)
                selected = candidates[:free]
                unselected = candidates[free:]
            else:
                selected = []
                unselected = candidates

            skipped_count += len(unselected)

            for t in selected:
                if first_entry_date is None or t["entry_date"] < first_entry_date:
                    first_entry_date = t["entry_date"]
                alloc = current_equity / slots
                cash -= alloc
                taken_trades.append((t, alloc))
                if t["exit_date"] <= day:
                    returned = alloc * (1.0 + t["ret_pct"] / 100.0)
                    cash += returned
                    current_equity = cash + sum(it["allocated"] for it in active_slots)
                    peak = max(peak, current_equity)
                    dd = (current_equity - peak) / peak * 100.0 if peak > 0 else 0.0
                    max_dd = min(max_dd, dd)
                    raw_equity_pts.append((day, round(current_equity, 2)))
                else:
                    active_slots.append({"trade": t, "allocated": alloc, "exit_date": t["exit_date"]})

    final_equity = cash + sum(it["allocated"] for it in active_slots)
    final_val = round(final_equity, 2)

    pts = []
    if first_entry_date is not None:
        start_str = first_entry_date.strftime("%Y-%m-%d")
        pts.append({"d": start_str, "v": float(capital)})

    for d, val in raw_equity_pts:
        d_str = d.strftime("%Y-%m-%d")
        if pts and pts[-1]["d"] == d_str:
            pts[-1]["v"] = val
        else:
            pts.append({"d": d_str, "v": val})

    if len(pts) == 1:
        pts.append({"d": pts[0]["d"], "v": final_val})

    cagr = None
    if taken_trades and first_entry_date is not None:
        last_exit_date = max(t["exit_date"] for t, _ in taken_trades)
        total_days = (last_exit_date - first_entry_date).days
        years = total_days / 365.25
        if years >= 0.1 and final_val > 0:
            cagr = round(((final_val / capital) ** (1.0 / years) - 1.0) * 100.0, 2)
        elif final_val > 0:
            cagr = round((final_val / capital - 1.0) * 100.0, 2)
        else:
            cagr = -100.0

    avg_slots = 0.0
    if taken_trades and first_entry_date is not None:
        last_exit_date = max(t["exit_date"] for t, _ in taken_trades)
        total_period_days = max(1, (last_exit_date - first_entry_date).days)
        total_slot_days = sum(max(1, (t["exit_date"] - t["entry_date"]).days) for t, _ in taken_trades)
        avg_slots = round(min(float(slots), total_slot_days / total_period_days), 1)

    return {
        "equity": _downsample(pts, 400),
        "max_dd": _clean_num(max_dd, 2),
        "cagr": _clean_num(cagr, 2),
        "final": final_val,
        "taken": len(taken_trades),
        "skipped": skipped_count,
        "avg_slots_used": avg_slots,
        "slots": slots,
        "capital": float(capital),
    }


def summarize(t: pd.DataFrame, slots: int = 10) -> dict:
    c = t[~t["is_open"].astype(bool)].copy() if len(t) and "is_open" in t.columns else (t.copy() if len(t) else t)
    if len(c) and "ret_pct" in c.columns:
        c = c[np.isfinite(c["ret_pct"])]
    n = len(c)
    if n == 0:
        port = portfolio(t, slots=slots)
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_ret": None, "median_ret": None,
                "profit_factor": None, "max_dd": None, "avg_bars": None, "best": None, "worst": None, "portfolio": port}
    r = c["ret_pct"].to_numpy()
    w, ls = r[r > 0], r[r <= 0]
    port = portfolio(c, slots=slots)
    dd = port.get("max_dd")
    pf = float(w.sum() / -ls.sum()) if ls.sum() < 0 and np.isfinite(w.sum() / -ls.sum()) else None
    return {"trades": n, "wins": int(len(w)), "losses": int(len(ls)),
            "win_rate": _clean_num(len(w) / n * 100, 1),
            "avg_ret": _clean_num(r.mean(), 2),
            "median_ret": _clean_num(np.median(r), 2),
            "profit_factor": _clean_num(pf, 2),
            "max_dd": _clean_num(dd, 2),
            "avg_bars": _clean_num(c["bars"].mean(), 1) if "bars" in c.columns else None,
            "best": _clean_num(r.max(), 2),
            "worst": _clean_num(r.min(), 2),
            "portfolio": port}


def by_year(t: pd.DataFrame, years: list[int], slots: int = 10) -> dict:
    out = {"All": summarize(t[t["entry_date"] >= pd.Timestamp(f"{min(years)}-01-01")] if len(t) else t, slots=slots)}
    for y in years:
        m = t["entry_date"].dt.year == y if len(t) else []
        out[str(y)] = summarize(t[m] if len(t) else t, slots=slots)
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


def analytics(t: pd.DataFrame, slots: int = 10) -> dict:
    """Chart data for a strategy page, computed from closed trades only.

    equity:  portfolio equity curve with up to `slots` concurrent slots (starts at capital=100,000)
    spark:   32-point sparkline derived from portfolio equity
    monthly: compounded return of the trades that closed in each month
    dist:    how many trades ended in each 2%-wide return bucket (tails clipped at +/-14%)
    exits:   share of trades by how they closed
    """
    empty = {"equity": [], "spark": [], "monthly": [], "dist": [], "exits": [], "avg_win": None, "avg_loss": None,
             "max_losing_streak": 0, "first_date": None, "last_date": None,
             "portfolio": portfolio(None, slots=slots)}
    if not len(t):
        return empty
    c = t[~t["is_open"].astype(bool)].copy() if "is_open" in t.columns else t.copy()
    c = c[np.isfinite(c["ret_pct"])] if "ret_pct" in c.columns and len(c) else c
    c = c[c["exit_date"].notna()] if "exit_date" in c.columns and len(c) else c
    if not len(c):
        return empty

    c["exit_date"] = pd.to_datetime(c["exit_date"])
    port = portfolio(c, slots=slots)
    pts = port["equity"]

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
    return {"equity": pts, "spark": [p["v"] for p in _downsample(pts, 32)], "monthly": monthly, "dist": dist,
            "exits": exits, "avg_win": _clean_num(w.mean()) if len(w) else None, "avg_loss": _clean_num(ls.mean()) if len(ls) else None,
            "max_losing_streak": int(best), "first_date": pts[0]["d"] if pts else None, "last_date": pts[-1]["d"] if pts else None,
            "portfolio": port}
