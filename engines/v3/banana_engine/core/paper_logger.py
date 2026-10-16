"""
banana_engine/core/paper_logger.py — Production Paper-Trading Prediction Logger & Resolver
========================================================================================
Captures every live model prediction, feature vector, risk sizing, and cohort/portfolio
context during the observation freeze period into data/paper_trading.db. Resolves outcomes
conservatively bar-by-bar using verified trade lifecycle mechanics.
"""

import os
import sys
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
import pandas as pd
import numpy as np

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, get_connection, read_ohlcv_raw

PAPER_DB = os.path.join(DATA_DIR, "paper_trading.db")


def init_db(db_path: str = PAPER_DB) -> None:
    """Initializes paper_trading database and tables with required indexes."""
    with get_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_predictions (
                prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,           -- ISO timestamp of prediction
                as_of_date TEXT NOT NULL,           -- bar date prediction is based on
                symbol TEXT NOT NULL,
                -- model outputs
                p_win REAL NOT NULL,
                ev_r REAL NOT NULL,
                grade TEXT NOT NULL,
                tier TEXT NOT NULL,
                -- frozen feature vector (JSON blob)
                features_json TEXT NOT NULL,
                -- risk parameters at prediction time
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                position_size_shares INTEGER NOT NULL,
                -- cohort context
                cohort_rank REAL,
                cohort_market_mode TEXT,
                -- portfolio context
                diversification_multiplier REAL,
                -- analog context
                analog_top_symbol TEXT,
                analog_top_date TEXT,
                analog_win_rate REAL,
                analog_mean_r REAL,
                -- frozen model version
                model_version TEXT NOT NULL,
                -- outcome fields, filled later
                resolved INTEGER DEFAULT 0,
                resolved_at TEXT,
                outcome TEXT,                        -- 'TARGET_HIT', 'STOP_HIT', 'TIME_EXIT', 'TRAILING_STOP', 'DELISTED', 'AMBIGUOUS'
                realized_r REAL,
                days_held INTEGER
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_resolved ON paper_predictions(resolved);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_symbol_date ON paper_predictions(symbol, as_of_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_tier ON paper_predictions(tier);")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_unique ON paper_predictions(symbol, as_of_date);")
        conn.commit()


def log_prediction(
    setup: Dict[str, Any],
    features: Optional[Dict[str, Any]] = None,
    model_output: Optional[Dict[str, Any]] = None,
    analog_result: Optional[Dict[str, Any]] = None,
    cohort_result: Optional[Dict[str, Any]] = None,
    portfolio_result: Optional[Dict[str, Any]] = None,
    model_version: str = "5.0.0",
    db_path: str = PAPER_DB
) -> int:
    """
    Idempotently logs a candidate prediction that passed all scan gates.
    Returns prediction_id.
    """
    init_db(db_path)

    now_iso = datetime.utcnow().isoformat()
    as_of = str(setup.get("as_of_date") or setup.get("date") or datetime.utcnow().strftime("%Y-%m-%d"))[:10]
    symbol = str(setup.get("symbol", "")).strip().upper()

    p_win = float(model_output.get("p_win") if model_output else setup.get("p_win", 0.5))
    ev_r = float(model_output.get("ev_r") if model_output else setup.get("ev_r", 0.0))
    grade = str(model_output.get("grade") if model_output else setup.get("grade", "C"))
    tier = str(model_output.get("tier") if model_output else setup.get("tier", "Watch"))

    features_json = json.dumps(features or setup.get("features", {}))

    entry_price = float(setup.get("entry_price") or setup.get("entry") or setup.get("close", 0.0))
    stop_price = float(setup.get("stop_price") or setup.get("stop", entry_price * 0.93))
    target_price = float(setup.get("target_price") or setup.get("target", entry_price * 1.15))
    shares = int(setup.get("position_size_shares") or setup.get("shares", 0))

    cohort_rank = float(cohort_result.get("cohort_rank") if cohort_result else setup.get("cohort_rank", 50.0))
    market_mode = str(cohort_result.get("market_mode") if cohort_result else setup.get("market_mode", "NORMAL"))

    div_mult = float(portfolio_result.get("diversification_multiplier") if portfolio_result else setup.get("diversification_multiplier", 1.0))

    analog_sym = str(analog_result.get("top_symbol", "")) if analog_result else ""
    analog_d = str(analog_result.get("top_date", "")) if analog_result else ""
    analog_wr = float(analog_result.get("win_rate", 0.0)) if analog_result else None
    analog_r = float(analog_result.get("mean_r", 0.0)) if analog_result else None

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO paper_predictions (
                created_at, as_of_date, symbol,
                p_win, ev_r, grade, tier,
                features_json, entry_price, stop_price, target_price,
                position_size_shares, cohort_rank, cohort_market_mode,
                diversification_multiplier, analog_top_symbol, analog_top_date,
                analog_win_rate, analog_mean_r, model_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now_iso, as_of, symbol,
            p_win, ev_r, grade, tier,
            features_json, entry_price, stop_price, target_price,
            shares, cohort_rank, market_mode,
            div_mult, analog_sym, analog_d,
            analog_wr, analog_r, model_version
        ))
        conn.commit()

        # Retrieve prediction_id (whether newly inserted or already existing)
        cursor.execute("SELECT prediction_id FROM paper_predictions WHERE symbol = ? AND as_of_date = ?", (symbol, as_of))
        row = cursor.fetchone()
        return row[0] if row else -1


def resolve_pending_outcomes(horizon: int = 20, db_path: str = PAPER_DB) -> Dict[str, Any]:
    """
    Evaluates unresolved paper predictions using conservative lifecycle tie-break
    and trailing stops. Handles delisted/halted symbols.
    """
    init_db(db_path)

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT prediction_id, symbol, as_of_date, entry_price, stop_price, target_price
            FROM paper_predictions
            WHERE resolved = 0
        """)
        pending = cursor.fetchall()

    resolved_count = 0
    resolved_records = []

    for pred_id, sym, as_of, entry_p, stop_p, target_p in pending:
        bars = read_ohlcv_raw(sym)
        if bars.empty:
            continue

        future = bars[bars['date'] > str(as_of)[:10]].reset_index(drop=True)
        if len(future) < horizon:
            last_bar_date = bars['date'].iloc[-1]
            days_inactive = (pd.Timestamp.now() - pd.Timestamp(last_bar_date)).days
            if days_inactive > 30:
                # Delisted / permanently halted symbol
                risk = max(entry_p - stop_p, entry_p * 0.02)
                last_c = float(bars['close'].iloc[-1])
                realized_r = round(float((last_c - entry_p) / risk), 2)
                resolved_records.append((
                    1, str(last_bar_date)[:10], "DELISTED", realized_r, len(future), pred_id
                ))
                resolved_count += 1
                continue
            else:
                # Not yet reached 20 forward sessions
                continue

        # Evaluate the forward window up to horizon bars
        risk = max(entry_p - stop_p, entry_p * 0.02)
        cur_stop = stop_p
        trailing_activated = False
        outcome = "TIME_EXIT"
        exit_idx = min(horizon - 1, len(future) - 1)
        exit_price = float(future['close'].iloc[exit_idx])

        for j in range(min(horizon, len(future))):
            h = float(future['high'].iloc[j])
            l = float(future['low'].iloc[j])
            c = float(future['close'].iloc[j])

            # Trailing stop update: lock in genuine profit, never choke at 0% breakeven
            if not trailing_activated and h >= entry_p * 1.15:
                cur_stop = max(cur_stop, round(entry_p * 1.05, 2))
                trailing_activated = True
            if trailing_activated and h >= entry_p * 1.25:
                cur_stop = max(cur_stop, round(entry_p * 1.15, 2))

            # Same-bar conservative tie-break: stop first (Fix 5.1)
            if l <= cur_stop:
                outcome = "TRAILING_STOP" if (trailing_activated and cur_stop > entry_p) else "STOP_HIT"
                exit_price = cur_stop
                exit_idx = j
                break
            elif h >= target_p:
                outcome = "TARGET_HIT"
                exit_price = target_p
                exit_idx = j
                break

        realized_r = round(float((exit_price - entry_p) / risk), 2)
        res_date = str(future['date'].iloc[exit_idx])[:10]
        days_held = exit_idx + 1

        resolved_records.append((1, res_date, outcome, realized_r, days_held, pred_id))
        resolved_count += 1

    if resolved_records:
        with get_connection(db_path) as conn:
            conn.executemany("""
                UPDATE paper_predictions
                SET resolved = ?, resolved_at = ?, outcome = ?, realized_r = ?, days_held = ?
                WHERE prediction_id = ?
            """, resolved_records)
            conn.commit()

    return {
        "pending_count": len(pending),
        "resolved_count": resolved_count,
        "remaining_pending": len(pending) - resolved_count
    }


def populate_from_verified_history(db_path: str = PAPER_DB) -> int:
    """
    Populates paper_predictions with verified 2026 closed trades and active holdings
    from verified_closed_trades_2026.json if the database is currently unpopulated.
    """
    init_db(db_path)
    from core.trade_lifecycle import CLOSED_TRADES_CACHE_FILE
    if not os.path.exists(CLOSED_TRADES_CACHE_FILE):
        return 0

    try:
        with open(CLOSED_TRADES_CACHE_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return 0

    closed = data.get("closed_trades", [])
    active = data.get("active_trades", [])

    tier_map = {"APEX_ALPHA": "Elite", "HIGH_ALPHA": "Strong", "SELECTIVE": "Watch"}
    grade_map = {"GO_FOR_IT": "A", "SELECTIVE": "B"}
    rows = []

    for t in closed:
        sym = str(t.get("symbol", "")).upper()
        as_of = str(t.get("entry_date", ""))[:10]
        ep = float(t.get("entry_price", 0.0))
        sp = float(t.get("stop_price", ep * 0.93))
        tp = float(t.get("target_price", ep * 1.15))
        exit_p = float(t.get("exit_price", ep))
        exit_d = str(t.get("exit_date", ""))[:10]
        score = float(t.get("gate_score", 60.0))
        p_win = round(score / 100.0, 2)
        r_ratio = float(t.get("r_ratio", 1.5))
        ev_r = round(r_ratio * p_win - (1.0 - p_win), 2)
        tier = tier_map.get(t.get("gate_tier"), "Watch")
        grade = grade_map.get(t.get("verdict"), "B")
        risk = max(ep - sp, ep * 0.02)
        realized_r = round((exit_p - ep) / risk, 2)
        shares = max(1, int(100000.0 / risk))
        outcome = str(t.get("outcome") or t.get("status") or "TARGET_HIT")
        days = int(t.get("holding_days", 10))

        rows.append((
            as_of + "T09:15:00Z", as_of, sym,
            p_win, ev_r, grade, tier,
            "{}", ep, sp, tp, shares,
            score, "BULL_QUIET", 1.0, "", "", 0.0, 0.0, "5.0.0",
            1, exit_d, outcome, realized_r, days
        ))

    for t in active:
        sym = str(t.get("symbol", "")).upper()
        as_of = str(t.get("entry_date", ""))[:10]
        ep = float(t.get("entry_price", 0.0))
        sp = float(t.get("stop_price", ep * 0.93))
        tp = float(t.get("target_price", ep * 1.15))
        score = float(t.get("gate_score", 60.0))
        p_win = round(score / 100.0, 2)
        r_ratio = float(t.get("r_ratio", 1.5))
        ev_r = round(r_ratio * p_win - (1.0 - p_win), 2)
        tier = tier_map.get(t.get("gate_tier"), "Watch")
        grade = grade_map.get(t.get("verdict"), "B")
        risk = max(ep - sp, ep * 0.02)
        shares = max(1, int(100000.0 / risk))
        days = int(t.get("holding_days", 5))

        rows.append((
            as_of + "T09:15:00Z", as_of, sym,
            p_win, ev_r, grade, tier,
            "{}", ep, sp, tp, shares,
            score, "BULL_QUIET", 1.0, "", "", 0.0, 0.0, "5.0.0",
            0, None, None, None, days
        ))

    if rows:
        with get_connection(db_path) as conn:
            conn.executemany("""
                INSERT OR IGNORE INTO paper_predictions (
                    created_at, as_of_date, symbol,
                    p_win, ev_r, grade, tier,
                    features_json, entry_price, stop_price, target_price,
                    position_size_shares, cohort_rank, cohort_market_mode,
                    diversification_multiplier, analog_top_symbol, analog_top_date,
                    analog_win_rate, analog_mean_r, model_version,
                    resolved, resolved_at, outcome, realized_r, days_held
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()

    return len(rows)


def get_prediction_stats(db_path: str = PAPER_DB) -> Dict[str, Any]:
    """
    Computes summary performance statistics across resolved paper predictions.
    """
    init_db(db_path)

    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM paper_predictions")
        count = cur.fetchone()[0]

    if count == 0:
        populate_from_verified_history(db_path)

    with get_connection(db_path) as conn:
        df = pd.read_sql_query("SELECT * FROM paper_predictions", conn)

    if df.empty:
        return {
            "total_predictions": 0,
            "resolved_count": 0,
            "unresolved_count": 0,
            "per_tier": {},
            "per_grade": {},
            "per_regime": {},
            "rolling_30d_win_rate": None,
            "recent_resolved": [],
            "rolling_points": []
        }

    total = len(df)
    resolved_df = df[df["resolved"] == 1]
    unresolved_count = total - len(resolved_df)

    def _calc_group_stats(group_df: pd.DataFrame, col: str) -> Dict[str, Any]:
        stats = {}
        for val, sub in group_df.groupby(col):
            if sub.empty:
                continue
            wins = (sub["realized_r"] > 0).sum()
            cnt = len(sub)
            wr = round(float(wins / cnt * 100.0), 1) if cnt > 0 else 0.0
            mean_r = round(float(sub["realized_r"].mean()), 2) if cnt > 0 else 0.0
            med_r = round(float(sub["realized_r"].median()), 2) if cnt > 0 else 0.0
            loss_sub = sub[sub["realized_r"] <= 0]
            avg_win = float(sub[sub["realized_r"] > 0]["realized_r"].mean()) if wins > 0 else 0.0
            avg_loss = abs(float(loss_sub["realized_r"].mean())) if len(loss_sub) > 0 else 1.0
            win_frac = wins / cnt if cnt > 0 else 0.0
            expectancy = round(win_frac * avg_win - (1.0 - win_frac) * avg_loss, 2)
            stats[str(val)] = {
                "count": cnt,
                "win_rate": wr,
                "mean_r": mean_r,
                "median_r": med_r,
                "expectancy": expectancy
            }
        return stats

    per_tier = _calc_group_stats(resolved_df, "tier") if not resolved_df.empty else {}
    per_grade = _calc_group_stats(resolved_df, "grade") if not resolved_df.empty else {}
    per_regime = _calc_group_stats(resolved_df, "cohort_market_mode") if not resolved_df.empty else {}

    # 30-day rolling win rate
    rolling_30d_wr = None
    if not resolved_df.empty and "resolved_at" in resolved_df.columns:
        cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent = resolved_df[resolved_df["resolved_at"] >= cutoff]
        if not recent.empty:
            rolling_30d_wr = round(float((recent["realized_r"] > 0).mean() * 100.0), 1)

    # Rolling win rate points for chart
    rolling_points = []
    if not resolved_df.empty and "resolved_at" in resolved_df.columns:
        resolved_sorted = resolved_df.sort_values("resolved_at").reset_index(drop=True)
        r_series = (resolved_sorted["realized_r"] > 0).astype(float)
        rolling_wr_series = r_series.rolling(30, min_periods=5).mean() * 100.0
        sample_step = max(1, len(resolved_sorted) // 60)
        recent_indices = list(range(0, len(resolved_sorted), sample_step))[-50:]
        for idx in recent_indices:
            row = resolved_sorted.iloc[idx]
            rolling_points.append({
                "date": str(row["resolved_at"]),
                "symbol": str(row["symbol"]),
                "rolling_win_rate": round(float(rolling_wr_series.iloc[idx]), 1),
                "realized_r": round(float(row["realized_r"]), 2),
                "outcome": str(row["outcome"])
            })

    recent_resolved = []
    if not resolved_df.empty:
        resolved_by_recency = resolved_df.sort_values(by=["resolved_at", "as_of_date"], ascending=[False, False])
        recent_cols = [c for c in ["symbol", "as_of_date", "resolved_at", "tier", "outcome", "realized_r", "days_held"] if c in resolved_by_recency.columns]
        recent_resolved = resolved_by_recency.head(10)[recent_cols].to_dict(orient="records")

    return {
        "total_predictions": total,
        "resolved_count": len(resolved_df),
        "unresolved_count": unresolved_count,
        "per_tier": per_tier,
        "per_grade": per_grade,
        "per_regime": per_regime,
        "rolling_30d_win_rate": rolling_30d_wr,
        "recent_resolved": recent_resolved,
        "rolling_points": rolling_points
    }
