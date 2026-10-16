"""
deploy/l0_l5_observation/shadow_logger.py
=========================================
Shadow-mode logging for the ML Engine v2 (L5) running alongside L0 production.

Runs L5's scoring on the same universe as L0, logs every decision L5 would
have made, but does not act on it. This produces the parallel dataset that
will be used for the 90-day comparison.
"""
import sqlite3
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Ensure banana_engine is on path
_PKG_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "QuantSync_Lines", "banana_engine"))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = Path(_REPO_ROOT) / 'deploy' / 'l0_l5_observation' / 'shadow_predictions.db'
SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_predictions (
    prediction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    pattern         TEXT NOT NULL,
    -- L5 outputs
    meta_score      REAL,
    analog_win_rate REAL,
    analog_weighted_r REAL,
    combined_score  REAL,
    percentile_score REAL,
    size_pct        REAL,
    -- risk parameters
    entry_price     REAL,
    stop_price      REAL,
    target_price    REAL,
    exit_rule_type  TEXT,
    pred_mfe_r      REAL,
    pred_days       REAL,
    -- system marker
    system          TEXT NOT NULL DEFAULT 'L5',
    -- outcome fields, filled by resolver
    resolved        INTEGER DEFAULT 0,
    resolved_at     TEXT,
    outcome         TEXT,
    realized_r      REAL,
    days_held       INTEGER,
    UNIQUE(as_of_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_shadow_resolved ON shadow_predictions(resolved);
CREATE INDEX IF NOT EXISTS idx_shadow_symbol_date ON shadow_predictions(symbol, as_of_date);
"""


def init_db():
    os.makedirs(DB_PATH.parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log_shadow_prediction(symbol, as_of_date, pattern, meta_score,
                          analog_win_rate, analog_weighted_r,
                          combined_score, percentile_score, size_pct,
                          entry_price, stop_price, target_price,
                          exit_rule_type, pred_mfe_r, pred_days):
    conn = init_db()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO shadow_predictions (
                created_at, as_of_date, symbol, pattern,
                meta_score, analog_win_rate, analog_weighted_r,
                combined_score, percentile_score, size_pct,
                entry_price, stop_price, target_price,
                exit_rule_type, pred_mfe_r, pred_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            str(as_of_date), symbol, pattern,
            meta_score, analog_win_rate, analog_weighted_r,
            combined_score, percentile_score, size_pct,
            entry_price, stop_price, target_price,
            exit_rule_type, pred_mfe_r, pred_days,
        ))
        conn.commit()
    finally:
        conn.close()


def resolve_pending_outcomes(horizon=40):
    """Walk unresolved shadow predictions forward 40 bars and compute realized R."""
    from core.data_layer import read_ohlcv_raw
    conn = init_db()
    cur = conn.execute("""
        SELECT prediction_id, symbol, as_of_date, entry_price, stop_price,
               target_price, exit_rule_type, pred_days
        FROM shadow_predictions
        WHERE resolved = 0
    """)
    rows = cur.fetchall()
    resolved_count = 0
    for (pid, sym, aod, ep, sp, tp, rule, pdays) in rows:
        try:
            bars = read_ohlcv_raw(sym)
        except Exception:
            continue
        if bars is None or bars.empty:
            continue
        bars = bars.reset_index(drop=True)
        date_col = 'date' if 'date' in bars.columns else 't'
        sig_idxs = bars.index[bars[date_col].astype(str).str[:10] == str(aod)]
        if len(sig_idxs) == 0:
            continue
        i = int(sig_idxs[0])
        if i + 1 >= len(bars):
            continue
        future = bars.iloc[i+1 : i+1+horizon]
        if len(future) < 5:
            continue
        r_dist = ep - sp
        if r_dist <= 0:
            continue
        realized_r = 0.0
        outcome = 'TIME_EXIT'
        max_bars = min(len(future), int(pdays * 1.5) if pdays else horizon)
        for bi in range(max_bars):
            hb = float(future.iloc[bi]['h'] if 'h' in future.columns else future.iloc[bi]['high'])
            lb = float(future.iloc[bi]['l'] if 'l' in future.columns else future.iloc[bi]['low'])
            cb = float(future.iloc[bi]['c'] if 'c' in future.columns else future.iloc[bi]['close'])
            if lb <= sp:
                realized_r = (sp - ep) / r_dist
                outcome = 'STOP_HIT'
                break
            if tp and hb >= tp:
                realized_r = (tp - ep) / r_dist
                outcome = 'TARGET_HIT'
                break
            if bi == max_bars - 1:
                realized_r = (cb - ep) / r_dist
                outcome = 'TIME_EXIT'
                break
        conn.execute("""
            UPDATE shadow_predictions
            SET resolved = 1, resolved_at = ?, outcome = ?,
                realized_r = ?, days_held = ?
            WHERE prediction_id = ?
        """, (datetime.utcnow().isoformat(), outcome, realized_r, max_bars, pid))
        resolved_count += 1
    conn.commit()
    conn.close()
    return resolved_count


def get_shadow_stats():
    conn = init_db()
    cur = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved,
            AVG(CASE WHEN resolved = 1 THEN realized_r END) as mean_r,
            SUM(CASE WHEN resolved = 1 AND realized_r > 0 THEN 1 ELSE 0 END) * 1.0
                / NULLIF(SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END), 0) as win_rate
        FROM shadow_predictions
    """)
    row = cur.fetchone()
    conn.close()
    return {
        'total': row[0] or 0,
        'resolved': row[1] or 0,
        'mean_r': float(row[2] or 0.0),
        'win_rate': float(row[3] or 0.0),
    }
