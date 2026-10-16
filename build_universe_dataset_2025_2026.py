#!/usr/bin/env python3
"""
build_universe_dataset_2025_2026.py
-----------------------------------
Institutional Feature Extractor for 2025 and 2026 (2025-01-01 to 2026-09-22)
Extracts setups across the entire ~2,367 stock universe with all 79 feature columns
identical to v4_universe_2500_dataset_2019_2024.parquet.
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
from build_universe_fingerprint_dataset import (
    load_sector_map,
    precompute_index_history,
    compute_stock_indicators,
    DB_PATH,
    SECTOR_DB_PATH
)

OUTPUT_PARQUET = os.path.join(SCRIPT_DIR, "v4_universe_2500_dataset_2025_2026.parquet")


def main():
    print("=" * 80)
    print("  QuantSync V4 Universal Feature Extractor for 2025 & 2026")
    print("  Target Period: 2025-01-01 to 2026-09-22 | Across Full Universe")
    print("=" * 80)
    t_start = time.time()

    sector_map = load_sector_map(SECTOR_DB_PATH)
    nifty_dict = precompute_index_history(DB_PATH, "NIFTY100")
    midcap_dict = precompute_index_history(DB_PATH, "NIFTYMIDCAP50")

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
    print(f"[*] Found {total_stocks} stocks in database.")

    analyst = ClassicalPatternAnalystDNA()
    sd = SwingDetector()

    all_dataset_rows = []
    signals_count = 0
    stocks_processed = 0

    for sym_idx, (sym, raw_candles, candle_count) in enumerate(stocks):
        stocks_processed += 1
        if stocks_processed % 300 == 0 or stocks_processed == total_stocks:
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

            # Signal filtering: 2025-01-01 to 2026-09-22
            mask = (
                (df["date"] >= "2025-01-01") &
                (df["date"] <= "2026-09-22") &
                (df["ret1d"] >= 2.5) &
                (df["close"] > df["open"]) &
                (df["volume"] >= 1.3 * df["sma20_vol"]) &
                (df["close_in_range"] >= 0.50) &
                (df.index >= 59) &
                (df.index < len(df) - 1)
            )

            cand_indices = df[mask].index.tolist()
            if not cand_indices:
                continue

            stock_sector = sector_map.get(sym, "Unknown")

            for idx in cand_indices:
                sub_df = df.iloc[: idx + 1]
                sig_bar = sub_df.iloc[-1]
                sig_date = sig_bar["date"]

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

                max_high_20d = float(fwd_window["high"].max()) if len(fwd_window) > 0 else entry_price
                min_low_20d = float(fwd_window["low"].min()) if len(fwd_window) > 0 else entry_price
                close_20d = float(fwd_window.iloc[-1]["close"]) if len(fwd_window) > 0 else entry_price

                mfe_pct = (max_high_20d - entry_price) / entry_price * 100.0
                mae_pct = (min_low_20d - entry_price) / entry_price * 100.0
                ret_20d = (close_20d - entry_price) / entry_price * 100.0

                idx_10 = min(idx + 10, len(df) - 1)
                close_10d = float(df.iloc[idx_10]["close"])
                ret_10d = (close_10d - entry_price) / entry_price * 100.0

                idx_5 = min(idx + 5, len(df) - 1)
                close_5d = float(df.iloc[idx_5]["close"])
                ret_5d = (close_5d - entry_price) / entry_price * 100.0

                label_win_20d = 1 if (hit_target_first and not hit_stop_first) else 0

                if hit_target_first and not hit_stop_first:
                    label_5class = "CLEAN_WIN"
                elif hit_stop_first and not hit_target_first:
                    label_5class = "CLEAN_LOSS"
                elif hit_target_first and hit_stop_first:
                    label_5class = "TARGET_THEN_STOP"
                elif ret_20d > 0:
                    label_5class = "TIMESTOP_GAIN"
                else:
                    label_5class = "TIMESTOP_LOSS"

                # -------------------------------------------------------------
                # Classical Pattern DNA
                # -------------------------------------------------------------
                try:
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
                except Exception:
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

                champ = detect_all_champion_patterns(bars=sub_df, i=len(sub_df) - 1, symbol=sym)
                if champ:
                    has_champ_pattern = 1
                    champ_pat = champ.pattern
                    champ_cir = float(champ.cir)
                    champ_vol_ratio = float(champ.volume_ratio)
                    champ_upper_wick = float(champ.upper_wick)
                    champ_risk = float(champ.risk_pct)
                else:
                    has_champ_pattern = 0
                    champ_pat = "NONE"
                    champ_cir = 0.0
                    champ_vol_ratio = 0.0
                    champ_upper_wick = 0.0
                    champ_risk = 0.0

                is_double_confluence = 1 if (has_classical_pattern == 1 and has_champ_pattern == 1) else 0

                # Fib
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

                # Macro
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

                row_dict = {
                    "symbol": sym,
                    "date": sig_date,
                    "entry_date": entry_date,
                    "entry_price": round(entry_price, 2),
                    "sector": stock_sector,
                    "bar_return_pct": round(float(sig_bar["ret1d"]), 2),
                    "close_in_range": round(float(sig_bar["close_in_range"]), 3),
                    "upper_wick_pct": round(float(sig_bar["upper_wick_pct"]), 3),
                    "lower_tail_pct": round(float(sig_bar["lower_tail_pct"]), 3),
                    "body_to_range_ratio": round(float(sig_bar["body_to_range_ratio"]), 3),
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
                    "has_champ_pattern": has_champ_pattern,
                    "champ_pattern": champ_pat,
                    "champ_cir": round(champ_cir, 3),
                    "champ_vol_ratio": round(champ_vol_ratio, 2),
                    "champ_upper_wick": round(champ_upper_wick, 3),
                    "champ_risk_pct": round(champ_risk, 2),
                    "is_double_confluence": is_double_confluence,
                    "fib_has_structure": fib_has_structure,
                    "fib_impulse_pct": round(fib_impulse_pct, 2),
                    "fib_pullback_pct": round(fib_pullback_pct, 2),
                    "fib_dist_1000_pct": round(fib_dist_1000, 2),
                    "fib_dist_1272_pct": round(fib_dist_1272, 2),
                    "fib_dist_1618_pct": round(fib_dist_1618, 2),
                    "fib_structure_age_weeks": fib_age_weeks,
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
                signals_count += 1

        except Exception as e:
            continue

    print(f"\n[*] Extraction completed in {time.time() - t_start:.1f}s.")
    print(f"[*] Total Setups Extracted for 2025-2026: {len(all_dataset_rows):,}")

    df_out = pd.DataFrame(all_dataset_rows)
    df_out.to_parquet(OUTPUT_PARQUET, index=False, compression="snappy")
    print(f"[+] Saved dataset to {OUTPUT_PARQUET} ({os.path.getsize(OUTPUT_PARQUET)/(1024*1024):.2f} MB)")


if __name__ == "__main__":
    main()
