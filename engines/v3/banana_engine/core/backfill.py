"""
banana_engine/core/backfill.py — Historical Backfill & Labeling Engine
======================================================================
Generates labeled training and validation datasets across historical weekly
checkpoints using conservative tie-breaking as specified in Section 5 of BananaPatterns v4.
"""

import os
import sys
import json
import sqlite3
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from collections import Counter
from core.data_layer import load_adjusted, STOCK_HISTORY_DB, DATA_DIR
from core.features import FEATURES, extract_features
from core.structure import detect_base_at


def label_event(
    bars: pd.DataFrame,
    T: int,
    entry_price: float,
    target_pct: float = 10.0,
    stop_pct: float = 7.0,
    horizon: int = 20
) -> int:
    """
    Returns 0 or 1.
      • 1 if target is hit strictly before stop within horizon bars.
      • 0 otherwise (stop first, neither, or ambiguous same-bar — conservative).
    """
    target_price = entry_price * (1.0 + target_pct / 100.0)
    stop_price   = entry_price * (1.0 - stop_pct / 100.0)
    future = bars.iloc[T + 1: T + 1 + horizon]

    if future.empty:
        return 0

    hit_target_idx = None
    hit_stop_idx   = None

    for j, row in enumerate(future.itertuples()):
        if hit_target_idx is None and row.high >= target_price:
            hit_target_idx = j
        if hit_stop_idx is None and row.low <= stop_price:
            hit_stop_idx = j
        if hit_target_idx is not None and hit_stop_idx is not None:
            break

    if hit_target_idx is None and hit_stop_idx is None:
        # Neither triggered: use final close vs entry as tiebreak
        return int(future['close'].iloc[-1] > entry_price)
    if hit_stop_idx is None:
        return 1
    if hit_target_idx is None:
        return 0
    if hit_target_idx < hit_stop_idx:
        return 1
    if hit_stop_idx < hit_target_idx:
        return 0
    return 0  # Same bar: stop wins (conservative)


# DEPRECATED: Weekly sampling missed 80% of daily breakout candidates (Fix 0.1/0.2)
def weekly_indices(bars: pd.DataFrame, start_date: str, end_date: str) -> List[int]:
    """
    Returns indices corresponding to approximately weekly interval checkpoints (every 5 bars)
    within the date range.
    """
    idxs = []
    for i in range(len(bars)):
        d = str(bars['date'].iloc[i])
        if start_date <= d <= end_date and i % 5 == 0:
            idxs.append(i)
    return idxs


def daily_indices(bars: pd.DataFrame, start_date: str, end_date: str) -> List[int]:
    """
    Returns indices corresponding to every trading day within the date range.
    """
    idxs = []
    for i in range(len(bars)):
        d = str(bars['date'].iloc[i])
        if start_date <= d <= end_date:
            idxs.append(i)
    return idxs


# FIXED: Fix 1.1 (BLOCKER-01) — Point-in-time Relative Strength calculation
def compute_rs_pit(bars: pd.DataFrame, as_of_idx: int) -> Optional[float]:
    """Front-weighted ROC percentile rank as of as_of_idx."""
    if as_of_idx < 250:
        return None
    c_now = bars['close'].iloc[as_of_idx]
    c_3m = bars['close'].iloc[as_of_idx - 63]
    c_6m = bars['close'].iloc[as_of_idx - 125]
    c_1y = bars['close'].iloc[as_of_idx - 250]
    if c_3m <= 0 or c_6m <= 0 or c_1y <= 0:
        return None
    return (2.0 * (c_now / c_3m - 1.0)
            + 1.0 * (c_now / c_6m - 1.0)
            + 1.0 * (c_now / c_1y - 1.0))


# FIXED: Fix 1.1 (BLOCKER-01) — Cross-sectional PIT RS precomputation across market
def precompute_rs_ratings(
    target_symbols: List[str],
    start_date: str,
    end_date: str
) -> Dict[str, Dict[str, float]]:
    """
    Precomputes point-in-time rolling RS for every symbol across the full date range.
    Ranks cross-sectionally per date across market universe, converted to 1-99 percentile.
    Returns {symbol: {date: rs_value}}.
    """
    with sqlite3.connect(STOCK_HISTORY_DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT symbol, rawCandles FROM StockHistory WHERE symbol NOT LIKE 'NIFTY%' AND symbol NOT LIKE 'BANKNIFTY%' AND symbol != ''")
        market_rows = cur.fetchall()

    raw_by_date: Dict[str, Dict[str, float]] = {}
    for sym, raw in market_rows:
        try:
            candles = json.loads(raw)
        except Exception:
            continue
        if len(candles) < 250:
            continue
        closes = np.array([c['c'] for c in candles], dtype=float)
        dates = [c['t'][:10] for c in candles]
        n = len(closes)
        for i in range(250, n):
            d = dates[i]
            if d < start_date:
                continue
            if d > end_date:
                break
            c_now = closes[i]
            c_3m = closes[i - 63]
            c_6m = closes[i - 125]
            c_1y = closes[i - 250]
            if c_3m <= 0 or c_6m <= 0 or c_1y <= 0:
                continue
            val = (2.0 * (c_now / c_3m - 1.0)
                   + 1.0 * (c_now / c_6m - 1.0)
                   + 1.0 * (c_now / c_1y - 1.0))
            if d not in raw_by_date:
                raw_by_date[d] = {}
            raw_by_date[d][sym] = val

    target_set = set(target_symbols)
    rs_pit: Dict[str, Dict[str, float]] = {s: {} for s in target_symbols}
    for d, sym_scores in raw_by_date.items():
        if not sym_scores:
            continue
        series = pd.Series(sym_scores)
        if len(series) <= 1:
            ranks = pd.Series([50.0], index=series.index)
        else:
            ranks = (series.rank(method='average') - 1.0) / (len(series) - 1.0) * 98.0 + 1.0
        for sym, rank_val in ranks.items():
            if sym in target_set:
                rs_pit[sym][d] = round(float(rank_val), 1)

    return rs_pit


# FIXED: Fix 0.2 & Fix 0.3 — Daily universe iteration, drop reasons logging, and symbols parameter
# FIXED: Fix 1.1 (BLOCKER-01) — Point-in-time RS precomputation passed to extract_features
def backfill_universe(
    symbols: Optional[List[str]] = None,
    max_symbols: Optional[int] = None,
    start_date: Optional[str] = "2015-01-01",
    end_date: Optional[str] = "2026-09-01",
    start: Optional[str] = None,
    end: Optional[str] = None,
    output_path: Optional[str] = None
) -> pd.DataFrame:
    """
    Backfills feature-label dataset across universe.
    """
    if start is not None:
        start_date = start
    if end is not None:
        end_date = end
    if start_date is None:
        start_date = "2015-01-01"
    if end_date is None:
        end_date = "2026-09-01"

    if output_path is None:
        output_path = os.path.join(DATA_DIR, "backfill_dataset.parquet")

    # Load symbol list
    if symbols is not None:
        universe = list(symbols)
    else:
        with sqlite3.connect(STOCK_HISTORY_DB) as conn:
            cur = conn.cursor()
            cur.execute("SELECT symbol FROM StockHistory")
            rows = cur.fetchall()
        universe = [r[0] for r in rows if r[0] and not r[0].startswith("NIFTY") and not r[0].startswith("BANKNIFTY") and not r[0].startswith("SENSEX")]

    if max_symbols is not None:
        universe = universe[:max_symbols]

    print(f"{len(universe)} symbols")
    print(f"[Backfill] Precomputing market-wide PIT RS ratings...")
    rs_pit = precompute_rs_ratings(universe, start_date, end_date)
    print(f"[Backfill] Running point-in-time extraction on {len(universe)} symbols from {start_date} to {end_date}...")

    drop_reasons: Counter = Counter()
    all_rows: List[Dict[str, Any]] = []

    for idx, sym in enumerate(universe):
        if idx % 25 == 0 and idx > 0:
            print(f"  Processed {idx}/{len(universe)} symbols ({len(all_rows)} valid setups found)...")

        bars = load_adjusted(sym)
        if len(bars) < 250:
            continue

        checkpoints = daily_indices(bars, start_date, end_date)
        for T in checkpoints:
            if T < 150:
                continue
            cur_date = str(bars['date'].iloc[T])

            # Drop reasons diagnostic tracking
            base = detect_base_at(bars, T)
            if base is None:
                drop_reasons["no_base"] += 1
                continue
            if base.duration_weeks < 3.0:
                drop_reasons["duration_too_short"] += 1
                continue
            if base.depth_pct < 5.0 or base.depth_pct > 40.0:
                drop_reasons["depth_out_of_range"] += 1
                continue

            rs_val = rs_pit.get(sym, {}).get(cur_date)
            if rs_val is None:
                drop_reasons["no_features"] += 1
                continue

            feats = extract_features(symbol=sym, as_of=cur_date, bars_all=bars, rs_rating=rs_val)
            if feats is None:
                drop_reasons["no_features"] += 1
                continue

            entry_p = float(bars['close'].iloc[T])
            lbl = label_event(bars, T, entry_price=entry_p)
            if lbl is None:
                drop_reasons["no_label"] += 1
                continue

            meta = feats.pop("_meta", {})
            feats["label"] = lbl
            feats["symbol"] = sym
            feats["date"] = cur_date
            feats["entry"] = entry_p
            feats["pivot"] = meta.get("pivot", entry_p)
            feats["floor"] = meta.get("floor", entry_p * 0.9)
            all_rows.append(feats)

    print(f"[Backfill] Drop reasons breakdown: {dict(drop_reasons)}")
    df_out = pd.DataFrame(all_rows)
    print(f"[Backfill] Completed: {len(df_out)} labeled samples extracted.")

    if not df_out.empty:
        df_out.sort_values("date").reset_index(drop=True).to_parquet(output_path, index=False)
        print(f"[Backfill] Saved dataset to {output_path}")

    return df_out


if __name__ == "__main__":
    backfill_universe(max_symbols=10)
