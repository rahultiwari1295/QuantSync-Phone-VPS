"""SQLite (WAL) schema and helpers. All IDs are prefix + ULID."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from ulid import ULID

from .config import Config

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users(
  uid TEXT PRIMARY KEY, handle TEXT UNIQUE COLLATE NOCASE, plan TEXT NOT NULL DEFAULT 'free',
  credits INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS strategies(
  id TEXT PRIMARY KEY, owner_uid TEXT, kind TEXT NOT NULL DEFAULT 'user',      -- user | flagship
  name TEXT NOT NULL, tf TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'private', -- private | public
  current_version TEXT, copied_from TEXT, archived INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_strat_owner ON strategies(owner_uid);
CREATE INDEX IF NOT EXISTS ix_strat_pub ON strategies(visibility, kind);
CREATE TABLE IF NOT EXISTS strategy_versions(
  id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES strategies(id), n INTEGER NOT NULL,
  entry_json TEXT NOT NULL, exit_json TEXT NOT NULL, rule_hash TEXT NOT NULL, fsv INTEGER NOT NULL,
  ai_enhancement_id TEXT, ai_gate INTEGER, exit_choice TEXT NOT NULL DEFAULT 'user',  -- user | ai
  live_from TEXT, created_at REAL NOT NULL, UNIQUE(strategy_id, n));
CREATE TABLE IF NOT EXISTS backtest_runs(
  id TEXT PRIMARY KEY, version_id TEXT NOT NULL, rule_hash TEXT NOT NULL, exit_hash TEXT NOT NULL,
  as_of TEXT NOT NULL, summary_json TEXT NOT NULL, trades_path TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_bt_cache ON backtest_runs(rule_hash, exit_hash, as_of);
CREATE TABLE IF NOT EXISTS ai_enhancements(
  id TEXT PRIMARY KEY, version_id TEXT NOT NULL, status TEXT NOT NULL,       -- queued|running|done|failed|insufficient
  candidate TEXT, model_path TEXT, result_json TEXT, exit_result_json TEXT, reason TEXT,
  created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, lane TEXT NOT NULL, uid TEXT, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued', progress INTEGER NOT NULL DEFAULT 0, stage TEXT,
  result_json TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0, timeout_s INTEGER NOT NULL,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL);
CREATE INDEX IF NOT EXISTS ix_jobs_q ON jobs(lane, status, created_at);
CREATE TABLE IF NOT EXISTS live_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL, symbol TEXT NOT NULL,
  signal_date TEXT NOT NULL, entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
  exit_reason TEXT, ret_pct REAL, is_open INTEGER NOT NULL DEFAULT 1, score REAL,
  UNIQUE(version_id, symbol, signal_date));
CREATE INDEX IF NOT EXISTS ix_live_v ON live_trades(version_id, is_open);
CREATE TABLE IF NOT EXISTS follows(uid TEXT NOT NULL, strategy_id TEXT NOT NULL, alerts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, PRIMARY KEY(uid, strategy_id));
CREATE TABLE IF NOT EXISTS alerts_log(id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, strategy_id TEXT,
  symbol TEXT, kind TEXT, sent_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS credit_ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,
  delta INTEGER NOT NULL, reason TEXT NOT NULL, ref TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, name TEXT, monthly_ai_jobs INTEGER, price_inr INTEGER);
CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL,
  uid TEXT, reason TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS app_config(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS holdings(uid TEXT NOT NULL, symbol TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL NOT NULL,
  updated_at REAL NOT NULL, PRIMARY KEY(uid, symbol));
INSERT OR IGNORE INTO app_config VALUES('pricing_enabled','false');
INSERT OR IGNORE INTO plans VALUES('free','Free',5,0),('pro','Pro',100,0);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{str(ULID()).lower()}"


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI may open the per-request connection (dependency) on one threadpool thread and run
    # the endpoint on another. Each request still has its own connection, used by one thread at a time.
    con = sqlite3.connect(cfg.db_path, timeout=15, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL"); con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON"); con.execute("PRAGMA busy_timeout=15000")
    return con


def migrate(cfg: Config) -> None:
    con = connect(cfg)
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    con.close()


@contextmanager
def tx(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def jdump(x) -> str:
    return json.dumps(x, separators=(",", ":"), default=str)


now = time.time
