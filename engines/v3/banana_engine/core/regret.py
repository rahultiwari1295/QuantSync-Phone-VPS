"""
banana_engine/core/regret.py — Counterfactual Regret Ledger & Perception Recall Audit (v5)
=========================================================================================
Implements:
  1. SQLite Counterfactual Regret Ledger logging both TAKEN and SKIPPED setups.
  2. 20-Day Path Outcome Resolver for unresolved setups.
  3. Constrained Regret Optimization: Maximizes average return R_bar subject to N_takes/month >= 4.
  4. Monthly Perception Recall Audit: Detects missed mega-winners (>30% moves) not captured by bases.
"""

import os
import sys
import json
import sqlite3
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB, read_ohlcv_raw, get_connection
from core.structure import detect_base_at

REGRET_DB = os.path.join(DATA_DIR, "regret_ledger.db")


# FIXED: Fix 6.1 (MINOR-01) — Module-level DB initialization helper
def initialize_db() -> None:
    """Initializes regret ledger database schema and performance indices."""
    ledger = RegretLedger()
    ledger._init_db()


class RegretLedger:
    """
    Manages counterfactual decision logging and threshold optimization.
    """
    def __init__(self):
        self._init_db()

    def _init_db(self):
        with get_connection(REGRET_DB) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS regret_entries (
                    setup_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    breakout_date TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    skip_reason TEXT,
                    score REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    resolved INTEGER DEFAULT 0,
                    resolved_date TEXT,
                    outcome TEXT,
                    realized_r REAL,
                    mfe_pct REAL,
                    mae_pct REAL
                )
            """)
            # FIXED: Fix 6.1 (MINOR-01) — Indices for resolution and symbol queries
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regret_resolved ON regret_entries(resolved);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regret_symbol ON regret_entries(symbol, breakout_date);")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS perception_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    peak_date TEXT NOT NULL,
                    actual_gain_pct REAL NOT NULL,
                    base_detected INTEGER NOT NULL,
                    notes TEXT
                )
            """)
            conn.commit()

    def log_setup(
        self,
        symbol: str,
        breakout_date: str,
        decision: str,  # "TAKEN" or "SKIPPED"
        score: float,
        entry_price: float,
        stop_price: float,
        target_price: float,
        skip_reason: Optional[str] = None
    ) -> str:
        """
        Logs a setup decision (either TAKEN or SKIPPED) to the counterfactual ledger.
        """
        setup_id = f"{symbol.upper()}_{breakout_date}_{int(entry_price * 10)}"
        with get_connection(REGRET_DB) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO regret_entries 
                (setup_id, symbol, breakout_date, decision, skip_reason, score, entry_price, stop_price, target_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (setup_id, symbol.upper().strip(), breakout_date, decision, skip_reason, score, entry_price, stop_price, target_price))
            conn.commit()
        return setup_id

    def resolve_pending_outcomes(self, horizon: int = 20) -> int:
        """
        Scans all unresolved setups where 20 trading days have elapsed and evaluates outcomes.
        """
        with get_connection(REGRET_DB) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT setup_id, symbol, breakout_date, entry_price, stop_price, target_price FROM regret_entries WHERE resolved = 0")
            pending = cursor.fetchall()

        resolved_count = 0
        updates = []

        for setup_id, sym, b_date, entry, stop, target in pending:
            bars = read_ohlcv_raw(sym)
            if bars.empty:
                continue

            future = bars[bars['date'] > str(b_date)[:10]].head(horizon)
            if len(future) < horizon:
                # Check if this symbol has stopped trading entirely (Fix 5.3)
                last_bar_date = bars['date'].iloc[-1]
                days_since_last_bar = (pd.Timestamp.now() - pd.Timestamp(last_bar_date)).days
                if days_since_last_bar > 30:
                    # FIXED: Fix 5.3 (MAJOR-07) — Delisted. Resolve as terminal loss.
                    risk_per_share = max(entry - stop, entry * 0.02)
                    last_close = float(bars['close'].iloc[-1])
                    realized_r = round(float((last_close - entry) / risk_per_share), 2)
                    res_date = str(last_bar_date)[:10]
                    updates.append((1, res_date, 'DELISTED', realized_r, 0.0, -100.0, setup_id))
                    resolved_count += 1
                    continue
                else:
                    continue  # Not yet 20 trading days

            risk_per_share = max(entry - stop, entry * 0.02)
            target_r = (target - entry) / risk_per_share

            highs = future['high'].values
            lows = future['low'].values
            closes = future['close'].values

            mfe = round(float((np.max(highs) - entry) / entry * 100.0), 2)
            mae = round(float((np.min(lows) - entry) / entry * 100.0), 2)

            # Evaluate order-dependent first touch
            outcome = "TIMEOUT"
            realized_r = round(float((closes[-1] - entry) / risk_per_share), 2)

            for _, row in future.iterrows():
                if row['low'] <= stop:
                    outcome = "LOSS"
                    realized_r = -1.0
                    break
                if row['high'] >= target:
                    outcome = "WIN"
                    realized_r = round(float(target_r), 2)
                    break

            res_date = str(future['date'].iloc[-1])[:10]
            updates.append((1, res_date, outcome, realized_r, mfe, mae, setup_id))
            resolved_count += 1

        if updates:
            with get_connection(REGRET_DB) as conn:
                conn.executemany("""
                    UPDATE regret_entries 
                    SET resolved = ?, resolved_date = ?, outcome = ?, realized_r = ?, mfe_pct = ?, mae_pct = ?
                    WHERE setup_id = ?
                """, updates)
                conn.commit()

        return resolved_count

    def optimize_threshold(
        self,
        min_takes_per_month: int = 4,
        max_takes_per_month: int = 15
    ) -> Dict[str, Any]:
        """
        Evaluates candidate thresholds theta in [0.40, 0.75] to find theta* that maximizes
        average realized return R_bar subject to taking at least min_takes_per_month setups.
        """
        with get_connection(REGRET_DB) as conn:
            df = pd.read_sql_query("SELECT * FROM regret_entries WHERE resolved = 1", conn)

        if len(df) < 10:
            return {
                "status": "INSUFFICIENT_DATA",
                "recommended_threshold": 0.55,
                "current_sample_count": len(df),
                "message": "Fewer than 10 resolved setups in ledger. Retaining default 0.55 hurdle."
            }

        # Estimate number of months in resolved sample
        df['breakout_date'] = pd.to_datetime(df['breakout_date'])
        time_span_days = max(30, (df['breakout_date'].max() - df['breakout_date'].min()).days)
        months = max(1.0, time_span_days / 30.0)

        thresholds = np.arange(0.40, 0.76, 0.02)
        curve = []

        best_theta = 0.55
        best_r_bar = -999.0

        for theta in thresholds:
            sub = df[df['score'] >= theta]
            n_takes = len(sub)
            takes_per_month = n_takes / months

            if takes_per_month < min_takes_per_month:
                continue

            r_bar = float(sub['realized_r'].mean()) if n_takes > 0 else 0.0
            win_rate = float((sub['outcome'] == "WIN").mean() * 100.0) if n_takes > 0 else 0.0

            curve.append({
                "threshold": round(float(theta), 2),
                "n_takes": n_takes,
                "takes_per_month": round(float(takes_per_month), 1),
                "r_bar": round(r_bar, 3),
                "win_rate": round(win_rate, 1)
            })

            if r_bar > best_r_bar:
                best_r_bar = r_bar
                best_theta = float(theta)

        return {
            "status": "OPTIMIZED",
            "recommended_threshold": round(best_theta, 2),
            "expected_r_bar": round(best_r_bar, 3),
            "resolved_trades_evaluated": len(df),
            "optimization_curve": curve
        }

    def run_perception_recall_audit(self, lookback_days: int = 180, min_gain_pct: float = 30.0) -> Dict[str, Any]:
        """
        Audits whether the system missed any huge multi-week winners (>30% gain).
        Identifies potential blind spots in swing/base detection.
        """
        with get_connection(STOCK_HISTORY_DB) as conn:
            symbols = [r[0] for r in conn.execute("SELECT symbol FROM StockHistory").fetchall()][:20]

        audits = []
        for sym in symbols:
            bars = read_ohlcv_raw(sym)
            if len(bars) < 60:
                continue

            recent = bars.tail(lookback_days).reset_index(drop=True)
            if len(recent) < 40:
                continue

            # Look for 40-day windows with >30% gain
            for i in range(0, len(recent) - 30, 15):
                start_c = float(recent['close'].iloc[i])
                future_sub = recent.iloc[i : i + 30]
                peak_h = float(future_sub['high'].max())
                gain_pct = (peak_h - start_c) / start_c * 100.0

                if gain_pct >= min_gain_pct:
                    # Check if base was detected around start_c
                    base = detect_base_at(recent, i)
                    audits.append({
                        "symbol": sym,
                        "start_date": str(recent['date'].iloc[i])[:10],
                        "peak_gain_pct": round(gain_pct, 1),
                        "base_detected": 1 if base is not None else 0
                    })
                    break

        detected_count = sum(1 for a in audits if a['base_detected'] == 1)
        total_missed = len(audits) - detected_count
        recall_pct = round(detected_count / max(1, len(audits)) * 100.0, 1)

        return {
            "audit_date": pd.Timestamp.now().strftime("%Y-%m-%d"),
            "big_movers_identified": len(audits),
            "bases_captured": detected_count,
            "missed_moves": total_missed,
            "perception_recall_pct": recall_pct,
            "sample_audits": audits[:5]
        }


if __name__ == "__main__":
    print("Testing RegretLedger & Perception Audit...")
    ledger = RegretLedger()

    # Log sample setups
    s1 = ledger.log_setup("RELIANCE", "2023-01-10", "TAKEN", score=0.68, entry_price=1200, stop_price=1120, target_price=1400)
    s2 = ledger.log_setup("TCS", "2023-02-15", "SKIPPED", score=0.48, entry_price=3200, stop_price=3050, target_price=3500, skip_reason="Below EV_R hurdle")
    print(f"Logged sample setups: {s1}, {s2}")

    resolved = ledger.resolve_pending_outcomes()
    print(f"Resolved {resolved} pending setups.")

    opt = ledger.optimize_threshold()
    print("Threshold Optimization:", json.dumps(opt, indent=2))

    audit = ledger.run_perception_recall_audit(lookback_days=180)
    print("Perception Recall Audit:", json.dumps(audit, indent=2))
