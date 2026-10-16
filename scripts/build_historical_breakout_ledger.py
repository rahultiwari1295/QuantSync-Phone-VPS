#!/usr/bin/env python3
"""
build_historical_breakout_ledger.py (v2 — Institutional ML Feature Store Edition)
=================================================================================
Realistic Walk-Forward Breakout Auditor & Machine Learning Dataset Generator (2020-2026).

Zero-Lookahead Guarantee:
- At any bar index t, only candles strictly up to bar t are passed into classical pattern detection.
- All technical features (RSI, volume spike, 30-day returns, EMA distances) are computed strictly at bar t.
- Forward evaluation begins at bar t+1 and strictly enforces:
  1. Maximum Time-Stop Horizon (Daily: 25 bars, Weekly: 16 bars, Monthly: 9 bars)
  2. Structural Pivot Stop Loss (Exit immediately if daily close < pivot * 0.96)
  3. Slippage & Transaction Costs (0.25% round-trip deducted from gross returns)
  4. Real-World Outcomes: TARGET_HIT, STOP_HIT, TIME_STOP_EXIT, or ACTIVE.

Populates:
- `historical_breakout_records` (for UI year-by-year and pattern statistics)
- `ml_breakout_dataset` (42-column tabular dataset ready for XGBoost / LightGBM)
"""

import os
import sys
import json
import time
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [QuantML_Auditor] %(message)s"
)
logger = logging.getLogger("QuantML_Auditor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from classical_pattern_dna import ClassicalPatternAnalystDNA, resample_to_weekly, resample_to_monthly
except ImportError:
    sys.path.insert(0, "/root/quantsync")
    from classical_pattern_dna import ClassicalPatternAnalystDNA, resample_to_weekly, resample_to_monthly


# ─────────────────────────────────────────────────────────────────────────────
# Technical Feature Extractor (At Bar t strictly - Zero Lookahead)
# ─────────────────────────────────────────────────────────────────────────────
def compute_rsi(series: pd.Series, period: int = 14) -> float:
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    last_gain = avg_gain.iloc[-1]
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0 or np.isnan(last_loss):
        return 100.0 if (last_gain and last_gain > 0) else 50.0
    rs = last_gain / last_loss
    return round(float(100.0 - (100.0 / (1.0 + rs))), 2)


def extract_features_at_entry(df_hist: pd.DataFrame, pivot_price: float, pattern_type: str, tf: str) -> Dict[str, Any]:
    """
    Extracts 30-day pre-breakout momentum, volume spike, volatility contraction,
    and moving average features strictly using bars available at entry time t.
    """
    n = len(df_hist)
    closes = df_hist['Close'].values
    highs = df_hist['High'].values
    lows = df_hist['Low'].values
    volumes = df_hist['Volume'].values if 'Volume' in df_hist.columns else np.zeros(n)

    curr_close = float(closes[-1])
    
    # 1. Pre-breakout returns over 5, 10, 20, 30 bars
    ret_5d = round(((curr_close - closes[-5]) / closes[-5]) * 100.0, 2) if n >= 5 else 0.0
    ret_10d = round(((curr_close - closes[-10]) / closes[-10]) * 100.0, 2) if n >= 10 else 0.0
    ret_20d = round(((curr_close - closes[-20]) / closes[-20]) * 100.0, 2) if n >= 20 else 0.0
    ret_30d = round(((curr_close - closes[-30]) / closes[-30]) * 100.0, 2) if n >= 30 else 0.0

    # 2. Pre-breakout 30-day close series normalized to pivot (for neural sequence input)
    past_30 = closes[-30:] if n >= 30 else closes
    norm_30_series = [round(float(c / pivot_price), 4) for c in past_30]

    # 3. Volatility & VCP Tightness (High-Low spread of last 5 bars vs last 20 bars)
    spread_5d = (np.max(highs[-5:]) - np.min(lows[-5:])) / pivot_price if n >= 5 else 0.05
    spread_20d = (np.max(highs[-20:]) - np.min(lows[-20:])) / pivot_price if n >= 20 else 0.15
    vcp_tightness_ratio = round(float(spread_5d / spread_20d), 3) if spread_20d > 0 else 1.0

    # 4. ATR-14 % of Price
    tr_series = []
    for i in range(max(1, n - 15), n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_series.append(tr)
    atr_val = np.mean(tr_series) if tr_series else (curr_close * 0.02)
    atr_pct = round(float((atr_val / curr_close) * 100.0), 2)

    # 5. Volume Spike on Breakout Bar vs 20-bar Average
    vol_breakout = float(volumes[-1])
    avg_vol_20 = float(np.mean(volumes[-21:-1])) if n >= 21 else (vol_breakout or 1.0)
    vol_spike_ratio = round(float(vol_breakout / avg_vol_20), 2) if avg_vol_20 > 0 else 1.0

    # 6. Moving Average Alignments (20, 50, 200)
    ema20 = float(pd.Series(closes).ewm(span=20).mean().iloc[-1]) if n >= 20 else curr_close
    ema50 = float(pd.Series(closes).ewm(span=50).mean().iloc[-1]) if n >= 50 else curr_close
    ema200 = float(pd.Series(closes).ewm(span=200).mean().iloc[-1]) if n >= 200 else curr_close

    price_vs_ema20 = round(((curr_close - ema20) / ema20) * 100.0, 2)
    price_vs_ema50 = round(((curr_close - ema50) / ema50) * 100.0, 2)
    price_vs_ema200 = round(((curr_close - ema200) / ema200) * 100.0, 2)
    ema_bullish_stacked = int(ema20 > ema50 and ema50 > ema200)

    # 7. RSI at Entry
    rsi_14 = compute_rsi(pd.Series(closes), period=14)

    return {
        "ret_5d": ret_5d,
        "ret_10d": ret_10d,
        "ret_20d": ret_20d,
        "ret_30d": ret_30d,
        "norm_30_series_json": json.dumps(norm_30_series),
        "vcp_tightness_ratio": vcp_tightness_ratio,
        "atr_pct": atr_pct,
        "vol_spike_ratio": vol_spike_ratio,
        "price_vs_ema20": price_vs_ema20,
        "price_vs_ema50": price_vs_ema50,
        "price_vs_ema200": price_vs_ema200,
        "ema_bullish_stacked": ema_bullish_stacked,
        "rsi_14": rsi_14
    }


# ─────────────────────────────────────────────────────────────────────────────
# Forward Evaluator with Realistic Time-Stops & Round-Trip Friction
# ─────────────────────────────────────────────────────────────────────────────
ROUND_TRIP_COST_PCT = 0.25  # 0.15% slippage + 0.10% STT/brokerage/exchange fees

# Strict maximum holding horizons
MAX_HORIZON_BARS = {
    "DAILY": 25,    # 25 trading days (~5 weeks)
    "WEEKLY": 16,   # 16 weeks (~4 months)
    "MONTHLY": 9    # 9 months
}


def evaluate_forward_realistic(
    df: pd.DataFrame,
    breakout_idx: int,
    pivot_price: float,
    target_price: float,
    raw_stop_loss: float,
    timeframe: str,
    entry_price: Optional[float] = None
) -> Dict[str, Any]:
    """
    Evaluates forward trajectory with strict time stops, realistic stop-loss clamping,
    and deducted transaction fees.
    """
    total_bars = len(df)
    max_bars = MAX_HORIZON_BARS.get(timeframe.upper(), 25)
    
    # Structural stop loss: cannot be deeper than 4% below pivot for Daily,
    # 7% for Weekly, and 12% for Monthly. A true breakout that falls below this floor is dead.
    max_risk_pct = 4.0 if timeframe == "DAILY" else (7.0 if timeframe == "WEEKLY" else 12.0)
    # P&L is measured from the price you could actually buy at (next bar open), not the pivot.
    entry = float(entry_price) if entry_price else pivot_price
    effective_stop_loss = max(raw_stop_loss, entry * (1.0 - max_risk_pct / 100.0))
    pivot_price = entry  # every return below is relative to the real entry

    if breakout_idx >= total_bars:
        return {
            "outcome_status": "ACTIVE",
            "is_win": 0,
            "max_gain_pct": 0.0,
            "realized_pnl_pct": 0.0,
            "exit_date": str(df.iloc[-1]['Date'])[:10],
            "holding_bars": 1,
            "exit_idx": total_bars - 1
        }

    max_high = pivot_price
    outcome = "ACTIVE"
    realized_pnl = 0.0
    exit_idx = min(breakout_idx + max_bars, total_bars - 1)
    exit_date = str(df.iloc[exit_idx]['Date'])[:10]

    end_idx = min(breakout_idx + max_bars, total_bars)

    for j in range(breakout_idx, end_idx):
        bar = df.iloc[j]
        h = float(bar['High'])
        l = float(bar['Low'])
        o = float(bar['Open'])
        c = float(bar['Close'])
        d_str = str(bar['Date'])[:10]

        if h > max_high:
            max_high = h

        # 1. Gap Check at Open
        if o <= effective_stop_loss:
            outcome = "STOP_HIT"
            realized_pnl = ((o - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = j
            exit_date = d_str
            break
        elif o >= target_price:
            outcome = "TARGET_HIT"
            realized_pnl = ((o - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = j
            exit_date = d_str
            break

        # 2. Intraday Extreme Check
        if l <= effective_stop_loss and h >= target_price:
            if c < o:
                outcome = "STOP_HIT"
                realized_pnl = ((effective_stop_loss - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            else:
                outcome = "TARGET_HIT"
                realized_pnl = ((target_price - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = j
            exit_date = d_str
            break
        elif l <= effective_stop_loss:
            outcome = "STOP_HIT"
            realized_pnl = ((effective_stop_loss - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = j
            exit_date = d_str
            break
        elif h >= target_price:
            outcome = "TARGET_HIT"
            realized_pnl = ((target_price - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = j
            exit_date = d_str
            break

    # 3. Time-Stop Horizon Reached (Position timed out before target or stop)
    if outcome == "ACTIVE":
        if end_idx < total_bars:
            # Reached max bars, closed at market close of the last allowed bar
            timeout_bar = df.iloc[end_idx - 1]
            timeout_close = float(timeout_bar['Close'])
            outcome = "TIME_STOP_EXIT"
            realized_pnl = ((timeout_close - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = end_idx - 1
            exit_date = str(timeout_bar['Date'])[:10]
        else:
            # Still open today (active ongoing trade)
            latest_close = float(df.iloc[-1]['Close'])
            realized_pnl = ((latest_close - pivot_price) / pivot_price) * 100.0 - ROUND_TRIP_COST_PCT
            exit_idx = total_bars - 1
            exit_date = str(df.iloc[-1]['Date'])[:10]

    max_gain_pct = ((max_high - pivot_price) / pivot_price) * 100.0
    holding_bars = exit_idx - breakout_idx + 1

    # A win is strictly a TARGET_HIT, or a TIME_STOP_EXIT that closed with positive net return
    is_win = 1 if (outcome == "TARGET_HIT" or (outcome == "TIME_STOP_EXIT" and realized_pnl > 0)) else 0

    return {
        "outcome_status": outcome,
        "is_win": is_win,
        "max_gain_pct": round(max_gain_pct, 2),
        "realized_pnl_pct": round(realized_pnl, 2),
        "exit_date": exit_date,
        "holding_bars": holding_bars,
        "exit_idx": exit_idx
    }


# ─────────────────────────────────────────────────────────────────────────────
# Database Initialization
# ─────────────────────────────────────────────────────────────────────────────
def init_db(master_db_path: str):
    conn = sqlite3.connect(master_db_path, timeout=30.0)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")

    # Clean existing table for clean rebuild
    cur.execute("DROP TABLE IF EXISTS historical_breakout_records")
    cur.execute("DROP TABLE IF EXISTS ml_breakout_dataset")

    cur.execute("""
        CREATE TABLE historical_breakout_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            pattern_name TEXT NOT NULL,
            breakout_date TEXT NOT NULL,
            breakout_year INTEGER NOT NULL,
            pivot_price REAL NOT NULL,
            entry_price REAL,
            target_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            outcome_status TEXT NOT NULL,
            is_win INTEGER NOT NULL,
            max_gain_pct REAL NOT NULL,
            realized_pnl_pct REAL NOT NULL,
            exit_date TEXT,
            holding_bars INTEGER,
            fair_hit10 INTEGER,
            ret_20d REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, timeframe, pattern_type, breakout_date)
        )
    """)

    cur.execute("""
        CREATE TABLE ml_breakout_dataset (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            breakout_date TEXT NOT NULL,
            breakout_year INTEGER NOT NULL,
            pivot_price REAL NOT NULL,
            ret_5d REAL,
            ret_10d REAL,
            ret_20d REAL,
            ret_30d REAL,
            vcp_tightness_ratio REAL,
            atr_pct REAL,
            vol_spike_ratio REAL,
            price_vs_ema20 REAL,
            price_vs_ema50 REAL,
            price_vs_ema200 REAL,
            ema_bullish_stacked INTEGER,
            rsi_14 REAL,
            norm_30_series_json TEXT,
            outcome_status TEXT NOT NULL,
            is_win INTEGER NOT NULL,
            realized_pnl_pct REAL NOT NULL,
            holding_bars INTEGER NOT NULL
        )
    """)

    cur.execute("CREATE INDEX idx_hbr_year ON historical_breakout_records(breakout_year)")
    cur.execute("CREATE INDEX idx_hbr_tf_year ON historical_breakout_records(timeframe, breakout_year)")
    cur.execute("CREATE INDEX idx_hbr_tf_pat ON historical_breakout_records(timeframe, pattern_type)")
    cur.execute("CREATE INDEX idx_hbr_sym ON historical_breakout_records(symbol)")
    cur.execute("CREATE INDEX idx_ml_win ON ml_breakout_dataset(is_win)")
    conn.commit()
    conn.close()
    logger.info("Initialized realistic schema and ML dataset tables successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# Universe Scanning
# ─────────────────────────────────────────────────────────────────────────────
def scan_stock_history_realistic(symbol: str, raw_json: str, analyst: ClassicalPatternAnalystDNA) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        raw_list = json.loads(raw_json)
    except Exception:
        return [], []

    if len(raw_list) < 150:
        return [], []

    df = pd.DataFrame(raw_list)
    df['Date'] = pd.to_datetime(df['t'])
    df = df.rename(columns={'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
    df = df.sort_values('Date').reset_index(drop=True)

    records = []
    ml_records = []
    last_exit_by_tf = {"DAILY": 0, "WEEKLY": 0, "MONTHLY": 0}
    seen: List[Tuple[str, str, float, int]] = []   # (tf, pattern, pivot, idx) — one record per pattern instance

    step = 5
    total_bars = len(df)
    highs = df['High'].to_numpy(float); lows = df['Low'].to_numpy(float); closes = df['Close'].to_numpy(float)

    for idx in range(120, total_bars - 1, step):
        sub_df = df.iloc[:idx]                 # bars 0..idx-1 are known at the decision
        current_date = sub_df['Date'].iloc[-1]
        if current_date.year < 2020:
            continue

        for tf in ['DAILY', 'WEEKLY', 'MONTHLY']:
            if idx <= last_exit_by_tf[tf]:
                continue
            if tf == 'MONTHLY' and len(sub_df) < 300:
                continue
            if tf == 'WEEKLY' and len(sub_df) < 150:
                continue
            try:
                res = analyst.analyze(sub_df, timeframe=tf)
            except Exception:
                continue
            if not res or res.stance != 'BULLISH' or res.lifecycle_state != 'CONFIRMED':
                continue
            if not res.breakout_level or res.breakout_level <= 0:
                continue

            pvt = float(res.breakout_level)
            entry = float(df['Open'].iloc[idx])                       # next session open
            # a breakout trade only exists while price is near the pivot it broke
            if not (pvt * 0.98 <= entry <= pvt * 1.06):
                continue
            tgt = float(res.target_price) if res.target_price else pvt * (1.15 if tf == 'DAILY' else (1.25 if tf == 'WEEKLY' else 1.40))
            sl = float(res.invalidation_level) if res.invalidation_level else pvt * 0.96
            if tgt <= entry * 1.03 or sl >= entry:
                continue                                              # no room to target / invalid stop
            if any(t == tf and pt == res.pattern_type and abs(pvt / p0 - 1) < 0.02 and idx - i0 < 120
                   for t, pt, p0, i0 in seen):
                continue                                              # same pattern instance re-detected
            seen.append((tf, res.pattern_type, pvt, idx))

            eval_res = evaluate_forward_realistic(df, idx, pvt, tgt, sl, tf, entry_price=entry)
            feats = extract_features_at_entry(sub_df, pvt, res.pattern_type, tf)

            # uniform yardstick shared with the base-rate table: +10% before -7% within 60 sessions
            fair = None
            up, dn = entry * 1.10, entry * 0.93
            for j in range(idx, min(idx + 60, total_bars)):
                if lows[j] <= dn:
                    fair = 0; break
                if highs[j] >= up:
                    fair = 1; break
            ret20 = round((closes[idx + 19] / entry - 1) * 100, 2) if idx + 19 < total_bars else None

            breakout_date_str = str(current_date)[:10]
            record = {
                "symbol": symbol, "timeframe": tf,
                "pattern_type": res.pattern_type, "pattern_name": res.pattern_name,
                "breakout_date": breakout_date_str, "breakout_year": current_date.year,
                "pivot_price": round(pvt, 2), "entry_price": round(entry, 2),
                "target_price": round(tgt, 2), "stop_loss": round(sl, 2),
                "outcome_status": eval_res["outcome_status"], "is_win": eval_res["is_win"],
                "max_gain_pct": eval_res["max_gain_pct"], "realized_pnl_pct": eval_res["realized_pnl_pct"],
                "exit_date": eval_res["exit_date"], "holding_bars": eval_res["holding_bars"],
                "fair_hit10": fair, "ret_20d": ret20,
            }
            records.append(record)
            ml_records.append({
                "symbol": symbol, "timeframe": tf, "pattern_type": res.pattern_type,
                "breakout_date": breakout_date_str, "breakout_year": current_date.year,
                "pivot_price": round(pvt, 2), **feats,
                "outcome_status": eval_res["outcome_status"], "is_win": eval_res["is_win"],
                "realized_pnl_pct": eval_res["realized_pnl_pct"], "holding_bars": eval_res["holding_bars"],
            })
            if eval_res["outcome_status"] in ("TARGET_HIT", "STOP_HIT", "TIME_STOP_EXIT"):
                last_exit_by_tf[tf] = max(last_exit_by_tf[tf], eval_res["exit_idx"])
            else:
                last_exit_by_tf[tf] = idx + (15 if tf == 'DAILY' else (30 if tf == 'WEEKLY' else 60))

    return records, ml_records


_WORKER_ANALYST = None


def _worker_init():
    global _WORKER_ANALYST
    _WORKER_ANALYST = ClassicalPatternAnalystDNA()


def _scan_job(job, analyst=None):
    sym, raw = job
    try:
        recs, ml = scan_stock_history_realistic(sym, raw, analyst or _WORKER_ANALYST)
        return sym, recs, ml, None
    except Exception as e:  # keep the batch alive
        return sym, [], [], str(e)


def run_realistic_ledger_build(max_stocks: int = 5000, hist_db: Optional[str] = None, master_db: Optional[str] = None):
    hist_db = hist_db or os.path.join(BASE_DIR, "Data", "stock_history.db")
    master_db = master_db or os.path.join(BASE_DIR, "Data", "quantsync_master.db")

    if not os.path.exists(hist_db):
        hist_db = "/root/quantsync/Data/stock_history.db"
    if not os.path.exists(master_db):
        master_db = "/root/quantsync/Data/quantsync_master.db"

    init_db(master_db)

    h_conn = sqlite3.connect(f"file:{hist_db}?mode=ro", uri=True)
    h_cur = h_conn.cursor()
    h_cur.execute("""
        SELECT symbol, rawCandles 
        FROM StockHistory 
        WHERE candleCount >= 500 
          AND (isAlgoEligible = 1 OR isV3Eligible = 1)
        ORDER BY candleCount DESC, symbol ASC 
        LIMIT ?
    """, (max_stocks,))
    stock_rows = h_cur.fetchall()
    h_conn.close()

    logger.info(f"Loaded {len(stock_rows)} target stocks for realistic time-stop walk-forward audit.")

    m_conn = sqlite3.connect(master_db, timeout=30.0)
    m_cur = m_conn.cursor()
    total_inserted = 0
    start_time = time.time()
    workers = max(1, int(os.environ.get("LEDGER_WORKERS", max(1, (os.cpu_count() or 2) - 1))))
    logger.info(f"Scanning with {workers} worker process(es)")

    def _write(records, ml_records):
        m_cur.executemany("""
            INSERT OR REPLACE INTO historical_breakout_records (
                symbol, timeframe, pattern_type, pattern_name, breakout_date,
                breakout_year, pivot_price, entry_price, target_price, stop_loss, outcome_status,
                is_win, max_gain_pct, realized_pnl_pct, exit_date, holding_bars, fair_hit10, ret_20d
            ) VALUES (
                :symbol, :timeframe, :pattern_type, :pattern_name, :breakout_date,
                :breakout_year, :pivot_price, :entry_price, :target_price, :stop_loss, :outcome_status,
                :is_win, :max_gain_pct, :realized_pnl_pct, :exit_date, :holding_bars, :fair_hit10, :ret_20d
            )
        """, records)
        if ml_records:
            m_cur.executemany("""
                INSERT INTO ml_breakout_dataset (
                    symbol, timeframe, pattern_type, breakout_date, breakout_year,
                    pivot_price, ret_5d, ret_10d, ret_20d, ret_30d, vcp_tightness_ratio,
                    atr_pct, vol_spike_ratio, price_vs_ema20, price_vs_ema50, price_vs_ema200,
                    ema_bullish_stacked, rsi_14, norm_30_series_json, outcome_status,
                    is_win, realized_pnl_pct, holding_bars
                ) VALUES (
                    :symbol, :timeframe, :pattern_type, :breakout_date, :breakout_year,
                    :pivot_price, :ret_5d, :ret_10d, :ret_20d, :ret_30d, :vcp_tightness_ratio,
                    :atr_pct, :vol_spike_ratio, :price_vs_ema20, :price_vs_ema50, :price_vs_ema200,
                    :ema_bullish_stacked, :rsi_14, :norm_30_series_json, :outcome_status,
                    :is_win, :realized_pnl_pct, :holding_bars
                )
            """, ml_records)
        m_conn.commit()

    jobs = [(sym, raw) for sym, raw in stock_rows if raw]
    if workers == 1:
        analyst = ClassicalPatternAnalystDNA()
        results = (( _scan_job((sym, raw), analyst)) for sym, raw in jobs)
    else:
        import multiprocessing as mp
        pool = mp.Pool(workers, initializer=_worker_init)
        results = pool.imap_unordered(_scan_job, jobs, chunksize=2)
    for i, (sym, records, ml_records, err) in enumerate(results):
        if err:
            logger.warning(f"Error processing {sym}: {err}")
        elif records:
            _write(records, ml_records)
            total_inserted += len(records)
        if (i + 1) % 25 == 0 or (i + 1) == len(jobs):
            logger.info(f"Progress: [{i+1}/{len(jobs)}] stocks | {total_inserted} trades audited | Elapsed: {time.time() - start_time:.1f}s")
    if workers > 1:
        pool.close(); pool.join()

    # Export ML CSV dataset
    try:
        csv_path = os.path.join(os.path.dirname(master_db), "breakout_ml_features.csv")
        df_ml = pd.read_sql_query("SELECT * FROM ml_breakout_dataset", m_conn)
        df_ml.to_csv(csv_path, index=False)
        logger.info(f"Exported ML dataset ({len(df_ml)} rows, {len(df_ml.columns)} cols) to {csv_path}")
    except Exception as e:
        logger.warning(f"Could not export CSV: {e}")

    # Print Year-by-Year Institutional Performance Matrix
    logger.info("================================================================================")
    logger.info("YEAR-BY-YEAR REALISTIC BREAKOUT PERFORMANCE MATRIX (WITH TIME-STOPS & COSTS)")
    logger.info("================================================================================")
    
    m_cur.execute("""
        SELECT 
            breakout_year,
            timeframe,
            COUNT(*) as total,
            SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN is_win = 0 AND outcome_status != 'ACTIVE' THEN 1 ELSE 0 END) as losses,
            ROUND(AVG(holding_bars), 1) as avg_bars,
            ROUND(AVG(realized_pnl_pct), 1) as avg_pnl,
            ROUND(AVG(CASE WHEN is_win = 1 THEN realized_pnl_pct ELSE NULL END), 1) as avg_win,
            ROUND(AVG(CASE WHEN is_win = 0 AND outcome_status != 'ACTIVE' THEN realized_pnl_pct ELSE NULL END), 1) as avg_loss
        FROM historical_breakout_records
        GROUP BY breakout_year, timeframe
        ORDER BY breakout_year ASC, timeframe ASC
    """)
    rows = m_cur.fetchall()
    for yr, tf, tot, wins, losses, avg_bars, avg_pnl, avg_w, avg_l in rows:
        resolved = wins + losses
        wr = round((wins / resolved) * 100, 1) if resolved > 0 else 0.0
        logger.info(f"Year {yr} | {tf:<7} | Trades: {tot:<4} | WinRate: {wr}% | AvgBars: {avg_bars:<4} | AvgPnL: {avg_pnl}% | Win: +{avg_w}% | Loss: {avg_l}%")

    m_conn.close()
    build_pattern_edge(master_db)
    logger.info("Realistic ledger build and ML dataset export completed.")


def build_pattern_edge(master_db: str, cache_db: Optional[str] = None, min_samples: int = 30) -> List[Dict[str, Any]]:
    """
    pattern_edge: one honest row per (pattern, timeframe) for the Radar cards.
      fair_win_rate   share of entries that rose 10% before falling 7% (60 sessions)
      baseline        same yardstick on a random stock-day (dna_base_rates 'ALL')
      edge_pts        fair_win_rate − baseline, in percentage points
      rule_win_rate   the pattern's own target/stop/time rule (what the old UI showed, now entry-based)
    Rows with fewer than min_samples resolved entries are kept but flagged low_confidence.
    """
    cache_db = cache_db or os.path.join(os.path.dirname(master_db), "stock_dna_cache.db")
    baseline = None
    try:
        c = sqlite3.connect(cache_db)
        r = c.execute("SELECT p_hit10_before_dn7 FROM dna_base_rates WHERE state_key='ALL'").fetchone()
        c.close()
        baseline = r[0] if r else None
    except sqlite3.Error:
        pass
    conn = sqlite3.connect(master_db)
    rows = conn.execute("""
        SELECT pattern_type, timeframe, COUNT(*),
               SUM(fair_hit10 IS NOT NULL), SUM(COALESCE(fair_hit10, 0)),
               SUM(outcome_status != 'ACTIVE'), SUM(CASE WHEN outcome_status != 'ACTIVE' THEN is_win ELSE 0 END),
               MIN(breakout_date), MAX(breakout_date)
        FROM historical_breakout_records GROUP BY pattern_type, timeframe
    """).fetchall()
    out = []
    for pt, tf, n, resolved, hits, closed, wins, d0, d1 in rows:
        rets = sorted(x[0] for x in conn.execute(
            "SELECT ret_20d FROM historical_breakout_records WHERE pattern_type=? AND timeframe=? AND ret_20d IS NOT NULL", (pt, tf)))
        holds = sorted(x[0] for x in conn.execute(
            "SELECT holding_bars FROM historical_breakout_records WHERE pattern_type=? AND timeframe=? AND outcome_status='TARGET_HIT'", (pt, tf)))
        fwr = hits / resolved if resolved else None
        out.append({
            "pattern_type": pt, "timeframe": tf, "samples": n, "resolved": resolved,
            "fair_win_rate": round(fwr * 100, 1) if fwr is not None else None,
            "baseline": round(baseline * 100, 1) if baseline else None,
            "edge_pts": round((fwr - baseline) * 100, 1) if (fwr is not None and baseline) else None,
            "rule_win_rate": round(wins / closed * 100, 1) if closed else None,
            "median_ret_20d": rets[len(rets) // 2] if rets else None,
            "median_days_to_target": holds[len(holds) // 2] if holds else None,
            "low_confidence": int(resolved < min_samples),
            "first_date": d0, "last_date": d1,
        })
    conn.execute("DROP TABLE IF EXISTS pattern_edge")
    conn.execute("""CREATE TABLE pattern_edge(pattern_type TEXT, timeframe TEXT, samples INT, resolved INT,
        fair_win_rate REAL, baseline REAL, edge_pts REAL, rule_win_rate REAL, median_ret_20d REAL,
        median_days_to_target REAL, low_confidence INT, first_date TEXT, last_date TEXT,
        built_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(pattern_type, timeframe))""")
    conn.executemany("""INSERT INTO pattern_edge(pattern_type, timeframe, samples, resolved, fair_win_rate, baseline,
        edge_pts, rule_win_rate, median_ret_20d, median_days_to_target, low_confidence, first_date, last_date)
        VALUES (:pattern_type,:timeframe,:samples,:resolved,:fair_win_rate,:baseline,:edge_pts,:rule_win_rate,
        :median_ret_20d,:median_days_to_target,:low_confidence,:first_date,:last_date)""", out)
    conn.commit()
    conn.close()
    logger.info(f"pattern_edge rebuilt: {len(out)} rows (baseline {baseline})")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Point-in-time breakout ledger + pattern_edge")
    ap.add_argument("max_stocks", nargs="?", type=int, default=5000)
    ap.add_argument("--hist-db"); ap.add_argument("--master-db")
    ap.add_argument("--edge-only", action="store_true", help="only rebuild pattern_edge from existing records")
    a = ap.parse_args()
    if a.edge_only:
        build_pattern_edge(a.master_db or os.path.join(BASE_DIR, "Data", "quantsync_master.db"))
    else:
        run_realistic_ledger_build(max_stocks=a.max_stocks, hist_db=a.hist_db, master_db=a.master_db)
