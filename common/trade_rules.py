"""
common/trade_rules.py — the ONE definition of breakout / retest states and the trade plan.

Why this exists
---------------
Before: "CONFIRMED" meant "last close is above the level" (classical_pattern_dna), "SUCCESS" meant
"price is >3% above the level" (build_screener_snapshot) and "retest" was an SQL guess (radar_feed).
Tested on 9,000 real breakouts (liquid NSE stocks, 2019-2026) a bare close above a level had NO edge
(+0.00R per trade, 34% closed back below the level within 15 bars).

What the data supports (see scripts/build_breakout_edge.py, which rebuilds Data/breakout_edge.json):
  * a DECISIVE close   — closes >= 0.8 ATR beyond the level (marginal closes mostly fail)
  * headroom           — little overhead supply: the 52-week high is < 3 ATR above the level
  * trend              — close above the long EMA (200d / 40w / 10m)
Volume and close-location were tested and did NOT help, so they are shown but never gate a signal.

All functions are point-in-time: they only look at bars <= the last bar.
Entry is ALWAYS "next bar open" (we only know a close after it prints); stop = level - 1 ATR;
target = entry + 2R; costs 0.25% round trip. These are exactly the numbers the edge table was measured with.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

# ── parameters (single source of truth; the edge table is measured with these) ──────────────
DECISIVE_ATR = 0.8          # close must be this many ATRs beyond the level
HEADROOM_MAX_ATR = 3.0      # 52w-high distance above the level, in ATRs
FAIL_ATR = 0.25             # a close this far below the level = breakout failed
STOP_ATR = 1.0              # stop sits this far below the level
TARGET_R = 2.0              # target = entry + TARGET_R * risk
RETEST_UP_ATR = 0.5         # dip must come within level + 0.5 ATR ...
RETEST_DOWN_ATR = 0.75      # ... but not further than level - 0.75 ATR
ENTRY_AGE_MAX = 2           # a fresh breakout is actionable for this many bars after the event
RETEST_ENTRY_AGE = 1        # a retest-hold is actionable this many bars after the dip bar
NEAR_ATR = 1.5              # "near" = closes within this many ATRs below the level
COST = 0.0025

TF_CFG = {
    #       horizon  event-window  52w-lookback  trend-EMA span
    "D": dict(horizon=25, window=40, hi_lookback=250, ema=200, retest_window=15),
    "W": dict(horizon=12, window=14, hi_lookback=52, ema=40, retest_window=6),
    "M": dict(horizon=6, window=8, hi_lookback=36, ema=10, retest_window=3),
}
TF_ALIASES = {"DAILY": "D", "WEEKLY": "W", "MONTHLY": "M", "D": "D", "W": "W", "M": "M"}

EDGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Data", "breakout_edge.json")


# ── small numeric helpers ───────────────────────────────────────────────────────────────────
def atr_series(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        cs = np.cumsum(np.r_[0.0, tr])
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def ema_series(x: np.ndarray, span: int) -> np.ndarray:
    a = 2.0 / (span + 1.0)
    out = np.empty(len(x))
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def grade_event(buf_atr: float, headroom_atr: float, above_trend: bool) -> str:
    """A = decisive + clear air + trend. B = decisive but one of those missing. C = marginal close (not a buy)."""
    if buf_atr < DECISIVE_ATR:
        return "C"
    if headroom_atr < HEADROOM_MAX_ATR and above_trend:
        return "A"
    return "B"


def wilson(k: int, n: int, z: float = 1.96) -> List[float]:
    if n <= 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def simulate(o, h, l, c, j: int, stop: float, horizon: int, target_r: float = TARGET_R):
    """Enter at o[j]; stop-first on ambiguous bars. Returns (R, bars, exit) with costs deducted."""
    e = float(o[j]); risk = e - stop
    if risk <= 0:
        return None
    tgt = e + target_r * risk
    cost_r = COST * e / risk
    end = min(j + horizon, len(c) - 1)
    for k in range(j, end + 1):
        if k == j and o[k] <= stop:
            return (-1.0 - cost_r, 0, "GAP")
        if l[k] <= stop:
            return (-1.0 - cost_r, k - j, "STOP")
        if h[k] >= tgt:
            return (target_r - cost_r, k - j, "TARGET")
    return ((float(c[end]) - e) / risk - cost_r, end - j, "TIME")


# ── edge table ──────────────────────────────────────────────────────────────────────────────
_EDGE_CACHE: Dict[str, Any] = {}


def load_edge(path: Optional[str] = None) -> Dict[str, Any]:
    p = path or EDGE_PATH
    try:
        mt = os.path.getmtime(p)
        if _EDGE_CACHE.get("p") == p and _EDGE_CACHE.get("mt") == mt:
            return _EDGE_CACHE["data"]
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        _EDGE_CACHE.update(p=p, mt=mt, data=data)
        return data
    except (OSError, ValueError):
        return {}


def edge_for(edge: Dict[str, Any], tf: str, kind: str, grade: str) -> Optional[Dict[str, Any]]:
    return ((edge or {}).get("cells") or {}).get(f"{tf}|{kind}|{grade}")


# A setup may trigger a BUY only if its measured edge is real: validated in-sample AND out-of-sample, robust once
# events are clustered by month, and clearly better than entering a random stock on a random day (market drift).
MIN_EDGE_VS_BASELINE_R = 0.10
# Used only if Data/breakout_edge.json is missing (first deploy): the two setups that passed in the study.
FALLBACK_TRADEABLE = {("D", "BREAKOUT", "A"), ("W", "BREAKOUT", "A")}


def tradeable(cell: Optional[Dict[str, Any]], tf: str, kind: str, grade: str, have_table: bool) -> bool:
    if not have_table:
        return (tf, kind, grade) in FALLBACK_TRADEABLE
    return bool(cell and cell.get("verdict") == "VALIDATED" and (cell.get("vs_baseline_R") or 0) >= MIN_EDGE_VS_BASELINE_R)


def describe_edge(cell: Optional[Dict[str, Any]], tf: str, kind: str, ok: bool) -> Dict[str, Any]:
    """Plain-language honesty line shown next to a signal. Never invents a number: it only restates the table."""
    if not cell:
        return dict(label="No track record", text="Not enough history to measure this setup — treat as unproven.")
    unit = {"D": "daily", "W": "weekly", "M": "monthly"}[tf]
    what = "retests" if kind == "RETEST" else "breakouts"
    exp_pct = round((cell.get("mean_R") or 0) * (cell.get("avg_risk_pct") or 0), 1)
    base_pct = round(((cell.get("mean_R") or 0) - (cell.get("vs_baseline_R") or 0)) * (cell.get("avg_risk_pct") or 0), 1)
    label = "Validated edge" if ok else ("Promising, not yet proven" if cell.get("verdict") == "POSITIVE_LOW_SAMPLE" else "No validated edge")
    text = (f"{cell['n']:,} similar {unit} {what} since 2020: {round(100 * cell['win_rate'])}% profitable, "
            f"{round(100 * cell['hit_target'])}% reached +2R before the stop, average {exp_pct:+.1f}% per trade after costs "
            f"(risking ~{cell.get('avg_risk_pct')}%). A random stock on a random day with the same stop/target made "
            f"{base_pct:+.1f}%.")
    return dict(label=label, text=text, p_target=cell["hit_target"], ci95=cell.get("hit_ci95"), win_rate=cell["win_rate"],
                expectancy_R=cell["mean_R"], expectancy_pct=exp_pct, n=cell["n"], vs_baseline_R=cell.get("vs_baseline_R"))


# ── the state machine ───────────────────────────────────────────────────────────────────────
def evaluate_arrays(o, h, l, c, v, level: float, tf: str = "D", edge: Optional[Dict[str, Any]] = None,
                    stance: str = "BULLISH", grade_gate: bool = True) -> Dict[str, Any]:
    """Classify where price is relative to `level` and return the trade call. Last bar = 'today'."""
    tf = TF_ALIASES.get(str(tf).upper(), "D")
    cfg = TF_CFG[tf]
    o, h, l, c, v = [np.asarray(x, dtype=float)[-420:] for x in (o, h, l, c, v)]
    n = len(c)
    out: Dict[str, Any] = dict(state="NA", action="NONE", grade=None, tf=tf, level=None, notes=[])
    if n < 40 or not level or level <= 0 or stance != "BULLISH":
        out["notes"].append("not enough bars" if n < 40 else "no bullish level")
        return out
    atr = atr_series(h, l, c)
    if not (atr[-1] > 0):
        out["notes"].append("no ATR")
        return out
    L = float(level)
    i = n - 1
    a_now = float(atr[i])
    out["level"] = round(L, 2)
    out["atr"] = round(a_now, 2)
    out["price"] = round(float(c[i]), 2)
    out["dist_atr"] = round((float(c[i]) - L) / a_now, 2)

    lo = max(1, n - 1 - cfg["window"] - cfg["retest_window"])
    thr = np.where(np.isnan(atr), -np.inf, L - FAIL_ATR * np.nan_to_num(atr))
    fail_idx = [k for k in range(lo, n) if c[k] < thr[k]]
    cross = [k for k in range(lo, n) if c[k] > L and c[k - 1] <= L]
    F = fail_idx[-1] if fail_idx else -1

    after_fail = [k for k in cross if k > F]
    before_fail = [k for k in cross if k < F]

    # ── no breakout in play ──
    if not after_fail:
        if F >= 0 and before_fail and F >= n - 1 - cfg["window"]:
            out.update(state="FAILED", action="AVOID", days_since=i - F)
            out["notes"].append("broke out, then closed back below the level — failed breakout")
            return out
        if c[i] > L:                       # above the level for longer than the window
            out.update(state="EXTENDED", action="AVOID")
            out["notes"].append("broke out long ago — not a fresh entry")
            return out
        dist = (L - float(c[i])) / a_now
        if h[i] >= L:
            out.update(state="ATTEMPT", action="WATCH")
            out["notes"].append("traded above the level but closed below it — not a breakout")
        elif dist <= NEAR_ATR:
            out.update(state="NEAR", action="WATCH")
            out["notes"].append(f"closes {dist:.1f} ATR under the level — wait for a decisive close above")
        elif dist <= 5:
            out.update(state="BUILDING", action="WATCH")
        else:
            out.update(state="FORMING", action="NONE")
        out["dist_atr"] = round(-dist, 2)
        return out

    # ── a breakout episode exists: j = first cross-up after the last failure ──
    j = after_fail[0]
    age = i - j
    aj = float(atr[j])
    hi_ref = float(np.max(h[max(0, j - cfg["hi_lookback"]):j])) if j > 0 else L
    headroom = max(0.0, (hi_ref - L) / aj)
    ema = ema_series(c, cfg["ema"])
    above_trend = bool(c[j] > ema[j])
    buf = (float(c[j]) - L) / aj
    grade = grade_event(buf, headroom, above_trend)
    stop = L - STOP_ATR * aj
    entry_ref = float(o[j + 1]) if j + 1 <= i else float(c[j])
    risk = entry_ref - stop
    out.update(grade=grade, event_age=age, buf_atr=round(buf, 2), headroom_atr=round(headroom, 1),
               above_trend=above_trend, is_new_high=bool(L >= hi_ref * 0.999),
               volume_ratio=round(float(v[j] / np.mean(v[max(0, j - 20):j])), 1) if j > 20 and np.mean(v[max(0, j - 20):j]) > 0 else None,
               prior_failure=bool(F >= 0 and F < j))
    if risk <= 0:
        out.update(state="EXTENDED", action="AVOID")
        out["notes"].append("price already back under the stop zone")
        return out
    target = entry_ref + TARGET_R * risk
    plan = dict(entry_ref=round(entry_ref, 2), stop=round(stop, 2), target=round(target, 2),
                risk_pct=round(100 * risk / entry_ref, 1), reward_risk=TARGET_R,
                size_pct_for_1pct_risk=round(100 * 0.01 / (risk / entry_ref), 1),
                horizon_bars=cfg["horizon"], entry="next bar open")

    # did the trade already resolve?
    state = None
    for k in range(j + 1, i + 1):
        if l[k] <= stop:
            state = "STOPPED"; break
        if h[k] >= target:
            state = "TARGET_ACHIEVED"; break
    retest_idx = None
    if state is None:
        for k in range(j + 1, min(i, j + cfg["retest_window"]) + 1):
            ak = float(atr[k])
            if l[k] <= L + RETEST_UP_ATR * ak and l[k] >= L - RETEST_DOWN_ATR * ak and c[k] > L and c[k] > o[k]:
                retest_idx = k; break

    kind = "BREAKOUT"
    if state is not None:
        action = "AVOID"
    elif retest_idx is not None and i - retest_idx <= RETEST_ENTRY_AGE:
        state, kind, action = "RETEST_HOLD", "RETEST", "BUY"
        ak = float(atr[retest_idx])
        stop = L - STOP_ATR * ak
        entry_ref = float(o[retest_idx + 1]) if retest_idx + 1 <= i else float(c[retest_idx])
        risk = entry_ref - stop
        if risk > 0:
            target = entry_ref + TARGET_R * risk
            plan.update(entry_ref=round(entry_ref, 2), stop=round(stop, 2), target=round(target, 2),
                        risk_pct=round(100 * risk / entry_ref, 1),
                        size_pct_for_1pct_risk=round(100 * 0.01 / (risk / entry_ref), 1))
        out["notes"].append("pulled back to the broken level and closed back above it")
    elif retest_idx is not None:
        state, action = "RUNNING", "HOLD"
    elif age >= 1 and l[i] <= L + RETEST_UP_ATR * a_now and l[i] >= L - RETEST_DOWN_ATR * a_now:
        state, action = "RETEST_WATCH", "WATCH"
        out["notes"].append("back at the broken level — buy only if it closes back above with a green candle")
    elif age <= ENTRY_AGE_MAX:
        state, action = "BREAKOUT", "BUY"
    elif age <= cfg["horizon"]:
        state, action = "RUNNING", "HOLD"
    else:
        state, action = "EXTENDED", "AVOID"

    if grade_gate and state in ("BREAKOUT", "RETEST_HOLD") and grade == "C":
        state, action = "WEAK_BREAKOUT", "AVOID"
        out["notes"].append(f"close only {buf:.1f} ATR beyond the level (needs ≥{DECISIVE_ATR}) — marginal breakouts usually fail")
    elif state in ("BREAKOUT", "RETEST_HOLD") and grade == "B":
        out["notes"].append("decisive close, but " + ("overhead supply within 3 ATR" if headroom >= HEADROOM_MAX_ATR else "below the long-term trend"))
    out.update(state=state, action=action, kind=kind, plan=plan)

    table = edge if edge is not None else load_edge()
    cell = edge_for(table, tf, kind, grade) if grade else None
    if grade_gate and action == "BUY" and not tradeable(cell, tf, kind, grade, have_table=bool(table)):
        out["action"] = "WATCH"
        out["notes"].append("setup is valid but has no validated edge in the backtest — do not size it as a trade")
    if grade_gate:
        out["confidence"] = describe_edge(cell, tf, kind, out["action"] == "BUY")
    if cell:
        out["edge"] = cell
    return out


def evaluate(df, level: float, timeframe: str = "DAILY", edge: Optional[Dict[str, Any]] = None,
             stance: str = "BULLISH") -> Dict[str, Any]:
    """DataFrame wrapper (Open/High/Low/Close/Volume or o/h/l/c/v). Resamples for W/M."""
    import pandas as pd
    tf = TF_ALIASES.get(str(timeframe).upper(), "D")
    d = df.copy()
    ren = {"Open": "o", "High": "h", "Low": "l", "Close": "c", "Volume": "v", "Date": "d"}
    d = d.rename(columns={k: v for k, v in ren.items() if k in d.columns})
    if "d" not in d.columns:
        d["d"] = d.index
    d["d"] = pd.to_datetime(d["d"])
    d = d.sort_values("d")
    if tf != "D":
        rule = "W-FRI" if tf == "W" else "ME"
        d = d.set_index("d").resample(rule).agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna().reset_index()
    return evaluate_arrays(d["o"].to_numpy(), d["h"].to_numpy(), d["l"].to_numpy(), d["c"].to_numpy(),
                           d["v"].to_numpy(), level, tf, edge, stance)


def evaluate_candles(rows: Sequence[Dict[str, Any]], level: float, timeframe: str = "D", edge: Optional[Dict[str, Any]] = None,
                     stance: str = "BULLISH") -> Dict[str, Any]:
    """rows = [{'t'|'time'|'date', 'o'|'open', 'h'|'high', 'l'|'low', 'c'|'close', 'v'|'volume'}], oldest first.
    Resamples to weekly/monthly if asked. Adds asof / event_date to the call."""
    import pandas as pd
    def g(r, *keys):
        for k in keys:
            if r.get(k) is not None:
                return r[k]
        return None
    recs = []
    for r in rows:
        o_, h_, l_, c_ = g(r, "o", "open"), g(r, "h", "high"), g(r, "l", "low"), g(r, "c", "close")
        if None in (o_, h_, l_, c_) or c_ <= 0:
            continue
        recs.append(dict(d=g(r, "t", "time", "date"), o=float(o_), h=float(h_), l=float(l_), c=float(c_), v=float(g(r, "v", "volume") or 0)))
    if len(recs) < 40:
        return dict(state="NA", action="NONE", grade=None, notes=["not enough candles"])
    df = pd.DataFrame(recs)
    df["d"] = pd.to_datetime(df["d"])
    df = df.sort_values("d").drop_duplicates("d").reset_index(drop=True)
    tf = TF_ALIASES.get(str(timeframe).upper(), "D")
    if tf != "D":
        try:
            grp = df.set_index("d").resample("W-FRI" if tf == "W" else "ME")
        except ValueError:
            grp = df.set_index("d").resample("W-FRI" if tf == "W" else "M")
        df = grp.agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna().reset_index()
    call = evaluate_arrays(df["o"].to_numpy(), df["h"].to_numpy(), df["l"].to_numpy(), df["c"].to_numpy(),
                           df["v"].to_numpy(), level, tf, edge, stance)
    n = len(df)
    call["asof"] = str(df["d"].iloc[-1].date())
    age = call.get("event_age")
    if age is not None and n - 1 - age >= 0:
        call["event_date"] = str(df["d"].iloc[n - 1 - age].date())
    return call


def legacy_breakout_status(call: Dict[str, Any]):
    """(breakout_status, days_since_breakout) in the vocabulary stock_screener_rows / the Radar SQL already use."""
    st, act = call.get("state"), call.get("action")
    days = None
    if call.get("event_date") and call.get("asof"):
        import datetime as _dt
        try:
            days = (_dt.date.fromisoformat(call["asof"]) - _dt.date.fromisoformat(call["event_date"])).days
        except ValueError:
            days = None
    if act == "BUY":
        return "SUCCESS", days
    if st in ("BREAKOUT", "RETEST_HOLD", "WEAK_BREAKOUT"):
        return "WEAK_BREAKOUT", days
    if st == "RETEST_WATCH":
        return "RETEST", days
    if st == "RUNNING":
        return "POST_BREAKOUT", days
    if st in ("FAILED", "STOPPED"):
        return "FAILED", days
    if st == "TARGET_ACHIEVED":
        return "TARGET_ACHIEVED", days
    if st == "EXTENDED":
        return "EXTENDED", days
    return "PENDING", None


# ── mapping to the lifecycle vocabulary the rest of the app already uses ────────────────────
def to_legacy(call: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """lifecycle_state / pattern_status / trading_signal / action_title for a call, or None to keep the old ones."""
    st, act = call.get("state"), call.get("action")
    if st in (None, "NA"):
        return None
    g = call.get("grade") or ""
    if act == "BUY":
        if st == "RETEST_HOLD":
            return dict(lifecycle="CONFIRMED", status=f"Retest holding · grade {g}", signal="BUY THE RETEST (validated)",
                        title="Buy Retest")
        return dict(lifecycle="CONFIRMED", status=f"Fresh breakout · grade {g}", signal="BUY BREAKOUT (validated)",
                    title="Buy Breakout")
    table = {
        "BREAKOUT": ("UNVALIDATED_BREAKOUT", "Breakout · no validated edge", "WATCH ONLY · SETUP HAS NO PROVEN EDGE", "Unproven Breakout"),
        "RETEST_HOLD": ("UNVALIDATED_BREAKOUT", "Retest holding · no validated edge", "WATCH ONLY · RETEST HAS NO PROVEN EDGE", "Unproven Retest"),
        "WEAK_BREAKOUT": ("WEAK_BREAKOUT", "Marginal close above the level", "NOT A BUY · CLOSE TOO CLOSE TO THE LEVEL", "Weak Breakout"),
        "RETEST_WATCH": ("RETEST_WATCH", "Back at the broken level", "WATCH · BUY ONLY IF IT CLOSES BACK ABOVE", "Retest Watch"),
        "NEAR": ("NEAR_CONFIRMATION", "Approaching breakout level", "WATCHLIST · AWAIT A DECISIVE CLOSE", "Awaiting Breakout"),
        "ATTEMPT": ("NEAR_CONFIRMATION", "Rejected at the level today", "WATCHLIST · TRIED AND CLOSED BELOW", "Breakout Attempt"),
        "BUILDING": ("FORMING", "Pattern base forming", "MONITOR EVOLUTION (NO SIGNAL)", "Base Forming"),
        "FORMING": ("FORMING", "Pattern base forming", "MONITOR EVOLUTION (NO SIGNAL)", "Base Forming"),
        "RUNNING": ("EXTENDED", "Breakout in progress", "IN TRADE · HOLD / TRAIL STOP · NO NEW ENTRY", "Breakout Running"),
        "EXTENDED": ("EXTENDED", "Broke out a while ago", "NOT A FRESH ENTRY", "Post-Breakout Run"),
        "FAILED": ("INVALIDATED", "Failed breakout (closed back below)", "PATTERN FAILED · STEP ASIDE", "Breakout Failed"),
        "STOPPED": ("INVALIDATED", "Stop level was hit", "STOPPED OUT · STEP ASIDE", "Stopped Out"),
        "TARGET_ACHIEVED": ("TARGET_ACHIEVED", "Target reached", "TARGET ACHIEVED · DO NOT CHASE", "Target Achieved"),
    }
    row = table.get(st)
    return dict(lifecycle=row[0], status=row[1], signal=row[2], title=row[3]) if row else None


# ── one decision per stock across timeframes ────────────────────────────────────────────────
def resolve_timeframes(calls: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Daily, weekly and monthly are DIFFERENT trades (different stop, different holding period), so they are never
    shown as competing verdicts. ONE primary call is chosen; the other timeframes are context.
      1. Only a BUY can be primary. If daily and weekly both buy, the weekly one wins (bigger move, same stock).
      2. A daily BUY against a FAILED / WEAK weekly breakout is downgraded to WATCH, with the conflict named.
      3. Monthly is context only (too few validated events to trade on its own).
      4. No BUY anywhere: the lowest timeframe that is on WATCH/HOLD is shown.
    """
    ctx = {tf: dict(state=c.get("state"), action=c.get("action"), grade=c.get("grade")) for tf, c in calls.items()}
    bad = ("FAILED", "WEAK_BREAKOUT")
    buys = [tf for tf in ("W", "D") if calls.get(tf, {}).get("action") == "BUY"]
    if buys:
        tf = buys[0]
        conflict = None
        for other in ("M", "W", "D"):
            if other != tf and calls.get(other, {}).get("state") in bad and other in ("M", "W"):
                conflict = f"{ {'M': 'monthly', 'W': 'weekly'}[other] } chart: {calls[other]['state'].replace('_', ' ').lower()}"
        if tf == "D" and calls.get("W", {}).get("state") in bad:
            return dict(primary_tf="D", action="WATCH", context=ctx, conflict=conflict)
        return dict(primary_tf=tf, action="BUY", context=ctx, conflict=conflict)
    for tf in ("D", "W"):                 # monthly never leads: no validated edge, so it is context only
        c = calls.get(tf)
        if c and c.get("action") in ("WATCH", "HOLD"):
            return dict(primary_tf=tf, action=c["action"], context=ctx, conflict=None)
    return dict(primary_tf=None, action="NONE", context=ctx, conflict=None)
