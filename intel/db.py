"""intel.db: every event about every company, plus the fact tables the scanner joins.

One file, SQLite WAL, separate from stock_history.db so long collector writes never block the trading engine.
Every row carries `known_at` = the moment the fact became public (point-in-time backtests depend on it)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

DATA_DIR = os.environ.get("QS_DATA_DIR", "/root/quantsync/Data")
INTEL_DB = os.environ.get("QS_INTEL_DB", os.path.join(DATA_DIR, "intel.db"))
HIST_DB = os.environ.get("QS_HIST_DB", os.path.join(DATA_DIR, "stock_history.db"))
COLLECTOR_DB = os.environ.get("QS_COLLECTOR_DB", os.path.join(DATA_DIR, "collectors.db"))
IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,                -- content hash: the same filing from NSE and BSE lands once
  symbol TEXT, isin TEXT, company TEXT,
  ts TEXT NOT NULL,                   -- when it happened / was filed (IST, ISO)
  known_at TEXT NOT NULL,             -- when we could first know it (date, IST)
  type TEXT NOT NULL,                 -- results | order_win | insider_buy | ... (classify.TYPES)
  category TEXT,                      -- filing | deal | insider | news | global | macro | meeting
  source TEXT, url TEXT, headline TEXT NOT NULL, detail TEXT,
  sentiment TEXT DEFAULT 'neutral',   -- positive | negative | neutral
  impact INTEGER DEFAULT 0,           -- 0..100
  numbers_json TEXT,                  -- exact numbers from structured sources (never from the model)
  summary TEXT,                       -- two plain lines (rules first, the model when it has read the document)
  ai_json TEXT, ai_status TEXT DEFAULT 'none',   -- none | pending | done | failed | skipped
  created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_ev_sym ON events(symbol, ts DESC);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS ix_ev_imp ON events(known_at, impact DESC);
CREATE INDEX IF NOT EXISTS ix_ev_ai ON events(ai_status);

CREATE TABLE IF NOT EXISTS insider_trades (
  id TEXT PRIMARY KEY, symbol TEXT, person TEXT, category TEXT, side TEXT, qty REAL, value_cr REAL,
  mode TEXT, pct_before REAL, pct_after REAL, trade_from TEXT, trade_to TEXT, known_at TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS ix_it_sym ON insider_trades(symbol, known_at);

CREATE TABLE IF NOT EXISTS deals (
  id TEXT PRIMARY KEY, symbol TEXT, party TEXT, side TEXT, qty REAL, price REAL, value_cr REAL,
  kind TEXT, deal_date TEXT, known_at TEXT, source TEXT
);
CREATE INDEX IF NOT EXISTS ix_deal_sym ON deals(symbol, known_at);
CREATE INDEX IF NOT EXISTS ix_deal_date ON deals(deal_date);

CREATE TABLE IF NOT EXISTS pledges (
  id TEXT PRIMARY KEY, symbol TEXT, pledged_pct REAL, promoter_pct REAL, known_at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS meetings (
  symbol TEXT, meeting_date TEXT, purpose TEXT, is_results INTEGER, known_at TEXT, source TEXT,
  PRIMARY KEY (symbol, meeting_date, purpose)
);
CREATE INDEX IF NOT EXISTS ix_meet_date ON meetings(meeting_date);

CREATE TABLE IF NOT EXISTS flows (
  day TEXT, who TEXT, buy_cr REAL, sell_cr REAL, net_cr REAL, PRIMARY KEY (day, who)
);
CREATE TABLE IF NOT EXISTS markets (
  key TEXT PRIMARY KEY, name TEXT, region TEXT, last REAL, chg_pct REAL, as_of TEXT
);

CREATE TABLE IF NOT EXISTS news_home (
  id INTEGER PRIMARY KEY CHECK (id = 1), built_at TEXT, body_json TEXT
);

CREATE TABLE IF NOT EXISTS facts_daily (
  symbol TEXT, known_at TEXT,
  mcap_cr REAL, pe REAL, pb REAL, earnings_yield REAL, dividend_yield REAL, roe REAL, roce REAL, debt_to_equity REAL,
  opm REAL, eps_ttm REAL, sales_ttm_cr REAL, profit_ttm_cr REAL, sales_growth_yoy REAL, profit_growth_yoy REAL,
  sales_growth_3y REAL, profit_growth_3y REAL, sales_growth_5y REAL, profit_growth_5y REAL,
  sales_growth_10y REAL, profit_growth_10y REAL, roe_3y REAL,
  PRIMARY KEY (symbol, known_at)
);
CREATE TABLE IF NOT EXISTS facts_owner (
  symbol TEXT, quarter_end TEXT, known_at TEXT,
  promoter_pct REAL, promoter_change_qoq REAL, pledged_pct REAL, pledge_change_qoq REAL,
  fii_pct REAL, fii_change_qoq REAL, dii_pct REAL, dii_change_qoq REAL, public_pct REAL,
  PRIMARY KEY (symbol, quarter_end)
);

CREATE TABLE IF NOT EXISTS briefs (
  id TEXT PRIMARY KEY, kind TEXT, day TEXT, created_at REAL, body_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_brief ON briefs(kind, day DESC);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, started REAL, finished REAL, ok INTEGER, added INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY, last_ok REAL, last_try REAL, fails INTEGER DEFAULT 0, note TEXT
);
"""


def now_ist() -> datetime:
    return datetime.now(IST)


def today() -> str:
    return now_ist().strftime("%Y-%m-%d")


def connect(path: str | None = None, ro: bool = False) -> sqlite3.Connection:
    path = path or INTEL_DB
    if ro:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        con = sqlite3.connect(path, timeout=30, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def migrate(con: sqlite3.Connection | None = None) -> None:
    c = con or connect()
    c.executescript(SCHEMA)
    for col, typ in (("prev", "REAL"), ("hi", "REAL"), ("lo", "REAL"), ("spark_json", "TEXT"), ("grp", "TEXT")):   # added later: old databases get them here
        try:
            c.execute(f"ALTER TABLE markets ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    c.commit()
    if con is None:
        c.close()


@contextlib.contextmanager
def session(path: str | None = None):
    con = connect(path)
    try:
        migrate(con)
        yield con
        con.commit()
    finally:
        con.close()


def h(*parts) -> str:
    return hashlib.sha1("|".join(str(p or "").strip().lower() for p in parts).encode()).hexdigest()[:20]


def jdump(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=str, ensure_ascii=False)


def upsert_event(con, ev: dict) -> bool:
    """Insert once by id. Returns True when the event is new."""
    ev = {**ev}
    ev.setdefault("created_at", time.time())
    if isinstance(ev.get("numbers_json"), (dict, list)):
        ev["numbers_json"] = jdump(ev["numbers_json"])
    cols = ["id", "symbol", "isin", "company", "ts", "known_at", "type", "category", "source", "url", "headline", "detail",
            "sentiment", "impact", "numbers_json", "summary", "ai_status", "created_at"]
    cur = con.execute(f"INSERT OR IGNORE INTO events({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [ev.get(k) for k in cols])
    return cur.rowcount > 0


def record_run(con, job: str, started: float, ok: bool, added: int, note: str = "") -> None:
    con.execute("INSERT INTO runs(job,started,finished,ok,added,note) VALUES(?,?,?,?,?,?)", (job, started, time.time(), int(ok), added, note[:500]))


def mark_source(con, source: str, ok: bool, note: str = "") -> None:
    t = time.time()
    if ok:
        con.execute("INSERT INTO source_state(source,last_ok,last_try,fails,note) VALUES(?,?,?,0,?) ON CONFLICT(source) DO UPDATE SET "
                    "last_ok=excluded.last_ok,last_try=excluded.last_try,fails=0,note=excluded.note", (source, t, t, note[:300]))
    else:
        con.execute("INSERT INTO source_state(source,last_try,fails,note) VALUES(?,?,1,?) ON CONFLICT(source) DO UPDATE SET "
                    "last_try=excluded.last_try,fails=fails+1,note=excluded.note", (source, t, note[:300]))


def attach_ro(con, path: str, alias: str) -> bool:
    if not os.path.exists(path):
        return False
    con.execute(f"ATTACH DATABASE ? AS {alias}", (path,))  # intel never writes to attached databases
    return True
