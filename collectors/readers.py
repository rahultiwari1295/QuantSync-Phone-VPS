"""
collectors/readers.py — what DNA / the API read. Everything shown to users passes through here.

sanitize_fundamentals() takes the scraped screener blob and returns a copy that:
  * drops annual/quarterly/dividend years that end before the stock was listed;
  * keeps only quarters that passed validation (present in CompanyFundamentalsHistory);
  * replaces the dividend list with exchange-verified dividends when we have them;
  * adds `data_checks` (listing date, freshness, what was hidden) so the UI can say so;
  * sets `usable` False when results are missing or too old — DNA must not score on those.
Never raises: on any internal error it returns the blob unchanged with data_checks.error set.
"""
from __future__ import annotations

import copy
import os
import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .core import db
from .core.parsers import month_end, to_num
from .core.pipeline import freshness_state


def _collector():
    p = db.COLLECTOR_DB
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def listing_date(symbol: str, conn=None) -> Optional[date]:
    own = conn is None
    conn = conn or _collector()
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT listing_date FROM listing WHERE symbol=?", (symbol,)).fetchone()
        return date.fromisoformat(r["listing_date"]) if r and r["listing_date"] else None
    except (sqlite3.Error, ValueError):
        return None
    finally:
        if own:
            conn.close()


def verified_dividends(symbol: str, conn=None) -> List[Dict[str, Any]]:
    own = conn is None
    conn = conn or _collector()
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute("SELECT ex_date, amount, subject FROM dividends_clean WHERE symbol=? ORDER BY ex_date DESC", (symbol,))]
    except sqlite3.Error:
        return []
    finally:
        if own:
            conn.close()


def _cut_table(tbl: Dict[str, Any], ld: date) -> int:
    """Drop year columns that end before listing. Columns: 'Mar 2015' ... 'TTM'."""
    yrs = tbl.get("years") or []
    keep = [i for i, y in enumerate(yrs) if y == "TTM" or (month_end(str(y)) or date.max) >= ld]
    dropped = len(yrs) - len(keep)
    if dropped:
        tbl["years"] = [yrs[i] for i in keep]
        for row in tbl.get("rows") or []:
            vals = row.get("values") or []
            row["values"] = [vals[i] for i in keep if i < len(vals)]
    return dropped


def sanitize_fundamentals(symbol: str, blob: Optional[Dict[str, Any]], price: Optional[float] = None,
                          hist_db: Optional[str] = None, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(blob, dict):
        return blob
    today = today or date.today()
    try:
        out = copy.deepcopy(blob)
        checks: Dict[str, Any] = {"hidden": {}, "notes": []}
        conn = _collector()
        ld = listing_date(symbol, conn) if conn else None
        checks["listing_date"] = ld.isoformat() if ld else None
        if ld is None:
            checks["notes"].append("Listing date not on record yet; pre-listing years can't be hidden.")
        # latest validated quarter
        hist = sqlite3.connect(hist_db or db.HIST_DB, timeout=10)
        valid = {r[0] for r in hist.execute("SELECT period_end_date FROM CompanyFundamentalsHistory WHERE symbol=? AND period_type='QUARTERLY'", (symbol,))}
        hist.close()
        latest = max(valid) if valid else None
        state = freshness_state(date.fromisoformat(latest) if latest else None, today)
        checks.update(results_state=state, latest_quarter=latest)
        # quarters: only validated ones
        qp = out.get("quarterly_performance") or []
        if valid:
            kept = [q for q in qp if (month_end(str(q.get("quarter"))) and month_end(str(q["quarter"])).isoformat() in valid)]
            if len(kept) != len(qp):
                checks["hidden"]["quarters"] = len(qp) - len(kept)
            out["quarterly_performance"] = kept
        elif qp:
            checks["hidden"]["quarters"] = len(qp)
            out["quarterly_performance"] = []
        # annual tables
        if ld:
            for k in ("annual_income_statement", "annual_balance_sheet", "annual_cash_flow", "efficiency_ratios"):
                if isinstance(out.get(k), dict):
                    n = _cut_table(out[k], ld)
                    if n:
                        checks["hidden"][k] = n
            div = out.get("dividends")
            if isinstance(div, dict):
                h = div.get("history") or []
                kept = [r for r in h if (month_end(str(r.get("year"))) or date.max) >= ld]
                if len(kept) != len(h):
                    checks["hidden"]["dividend_years"] = len(h) - len(kept)
                div["history"] = kept
        # exchange-verified dividends and a yield we can defend
        vd = verified_dividends(symbol, conn)
        if vd:
            out.setdefault("dividends", {})["verified"] = vd[:40]
            ttm = sum(d["amount"] for d in vd if d["ex_date"] >= (today - timedelta(days=365)).isoformat() and d["ex_date"] <= today.isoformat())
            if price and price > 0:
                y = round(ttm / price * 100, 2)
                out["dividends"]["dividend_yield_pct"] = y
                if isinstance(out.get("key_ratios"), dict):
                    out["key_ratios"]["dividend_yield_pct"] = y
                checks["dividend_yield_source"] = "exchange-verified last 12 months"
        elif ld is not None and isinstance(out.get("dividends"), dict) and not out["dividends"].get("history"):
            out["dividends"]["dividend_yield_pct"] = None
        # shareholding freshness
        if conn:
            r = conn.execute("SELECT MAX(quarter_end) q FROM shareholding WHERE symbol=?", (symbol,)).fetchone()
            checks["shareholding_quarter"] = r["q"] if r else None
            conn.close()
        out["usable"] = state in ("FRESH",)
        if not out["usable"]:
            checks["notes"].append({"MISSING": "No validated quarterly results on record.",
                                    "STALE": f"Latest validated quarter is {latest}; results may be out of date.",
                                    "DEAD": f"Latest validated quarter is {latest}; the company may have stopped reporting."}.get(state, ""))
        out["data_checks"] = checks
        return out
    except Exception as e:                                   # never break the DNA report over a sanitizer problem
        blob = dict(blob)
        blob["data_checks"] = {"error": f"{type(e).__name__}: {e}", "results_state": "UNKNOWN"}
        blob["usable"] = False
        return blob
