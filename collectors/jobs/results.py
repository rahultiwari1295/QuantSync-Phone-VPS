"""
results — quarterly results into CompanyFundamentalsHistory, validated.

Modes (kw `mode`):
  from_cache : re-validate and promote the existing screener cache (no network). Fixes data right away.
  daily      : symbols whose board meeting (results) happened in the last 3 days, plus any still missing.
  weekly     : every symbol that is STALE/MISSING, plus a re-check of the rest in rotation.
Source order per symbol: NSE results filing -> screener (fallback, marked as such).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from typing import Dict, List, Optional

from ..core import db, parsers
from ..core.fetcher import Attempt, Fetcher, FetchSpec
from ..core.pipeline import JobRun, freshness_state, listing_dates, promote_quarters
from ._common import NSE_API, hist_conn, nse_attempts, screener_attempts, universe

NAME = "results"

TICKER_ALIASES: Dict[str, List[str]] = {
    "ZOMATO": ["ETERNAL"],
    "TATAMOTORS": ["TMPV", "TMCV"],
    "MSUMI": ["MOTHERSON"],
}


def _latest_end(hist, symbol: str) -> Optional[date]:
    r = hist.execute("SELECT MAX(period_end_date) FROM CompanyFundamentalsHistory WHERE symbol=? AND period_type='QUARTERLY'", (symbol,)).fetchone()
    return date.fromisoformat(r[0]) if r and r[0] else None


def _cache_blob(symbol: str) -> Optional[dict]:
    try:
        c = sqlite3.connect(db.FUND_CACHE_DB, timeout=30)
        r = c.execute("SELECT data_json FROM stock_fundamentals WHERE symbol=?", (symbol,)).fetchone()
        c.close()
        return json.loads(r[0]) if r and r[0] else None
    except (sqlite3.Error, ValueError):
        return None


def _save_cache_blob(symbol: str, blob: dict) -> None:
    try:
        c = sqlite3.connect(db.FUND_CACHE_DB, timeout=30)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stock_fundamentals (
                symbol TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            INSERT INTO stock_fundamentals (symbol, data_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                data_json = excluded.data_json,
                updated_at = CURRENT_TIMESTAMP
        """, (symbol, json.dumps(blob)))
        c.commit()
        c.close()
    except Exception:
        pass


def _extract_latest_quarter_date(html_text: str) -> Optional[str]:
    sec_m = re.search(r'<section id="quarters".*?</section>', html_text, re.DOTALL)
    if not sec_m:
        return None
    keys = re.findall(r'data-date-key="([\d\-]+)"', sec_m.group(0))
    return max(keys) if keys else None


def _fetch_screener_blob(fetcher: Fetcher, sym: str) -> Optional[dict]:
    clean_sym = sym.replace("&", "").replace("-", "")
    raw_candidates = [sym] + TICKER_ALIASES.get(sym, []) + ([clean_sym] if clean_sym != sym else [])
    candidates = []
    for c in raw_candidates:
        if c not in candidates:
            candidates.append(c)

    best_html = None
    best_url = None
    cutoff_date = (date.today() - timedelta(days=120)).isoformat()

    for c in candidates:
        consol_url = f"https://www.screener.in/company/{c}/consolidated/"
        stand_url = f"https://www.screener.in/company/{c}/"

        # 1. Try consolidated
        r_c = fetcher.fetch(FetchSpec(NAME, "screener_quarters", sym, screener_attempts(consol_url), expect="text"))
        c_html = r_c.data if (r_c.ok and isinstance(r_c.data, str) and "data-date-key" in r_c.data) else None
        c_date = _extract_latest_quarter_date(c_html) if c_html else None

        if c_date and c_date >= cutoff_date:
            best_html = c_html
            best_url = consol_url
            break

        # 2. Try standalone if consolidated missing or stale
        r_s = fetcher.fetch(FetchSpec(NAME, "screener_quarters", sym, screener_attempts(stand_url), expect="text"))
        s_html = r_s.data if (r_s.ok and isinstance(r_s.data, str) and "data-date-key" in r_s.data) else None
        s_date = _extract_latest_quarter_date(s_html) if s_html else None

        if s_date and (not c_date or s_date > c_date):
            best_html = s_html
            best_url = stand_url
            break
        elif c_html:
            best_html = c_html
            best_url = consol_url
            break
        elif s_html:
            best_html = s_html
            best_url = stand_url
            break

    if not best_html:
        return None

    blob = parsers.parse_screener_html(best_html, symbol=sym, source_url=best_url or "")
    if blob and isinstance(blob, dict):
        _save_cache_blob(sym, blob)
    return blob


def _due(conn, hist, mode: str, syms: List[str]) -> List[str]:
    if mode == "daily":
        recent = {r[0] for r in conn.execute("SELECT symbol FROM board_meetings WHERE is_results=1 AND meeting_date BETWEEN ? AND ?",
                                              ((date.today() - timedelta(days=3)).isoformat(), date.today().isoformat()))}
        missing = {s for s in syms if freshness_state(_latest_end(hist, s), date.today()) == "MISSING"}
        return [s for s in syms if s in recent or s in missing]
    return [s for s in syms if freshness_state(_latest_end(hist, s), date.today()) != "FRESH"]


def run(run: JobRun, fetcher: Fetcher, mode: str = "daily", limit: int = 0, symbols: str = "", **kw) -> None:
    with db.session() as conn, hist_conn() as hist:
        listing = listing_dates(conn)
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else universe(hist)
        if mode == "from_cache":
            todo = syms
        elif symbols:
            todo = syms
        else:
            todo = _due(conn, hist, mode, syms)
        if limit:
            todo = todo[: int(limit)]
        run.log(f"mode={mode}: {len(todo)} of {len(syms)} symbols")
        for i, sym in enumerate(todo, 1):
            saved_before = run.saved
            try:
                if mode != "from_cache":
                    url = f"{NSE_API}/results-comparision?symbol={sym}"
                    res = fetcher.fetch(FetchSpec(NAME, "nse_results", sym, nse_attempts(url),
                                                  accept=lambda x: None if isinstance(x, (dict, list)) else "unexpected shape"))
                    if res.ok:
                        rows = parsers.parse_nse_results(res.data)
                        if rows:
                            promote_quarters(conn, hist, run, sym, rows, "NSE_RESULTS", url, listing.get(sym), source_kind="nse")

                # Screener fallback: promote from cache if already present
                blob = _cache_blob(sym)
                if blob:
                    rows = parsers.parse_screener_quarters(blob)
                    if rows:
                        promote_quarters(conn, hist, run, sym, rows, "SCREENER_QUARTERS", blob.get("source_url") or "",
                                         listing.get(sym), source_kind="screener")

                # If still not fresh and network allowed, fetch live from Screener via headless Chromium
                if mode != "from_cache" and freshness_state(_latest_end(hist, sym), date.today()) != "FRESH":
                    live_blob = _fetch_screener_blob(fetcher, sym)
                    if live_blob:
                        rows = parsers.parse_screener_quarters(live_blob)
                        if rows:
                            promote_quarters(conn, hist, run, sym, rows, "SCREENER_QUARTERS", live_blob.get("source_url") or "",
                                             listing.get(sym), source_kind="screener")
            except Exception as e:                         # one bad symbol must not stop the sweep
                run.rejected += 1
                db.log_validation(conn, NAME, "results", sym, None, "exception", f"{type(e).__name__}: {e}", "REJECT")
            if i % 100 == 0:
                conn.commit(); hist.commit(); run.checkpoint()
                run.log(f"{i}/{len(todo)} done, saved {run.saved}, rejected {run.rejected}")
        conn.commit(); hist.commit()
