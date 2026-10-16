"""
common/corporate_actions.py — split / bonus / consolidation adjustment of daily candles.

Before this, the candle store was raw: a 1:5 split looked like an 80% crash, which
broke EMA200, 52-week highs, stages, patterns, DNA archetypes and every backtest that
touched the stock (190 such jumps found in the Oct 2026 audit, e.g. VERANDA, LANCER,
INDIAGLYCO).

Flow (daily, after the official candle sync):
  1. fetch_nse_actions()      NSE corporate-actions API → list of {symbol, subject, ex_date}
  2. record_actions()         parsed into CorporateActionsAdj with a price factor
  3. apply_pending()          for every action not yet applied: verify it against the
                              actual price gap, back up the series, multiply all bars
                              before the ex-date by the factor (volume ÷ factor), mark applied.
Idempotent: an action is applied exactly once (ledger), and a back-up of the series is
kept in StockHistory_backup before every change.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("corporate_actions")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST_DB = os.path.join(_ROOT, "Data", "stock_history.db")

_FV = r"(?:rs\.?|re\.?|inr|₹)\s*([0-9]+(?:\.[0-9]+)?)"
_SPLIT_RE = re.compile(r"from\s*" + _FV + r".*?to\s*" + _FV, re.I | re.S)
_BONUS_RE = re.compile(r"bonus[^0-9]*([0-9]+)\s*:\s*([0-9]+)", re.I)

# a verified action's gap must land within this band of the expected ratio
VERIFY_TOLERANCE = 0.18


def parse_price_factor(subject: str) -> Optional[float]:
    """Multiplier for prices BEFORE the ex-date; None when prices need no adjustment."""
    if not subject:
        return None
    s = subject.lower()
    factor = 1.0
    touched = False
    if ("split" in s or "sub-division" in s or "subdivision" in s or "consolidat" in s):
        m = _SPLIT_RE.search(subject)
        if m:
            old_fv, new_fv = float(m.group(1)), float(m.group(2))
            if old_fv > 0 and new_fv > 0 and old_fv != new_fv:
                factor *= new_fv / old_fv
                touched = True
    m = _BONUS_RE.search(subject)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            factor *= b / (a + b)
            touched = True
    return round(factor, 8) if touched else None


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS CorporateActionsAdj(
        symbol TEXT, ex_date TEXT, subject TEXT, factor REAL, source TEXT,
        status TEXT DEFAULT 'PENDING',          -- PENDING / APPLIED / UNVERIFIED / SKIPPED
        observed_ratio REAL, note TEXT, created_at TEXT, applied_at TEXT,
        PRIMARY KEY(symbol, ex_date, factor))""")
    conn.execute("CREATE TABLE IF NOT EXISTS StockHistory_backup(symbol TEXT, rawCandles TEXT, backed_up_at TEXT, reason TEXT)")


def fetch_nse_actions(from_date: _dt.date, to_date: _dt.date, timeout: int = 20) -> List[Dict[str, str]]:
    """NSE equities corporate actions between two dates (needs the session cookie dance)."""
    import requests
    hdr = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/537.36 Chrome/120 Safari/537.36",
           "Accept": "application/json,text/plain,*/*", "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"}
    sess = requests.Session()
    sess.get("https://www.nseindia.com/", headers=hdr, timeout=timeout)
    url = ("https://www.nseindia.com/api/corporates-corporateActions?index=equities"
           f"&from_date={from_date:%d-%m-%Y}&to_date={to_date:%d-%m-%Y}")
    r = sess.get(url, headers=hdr, timeout=timeout)
    r.raise_for_status()
    out = []
    for row in r.json() or []:
        sym, subj, ex = row.get("symbol"), row.get("subject"), row.get("exDate")
        if not (sym and subj and ex) or ex == "-":
            continue
        try:
            ex_iso = _dt.datetime.strptime(ex, "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        out.append({"symbol": sym.strip().upper(), "subject": subj.strip(), "ex_date": ex_iso})
    return out


def record_actions(actions: Iterable[Dict[str, str]], source: str = "NSE", db_path: str = HIST_DB) -> int:
    conn = sqlite3.connect(db_path)
    _ensure_tables(conn)
    n = 0
    now = _dt.datetime.now().isoformat(timespec="seconds")
    for a in actions:
        f = parse_price_factor(a["subject"])
        if not f or abs(f - 1) < 1e-6:
            continue
        cur = conn.execute("INSERT OR IGNORE INTO CorporateActionsAdj(symbol, ex_date, subject, factor, source, created_at) VALUES (?,?,?,?,?,?)",
                           (a["symbol"], a["ex_date"], a["subject"], f, source, now))
        n += cur.rowcount
    conn.commit()
    conn.close()
    return n


def observed_gap_ratio(candles: List[Dict[str, Any]], ex_date: str) -> Optional[float]:
    """open(ex_date or first bar after) / close(last bar before)."""
    prev = None
    for b in candles:
        t = str(b["t"])[:10]
        if t < ex_date:
            prev = b
        elif prev is not None:
            return float(b["o"]) / float(prev["c"]) if float(prev["c"]) > 0 else None
        else:
            return None
    return None


def adjust_series(candles: List[Dict[str, Any]], ex_date: str, factor: float) -> List[Dict[str, Any]]:
    out = []
    for b in candles:
        if str(b["t"])[:10] < ex_date:
            nb = dict(b)
            for k in ("o", "h", "l", "c"):
                nb[k] = round(float(b[k]) * factor, 4)
            if b.get("v"):
                nb["v"] = int(round(float(b["v"]) / factor))
            out.append(nb)
        else:
            out.append(b)
    return out


def apply_pending(db_path: str = HIST_DB, force: bool = False) -> Dict[str, Any]:
    """Apply every PENDING action once, after checking the candle gap agrees with it."""
    conn = sqlite3.connect(db_path)
    _ensure_tables(conn)
    pend = conn.execute("SELECT rowid, symbol, ex_date, factor, subject FROM CorporateActionsAdj WHERE status='PENDING' ORDER BY ex_date").fetchall()
    res = {"applied": [], "unverified": [], "skipped": []}
    now = _dt.datetime.now().isoformat(timespec="seconds")
    for rowid, sym, ex, f, subj in pend:
        row = conn.execute("SELECT rawCandles FROM StockHistory WHERE symbol=?", (sym,)).fetchone()
        if not row or not row[0]:
            conn.execute("UPDATE CorporateActionsAdj SET status='SKIPPED', note='symbol not in store' WHERE rowid=?", (rowid,))
            res["skipped"].append(sym)
            continue
        cs = json.loads(row[0])
        if not cs or str(cs[-1]["t"])[:10] < ex:
            continue  # ex-date not reached in the store yet; keep pending
        obs = observed_gap_ratio(cs, ex)
        ok = obs is not None and abs(obs / f - 1) <= VERIFY_TOLERANCE
        if not ok and not force:
            conn.execute("UPDATE CorporateActionsAdj SET status='UNVERIFIED', observed_ratio=?, note=? WHERE rowid=?",
                         (obs, "price gap does not match the announced ratio", rowid))
            res["unverified"].append({"symbol": sym, "ex_date": ex, "factor": f, "observed": obs})
            continue
        conn.execute("INSERT INTO StockHistory_backup VALUES (?,?,?,?)", (sym, row[0], now, f"corp_action {ex} x{f}"))
        adj = adjust_series(cs, ex, f)
        conn.execute("UPDATE StockHistory SET rawCandles=?, candleCount=? WHERE symbol=?", (json.dumps(adj), len(adj), sym))
        conn.execute("UPDATE CorporateActionsAdj SET status='APPLIED', observed_ratio=?, applied_at=? WHERE rowid=?", (obs, now, rowid))
        res["applied"].append({"symbol": sym, "ex_date": ex, "factor": f, "observed": obs, "subject": subj})
    conn.commit()
    conn.close()
    return res


def explained_dates(symbol: str, db_path: str = HIST_DB) -> List[str]:
    """Ex-dates already accounted for (applied), so data_quality ignores those gaps."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT ex_date FROM CorporateActionsAdj WHERE symbol=? AND status='APPLIED'", (symbol,)).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def sync_and_apply(days_back: int = 30, db_path: str = HIST_DB, history_from: Optional[str] = None) -> Dict[str, Any]:
    """Daily job: pull recent NSE actions (or a full history window), record, apply."""
    to_d = _dt.date.today() + _dt.timedelta(days=7)
    from_d = _dt.date.fromisoformat(history_from) if history_from else to_d - _dt.timedelta(days=days_back)
    fetched = []
    # the NSE API is happiest with windows of about one year
    cur = from_d
    while cur <= to_d:
        end = min(to_d, cur + _dt.timedelta(days=360))
        try:
            fetched += fetch_nse_actions(cur, end)
        except Exception as e:
            logger.warning(f"NSE corporate actions fetch {cur}..{end} failed: {e}")
        cur = end + _dt.timedelta(days=1)
        time.sleep(1.0)
    recorded = record_actions(fetched, db_path=db_path)
    applied = apply_pending(db_path=db_path)
    return {"fetched": len(fetched), "recorded": recorded, **{k: len(v) for k, v in applied.items()}, "detail": applied}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=30)
    ap.add_argument("--history-from", help="ISO date: backfill actions since this date (first run: 2018-01-01)")
    a = ap.parse_args()
    print(json.dumps(sync_and_apply(a.days_back, history_from=a.history_from), indent=2, default=str))
