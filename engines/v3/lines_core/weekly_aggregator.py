"""
core/weekly_aggregator.py — Causal Weekly Candle Aggregation & Data Loader
==========================================================================
Converts daily OHLCV session candles into point-in-time weekly candles.
Preserves strict calendar week causality:
  - Week grouping based on ISO calendar (Monday-Sunday / trading week ending Friday)
  - Open: Open of first session in the week
  - High: Max high of all sessions in the week
  - Low:  Min low of all sessions in the week
  - Close: Close of final session in the week
  - Volume: Sum of volume across all sessions in the week
  - Date: Date of final session in the week (e.g. Friday)
"""

import os
import json
import sqlite3
import logging
from typing import Dict, List, Any, Optional, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger("QuantSync.Lines.WeeklyAggregator")


def aggregate_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregates a daily OHLCV DataFrame into weekly OHLCV bars.
    Required columns: ['date', 'open', 'high', 'low', 'close', 'volume']
    Returns DataFrame with columns:
      ['date', 'open', 'high', 'low', 'close', 'volume', 'bar_count']
    sorted strictly ascending by date.
    """
    if daily_df is None or daily_df.empty:
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume', 'bar_count'])

    df = daily_df.copy()
    if 't' in df.columns and 'date' not in df.columns:
        df.rename(columns={'t': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)

    df['date_dt'] = pd.to_datetime(df['date'])
    df.sort_values('date_dt', inplace=True)

    iso_cal = df['date_dt'].dt.isocalendar()
    df['iso_year'] = iso_cal.year
    df['iso_week'] = iso_cal.week

    weekly_df = (
        df.groupby(['iso_year', 'iso_week'], as_index=False, sort=False)
        .agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
            'date': 'last',
            'date_dt': 'count'
        })
        .rename(columns={'date_dt': 'bar_count'})
    )

    cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'bar_count']
    weekly_df = weekly_df[cols].sort_values('date').reset_index(drop=True)
    return weekly_df


def load_universe_weekly(
    db_path: str,
    symbols: Optional[List[str]] = None,
    is_v3_eligible_only: bool = False
) -> Dict[str, pd.DataFrame]:
    """
    Loads raw candles from SQLite StockHistory table and aggregates each stock into weekly bars.
    Returns: Dict[symbol, weekly_df]
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found at: {db_path}")

    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    cursor = conn.cursor()

    query = "SELECT symbol, rawCandles FROM StockHistory"
    conditions = []
    params = []

    if is_v3_eligible_only:
        conditions.append("(isAlgoEligible = 1 OR isV3Eligible = 1 OR symbol IN ('NIFTY', 'NIFTY100', 'NIFTY500'))")
    if symbols:
        placeholders = ",".join("?" for _ in symbols)
        conditions.append(f"symbol IN ({placeholders})")
        params.extend(symbols)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    universe_weekly = {}
    for sym, raw_json in rows:
        if not raw_json:
            continue
        try:
            raw_candles = json.loads(raw_json)
            if not raw_candles or len(raw_candles) < 20:
                continue
            daily_df = pd.DataFrame(raw_candles)
            daily_df.rename(columns={'t': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
            for c in ['open', 'high', 'low', 'close', 'volume']:
                daily_df[c] = pd.to_numeric(daily_df[c], errors='coerce')
            daily_df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
            daily_df = daily_df[(daily_df['open'] > 0) & (daily_df['high'] > 0) & (daily_df['low'] > 0) & (daily_df['close'] > 0)]

            w_df = aggregate_to_weekly(daily_df)
            if len(w_df) >= 10:
                universe_weekly[sym] = w_df
        except Exception as e:
            logger.warning(f"Error parsing symbol {sym}: {e}")

    logger.info(f"Loaded {len(universe_weekly)} symbols with weekly candles.")
    return universe_weekly
