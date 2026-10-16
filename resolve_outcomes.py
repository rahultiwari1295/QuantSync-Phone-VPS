#!/usr/bin/env python3
"""
resolve_outcomes.py — Nightly Signal Outcome Resolution Job
===========================================================
Run via cron at 4 AM IST every weekday:
    0 4 * * 1-5 /usr/bin/python3 /root/quantsync/resolve_outcomes.py >> /var/log/quantsync/resolve_outcomes.log 2>&1

What it does:
  For every stock_screener_rows row where:
    - pattern_stage IN ('NEAR_BREAKOUT', 'POST_BREAKOUT')
    - scan_date is >= 30 days ago (old enough to evaluate)
    - resolved_outcome IS NULL (not yet assessed)

  It looks up the stock's price history from StockHistory,
  finds what happened in the 30 days following the signal:
    - If price hit target_1 before stop_loss → SUCCESS
    - If price hit stop_loss before target_1 → FAILED
    - If neither in 30 days → INCONCLUSIVE

  Writes results back to resolved_outcome column.
"""

import sqlite3
import json
import os
import sys
import logging
from datetime import datetime, timedelta, timezone

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DB   = os.path.join(SCRIPT_DIR, "Data", "stock_dna_cache.db")
HIST_DB    = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")
LOG_DIR    = "/var/log/quantsync"
os.makedirs(LOG_DIR, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("resolve_outcomes")

# ── Config ─────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 30    # Days after signal to evaluate outcome
MIN_AGE_DAYS    = 30    # Signal must be at least this old to evaluate
BATCH_SIZE      = 200   # Commit every N rows to avoid long write locks


def get_price_history(sym: str) -> list[dict]:
    """
    Returns sorted list of {'date': 'YYYY-MM-DD', 'high': float, 'low': float, 'close': float}
    from StockHistory.rawCandles JSON for a given symbol.
    Returns [] if not found.
    """
    if not os.path.exists(HIST_DB):
        return []
    try:
        conn = sqlite3.connect(HIST_DB)
        cur = conn.cursor()
        cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (sym,))
        row = cur.fetchone()
        conn.close()
        if not row or not row[0]:
            return []
        raw = json.loads(row[0])
        bars = []
        for bar in raw:
            dt = str(bar.get("t", ""))[:10]
            if dt:
                bars.append({
                    "date":  dt,
                    "high":  float(bar.get("h", 0) or 0),
                    "low":   float(bar.get("l", 0) or 0),
                    "close": float(bar.get("c", 0) or 0),
                })
        bars.sort(key=lambda x: x["date"])
        return bars
    except Exception as e:
        log.warning(f"Could not load price history for {sym}: {e}")
        return []


def evaluate_outcome(
    signal_date: str,
    entry_price: float,
    target: float,
    stop_loss: float,
    bars: list[dict],
    lookback: int = 30
) -> str:
    """
    Walks daily bars from signal_date + 1 through signal_date + lookback days.
    Returns 'SUCCESS', 'FAILED', 'INCONCLUSIVE', or 'OPEN'.

    Rules:
    - If the HIGH of any bar >= target → SUCCESS
    - If the LOW of any bar <= stop_loss → FAILED
    - If both occur on the same day: the one with the better entry gap wins
    - If neither within lookback period and signal is >= lookback days old → INCONCLUSIVE
    - If neither within lookback period and signal is still fresh (< lookback days) → OPEN
    """
    if not target or not stop_loss or target <= entry_price or stop_loss >= entry_price:
        return "INCONCLUSIVE"

    try:
        signal_dt = datetime.strptime(signal_date[:10], "%Y-%m-%d")
    except Exception:
        return "INCONCLUSIVE"

    eval_start = signal_dt + timedelta(days=1)
    eval_end   = signal_dt + timedelta(days=lookback)

    for bar in bars:
        try:
            bar_dt = datetime.strptime(bar["date"][:10], "%Y-%m-%d")
        except Exception:
            continue
        if bar_dt < eval_start:
            continue
        if bar_dt > eval_end:
            break

        hit_target = bar["high"] >= target
        hit_stop   = bar["low"]  <= stop_loss

        if hit_target and hit_stop:
            target_gap = abs(target - entry_price)
            stop_gap   = abs(entry_price - stop_loss)
            return "SUCCESS" if target_gap <= stop_gap else "FAILED"
        elif hit_target:
            return "SUCCESS"
        elif hit_stop:
            return "FAILED"

    # If neither hit:
    if (datetime.now(timezone.utc).replace(tzinfo=None) - signal_dt).days >= lookback:
        return "INCONCLUSIVE"
    else:
        return "OPEN"


def add_resolved_outcome_column_if_missing(conn: sqlite3.Connection):
    """Adds resolved_outcome and resolved_date columns if they don't exist yet."""
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(stock_screener_rows)")
    cols = [r[1] for r in cur.fetchall()]
    if "resolved_outcome" not in cols:
        conn.execute("ALTER TABLE stock_screener_rows ADD COLUMN resolved_outcome TEXT")
        log.info("Added resolved_outcome column to stock_screener_rows")
    if "resolved_date" not in cols:
        conn.execute("ALTER TABLE stock_screener_rows ADD COLUMN resolved_date TEXT")
        log.info("Added resolved_date column to stock_screener_rows")
    conn.commit()


def run():
    if not os.path.exists(CACHE_DB):
        log.error(f"Cache DB not found: {CACHE_DB}")
        sys.exit(1)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    log.info("Starting outcome resolution on all unresolved breakout/near-breakout signals.")

    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row

    add_resolved_outcome_column_if_missing(conn)

    cur = conn.cursor()
    # Fetch all unresolved signals
    cur.execute("""
        SELECT id, symbol, scan_date, breakout_level, curr_price, target_1, target_2, stop_loss
        FROM stock_screener_rows
        WHERE pattern_stage IN ('NEAR_BREAKOUT', 'POST_BREAKOUT')
          AND resolved_outcome IS NULL
          AND breakout_level IS NOT NULL AND breakout_level > 0
          AND stop_loss IS NOT NULL AND stop_loss > 0
        ORDER BY scan_date ASC
    """)

    rows = cur.fetchall()
    log.info(f"Found {len(rows)} unresolved signals to evaluate.")

    if not rows:
        log.info("Nothing to resolve today.")
        conn.close()
        return

    from collections import defaultdict
    by_symbol = defaultdict(list)
    for row in rows:
        by_symbol[row["symbol"]].append(row)

    resolved = 0
    succeeded = 0
    failed = 0
    inconclusive = 0
    still_open = 0

    write_cur = conn.cursor()

    for sym, sym_rows in by_symbol.items():
        bars = get_price_history(sym)
        if not bars:
            continue

        for row in sym_rows:
            entry  = float(row["breakout_level"] or row["curr_price"] or 0)
            target = float(row["target_1"] or 0)
            stop   = float(row["stop_loss"] or 0)

            if not target or target <= entry:
                target = float(row["target_2"] or 0)

            outcome = evaluate_outcome(
                signal_date=row["scan_date"],
                entry_price=entry,
                target=target,
                stop_loss=stop,
                bars=bars,
                lookback=LOOKBACK_DAYS
            )

            if outcome == "OPEN":
                still_open += 1
                continue

            write_cur.execute("""
                UPDATE stock_screener_rows
                SET resolved_outcome = ?, resolved_date = ?
                WHERE id = ?
            """, (outcome, today_str, row["id"]))

            resolved += 1
            if outcome == "SUCCESS":
                succeeded += 1
            elif outcome == "FAILED":
                failed += 1
            else:
                inconclusive += 1

            if resolved % BATCH_SIZE == 0:
                conn.commit()
                log.info(f"  Progress: {resolved} resolved so far — {succeeded} SUCCESS / {failed} FAILED / {inconclusive} INCONCLUSIVE")

    conn.commit()
    conn.close()

    log.info("=" * 60)
    log.info(f"Outcome resolution complete.")
    log.info(f"  Total resolved  : {resolved}")
    log.info(f"  SUCCESS         : {succeeded}")
    log.info(f"  FAILED          : {failed}")
    log.info(f"  INCONCLUSIVE    : {inconclusive}")
    log.info(f"  STILL OPEN      : {still_open}")
    if (succeeded + failed) > 0:
        win_rate = round(succeeded / (succeeded + failed) * 100, 1)
        log.info(f"  Win Rate (excl. inconclusive): {win_rate}%")
    log.info("=" * 60)


if __name__ == "__main__":
    run()
