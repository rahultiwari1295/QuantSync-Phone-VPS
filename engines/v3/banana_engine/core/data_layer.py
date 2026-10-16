"""
banana_engine/core/data_layer.py — Point-in-Time Data Foundation
================================================================
Implements point-in-time adjusted OHLCV loading, corporate actions (splits, earnings),
sector history mapping, and universe membership as specified in Section 2 of BananaPatterns v4.
"""

import os
import sqlite3
import json
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd
import numpy as np

# Path configurations
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
_candidate_db = os.path.join(DATA_DIR, "stock_history.db")
_vps_db = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "..", "Data", "stock_history.db"))
_parent_db = os.path.abspath(os.path.join(BASE_DIR, "..", "data", "stock_history.db"))

if os.path.exists(_candidate_db):
    STOCK_HISTORY_DB = _candidate_db
elif os.path.exists(_vps_db):
    STOCK_HISTORY_DB = _vps_db
else:
    STOCK_HISTORY_DB = _parent_db
CORP_ACTIONS_DB = os.path.join(DATA_DIR, "corporate_actions.db")
SECTOR_MAP_DB = os.path.join(DATA_DIR, "sector_map.db")
UNIVERSE_MEMBERSHIP_DB = os.path.join(DATA_DIR, "universe_membership.db")


# FIXED: Fix 6.2 (INFO-02) — Centralized connection helper enabling WAL and NORMAL synchronous mode
def get_connection(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn


# Standard Sector / Industry taxonomy for NSE/BSE Universe
STANDARD_SECTORS: Dict[str, str] = {
    "RELIANCE": "Energy & Petrochemicals",
    "CHENNPETRO": "Energy & Petrochemicals",
    "ONGC": "Energy & Petrochemicals",
    "BPCL": "Energy & Petrochemicals",
    "IOC": "Energy & Petrochemicals",
    "ANTELOPUS": "Energy & Petrochemicals",
    "TCS": "Information Technology",
    "INFY": "Information Technology",
    "WIPRO": "Information Technology",
    "HCLTECH": "Information Technology",
    "TECHM": "Information Technology",
    "LTIM": "Information Technology",
    "HDFCBANK": "Financial Services",
    "ICICIBANK": "Financial Services",
    "SBIN": "Financial Services",
    "KOTAKBANK": "Financial Services",
    "AXISBANK": "Financial Services",
    "BAJFINANCE": "Financial Services",
    "BAJAJFINSV": "Financial Services",
    "SUNPHARMA": "Healthcare & Pharma",
    "CIPLA": "Healthcare & Pharma",
    "DRREDDY": "Healthcare & Pharma",
    "WOCKPHARMA": "Healthcare & Pharma",
    "DIVISLAB": "Healthcare & Pharma",
    "LUPIN": "Healthcare & Pharma",
    "IOLCP": "Healthcare & Pharma",
    "TATAMOTORS": "Automotive",
    "M&M": "Automotive",
    "MARUTI": "Automotive",
    "BAJAJ-AUTO": "Automotive",
    "HEROMOTOCO": "Automotive",
    "TVSMOTOR": "Automotive",
    "BHARTIARTL": "Telecommunications",
    "IDEA": "Telecommunications",
    "TRENT": "Consumer Goods",
    "TITAN": "Consumer Goods",
    "ITC": "Consumer Goods",
    "HINDUNILVR": "Consumer Goods",
    "NESTLEIND": "Consumer Goods",
    "DABUR": "Consumer Goods",
    "BRITANNIA": "Consumer Goods",
    "BEL": "Defence & Capital Goods",
    "HAL": "Defence & Capital Goods",
    "BDL": "Defence & Capital Goods",
    "LT": "Infrastructure & Capital Goods",
    "ADANIENT": "Conglomerates",
    "ADANIPORTS": "Infrastructure",
    "SYRMA": "Industrial Manufacturing",
    "RAYMOND": "Industrial Manufacturing",
    "POLYCAB": "Industrial Manufacturing",
    "KEI": "Industrial Manufacturing",
    "ZOMATO": "Consumer Tech",
    "NYKAA": "Consumer Tech",
    "POLICYBZR": "Consumer Tech",
}


def init_database_schemas() -> None:
    """
    Initializes corporate actions, sector history, and universe membership tables.
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    # 1. Corporate Actions DB
    with get_connection(CORP_ACTIONS_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS earnings (
                symbol TEXT NOT NULL,
                announce_ts TEXT NOT NULL,
                event_ts TEXT NOT NULL,
                PRIMARY KEY (symbol, announce_ts, event_ts)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS splits (
                symbol TEXT NOT NULL,
                ex_date TEXT NOT NULL,
                ratio REAL NOT NULL,
                PRIMARY KEY (symbol, ex_date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dividends (
                symbol TEXT NOT NULL,
                ex_date TEXT NOT NULL,
                amount REAL NOT NULL,
                PRIMARY KEY (symbol, ex_date)
            )
        """)
        conn.commit()

    # 2. Sector Map DB
    with get_connection(SECTOR_MAP_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sector_history (
                symbol TEXT NOT NULL,
                sector TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                PRIMARY KEY (symbol, valid_from)
            )
        """)
        conn.commit()

    # 3. Universe Membership DB
    with get_connection(UNIVERSE_MEMBERSHIP_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS membership (
                symbol TEXT NOT NULL,
                listing_date TEXT NOT NULL,
                delisting_date TEXT,
                PRIMARY KEY (symbol, listing_date)
            )
        """)
        conn.commit()


def seed_metadata_if_empty() -> None:
    """
    Seeds initial sector and membership records from stock_history.db if empty.
    """
    init_database_schemas()

    # Check if sector_map already populated
    with get_connection(SECTOR_MAP_DB) as s_conn:
        cur = s_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sector_history")
        count = cur.fetchone()[0]

    if count == 0 and os.path.exists(STOCK_HISTORY_DB):
        with get_connection(STOCK_HISTORY_DB) as st_conn:
            cur = st_conn.cursor()
            cur.execute("SELECT symbol, rawCandles FROM StockHistory")
            rows = cur.fetchall()

        with get_connection(SECTOR_MAP_DB) as s_conn, get_connection(UNIVERSE_MEMBERSHIP_DB) as m_conn:
            for sym, raw in rows:
                if sym.startswith("NIFTY") or sym.startswith("BANKNIFTY") or sym.startswith("SENSEX"):
                    continue

                # Sector assignment
                sec = STANDARD_SECTORS.get(sym)
                if not sec:
                    s_u = sym.upper()
                    if any(k in s_u for k in ["BANK", "FIN", "CAP", "HOLD", "SEC"]):
                        sec = "Financial Services"
                    elif any(k in s_u for k in ["TECH", "SOFT", "INFO", "DIGI"]):
                        sec = "Information Technology"
                    elif any(k in s_u for k in ["PHARM", "LAB", "BIO", "HEALTH"]):
                        sec = "Healthcare & Pharma"
                    elif any(k in s_u for k in ["ENG", "IND", "STEEL", "POWER", "ELECT"]):
                        sec = "Industrial Manufacturing"
                    elif any(k in s_u for k in ["AUTO", "MOT", "TYRE", "GEAR"]):
                        sec = "Automotive"
                    else:
                        sec = "General Industrials"

                s_conn.execute(
                    "INSERT OR IGNORE INTO sector_history (symbol, sector, valid_from, valid_to) VALUES (?, ?, ?, ?)",
                    (sym, sec, "2015-01-01", None)
                )

                # Membership / Listing date determination from earliest candle
                try:
                    candles = json.loads(raw)
                    first_dt = str(candles[0]['t'])[:10] if candles else "2015-01-01"
                except Exception:
                    first_dt = "2015-01-01"

                m_conn.execute(
                    "INSERT OR IGNORE INTO membership (symbol, listing_date, delisting_date) VALUES (?, ?, ?)",
                    (sym, first_dt, None)
                )

            s_conn.commit()
            m_conn.commit()


# Initialize upon module load
init_database_schemas()
seed_metadata_if_empty()


def cumulative_adjustment_factor(symbol: str, as_of: Optional[str] = None) -> float:
    """
    Computes split/corporate action cumulative multiplier up to as_of date.
    Returns 1.0 if no corporate splits occurred after as_of date.
    """
    if not os.path.exists(CORP_ACTIONS_DB):
        return 1.0

    with get_connection(CORP_ACTIONS_DB) as conn:
        cur = conn.cursor()
        if as_of:
            cur.execute(
                "SELECT ratio FROM splits WHERE symbol = ? AND ex_date > ? ORDER BY ex_date ASC",
                (symbol, as_of)
            )
        else:
            return 1.0
        rows = cur.fetchall()

    factor = 1.0
    for (ratio,) in rows:
        if ratio > 0:
            factor *= float(ratio)
    return factor


def read_ohlcv_raw(symbol: str, end_date: Optional[str] = None) -> pd.DataFrame:
    """
    Reads unadjusted raw OHLCV records from stock_history.db up to end_date.
    """
    sym = symbol.upper().strip()
    if not os.path.exists(STOCK_HISTORY_DB):
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    with get_connection(STOCK_HISTORY_DB) as conn:
        cur = conn.cursor()
        cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (sym,))
        row = cur.fetchone()

    if not row or not row[0]:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    candles = json.loads(row[0])
    df = pd.DataFrame(candles).rename(columns={'t': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'})
    df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)

    if end_date:
        df = df[df['date'] <= str(end_date)].copy()

    return df.sort_values('date').reset_index(drop=True)


# FIXED: Fix 1.4 (INFO-01) — Reject empty or null symbol
def load_adjusted(symbol: str, end_date: Optional[str] = None) -> pd.DataFrame:
    """
    Returns OHLCV bars, all adjusted to the price basis in effect at end_date (defaults to today).
    A given (symbol, end_date) pair always returns the same frame — reproducibility is guaranteed.
    """
    if not symbol or symbol == '':
        raise ValueError("empty symbol")

    raw = read_ohlcv_raw(symbol, end_date=end_date)
    if raw.empty:
        return raw

    factor = cumulative_adjustment_factor(symbol, as_of=end_date)
    if factor != 1.0:
        for col in ("open", "high", "low", "close"):
            raw[col] = raw[col] * factor
        raw["volume"] = raw["volume"] / factor

    raw['symbol'] = symbol.upper().strip()
    return raw


def days_to_next_earnings(symbol: str, as_of: str) -> int:
    """
    Point-in-time lookup of days until next announced earnings event.
    Returns 999 if unknown.
    """
    if not os.path.exists(CORP_ACTIONS_DB):
        return 999

    as_of_str = str(as_of)[:10]
    with get_connection(CORP_ACTIONS_DB) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT event_ts FROM earnings WHERE symbol = ? AND announce_ts <= ? AND event_ts >= ? "
            "ORDER BY event_ts ASC LIMIT 1",
            (symbol, as_of_str, as_of_str)
        )
        row = cur.fetchone()

    if not row:
        return 999

    event_date = pd.to_datetime(row[0]).date()
    curr_date = pd.to_datetime(as_of_str).date()
    return max(0, (event_date - curr_date).days)


def sector_at(symbol: str, as_of: str) -> str:
    """
    Point-in-time lookup of stock sector classification as of query date.
    """
    as_of_str = str(as_of)[:10]
    with get_connection(SECTOR_MAP_DB) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT sector FROM sector_history WHERE symbol = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
            "ORDER BY valid_from DESC LIMIT 1",
            (symbol, as_of_str, as_of_str)
        )
        row = cur.fetchone()

    if row:
        return str(row[0])
    return STANDARD_SECTORS.get(symbol, "General Industrials")


def listing_date_at(symbol: str, as_of: Optional[str] = None) -> date:
    """
    Point-in-time lookup of listing date for symbol.
    """
    with get_connection(UNIVERSE_MEMBERSHIP_DB) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT listing_date FROM membership WHERE symbol = ? ORDER BY listing_date ASC LIMIT 1",
            (symbol,)
        )
        row = cur.fetchone()

    if row and row[0]:
        return pd.to_datetime(row[0]).date()
    return date(2015, 1, 1)
