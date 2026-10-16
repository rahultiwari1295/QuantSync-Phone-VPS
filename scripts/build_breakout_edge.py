#!/usr/bin/env python3
"""
build_breakout_edge.py — measure how much each breakout / retest signal is really worth, and write
Data/breakout_edge.json (read by common.trade_rules and shown next to every Radar signal).

How it is measured (no look-ahead):
  1. Events: first close above a real resistance level (highest high of the last 60/120/250 daily bars,
     26/52 weekly, 24/36 monthly bars; touched at least twice, 5+ bars apart; the high is 8+ bars old).
  2. Replay: for every event the REAL state machine (common.trade_rules.evaluate_arrays) is run bar by bar using only
     bars up to that day. The first bar on which it says BUY is the signal. Entry = next bar open.
  3. Outcome: stop = level - 1 ATR, target = entry + 2R, time stop (25 daily / 12 weekly / 6 monthly bars),
     0.25% round-trip cost. R = profit measured in units of the money risked.
  4. 'NAIVE' = the old rule (buy the next open after ANY close above the level) — kept as the yardstick.
  5. Split: events before 2023 = in-sample, 2023+ = out-of-sample. A cell is VALIDATED only if both are positive.

Usage:  python scripts/build_breakout_edge.py [--db Data/stock_history.db] [--out Data/breakout_edge.json]
"""
import argparse
import json
import math
import os
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from common import trade_rules as tr  # noqa: E402

LEVEL_WINDOWS = {"D": (60, 120, 250), "W": (26, 52), "M": (24, 36)}
DEDUP_BARS = {"D": 20, "W": 8, "M": 3}
OOS_FROM_YEAR = 2023
MIN_TURNOVER = 2e6          # Rs 20 lakh median daily turnover
MIN_PRICE = 20.0


def load_candles(db: str, min_bars: int = 400):
    import sqlite3
    out = {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    for sym, raw in con.execute("SELECT symbol, rawCandles FROM StockHistory"):
        try:
            rows = json.loads(raw)
        except Exception:
            continue
        if len(rows) < min_bars:
            continue
        df = pd.DataFrame(rows).rename(columns={"t": "d"})
        df["d"] = pd.to_datetime(df["d"])
        df = df.sort_values("d").drop_duplicates("d")
        df = df[(df.c > 0) & (df.h >= df.l) & (df.o > 0)]
        if len(df) < min_bars or df.c.median() < MIN_PRICE or (df.v * df.c).tail(60).median() < MIN_TURNOVER:
            continue
        out[sym] = df.reset_index(drop=True)
    con.close()
    return out


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "D":
        return df
    rule = "W-FRI" if tf == "W" else "ME"
    try:
        g = df.set_index("d").resample(rule)
    except ValueError:                       # older pandas
        g = df.set_index("d").resample("M")
    return g.agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna().reset_index()


def scan_symbol(args):
    sym, df0, tf = args
    df = resample(df0, tf)
    cfg = tr.TF_CFG[tf]
    Ws = LEVEL_WINDOWS[tf]
    o, h, l, c, v = [df[k].to_numpy(float) for k in "ohlcv"]
    d = df["d"].to_numpy()
    n = len(c)
    if n < max(Ws) + cfg["horizon"] + 10:
        return []
    atr = tr.atr_series(h, l, c)
    rmax = {W: pd.Series(h).rolling(W).max().shift(1).to_numpy() for W in Ws}
    rows, last = [], {}
    horizon = cfg["horizon"]
    for i in range(max(Ws) + 2, n - horizon - 3):
        if not atr[i] > 0:
            continue
        W = None
        for w_ in sorted(Ws, reverse=True):
            L = rmax[w_][i]
            if L == L and c[i] > L and c[i - 1] <= rmax[w_][i - 1] * 1.0001 and c[i - 1] <= L:
                W = w_
                break
        if W is None:
            continue
        L = float(rmax[W][i])
        if i - last.get(round(L, 0), -999) < DEDUP_BARS[tf]:
            continue
        win = h[i - W:i]
        touches, prev = 0, -99
        for x in np.flatnonzero(win >= L * 0.985):
            if x - prev >= 5:
                touches += 1
                prev = x
        if touches < 2 or W - int(np.argmax(win)) < 8:
            continue
        last[round(L, 0)] = i
        year = int(pd.Timestamp(d[i]).year)

        # NAIVE yardstick: next open after the first close above the level, stop level-1ATR, target 2R
        stop = L - tr.STOP_ATR * atr[i]
        naive = tr.simulate(o, h, l, c, i + 1, stop, horizon)
        a_buf = (c[i] - L) / atr[i]
        hi_ref = float(np.max(h[max(0, i - cfg["hi_lookback"]):i]))
        ema = tr.ema_series(c[max(0, i - 420):i + 1], cfg["ema"])
        g_naive = tr.grade_event(a_buf, max(0.0, (hi_ref - L) / atr[i]), bool(c[i] > ema[-1]))
        if naive:
            rows.append(dict(sym=sym, tf=tf, kind="NAIVE", grade=g_naive, year=year, R=naive[0], exit=naive[2],
                             risk_pct=(o[i + 1] - stop) / o[i + 1], risk_atr=(o[i + 1] - stop) / atr[i],
                             date=str(pd.Timestamp(d[i]).date())))

        # REPLAY: run the live state machine day by day; first BUY is the signal
        end = min(n - 2, i + cfg["retest_window"] + tr.ENTRY_AGE_MAX + 2)
        seen = set()
        for k in range(i, end + 1):
            res = tr.evaluate_arrays(o[:k + 1], h[:k + 1], l[:k + 1], c[:k + 1], v[:k + 1], L, tf, edge={}, grade_gate=False)
            if res["action"] == "BUY" and res["kind"] not in seen:
                seen.add(res["kind"])
                p = res["plan"]
                r = tr.simulate(o, h, l, c, k + 1, p["stop"], horizon)
                if r:
                    rows.append(dict(sym=sym, tf=tf, kind=res["kind"], grade=res["grade"], year=year, R=r[0], exit=r[2],
                                     risk_pct=(o[k + 1] - p["stop"]) / o[k + 1], risk_atr=(o[k + 1] - p["stop"]) / atr[k],
                                     date=str(pd.Timestamp(d[k]).date())))
            if res["state"] in ("FAILED", "STOPPED", "TARGET_ACHIEVED", "EXTENDED") and k > i:
                break
    return rows


def baseline_symbol(args):
    sym, df0, tf, risk_atr = args
    df = resample(df0, tf)
    cfg = tr.TF_CFG[tf]
    o, h, l, c = [df[k].to_numpy(float) for k in "ohlc"]
    d = df["d"].to_numpy()
    atr = tr.atr_series(h, l, c)
    rows, step = [], (7 if tf == "D" else 3)
    for i in range(max(LEVEL_WINDOWS[tf]) + 2, len(c) - cfg["horizon"] - 3, step):
        if not atr[i] > 0:
            continue
        stop = o[i + 1] - risk_atr * atr[i]
        r = tr.simulate(o, h, l, c, i + 1, stop, cfg["horizon"])
        if r:
            rows.append(dict(tf=tf, year=int(pd.Timestamp(d[i]).year), R=r[0], exit=r[2]))
    return rows


def month_t(x: pd.DataFrame):
    """Events cluster in time (a bull month throws up hundreds of breakouts), so n overstates certainty.
    Average within each calendar month first, then t-test across months."""
    if "date" not in x or len(x) < 30:
        return None
    m = x.groupby(x.date.str[:7]).R.agg(["mean", "size"])
    m = m[m["size"] >= 3]["mean"]
    if len(m) < 8 or m.std() == 0:
        return None
    return dict(months=int(len(m)), pos_months=round(float((m > 0).mean()), 2), t=round(float(m.mean() / (m.std() / math.sqrt(len(m)))), 2))


def cell(x: pd.DataFrame, base_r: float = 0.0):
    n = len(x)
    if n == 0:
        return None
    ins, oos = x[x.year < OOS_FROM_YEAR], x[x.year >= OOS_FROM_YEAR]
    hits = int((x.exit == "TARGET").sum())
    mean_r = float(x.R.mean())
    ok = n >= 200 and mean_r > 0.05 and len(ins) >= 30 and ins.R.mean() > 0 and len(oos) >= 30 and oos.R.mean() > 0
    mt = month_t(x)
    if ok and mt and mt["t"] < 2.0:                       # not robust once clustering is respected
        ok = False
    verdict = "VALIDATED" if ok else ("POSITIVE_LOW_SAMPLE" if mean_r > 0 and (n < 200 or (mt and mt["t"] < 2.0)) else "NO_EDGE")
    return dict(n=n, win_rate=round(float((x.R > 0).mean()), 3), hit_target=round(hits / n, 3),
                hit_ci95=tr.wilson(hits, n), mean_R=round(mean_r, 3), median_R=round(float(x.R.median()), 3),
                is_n=len(ins), is_mean_R=round(float(ins.R.mean()), 3) if len(ins) else None,
                oos_n=len(oos), oos_mean_R=round(float(oos.R.mean()), 3) if len(oos) else None,
                stop_rate=round(float(x.exit.isin(["STOP", "GAP"]).mean()), 3), vs_baseline_R=round(mean_r - base_r, 3), clustered=mt,
                avg_risk_pct=round(100 * float(x.risk_pct.mean()), 1), verdict=verdict)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "Data", "stock_history.db"))
    ap.add_argument("--out", default=tr.EDGE_PATH)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    t0 = time.time()
    data = load_candles(a.db)
    if a.limit:
        data = dict(list(data.items())[:a.limit])
    jobs = [(s, df, tf) for s, df in data.items() for tf in ("D", "W", "M")]
    with Pool(a.workers) as p:
        res = p.map(scan_symbol, jobs, chunksize=20)
    ev = pd.DataFrame([r for rs in res for r in rs])
    if ev.empty:
        print("no events"); return 1
    baseline = {}
    for tf in ("D", "W", "M"):
        sig = ev[(ev.tf == tf) & (ev.kind == "NAIVE")]
        if sig.empty:
            continue
        risk_atr = float(sig.risk_atr.median())
        with Pool(a.workers) as p:
            br = p.map(baseline_symbol, [(s_, df, tf, risk_atr) for s_, df in data.items()], chunksize=20)
        b = pd.DataFrame([r for rs in br for r in rs])
        if len(b):
            baseline[tf] = dict(n=int(len(b)), mean_R=round(float(b.R.mean()), 3), hit_target=round(float((b.exit == "TARGET").mean()), 3),
                                win_rate=round(float((b.R > 0).mean()), 3), risk_atr=round(risk_atr, 2),
                                yearly={str(y): round(float(g.R.mean()), 3) for y, g in b.groupby("year")})
    cells = {}
    for (tf, kind, grade), g in ev.groupby(["tf", "kind", "grade"]):
        cells[f"{tf}|{kind}|{grade}"] = cell(g, (baseline.get(tf) or {}).get("mean_R", 0.0))
    a_d = ev[(ev.tf == "D") & (ev.kind == "BREAKOUT") & (ev.grade == "A")]
    out = dict(built_at=time.strftime("%Y-%m-%dT%H:%M:%S"), symbols=len(data),
               events=int(len(ev)), oos_from_year=OOS_FROM_YEAR,
               params=dict(decisive_atr=tr.DECISIVE_ATR, headroom_max_atr=tr.HEADROOM_MAX_ATR, stop_atr=tr.STOP_ATR,
                           target_r=tr.TARGET_R, cost=tr.COST),
               date_range=[str(ev.date.min()), str(ev.date.max())], cells=cells, baseline=baseline,
               yearly={f"{tf}|{kind}|{grade}": {str(y): dict(n=int(len(g)), mean_R=round(float(g.R.mean()), 3)) for y, g in gg.groupby("year")}
                       for (tf, kind, grade), gg in ev.groupby(["tf", "kind", "grade"]) if len(gg) >= 100},
               yearly_D_breakout_A={str(y): dict(n=int(len(g)), mean_R=round(float(g.R.mean()), 3)) for y, g in a_d.groupby("year")},
               how="Point-in-time replay of common.trade_rules; entry next open; stop level-1ATR; target 2R; 0.25% costs. "
                   "R = profit in units of money risked. NAIVE = buy any close above the level (the old rule).")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"{len(data)} symbols, {len(ev)} events, {time.time() - t0:.0f}s -> {a.out}")
    for k in sorted(cells):
        c = cells[k]
        print(f"{k:22s} n={c['n']:5d} win={c['win_rate']:.2f} hit2R={c['hit_target']:.2f} meanR={c['mean_R']:+.3f} "
              f"IS={c['is_mean_R']} OOS={c['oos_mean_R']} vsBase={c['vs_baseline_R']:+.3f} risk={c['avg_risk_pct']}% t={(c['clustered'] or {}).get('t')} {c['verdict']}")
    print("baseline:", {k: (v['mean_R'], v['hit_target']) for k, v in baseline.items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
