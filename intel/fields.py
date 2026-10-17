"""Builds the slow-moving scanner facts (company numbers, ownership) into intel.db, keyed by the day they were public.

facts_daily  : one row per symbol per day it is built (ratios + compounded growth). History accumulates from the first run.
facts_owner  : one row per symbol per quarter, known ~21 days after quarter end (SEBI filing deadline), with QoQ changes.
The Lab joins both point-in-time when it cooks features (quantsync-lab/lab/facts.py)."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import date, timedelta

from . import db

FUND_DB = os.environ.get("QS_FUND_DB", os.path.join(db.DATA_DIR, "fundamental_cache.db"))


def _find(table: str) -> sqlite3.Connection | None:
    for p in (db.COLLECTOR_DB, db.HIST_DB, FUND_DB):
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=20)
            c.row_factory = sqlite3.Row
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return c
            c.close()
        except sqlite3.Error:
            continue
    return None


def _pct(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _growth(items: list, years: int) -> float | None:
    for it in items or []:
        p = str(it.get("period", "")).lower()
        if p.startswith(f"{years} year"):
            return _pct(it.get("value"))
    return None


def build_daily(con, day: str | None = None) -> int:
    day = day or db.today()
    rat = _find("ratios_daily")
    fund = _find("stock_fundamentals")
    rows: dict[str, dict] = {}
    if rat:
        for r in rat.execute("SELECT * FROM ratios_daily"):
            r = dict(r)
            pe = r.get("pe")
            rows[r["symbol"]] = {"mcap_cr": r.get("market_cap_cr"), "pe": pe, "pb": r.get("pb"),
                                 "earnings_yield": (100.0 / pe) if pe and pe > 0 else None,
                                 "roe": r.get("roe_pct"), "roce": r.get("roce_pct"), "debt_to_equity": r.get("debt_to_equity"),
                                 "opm": r.get("opm_ttm_pct"), "eps_ttm": r.get("ttm_eps"), "sales_ttm_cr": r.get("ttm_revenue_cr"),
                                 "profit_ttm_cr": r.get("ttm_net_profit_cr"), "sales_growth_yoy": r.get("revenue_yoy_pct"),
                                 "profit_growth_yoy": r.get("profit_yoy_pct")}
        rat.close()
    if fund:
        for sym, blob in fund.execute("SELECT symbol, data_json FROM stock_fundamentals"):
            try:
                d = json.loads(blob or "{}")
            except ValueError:
                continue
            row = rows.setdefault(sym, {})
            k = d.get("key_ratios") or {}
            cg = d.get("compounded_growth") or {}
            fill = {"dividend_yield": _pct(k.get("dividend_yield_pct")), "mcap_cr": d.get("market_cap_cr"),
                    "pe": _pct(k.get("stock_pe")), "roe": _pct(k.get("roe_pct")), "roce": _pct(k.get("roce_pct")),
                    "debt_to_equity": _pct(k.get("debt_to_equity")),
                    "sales_growth_3y": _growth(cg.get("sales_growth"), 3), "profit_growth_3y": _growth(cg.get("profit_growth"), 3),
                    "sales_growth_5y": _growth(cg.get("sales_growth"), 5), "profit_growth_5y": _growth(cg.get("profit_growth"), 5),
                    "sales_growth_10y": _growth(cg.get("sales_growth"), 10), "profit_growth_10y": _growth(cg.get("profit_growth"), 10),
                    "roe_3y": _growth(cg.get("roe"), 3)}
            for kk, v in fill.items():
                if row.get(kk) is None and v is not None:
                    row[kk] = v
            if row.get("earnings_yield") is None and row.get("pe"):
                row["earnings_yield"] = 100.0 / row["pe"] if row["pe"] > 0 else None
        fund.close()
    cols = ["mcap_cr", "pe", "pb", "earnings_yield", "dividend_yield", "roe", "roce", "debt_to_equity", "opm", "eps_ttm", "sales_ttm_cr",
            "profit_ttm_cr", "sales_growth_yoy", "profit_growth_yoy", "sales_growth_3y", "profit_growth_3y", "sales_growth_5y",
            "profit_growth_5y", "sales_growth_10y", "profit_growth_10y", "roe_3y"]
    con.executemany(f"INSERT OR REPLACE INTO facts_daily(symbol,known_at,{','.join(cols)}) VALUES(?,?,{','.join('?' * len(cols))})",
                    [(s, day, *[r.get(c) for c in cols]) for s, r in rows.items() if any(r.get(c) is not None for c in cols)])
    return len(rows)


def build_owner(con) -> int:
    sh = _find("shareholding")
    if not sh:
        return 0
    data: dict[str, list] = {}
    for r in sh.execute("SELECT symbol, quarter_end, promoter_pct, pledged_pct, fii_pct, dii_pct, public_pct FROM shareholding ORDER BY symbol, quarter_end"):
        data.setdefault(r["symbol"], []).append(dict(r))
    sh.close()
    out = []
    for sym, qs in data.items():
        prev = None
        for q in qs:
            try:
                qe = date.fromisoformat(str(q["quarter_end"])[:10])
            except ValueError:
                continue
            ch = lambda k: (q[k] - prev[k]) if prev and q.get(k) is not None and prev.get(k) is not None else None
            out.append((sym, str(qe), str(qe + timedelta(days=21)), q["promoter_pct"], ch("promoter_pct"), q["pledged_pct"], ch("pledged_pct"),
                        q["fii_pct"], ch("fii_pct"), q["dii_pct"], ch("dii_pct"), q["public_pct"]))
            prev = q
    con.executemany("INSERT OR REPLACE INTO facts_owner(symbol,quarter_end,known_at,promoter_pct,promoter_change_qoq,pledged_pct,pledge_change_qoq,"
                    "fii_pct,fii_change_qoq,dii_pct,dii_change_qoq,public_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def run(log=print) -> dict:
    t0 = time.time()
    with db.session() as con:
        d, o = build_daily(con), build_owner(con)
        db.record_run(con, "fields", t0, True, d + o, f"daily={d} owner={o}")
    log(f"facts_daily={d} facts_owner={o}")
    return {"facts_daily": d, "facts_owner": o}
