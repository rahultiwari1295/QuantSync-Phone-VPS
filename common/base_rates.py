"""
common/base_rates.py — measured historical odds for a chart state.

Replaces asserted claims in the DNA page ("Low win rate: new lows are more likely")
with measured ones:  "Of 38,412 past cases in this state, 31% rose 10% before
falling 7% within 60 sessions (all stocks: 43%)."

State (computed only from bars up to the day, so no look-ahead):
  trend   UP    close > EMA200 and EMA200 rising over 20 sessions
          DOWN  close < EMA200 and EMA200 falling
          MIXED otherwise
  rsi     Wilder RSI(14) bucket of width 10 (e.g. "40-50")
  high52  distance below the 52-week high: 0-5 / 5-15 / 15-30 / 30+
Outcome from the next session on:
  hit10   +10% reached before -7%  (first touch, 60-session window)
  ret20   close-to-close return after 20 sessions
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from typing import Dict, Optional

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DB = os.path.join(_ROOT, "Data", "stock_history.db")
CACHE_DB = os.path.join(_ROOT, "Data", "stock_dna_cache.db")

UP, DN, HORIZON = 0.10, 0.07, 60
MIN_SAMPLES = 300


def ema(arr: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = a * arr[i] + (1 - a) * out[i - 1]
    return out


def wilder_rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(close, prepend=close[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    a = 1.0 / n
    au, ad = np.empty_like(close), np.empty_like(close)
    au[0], ad[0] = up[0], dn[0]
    for i in range(1, len(close)):
        au[i] = a * up[i] + (1 - a) * au[i - 1]
        ad[i] = a * dn[i] + (1 - a) * ad[i - 1]
    rs = np.divide(au, ad, out=np.full_like(au, np.inf), where=ad > 0)
    return 100 - 100 / (1 + rs)


def trend_label(close: float, ema200: float, ema200_20ago: float) -> str:
    if close > ema200 and ema200 > ema200_20ago:
        return "UP"
    if close < ema200 and ema200 < ema200_20ago:
        return "DOWN"
    return "MIXED"


def rsi_bucket(rsi: float) -> str:
    lo = int(max(0, min(90, (rsi // 10) * 10)))
    return f"{lo}-{lo + 10}"


def high_bucket(dist_below_high_pct: float) -> str:
    d = abs(dist_below_high_pct)
    return "0-5" if d < 5 else "5-15" if d < 15 else "15-30" if d < 30 else "30+"


def state_key(trend: str, rsi: float, dist_below_high_pct: float) -> str:
    return f"{trend}|{rsi_bucket(rsi)}|{high_bucket(dist_below_high_pct)}"


def describe_state(key: str) -> str:
    trend, rsi, hb = key.split("|")
    t = {"UP": "above a rising 200-day line", "DOWN": "below a falling 200-day line",
         "MIXED": "around a flat 200-day line"}[trend]
    return f"{t}, RSI {rsi}, {hb}% below the 52-week high"


def _first_touch(h: np.ndarray, l: np.ndarray, entry: float) -> Optional[int]:
    up, dn = entry * (1 + UP), entry * (1 - DN)
    for hi, lo in zip(h, l):
        if lo <= dn:
            return 0
        if hi >= up:
            return 1
    return None


def build(hist_db: str = HIST_DB, cache_db: str = CACHE_DB, step: int = 3, until: Optional[str] = None) -> Dict:
    conn = sqlite3.connect(hist_db)
    rows = conn.execute("SELECT symbol, rawCandles FROM StockHistory").fetchall()
    conn.close()
    agg: Dict[str, list] = {}
    for sym, raw in rows:
        try:
            cs = json.loads(raw)
        except Exception:
            continue
        cs = [b for b in cs if not b.get("p")]
        if len(cs) < 320:
            continue
        if until:
            cs = [b for b in cs if str(b["t"])[:10] <= until]
        c = np.array([float(b["c"]) for b in cs]); h = np.array([float(b["h"]) for b in cs]); l = np.array([float(b["l"]) for b in cs])
        if (c <= 0).any():
            continue
        e200 = ema(c, 200); r = wilder_rsi(c)
        n = len(c)
        for i in range(252, n - HORIZON - 1, step):
            # skip windows with an unexplained >40% overnight jump (unadjusted corporate action)
            win = c[i - 1:i + HORIZON + 1]
            if np.any(np.abs(np.diff(np.log(win))) > 0.35):
                continue
            hi52 = h[i - 251:i + 1].max()
            key = state_key(trend_label(c[i], e200[i], e200[i - 20]), r[i], (c[i] / hi52 - 1) * 100)
            o = _first_touch(h[i + 1:i + 1 + HORIZON], l[i + 1:i + 1 + HORIZON], c[i])
            ret20 = c[i + 20] / c[i] - 1
            for k in (key, "ALL"):
                a = agg.setdefault(k, [0, 0, 0, []])
                a[0] += 1
                if o is not None:
                    a[1] += 1
                    a[2] += o
                if len(a[3]) < 20000:
                    a[3].append(ret20)
    now = _dt.datetime.now().isoformat(timespec="seconds")
    out = sqlite3.connect(cache_db)
    out.execute("""CREATE TABLE IF NOT EXISTS dna_base_rates(state_key TEXT PRIMARY KEY, samples INT, resolved INT,
                   p_hit10_before_dn7 REAL, median_ret_20d REAL, p_positive_20d REAL, built_at TEXT, data_to TEXT)""")
    out.execute("DELETE FROM dna_base_rates")
    for k, (n_, res, wins, rets) in agg.items():
        rr = np.array(rets) if rets else np.array([0.0])
        out.execute("INSERT INTO dna_base_rates VALUES (?,?,?,?,?,?,?,?)",
                    (k, n_, res, round(wins / res, 4) if res else None, round(float(np.median(rr)) * 100, 2),
                     round(float((rr > 0).mean()), 4), now, until))
    out.commit()
    out.close()
    return {"states": len(agg), "all": agg.get("ALL", [0])[0]}


_CACHE: Dict[str, dict] = {}


def lookup(key: str, cache_db: str = CACHE_DB) -> Optional[dict]:
    global _CACHE
    if not _CACHE:
        try:
            c = sqlite3.connect(cache_db)
            for row in c.execute("SELECT state_key, samples, resolved, p_hit10_before_dn7, median_ret_20d, p_positive_20d FROM dna_base_rates"):
                _CACHE[row[0]] = dict(zip(["state_key", "samples", "resolved", "p_hit", "median_ret_20d", "p_positive_20d"], row))
            c.close()
        except sqlite3.Error:
            return None
    r = _CACHE.get(key)
    if not r or r["samples"] < MIN_SAMPLES or r["p_hit"] is None:
        return None
    base = _CACHE.get("ALL")
    return {**r, "baseline_p_hit": base["p_hit"] if base else None, "state_text": describe_state(key)}


def sentence(info: dict) -> str:
    p = round(info["p_hit"] * 100)
    b = round((info["baseline_p_hit"] or 0) * 100)
    return (f"History check: of {info['samples']:,} past cases {info['state_text']}, "
            f"{p}% rose 10% before falling 7% within 60 sessions (all stocks: {b}%). "
            f"Median 20-session move: {info['median_ret_20d']:+.1f}%.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hist-db", default=HIST_DB)
    ap.add_argument("--cache-db", default=CACHE_DB)
    ap.add_argument("--step", type=int, default=3)
    a = ap.parse_args()
    print(build(a.hist_db, a.cache_db, a.step))
