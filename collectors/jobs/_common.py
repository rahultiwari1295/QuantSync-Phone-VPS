"""Shared helpers for jobs."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional

from ..core import db
from ..core.fetcher import Attempt, FetchSpec, Fetcher

NSE_API = "https://www.nseindia.com/api"
NSE_ARCH = "https://nsearchives.nseindia.com"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"


def nse_attempts(url: str, bse_url: Optional[str] = None, with_archive_direct: bool = False) -> List[Attempt]:
    a = [Attempt("direct", "nse", url), Attempt("chromium", "nse", url)]
    if bse_url:
        a.append(Attempt("direct", "bse", bse_url))
    a.append(Attempt("relay", "generic"))
    return a


def screener_attempts(url: str) -> List[Attempt]:
    return [Attempt("chromium", "screener", url), Attempt("direct", "screener", url), Attempt("relay", "generic")]


def universe(hist: sqlite3.Connection) -> List[str]:
    return [r[0] for r in hist.execute("SELECT symbol FROM StockHistory ORDER BY symbol")]


def d(x: date) -> str:
    return x.strftime("%d-%m-%Y")


def hist_conn() -> sqlite3.Connection:
    c = sqlite3.connect(db.HIST_DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c
