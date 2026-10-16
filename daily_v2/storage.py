"""
QuantSync Daily V2 Storage & Database Persistence Layer
-------------------------------------------------------
Isolated SQLite persistence layer for Daily V2 trade lifecycles and candidate audit logs.
Enforces hard Scenario A constraint: MAX 1 ACTIVE TRADE PER SYMBOL.
"""

import sqlite3
import json
import os
import time
from typing import Dict, List, Any, Optional
from contextlib import contextmanager
from daily_v2.config import DATABASE_PATH


class DailyV2Storage:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_v2_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    strategy_family TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_date TEXT,
                    exit_price REAL,
                    status TEXT NOT NULL,
                    health_state TEXT NOT NULL,
                    exit_reason TEXT,
                    raw_ml_score REAL NOT NULL,
                    calibrated_p REAL NOT NULL,
                    roi_pct REAL DEFAULT 0.0,
                    mfe_pct REAL DEFAULT 0.0,
                    mae_pct REAL DEFAULT 0.0,
                    holding_days INTEGER DEFAULT 0,
                    also_triggered_by TEXT DEFAULT '[]',
                    model_version TEXT NOT NULL,
                    live_price REAL DEFAULT NULL,
                    live_price_updated_at TIMESTAMP DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, signal_date)
                )
            """)

            # Auto-migration for existing tables without live_price columns
            cur.execute("PRAGMA table_info(daily_v2_trades)")
            cols = [r[1] for r in cur.fetchall()]
            if 'live_price' not in cols:
                cur.execute("ALTER TABLE daily_v2_trades ADD COLUMN live_price REAL DEFAULT NULL")
            if 'live_price_updated_at' not in cols:
                cur.execute("ALTER TABLE daily_v2_trades ADD COLUMN live_price_updated_at TIMESTAMP DEFAULT NULL")

            # Hard Constraint: Only ONE active trade per symbol
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_v2_one_active_per_symbol
                ON daily_v2_trades(symbol)
                WHERE status = 'ACTIVE'
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_v2_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_date TEXT NOT NULL,
                    raw_score REAL,
                    calibrated_p REAL,
                    threshold REAL,
                    decision TEXT NOT NULL,
                    rejection_reason TEXT,
                    competing_strategies TEXT DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Auto-migration for daily_v2_cycle_runs composite primary key
            cur.execute("PRAGMA table_info(daily_v2_cycle_runs)")
            c_cols = [r[1] for r in cur.fetchall()]
            if c_cols and 'phase' not in c_cols:
                cur.execute("DROP TABLE daily_v2_cycle_runs")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_v2_cycle_runs (
                    date TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    new_entries_count INTEGER DEFAULT 0,
                    exits_count INTEGER DEFAULT 0,
                    active_count INTEGER DEFAULT 0,
                    result_hash TEXT,
                    PRIMARY KEY (date, phase)
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS job_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_sec REAL NOT NULL,
                    symbols_processed INTEGER DEFAULT 0,
                    api_requests INTEGER DEFAULT 0,
                    db_rows_updated INTEGER DEFAULT 0,
                    summary_json TEXT DEFAULT '{}',
                    status TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def is_phase_committed(self, target_date: str, phase: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM daily_v2_cycle_runs WHERE date = ? AND phase = ? AND status IN ('COMMITTED', 'SUCCESS', 'DECISION_COMMITTED', 'EOD_FINALIZED')",
                (target_date, phase)
            )
            return cur.fetchone() is not None

    def get_cycle_run(self, target_date: str, phase: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM daily_v2_cycle_runs WHERE date = ? AND phase = ?",
                (target_date, phase)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def is_date_processed(self, target_date: str) -> bool:
        return self.is_phase_committed(target_date, "DECISION") or self.is_phase_committed(target_date, "FINALIZE")

    def record_cycle_run(
        self,
        target_date: str,
        phase: str,
        new_entries: int,
        exits: int,
        active_count: int,
        status: str = "COMMITTED",
        result_hash: Optional[str] = None
    ):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO daily_v2_cycle_runs (
                    date, phase, status, completed_at, new_entries_count, exits_count, active_count, result_hash
                ) VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
            """, (target_date, phase, status, new_entries, exits, active_count, result_hash))
            conn.commit()

    def reset_trade_database(self):
        """Cleans all trades, candidates, and cycle runs for a deterministic fresh rebuild."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM daily_v2_trades")
            cur.execute("DELETE FROM daily_v2_candidates")
            cur.execute("DELETE FROM daily_v2_cycle_runs")
            cur.execute("DELETE FROM sqlite_sequence WHERE name IN ('daily_v2_trades', 'daily_v2_candidates')")
            conn.commit()

    def record_candidate(self, cand_data: Dict[str, Any]):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO daily_v2_candidates (
                    strategy_id, symbol, signal_date, raw_score, calibrated_p, threshold, decision, rejection_reason, competing_strategies
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cand_data['strategy_id'],
                cand_data['symbol'],
                cand_data['date'],
                cand_data.get('raw_score', 0.0),
                cand_data.get('calibrated_probability', 0.0),
                cand_data.get('threshold', 0.60),
                cand_data['decision'],
                cand_data.get('rejection_reason'),
                json.dumps(cand_data.get('competing_strategies', []))
            ))
            conn.commit()

    def is_symbol_active(self, symbol: str) -> bool:
        """Checks if a position is currently ACTIVE for this symbol."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM daily_v2_trades WHERE symbol = ? AND status = 'ACTIVE'", (symbol,))
            return cur.fetchone()[0] > 0

    def create_trade(self, trade_data: Dict[str, Any]) -> Optional[int]:
        """
        Creates a new trade if no active position already exists for this symbol.
        Enforces 1 Stock = 1 Active Position rule.
        """
        sym = trade_data['symbol']
        if self.is_symbol_active(sym):
            return None

        also_trig = trade_data.get('also_triggered_by', [])
        also_json = json.dumps(also_trig) if isinstance(also_trig, list) else str(also_trig)

        with self._get_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("""
                    INSERT INTO daily_v2_trades (
                        strategy_id, strategy_family, symbol, signal_date, entry_date, entry_price,
                        status, health_state, raw_ml_score, calibrated_p, also_triggered_by, model_version
                    ) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', 'HEALTHY', ?, ?, ?, ?)
                """, (
                    trade_data['strategy_id'],
                    trade_data.get('strategy_family', 'TREND'),
                    sym,
                    trade_data['signal_date'],
                    trade_data['entry_date'],
                    trade_data['entry_price'],
                    trade_data.get('raw_score', 0.0),
                    trade_data.get('calibrated_probability', trade_data.get('calibrated_p', 0.60)),
                    also_json,
                    trade_data.get('model_version', '2.0.0-PROD-FROZEN')
                ))
                conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                # Active position or same-day entry already exists
                return None

    def get_active_trades(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM daily_v2_trades WHERE status = 'ACTIVE' ORDER BY entry_date DESC")
            return [dict(row) for row in cur.fetchall()]

    def get_closed_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM daily_v2_trades WHERE status = 'EXITED' ORDER BY exit_date DESC, id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    def update_trade_live_price(
        self,
        trade_id: int,
        live_price: float,
        roi_pct: float,
        mfe_pct: Optional[float] = None,
        mae_pct: Optional[float] = None,
        health_state: Optional[str] = None
    ):
        """
        Updates live price, live timestamp, and dynamic ROI for an active position.
        Never modifies immutable entry fields (entry_price, entry_date, etc.).
        """
        with self._get_connection() as conn:
            cur = conn.cursor()
            if mfe_pct is not None and mae_pct is not None and health_state is not None:
                cur.execute("""
                    UPDATE daily_v2_trades SET
                        live_price = ?,
                        live_price_updated_at = CURRENT_TIMESTAMP,
                        roi_pct = ?,
                        mfe_pct = ?,
                        mae_pct = ?,
                        health_state = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (live_price, roi_pct, mfe_pct, mae_pct, health_state, trade_id))
            else:
                cur.execute("""
                    UPDATE daily_v2_trades SET
                        live_price = ?,
                        live_price_updated_at = CURRENT_TIMESTAMP,
                        roi_pct = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (live_price, roi_pct, trade_id))
            conn.commit()

    def update_trade_health(self, trade_id: int, health_info: Dict[str, Any], days_held: int):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE daily_v2_trades SET
                    health_state = ?,
                    mfe_pct = ?,
                    mae_pct = ?,
                    roi_pct = ?,
                    holding_days = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                health_info['health_state'],
                health_info['mfe_pct'],
                health_info['mae_pct'],
                health_info['roi_pct'],
                days_held,
                trade_id
            ))
            conn.commit()

    def close_trade(self, trade_id: int, exit_info: Dict[str, Any], exit_date: str, days_held: int):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE daily_v2_trades SET
                    status = 'EXITED',
                    health_state = 'EXITED',
                    exit_date = ?,
                    exit_price = ?,
                    exit_reason = ?,
                    mfe_pct = ?,
                    mae_pct = ?,
                    roi_pct = ?,
                    holding_days = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (
                exit_date,
                exit_info['exit_price'],
                exit_info['exit_reason'],
                exit_info['mfe_pct'],
                exit_info['mae_pct'],
                exit_info['roi_pct'],
                days_held,
                trade_id
            ))
            conn.commit()

    def is_position_active(self, symbol: str, strategy_id: str = None) -> bool:
        return self.is_symbol_active(symbol)

    def record_job_execution(
        self,
        job_name: str,
        started_at: str,
        finished_at: str,
        duration_sec: float,
        symbols_processed: int,
        api_requests: int,
        db_rows_updated: int,
        summary_dict: Dict[str, Any],
        status: str = "SUCCESS"
    ):
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO job_executions (
                    job_name, started_at, finished_at, duration_sec, symbols_processed,
                    api_requests, db_rows_updated, summary_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_name,
                started_at,
                finished_at,
                duration_sec,
                symbols_processed,
                api_requests,
                db_rows_updated,
                json.dumps(summary_dict),
                status
            ))
            conn.commit()

    def get_recent_job_executions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM job_executions ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

