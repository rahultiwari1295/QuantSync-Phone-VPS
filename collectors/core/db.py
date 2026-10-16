"""
collectors/core/db.py — storage for the data-collection system.

Two databases:
  * Data/collectors.db   — raw fetches, staging, validation log, source health,
                           job runs/queue, review queue, admin audit log, clean
                           reference tables (listing, announcements, board meetings).
  * Data/stock_history.db — the app's existing store. Collectors only PROMOTE
                           validated rows into CompanyFundamentalsHistory,
                           CompanyMaster (listing columns) and CorporateEvents.

Nothing is promoted without passing validation; rejected rows go to
validation_log and (if worth a human look) review_queue.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.environ.get("QS_DATA_DIR", os.path.join(_ROOT, "Data"))
COLLECTOR_DB = os.environ.get("QS_COLLECTOR_DB", os.path.join(DATA_DIR, "collectors.db"))
HIST_DB = os.environ.get("QS_HIST_DB", os.path.join(DATA_DIR, "stock_history.db"))
FUND_CACHE_DB = os.environ.get("QS_FUND_DB", os.path.join(DATA_DIR, "fundamental_cache.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_fetch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT, source TEXT, route TEXT, url TEXT, key TEXT,
    status INTEGER, ok INTEGER, content_hash TEXT, bytes INTEGER,
    body BLOB, fetched_at TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_key ON raw_fetch(source, key, fetched_at);

CREATE TABLE IF NOT EXISTS validation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, job TEXT, dataset TEXT, symbol TEXT, period TEXT,
    rule TEXT, reason TEXT, severity TEXT, source TEXT, payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_vlog_ts ON validation_log(ts);
CREATE INDEX IF NOT EXISTS idx_vlog_sym ON validation_log(symbol, dataset);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, dataset TEXT, symbol TEXT, period TEXT, reason TEXT,
    old_json TEXT, new_json TEXT, alt_json TEXT, source TEXT,
    status TEXT DEFAULT 'PENDING', decided_at TEXT, decided_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status);

CREATE TABLE IF NOT EXISTS source_health (
    route TEXT PRIMARY KEY,
    ok_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0,
    consecutive_fail INTEGER DEFAULT 0, last_ok TEXT, last_fail TEXT, last_error TEXT,
    paused_until REAL DEFAULT 0, forced_off INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_daily (
    day TEXT, route TEXT, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,
    PRIMARY KEY (day, route)
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT, started_at TEXT, finished_at TEXT, status TEXT,
    rows_saved INTEGER DEFAULT 0, rows_rejected INTEGER DEFAULT 0, rows_seen INTEGER DEFAULT 0,
    trigger TEXT, args TEXT, summary TEXT, log_tail TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobruns_job ON job_runs(job, id);

CREATE TABLE IF NOT EXISTS job_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT, args TEXT, requested_at TEXT, requested_by TEXT,
    status TEXT DEFAULT 'QUEUED', picked_at TEXT, run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobqueue_status ON job_queue(status);

CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, who TEXT, ip TEXT, action TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS listing (
    symbol TEXT PRIMARY KEY, isin TEXT, name TEXT, series TEXT,
    listing_date TEXT, face_value REAL, status TEXT DEFAULT 'ACTIVE',
    source TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_listing_isin ON listing(isin);

CREATE TABLE IF NOT EXISTS board_meetings (
    symbol TEXT, meeting_date TEXT, purpose TEXT, detail TEXT, isin TEXT,
    is_results INTEGER, source TEXT, updated_at TEXT,
    PRIMARY KEY (symbol, meeting_date, purpose)
);

CREATE TABLE IF NOT EXISTS announcements (
    hash TEXT PRIMARY KEY, symbol TEXT, isin TEXT, ts TEXT, headline TEXT,
    detail TEXT, url TEXT, category TEXT, source TEXT, source_id TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_sym ON announcements(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_ann_ts ON announcements(ts);

CREATE TABLE IF NOT EXISTS dividends_clean (
    symbol TEXT, ex_date TEXT, amount REAL, subject TEXT, source TEXT, updated_at TEXT,
    PRIMARY KEY (symbol, ex_date, amount)
);

CREATE TABLE IF NOT EXISTS shareholding (
    symbol TEXT, quarter_end TEXT, promoter_pct REAL, pledged_pct REAL,
    fii_pct REAL, dii_pct REAL, public_pct REAL, source TEXT, updated_at TEXT,
    PRIMARY KEY (symbol, quarter_end)
);

CREATE TABLE IF NOT EXISTS ratios_daily (
    symbol TEXT PRIMARY KEY, as_of TEXT, computed_at TEXT, price REAL,
    ttm_revenue_cr REAL, ttm_net_profit_cr REAL, ttm_eps REAL, market_cap_cr REAL,
    pe REAL, pb REAL, roe_pct REAL, roce_pct REAL, debt_to_equity REAL,
    net_debt_cr REAL, revenue_yoy_pct REAL, profit_yoy_pct REAL, opm_ttm_pct REAL,
    quarters_used INTEGER
);

CREATE TABLE IF NOT EXISTS dataset_status (
    dataset TEXT, symbol TEXT, state TEXT, as_of TEXT, checked_at TEXT, note TEXT,
    PRIMARY KEY (dataset, symbol)
);
CREATE INDEX IF NOT EXISTS idx_dstatus_state ON dataset_status(dataset, state);

CREATE TABLE IF NOT EXISTS relay_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, source TEXT, key TEXT, url TEXT, status INTEGER, body BLOB,
    content_hash TEXT, processed INTEGER DEFAULT 0
);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or COLLECTOR_DB
    os.makedirs(os.path.dirname(p), exist_ok=True)
    # autocommit: no write lock is ever held across a network call, so jobs and the fetcher can't deadlock
    conn = sqlite3.connect(p, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init(path: Optional[str] = None) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(path: Optional[str] = None):
    conn = init(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def log_validation(conn, job: str, dataset: str, symbol: str, period: Optional[str], rule: str,
                   reason: str, severity: str = "REJECT", source: str = "", payload: Any = None) -> None:
    conn.execute(
        "INSERT INTO validation_log(ts,job,dataset,symbol,period,rule,reason,severity,source,payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now_iso(), job, dataset, symbol, period, rule, reason, severity, source,
         json.dumps(payload, default=str)[:4000] if payload is not None else None))


def queue_review(conn, dataset: str, symbol: str, period: Optional[str], reason: str,
                 old: Any, new: Any, alt: Any = None, source: str = "") -> None:
    """One pending review per (dataset, symbol, period) — don't pile up duplicates."""
    dup = conn.execute("SELECT 1 FROM review_queue WHERE status='PENDING' AND dataset=? AND symbol=? AND IFNULL(period,'')=IFNULL(?,'')",
                       (dataset, symbol, period)).fetchone()
    if dup:
        return
    conn.execute(
        "INSERT INTO review_queue(ts,dataset,symbol,period,reason,old_json,new_json,alt_json,source) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_iso(), dataset, symbol, period, reason, json.dumps(old, default=str),
         json.dumps(new, default=str), json.dumps(alt, default=str) if alt is not None else None, source))


def set_status(conn, dataset: str, symbol: str, state: str, as_of: Optional[str] = None, note: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO dataset_status(dataset,symbol,state,as_of,checked_at,note) VALUES (?,?,?,?,?,?)",
                 (dataset, symbol, state, as_of, now_iso(), note))


def audit(conn, who: str, action: str, detail: Any = None, ip: str = "") -> None:
    conn.execute("INSERT INTO admin_audit(ts,who,ip,action,detail) VALUES (?,?,?,?,?)",
                 (now_iso(), who, ip, action, json.dumps(detail, default=str)[:2000] if detail is not None else None))
