"""
common/ledger_truth.py — honest track record for the AI Buy/Sell engines.

Audit findings this module fixes:
  * 256 of 377 closed trades came from a walk-forward replay but were stored with
    created_at = exit date 15:30, so they looked recorded live. Every trade now
    carries origin = SIMULATED | LIVE (LIVE = entered on/after QS_LIVE_SINCE).
  * 77 fills sat outside that day's real high-low range. New fills are clamped to
    the official candle; historical ones are flagged (fill_check).
  * KPIs used invented fallbacks (PF 1.92 when no losses). Stats here return None
    when a number cannot be computed.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

LIVE_SINCE = os.environ.get("QS_LIVE_SINCE", "2026-09-08")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DB = os.path.join(_ROOT, "Data", "stock_history.db")
FILL_TOLERANCE = 0.005   # 0.5% outside the day's range is treated as a bad fill


def origin_of(trade: Dict[str, Any]) -> str:
    return "LIVE" if str(trade.get("entry_date") or "")[:10] >= LIVE_SINCE else "SIMULATED"


def _day_bar(symbol: str, date: str, hist_db: str = HIST_DB) -> Optional[Dict[str, Any]]:
    try:
        conn = sqlite3.connect(hist_db)
        row = conn.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (symbol,)).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    for b in reversed(json.loads(row[0])):
        t = str(b.get("t", ""))[:10]
        if t == date:
            return b
        if t < date:
            return None
    return None


def check_fill(price: float, bar: Optional[Dict[str, Any]]) -> str:
    if not bar:
        return "NO_CANDLE"
    lo, hi = float(bar["l"]), float(bar["h"])
    if price < lo * (1 - FILL_TOLERANCE) or price > hi * (1 + FILL_TOLERANCE):
        return "OUT_OF_RANGE"
    return "OK"


def realistic_fill(symbol: str, date: str, price: float, hist_db: str = HIST_DB) -> Tuple[float, bool]:
    """Clamp a simulated fill into the session's actual traded range."""
    bar = _day_bar(symbol, str(date)[:10], hist_db)
    if not bar:
        return price, False
    lo, hi = float(bar["l"]), float(bar["h"])
    clamped = min(max(price, lo), hi)
    return round(clamped, 2), abs(clamped - price) > 1e-6


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(closed_trades)")}
    if "origin" not in cols:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN origin TEXT")
    if "fill_check" not in cols:
        conn.execute("ALTER TABLE closed_trades ADD COLUMN fill_check TEXT")


def _ret_pct(t: Dict[str, Any]) -> float:
    ep, xp = float(t.get("entry_price") or 0), float(t.get("exit_price") or 0)
    return (xp / ep - 1) * 100 if ep > 0 else 0.0


def summarize(trades: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    ts = list(trades)
    n = len(ts)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "winRate": None, "profitFactor": None,
                "netPnl": 0.0, "avgWinPct": None, "avgLossPct": None, "expectancyPct": None,
                "medianHoldDays": None, "maxConsecutiveLosses": 0}
    pnl = [float(t.get("net_pnl") or 0) for t in ts]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    rets = [_ret_pct(t) for t in ts]
    wr = [r for r, p in zip(rets, pnl) if p > 0]
    lr = [r for r, p in zip(rets, pnl) if p <= 0]
    holds = sorted(int(t.get("holding_days") or 0) for t in ts)
    streak = best = 0
    for t in sorted(ts, key=lambda x: str(x.get("exit_date"))):
        if float(t.get("net_pnl") or 0) <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    gl = abs(sum(losses))
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": round(len(wins) / n * 100, 1),
        "profitFactor": round(sum(wins) / gl, 2) if gl > 0 else None,
        "netPnl": round(sum(pnl), 2),
        "avgWinPct": round(sum(wr) / len(wr), 2) if wr else None,
        "avgLossPct": round(sum(lr) / len(lr), 2) if lr else None,
        "expectancyPct": round(sum(rets) / n, 2),
        "medianHoldDays": holds[n // 2],
        "maxConsecutiveLosses": best,
    }


def track_record(closed: List[Dict[str, Any]]) -> Dict[str, Any]:
    for t in closed:
        t["origin"] = t.get("origin") or origin_of(t)
    live = [t for t in closed if t["origin"] == "LIVE"]
    sim = [t for t in closed if t["origin"] == "SIMULATED"]
    months: Dict[str, List] = defaultdict(list)
    for t in closed:
        months[str(t.get("exit_date") or "")[:7]].append(t)
    def _month_origin(v):
        kinds = {t["origin"] for t in v}
        return kinds.pop() if len(kinds) == 1 else "MIXED"
    by_month = [{"month": m, "origin": _month_origin(v),
                 "liveTrades": sum(1 for t in v if t["origin"] == "LIVE"), **summarize(v)}
                for m, v in sorted(months.items()) if m]
    strategies: Dict[str, List] = defaultdict(list)
    for t in closed:
        strategies[str(t.get("strategy_id") or "UNKNOWN")].append(t)
    return {
        "liveSince": LIVE_SINCE,
        "live": summarize(live),
        "simulated": summarize(sim),
        "combined": summarize(closed),
        "byMonth": by_month,
        "byStrategy": {k: summarize(v) for k, v in strategies.items()},
        "note": ("Trades entered before {d} come from a walk-forward simulation of the same rules; "
                 "trades from {d} onward were recorded live as they happened.").format(d=LIVE_SINCE),
    }


def label_ledger(master_db: str, hist_db: str = HIST_DB) -> Dict[str, int]:
    """One-off/backfill: set origin and fill_check on every closed trade."""
    conn = sqlite3.connect(master_db)
    ensure_columns(conn)
    rows = conn.execute("SELECT trade_id, symbol, entry_date, exit_date, entry_price, exit_price FROM closed_trades").fetchall()
    out: Dict[str, int] = defaultdict(int)
    for tid, sym, ed, xd, ep, xp in rows:
        org = origin_of({"entry_date": ed})
        c1 = check_fill(float(ep), _day_bar(sym, str(ed)[:10], hist_db))
        c2 = check_fill(float(xp), _day_bar(sym, str(xd)[:10], hist_db))
        fc = "OK" if c1 == c2 == "OK" else f"entry:{c1},exit:{c2}"
        conn.execute("UPDATE closed_trades SET origin=?, fill_check=? WHERE trade_id=?", (org, fc, tid))
        out[org] += 1
        out["fill_ok" if fc == "OK" else "fill_flagged"] += 1
    conn.commit()
    conn.close()
    return dict(out)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--master-db", default=os.path.join(_ROOT, "Data", "quantsync_master.db"))
    ap.add_argument("--hist-db", default=HIST_DB)
    a = ap.parse_args()
    print(label_ledger(a.master_db, a.hist_db))
