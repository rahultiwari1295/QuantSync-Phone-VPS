#!/usr/bin/env python3
"""
build_universe_fingerprint_dataset.py
--------------------------------------
Institutional-Grade ML & Pattern Fingerprint Dataset Generator
Extracts 100,000+ historical trade setups across the entire universe (~2,367 Indian equities)
from 2019-01-01 to 2024-12-31 from StockHistory SQLite.

Features Extracted:
1. Microstructure & Candlestick Anatomy (wicks, body, tails, close-in-range, turnover)
2. Volume Dynamics & Minervini Volume Dry-Up (VDU)
3. Trend, EMAs, 52W High/Low distance, Stage 2 confirmation
4. Classical Pattern DNA (Triangles, Cups, Double Bottoms, Wedges, Rectangles, Flags)
5. Champion Breakout Pattern (CIR, risk %, upper wick)
6. Fibonacci Swing Projections (Impulse, pullback, extension targets)
7. NIFTY 100 & NIFTY MIDCAP 50 Benchmark Regimes
8. 64-Dimensional Pattern Waveform Fingerprint Vector (Geometry, VCP Contraction, Volume Profile)
9. Clean, Lookahead-Free Forward Outcomes (Next-day Open entry, MFE, MAE, Win/Loss, velocity)
"""

import os
import sys
import json
import sqlite3
import time
import datetime
import numpy as np
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from classical_pattern_dna import ClassicalPatternAnalystDNA
from engines.v3.banana_engine.core.champion_patterns import detect_all_champion_patterns
from engines.v3.fibonacci import SwingDetector, aggregate_to_weekly

DB_PATH = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")
SECTOR_DB_PATH = os.path.join(SCRIPT_DIR, "engines", "v3", "banana_engine", "data", "sector_map.db")
OUTPUT_PARQUET = os.path.join(SCRIPT_DIR, "v4_universe_2500_dataset_2019_2024.parquet")
OUTPUT_EMBEDDINGS = os.path.join(SCRIPT_DIR, "models", "v4_fingerprint_embeddings.npy")
OUTPUT_METADATA = os.path.join(SCRIPT_DIR, "models", "v4_fingerprint_metadata.parquet")


def load_sector_map(db_path: str) -> dict:
    """Load symbol to sector mapping."""
    s_map = {}
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT symbol, sector FROM sector_history")
            for sym, sec in cur.fetchall():
                s_map[sym] = sec
            conn.close()
        except Exception as e:
            print(f"[WARN] Failed loading sector_map: {e}")
    return s_map


def precompute_index_history(db_path: str, symbol: str) -> dict:
    """Precompute point-in-time indicators & regime for benchmark index."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol=?", (symbol,))
    row = cur.fetchone()
    conn.close()

    if not row or not row[0]:
        print(f"[WARN] No index candles found for {symbol}")
        return {}

    candles = json.loads(row[0])
    df = pd.DataFrame(candles).rename(
        columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    df["date"] = df["date"].astype(str).str[:10]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    df["ret5d"] = (df["close"] - df["close"].shift(5)) / (df["close"].shift(5) + 1e-9) * 100
    df["ret10d"] = (df["close"] - df["close"].shift(10)) / (df["close"].shift(10) + 1e-9) * 100
    df["ret20d"] = (df["close"] - df["close"].shift(20)) / (df["close"].shift(20) + 1e-9) * 100

    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - (100 / (1 + (gain / (loss + 1e-9))))

    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1)))
    )
    df["atr14"] = tr.rolling(14).mean()
    df["atr_pct"] = df["atr14"] / (df["close"] + 1e-9) * 100

    def assign_regime(r):
        c, e20, e50, e200 = r["close"], r["ema20"], r["ema50"], r["ema200"]
        if c > e20 > e50 > e200:
            return "STRONG_BULL"
        elif c > e50 and e50 > e200:
            return "MILD_BULL"
        elif c < e50 and c > e200:
            return "CAUTION"
        else:
            return "DEFENSE_BEAR"

    df["regime"] = df.apply(assign_regime, axis=1)
    return df.set_index("date").to_dict(orient="index")


def compute_stock_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized calculation of indicators across the stock's full timeline."""
    df["prev_close"] = df["close"].shift(1)
    df["ret1d"] = (df["close"] - df["prev_close"]) / (df["prev_close"] + 1e-9) * 100
    df["bar_range"] = df["high"] - df["low"] + 1e-6
    df["close_in_range"] = (df["close"] - df["low"]) / df["bar_range"]
    df["upper_wick_pct"] = (df["high"] - np.maximum(df["open"], df["close"])) / df["bar_range"]
    df["lower_tail_pct"] = (np.minimum(df["open"], df["close"]) - df["low"]) / df["bar_range"]
    df["body_to_range_ratio"] = abs(df["close"] - df["open"]) / df["bar_range"]
    df["gap_up_pct"] = (df["open"] - df["prev_close"]) / (df["prev_close"] + 1e-9) * 100

    # Volume metrics
    df["sma5_vol"] = df["volume"].rolling(5).mean()
    df["sma20_vol"] = df["volume"].rolling(20).mean()
    df["sma60_vol"] = df["volume"].rolling(60).mean()
    df["vol_ratio_20d"] = df["volume"] / (df["sma20_vol"] + 1.0)
    df["vol_ratio_5d"] = df["volume"] / (df["sma5_vol"] + 1.0)
    df["vol_ratio_60d"] = df["volume"] / (df["sma60_vol"] + 1.0)
    df["vol_trend_5d"] = (df["sma5_vol"] / (df["sma20_vol"] + 1.0)) - 1.0
    df["vol_acceleration"] = df["volume"] / (df["volume"].shift(1) + 1.0)
    df["turnover_cr"] = (df["close"] * df["volume"]) / 10_000_000.0

    # Moving averages
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["dist_ema20_pct"] = (df["close"] - df["ema20"]) / (df["ema20"] + 1e-9) * 100
    df["dist_ema50_pct"] = (df["close"] - df["ema50"]) / (df["ema50"] + 1e-9) * 100
    df["dist_ema200_pct"] = (df["close"] - df["ema200"]) / (df["ema200"] + 1e-9) * 100

    # 52-week High/Low (252 bars)
    df["rolling_high_252"] = df["high"].rolling(252, min_periods=50).max()
    df["rolling_low_252"] = df["low"].rolling(252, min_periods=50).min()
    df["dist_52w_high_pct"] = (df["close"] - df["rolling_high_252"]) / (df["rolling_high_252"] + 1e-9) * 100
    df["dist_52w_low_pct"] = (df["close"] - df["rolling_low_252"]) / (df["rolling_low_252"] + 1e-9) * 100

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi14"] = 100 - (100 / (1 + (gain / (loss + 1e-9))))
    df["rsi_trend_5d"] = df["rsi14"] - df["rsi14"].shift(5)

    # ATR(14)
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1)))
    )
    df["atr14"] = tr.rolling(14).mean()
    df["atr14_pct"] = df["atr14"] / (df["close"] + 1e-9) * 100

    # Prior returns
    df["prior_60d_return_pct"] = (df["close"] - df["close"].shift(60)) / (df["close"].shift(60) + 1e-9) * 100
    df["prior_120d_return_pct"] = (df["close"] - df["close"].shift(120)) / (df["close"].shift(120) + 1e-9) * 100

    # Volume dryup in base: count of last 10 bars with vol < 0.7x 20d SMA
    is_dry = (df["volume"] < 0.7 * df["sma20_vol"]).astype(int)
    df["dryup_bars_in_10"] = is_dry.rolling(10).sum()

    # Stage 2 confirmation proxy: bars above EMA200 in last 50
    above_200 = (df["close"] > df["ema200"]).astype(int)
    df["above_ema200_bars_in_50"] = above_200.rolling(50).sum()

    return df


def extract_64d_fingerprint(window: pd.DataFrame, sig_bar: pd.Series) -> np.ndarray:
    """
    Computes a 64-dimensional scale-invariant geometric & momentum fingerprint vector
    from the 60-bar consolidation window ending at sig_bar.
    """
    closes = window["close"].values
    c_sig = float(sig_bar["close"]) + 1e-9
    v_sma = float(sig_bar["sma20_vol"]) + 1.0

    # 1. 30 sampled normalized close prices relative to pivot close
    sample_indices = np.linspace(0, len(closes) - 1, 30, dtype=int)
    norm_closes = np.clip((closes[sample_indices] / c_sig) - 1.0, -1.0, 1.0)

    # 2. 10 shape metrics (5 segments of 12 bars: range compression and segment drawdowns)
    seg_ranges = []
    seg_drawdowns = []
    for s in range(5):
        seg = window.iloc[s * 12 : (s + 1) * 12]
        s_rng = (seg["high"].max() - seg["low"].min()) / c_sig
        s_dd = (seg["close"].min() - seg["close"].max()) / (seg["close"].max() + 1e-9)
        seg_ranges.append(np.clip(s_rng, 0.0, 1.0))
        seg_drawdowns.append(np.clip(s_dd, -1.0, 0.0))

    # 3. 10 volume metrics
    seg_vols = []
    for s in range(5):
        seg = window.iloc[s * 12 : (s + 1) * 12]
        seg_vols.append(np.clip(seg["volume"].mean() / v_sma, 0.0, 5.0))
    v_spike = np.clip(float(sig_bar["volume"]) / v_sma, 0.0, 10.0)
    up_vol = window[window["close"] >= window["open"]]["volume"].sum()
    dn_vol = window[window["close"] < window["open"]]["volume"].sum()
    vol_ratio = np.clip(up_vol / (dn_vol + 1.0), 0.0, 10.0)
    dryup = float(sig_bar["dryup_bars_in_10"]) / 10.0
    to_cr = np.clip(float(sig_bar["turnover_cr"]) / 50.0, 0.0, 5.0)
    v_trend = np.clip(float(sig_bar["vol_trend_5d"]), -2.0, 5.0)
    vol_metrics = seg_vols + [v_spike, vol_ratio, dryup, to_cr, v_trend]

    # 4. 14 momentum & technical metrics
    stg2 = 1.0 if float(sig_bar["above_ema200_bars_in_50"]) >= 40 else 0.0
    mom = [
        np.clip(float(sig_bar["rsi14"]) / 100.0, 0.0, 1.0),
        np.clip(float(sig_bar["rsi_trend_5d"]) / 50.0, -1.0, 1.0),
        np.clip(float(sig_bar["atr14_pct"]) / 10.0, 0.0, 2.0),
        np.clip(float(sig_bar["dist_ema20_pct"]) / 20.0, -1.0, 1.0),
        np.clip(float(sig_bar["dist_ema50_pct"]) / 30.0, -1.0, 1.0),
        np.clip(float(sig_bar["dist_ema200_pct"]) / 50.0, -1.0, 1.0),
        np.clip(float(sig_bar["dist_52w_high_pct"]) / 50.0, -1.0, 0.0),
        np.clip(float(sig_bar["dist_52w_low_pct"]) / 100.0, 0.0, 3.0),
        np.clip(float(sig_bar["prior_60d_return_pct"]) / 100.0, -1.0, 2.0),
        np.clip(float(sig_bar["prior_120d_return_pct"]) / 150.0, -1.0, 3.0),
        np.clip(float(sig_bar["close_in_range"]), 0.0, 1.0),
        np.clip(float(sig_bar["upper_wick_pct"]), 0.0, 1.0),
        np.clip(float(sig_bar["body_to_range_ratio"]), 0.0, 1.0),
        stg2
    ]

    vec = np.concatenate([
        norm_closes,
        np.array(seg_ranges, dtype=np.float32),
        np.array(seg_drawdowns, dtype=np.float32),
        np.array(vol_metrics, dtype=np.float32),
        np.array(mom, dtype=np.float32)
    ])
    # Normalize vector to unit length for fast cosine similarity
    norm = np.linalg.norm(vec)
    if norm > 1e-6:
        vec = vec / norm
    return vec.astype(np.float32)


def main():
    print("=" * 80)
    print("  QuantSync V4 Universal Pattern Fingerprint Extractor (All ~2,367 Stocks)")
    print("  Target Period: 2019-01-01 to 2024-12-31 | Lookahead-Free Entry at Open")
    print("=" * 80)
    t_start = time.time()

    # 1. Sector mapping
    sector_map = load_sector_map(SECTOR_DB_PATH)
    print(f"[*] Loaded sector mappings: {len(sector_map)} symbols.")

    # 2. Precompute Benchmarks
    print("[*] Precomputing NIFTY 100 and NIFTY MIDCAP 50 benchmarks...")
    nifty_dict = precompute_index_history(DB_PATH, "NIFTY100")
    midcap_dict = precompute_index_history(DB_PATH, "NIFTYMIDCAP50")
    print(f"    NIFTY 100 days: {len(nifty_dict)} | MIDCAP 50 days: {len(midcap_dict)}")

    # 3. Load ALL stocks with candleCount >= 100
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, rawCandles, candleCount 
        FROM StockHistory 
        WHERE candleCount >= 100
        ORDER BY symbol ASC
    """)
    stocks = cur.fetchall()
    conn.close()

    total_stocks = len(stocks)
    print(f"[*] Found {total_stocks} stocks in database ready for processing.")

    analyst = ClassicalPatternAnalystDNA()
    sd = SwingDetector()

    all_dataset_rows = []
    all_fingerprints = []
    all_metadata_rows = []

    stocks_processed = 0
    signals_count = 0

    print("[*] Commencing universe feature and fingerprint extraction...")

    for sym_idx, (sym, raw_candles, candle_count) in enumerate(stocks):
        stocks_processed += 1
        if stocks_processed % 200 == 0 or stocks_processed == total_stocks:
            el = time.time() - t_start
            print(f"    [{stocks_processed}/{total_stocks} stocks] Processed | Setups extracted: {signals_count} | Elapsed: {el:.1f}s")

        try:
            candles = json.loads(raw_candles)
            if not candles or len(candles) < 80:
                continue

            df = pd.DataFrame(candles).rename(
                columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
            )
            df["date"] = df["date"].astype(str).str[:10]
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values("date").reset_index(drop=True)

            if len(df) < 80:
                continue

            # Indicators
            df = compute_stock_indicators(df)
            wdf = aggregate_to_weekly(df)

            # Signal filtering: 2019-01-01 to 2024-12-31
            mask = (
                (df["date"] >= "2019-01-01") &
                (df["date"] <= "2024-12-31") &
                (df["ret1d"] >= 2.5) &
                (df["close"] > df["open"]) &
                (df["volume"] >= 1.3 * df["sma20_vol"]) &
                (df["close_in_range"] >= 0.50) &
                (df.index >= 59) &
                (df.index < len(df) - 21)
            )

            cand_indices = df[mask].index.tolist()
            if not cand_indices:
                continue

            stock_sector = sector_map.get(sym, "Unknown")

            for idx in cand_indices:
                sub_df = df.iloc[: idx + 1]
                sig_bar = sub_df.iloc[-1]
                sig_date = sig_bar["date"]

                # -------------------------------------------------------------
                # Forward Outcome Tracking (Strictly Next-Day Open Entry)
                # -------------------------------------------------------------
                entry_price = float(df.iloc[idx + 1]["open"])
                entry_date = df.iloc[idx + 1]["date"]
                fwd_window = df.iloc[idx + 1 : idx + 21]

                hit_target_first = False
                hit_stop_first = False
                bars_to_mfe = 1
                max_high_val = -1e9

                for f_i in range(len(fwd_window)):
                    b_low = float(fwd_window.iloc[f_i]["low"])
                    b_high = float(fwd_window.iloc[f_i]["high"])

                    if b_high > max_high_val:
                        max_high_val = b_high
                        bars_to_mfe = f_i + 1

                    if not hit_target_first and not hit_stop_first:
                        if (entry_price - b_low) / entry_price >= 0.08:
                            hit_stop_first = True
                        elif (b_high - entry_price) / entry_price >= 0.07:
                            hit_target_first = True

                max_high_20d = float(fwd_window["high"].max())
                min_low_20d = float(fwd_window["low"].min())
                close_20d = float(fwd_window.iloc[-1]["close"])

                mfe_pct = (max_high_20d - entry_price) / entry_price * 100.0
                mae_pct = (min_low_20d - entry_price) / entry_price * 100.0
                ret_20d = (close_20d - entry_price) / entry_price * 100.0

                idx_10 = min(idx + 10, len(df) - 1)
                idx_5 = min(idx + 5, len(df) - 1)
                ret_10d = (float(df.iloc[idx_10]["close"]) - entry_price) / entry_price * 100.0
                ret_5d = (float(df.iloc[idx_5]["close"]) - entry_price) / entry_price * 100.0

                label_win_20d = 1 if hit_target_first else 0

                if mfe_pct >= 15.0 and not hit_stop_first:
                    label_5class = "RUNNER"
                elif mfe_pct >= 7.0 and not hit_stop_first:
                    label_5class = "WINNER"
                elif not hit_stop_first and mae_pct > -4.0 and mfe_pct < 7.0:
                    label_5class = "BREAKEVEN"
                elif mae_pct <= -4.0 and mae_pct > -8.0 and not hit_target_first:
                    label_5class = "WEAK_STOP"
                else:
                    label_5class = "HARD_STOP"

                # -------------------------------------------------------------
                # Classical Pattern DNA
                # -------------------------------------------------------------
                pat = analyst.analyze(sub_df)
                if pat:
                    has_classical_pattern = 1
                    pat_type = pat.pattern_type
                    pat_lifecycle = pat.lifecycle_state
                    pat_conf = float(pat.confidence)
                    pat_quality = float(pat.quality_score)
                    pat_rr = float(pat.risk_reward_ratio) if pat.risk_reward_ratio else 0.0
                    pat_breakout_dist = (float(sig_bar["close"]) - pat.breakout_level) / (pat.breakout_level + 1e-9) * 100.0 if pat.breakout_level else 0.0
                    pat_inval_dist = (float(sig_bar["close"]) - pat.invalidation_level) / (float(sig_bar["close"]) + 1e-9) * 100.0 if pat.invalidation_level else 0.0
                    pat_target_dist = (pat.target_price - float(sig_bar["close"])) / (float(sig_bar["close"]) + 1e-9) * 100.0 if pat.target_price else 0.0
                    pat_is_bullish = 1 if pat.stance == "BULLISH" else 0
                    competing_count = len(pat.competing_candidates) if pat.competing_candidates else 0
                else:
                    has_classical_pattern = 0
                    pat_type = "NONE"
                    pat_lifecycle = "NONE"
                    pat_conf = 0.0
                    pat_quality = 0.0
                    pat_rr = 0.0
                    pat_breakout_dist = 0.0
                    pat_inval_dist = 0.0
                    pat_target_dist = 0.0
                    pat_is_bullish = 0
                    competing_count = 0

                # -------------------------------------------------------------
                # Champion Breakout Pattern
                # -------------------------------------------------------------
                champ = detect_all_champion_patterns(bars=sub_df, i=len(sub_df) - 1, symbol=sym)
                if champ:
                    has_champ_pattern = 1
                    champ_pat = champ.pattern
                    champ_cir = float(champ.cir)
                    champ_vol_ratio = float(champ.volume_ratio)
                    champ_upper_wick = float(champ.upper_wick)
                    champ_risk_pct = float(champ.risk_pct)
                else:
                    has_champ_pattern = 0
                    champ_pat = "NONE"
                    champ_cir = 0.0
                    champ_vol_ratio = 0.0
                    champ_upper_wick = 0.0
                    champ_risk_pct = 0.0

                is_double_confluence = 1 if (has_classical_pattern == 1 and has_champ_pattern == 1) else 0

                # -------------------------------------------------------------
                # Fibonacci Swing Structure
                # -------------------------------------------------------------
                w_sub = wdf[wdf["date"] <= sig_date] if wdf is not None else None
                fib_struct = sd.build_active_structure(sym, w_sub, len(w_sub) - 1) if (w_sub is not None and len(w_sub) >= 10) else None

                if fib_struct and fib_struct.projection:
                    fib_has_structure = 1
                    proj = fib_struct.projection
                    fib_impulse_pct = float(proj.impulse_pct)
                    fib_pullback_pct = float(proj.pullback_pct)
                    fib_1000 = proj.levels.get(1.0, 0.0)
                    fib_1272 = proj.levels.get(1.272, 0.0)
                    fib_1618 = proj.levels.get(1.618, 0.0)
                    curr_c = float(sig_bar["close"])
                    fib_dist_1000 = (curr_c - fib_1000) / (fib_1000 + 1e-9) * 100.0 if fib_1000 > 0 else 0.0
                    fib_dist_1272 = (curr_c - fib_1272) / (fib_1272 + 1e-9) * 100.0 if fib_1272 > 0 else 0.0
                    fib_dist_1618 = (curr_c - fib_1618) / (fib_1618 + 1e-9) * 100.0 if fib_1618 > 0 else 0.0
                    fib_age_weeks = len(w_sub) - fib_struct.activated_bar_index if fib_struct.activated_bar_index else 0
                else:
                    fib_has_structure = 0
                    fib_impulse_pct = 0.0
                    fib_pullback_pct = 0.0
                    fib_dist_1000 = 0.0
                    fib_dist_1272 = 0.0
                    fib_dist_1618 = 0.0
                    fib_age_weeks = 0

                # -------------------------------------------------------------
                # NIFTY 100 & Midcap Macro Regime
                # -------------------------------------------------------------
                nifty_info = nifty_dict.get(sig_date, {})
                nifty_close = nifty_info.get("close", 0.0)
                nifty_ema20 = nifty_info.get("ema20", 0.0)
                nifty_ema50 = nifty_info.get("ema50", 0.0)
                nifty_ema200 = nifty_info.get("ema200", 0.0)
                nifty_dist_ema20 = (nifty_close - nifty_ema20) / (nifty_ema20 + 1e-9) * 100.0 if nifty_ema20 else 0.0
                nifty_dist_ema50 = (nifty_close - nifty_ema50) / (nifty_ema50 + 1e-9) * 100.0 if nifty_ema50 else 0.0
                nifty_dist_ema200 = (nifty_close - nifty_ema200) / (nifty_ema200 + 1e-9) * 100.0 if nifty_ema200 else 0.0
                nifty_regime = nifty_info.get("regime", "UNKNOWN")
                nifty_ret_5d = nifty_info.get("ret5d", 0.0)
                nifty_ret_10d = nifty_info.get("ret10d", 0.0)
                nifty_ret_20d = nifty_info.get("ret20d", 0.0)
                nifty_rsi14 = nifty_info.get("rsi14", 50.0)
                nifty_atr_pct = nifty_info.get("atr_pct", 1.0)

                midcap_info = midcap_dict.get(sig_date, {})
                midcap_regime = midcap_info.get("regime", "UNKNOWN")
                midcap_close = midcap_info.get("close", 0.0)
                midcap_ema50 = midcap_info.get("ema50", 0.0)
                midcap_dist_ema50 = (midcap_close - midcap_ema50) / (midcap_ema50 + 1e-9) * 100.0 if midcap_ema50 else 0.0
                dual_index_bull = 1 if (nifty_dist_ema50 > 0 and midcap_dist_ema50 > 0) else 0

                sig_dt = datetime.datetime.strptime(sig_date, "%Y-%m-%d")
                month_of_year = sig_dt.month
                quarter = f"Q{(month_of_year - 1) // 3 + 1}"
                day_of_week = sig_dt.weekday()

                # Stage 2 confirmation & EMA alignment
                c_now = float(sig_bar["close"])
                e20_now = float(sig_bar["ema20"])
                e50_now = float(sig_bar["ema50"])
                e200_now = float(sig_bar["ema200"])
                if c_now > e20_now > e50_now > e200_now:
                    ema_alignment = "STRONG_BULL"
                elif c_now > e50_now and e50_now > e200_now:
                    ema_alignment = "MILD_BULL"
                elif c_now < e50_now and c_now > e200_now:
                    ema_alignment = "CAUTION"
                else:
                    ema_alignment = "BEARISH"

                stage_2_confirmed = 1 if sig_bar["above_ema200_bars_in_50"] >= 40 else 0

                # -------------------------------------------------------------
                # 64-D Geometric & Waveform Pattern Fingerprint
                # -------------------------------------------------------------
                window_60 = df.iloc[idx - 59 : idx + 1]
                fp_vec = extract_64d_fingerprint(window_60, sig_bar)
                all_fingerprints.append(fp_vec)

                # Assemble Row
                row_dict = {
                    "symbol": sym,
                    "date": sig_date,
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 2),
                    "sector": stock_sector,
                    "bar_return_pct": round(float(sig_bar["ret1d"]), 2),
                    "close_in_range": round(float(sig_bar["close_in_range"]), 4),
                    "upper_wick_pct": round(float(sig_bar["upper_wick_pct"]), 4),
                    "lower_tail_pct": round(float(sig_bar["lower_tail_pct"]), 4),
                    "body_to_range_ratio": round(float(sig_bar["body_to_range_ratio"]), 4),
                    "gap_up_pct": round(float(sig_bar["gap_up_pct"]), 2),
                    "turnover_cr": round(float(sig_bar["turnover_cr"]), 2),
                    "vol_ratio_20d": round(float(sig_bar["vol_ratio_20d"]), 2),
                    "vol_ratio_5d": round(float(sig_bar["vol_ratio_5d"]), 2),
                    "vol_ratio_60d": round(float(sig_bar["vol_ratio_60d"]), 2),
                    "vol_trend_5d": round(float(sig_bar["vol_trend_5d"]), 2),
                    "vol_acceleration": round(float(sig_bar["vol_acceleration"]), 2),
                    "dryup_bars_in_10": int(sig_bar["dryup_bars_in_10"]),
                    "rsi_14": round(float(sig_bar["rsi14"]), 2),
                    "rsi_trend_5d": round(float(sig_bar["rsi_trend_5d"]), 2),
                    "atr_14_pct": round(float(sig_bar["atr14_pct"]), 2),
                    "dist_ema20_pct": round(float(sig_bar["dist_ema20_pct"]), 2),
                    "dist_ema50_pct": round(float(sig_bar["dist_ema50_pct"]), 2),
                    "dist_ema200_pct": round(float(sig_bar["dist_ema200_pct"]), 2),
                    "ema_alignment": ema_alignment,
                    "dist_52w_high_pct": round(float(sig_bar["dist_52w_high_pct"]), 2),
                    "dist_52w_low_pct": round(float(sig_bar["dist_52w_low_pct"]), 2),
                    "prior_60d_return_pct": round(float(sig_bar["prior_60d_return_pct"]), 2),
                    "prior_120d_return_pct": round(float(sig_bar["prior_120d_return_pct"]), 2),
                    "stage_2_confirmed": stage_2_confirmed,
                    # Pattern DNA
                    "has_classical_pattern": has_classical_pattern,
                    "pattern_type": pat_type,
                    "pattern_lifecycle": pat_lifecycle,
                    "pattern_confidence": round(pat_conf, 2),
                    "pattern_quality_score": round(pat_quality, 2),
                    "pattern_rr_ratio": round(pat_rr, 2),
                    "pattern_breakout_dist_pct": round(pat_breakout_dist, 2),
                    "pattern_inval_dist_pct": round(pat_inval_dist, 2),
                    "pattern_target_dist_pct": round(pat_target_dist, 2),
                    "pattern_is_bullish": pat_is_bullish,
                    "competing_pattern_count": competing_count,
                    # Champion Breakout
                    "has_champ_pattern": has_champ_pattern,
                    "champ_pattern": champ_pat,
                    "champ_cir": round(champ_cir, 2),
                    "champ_vol_ratio": round(champ_vol_ratio, 2),
                    "champ_upper_wick": round(champ_upper_wick, 2),
                    "champ_risk_pct": round(champ_risk_pct, 2),
                    "is_double_confluence": is_double_confluence,
                    # Fibonacci
                    "fib_has_structure": fib_has_structure,
                    "fib_impulse_pct": round(fib_impulse_pct, 2),
                    "fib_pullback_pct": round(fib_pullback_pct, 2),
                    "fib_dist_1000_pct": round(fib_dist_1000, 2),
                    "fib_dist_1272_pct": round(fib_dist_1272, 2),
                    "fib_dist_1618_pct": round(fib_dist_1618, 2),
                    "fib_structure_age_weeks": fib_age_weeks,
                    # Macro & Benchmark
                    "nifty_close": round(nifty_close, 2),
                    "nifty_dist_ema20": round(nifty_dist_ema20, 2),
                    "nifty_dist_ema50": round(nifty_dist_ema50, 2),
                    "nifty_dist_ema200": round(nifty_dist_ema200, 2),
                    "nifty_regime": nifty_regime,
                    "nifty_ret_5d": round(nifty_ret_5d, 2),
                    "nifty_ret_10d": round(nifty_ret_10d, 2),
                    "nifty_ret_20d": round(nifty_ret_20d, 2),
                    "nifty_rsi14": round(nifty_rsi14, 2),
                    "nifty_atr_pct": round(nifty_atr_pct, 2),
                    "midcap_regime": midcap_regime,
                    "midcap_dist_ema50": round(midcap_dist_ema50, 2),
                    "dual_index_bull": dual_index_bull,
                    "month_of_year": month_of_year,
                    "quarter": quarter,
                    "day_of_week": day_of_week,
                    # Strictly Lookahead-Free Forward Outcome Labels
                    "label_win_20d": label_win_20d,
                    "label_5class": label_5class,
                    "label_mfe_pct": round(mfe_pct, 2),
                    "label_mae_pct": round(mae_pct, 2),
                    "label_bars_to_mfe": bars_to_mfe,
                    "label_ret_5d": round(ret_5d, 2),
                    "label_ret_10d": round(ret_10d, 2),
                    "label_ret_20d": round(ret_20d, 2),
                }

                all_dataset_rows.append(row_dict)
                all_metadata_rows.append({
                    "setup_id": signals_count,
                    "symbol": sym,
                    "date": sig_date,
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 2),
                    "sector": stock_sector,
                    "pattern_type": pat_type,
                    "label_win_20d": label_win_20d,
                    "label_mfe_pct": round(mfe_pct, 2),
                    "label_mae_pct": round(mae_pct, 2),
                    "label_bars_to_mfe": bars_to_mfe,
                    "label_ret_20d": round(ret_20d, 2),
                })
                signals_count += 1

        except Exception as e:
            # Skip corrupted stock silently
            continue

    print(f"\n[*] Extraction completed in {time.time() - t_start:.1f}s.")
    print(f"[*] Total Valid Historical Trade Setups Extracted: {len(all_dataset_rows)}")

    # 4. Save Parquet Dataset
    df_all = pd.DataFrame(all_dataset_rows)
    df_all.to_parquet(OUTPUT_PARQUET, index=False)
    print(f"[+] Saved complete dataset to: {OUTPUT_PARQUET} ({os.path.getsize(OUTPUT_PARQUET) / (1024*1024):.2f} MB)")

    # 5. Save Fingerprint Vectors & Fast Lookup Metadata
    fp_matrix = np.array(all_fingerprints, dtype=np.float32)
    np.save(OUTPUT_EMBEDDINGS, fp_matrix)
    print(f"[+] Saved {fp_matrix.shape[0]}x{fp_matrix.shape[1]} fingerprint matrix to: {OUTPUT_EMBEDDINGS}")

    df_meta = pd.DataFrame(all_metadata_rows)
    df_meta.to_parquet(OUTPUT_METADATA, index=False)
    print(f"[+] Saved fingerprint metadata index to: {OUTPUT_METADATA}")

    print("\n" + "=" * 80)
    print("  Dataset Extraction Complete! Ready for Model Training & Twin Search Indexing.")
    print("=" * 80)


if __name__ == "__main__":
    main()
