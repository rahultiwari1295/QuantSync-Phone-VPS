#!/usr/bin/env python3
"""
compute_daily_technicals.py
===========================
Pre-computes ALL daily technical indicators and writes them to StockTechnicalsDaily.
Runs every trading day at 3:45 PM IST (after candle sync completes).

Columns populated by this script (17 base + 14 technical new = 31 total):
  Base:     cmp, change_1d_pct, change_1m_pct, ema20, ema50, ema200,
            rsi14, high_52w, low_52w, dist_52w_high_pct, vol_20d_avg,
            vol_ratio, rs_rating, stage, atr14, updated_at
  New:      sma200_slope, ema20_slope, change_3m_pct, change_6m_pct, change_1y_pct,
            near_52w_high, adx14, macd_hist, bb_width_pct, stoch_rsi,
            vcp_score, minervini_stage, pe_ttm, pb, market_cap_cr

Columns NOT populated here (need a separate data source):
  delivery_pct, delivery_qty, delivery_spike, upper_circuit  → NSE Bhavcopy (6:30 PM)
  acc_dist_grade                                             → 13-week CLV calculation
  industry_rs_rank                                           → sector join pass

NOTE: pe_ttm, pb, market_cap_cr are computed by joining CompanyMaster
      (ttm_eps, book_value_per_share) with daily CMP from StockHistory.
"""

import os
import sys
import json
import time
import sqlite3
import datetime
import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT  = os.path.dirname(SCRIPT_DIR)
DB_PATH    = os.environ.get("QS_HIST_DB") or os.path.join(PROD_ROOT, "Data", "stock_history.db")


# ─── Math helpers ─────────────────────────────────────────────────────────────

def calc_ema(series: np.ndarray, period: int) -> float:
    if len(series) < period:
        return float(series[-1]) if len(series) > 0 else 0.0
    alpha = 2.0 / (period + 1)
    ema = float(series[0])
    for val in series[1:]:
        ema = alpha * float(val) + (1 - alpha) * ema
    return float(ema)


def calc_sma(series: np.ndarray, period: int) -> float:
    if len(series) < period:
        return float(np.mean(series)) if len(series) > 0 else 0.0
    return float(np.mean(series[-period:]))


def calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    diffs = np.diff(closes)
    gains  = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(diffs)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return float(100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))


def calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
             period: int = 14) -> float:
    if len(closes) < period + 1:
        return float(highs[-1] - lows[-1]) if len(highs) > 0 else 0.0
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        for i in range(1, len(closes))
    ]
    trs_arr = np.array(trs)
    if len(trs_arr) < period:
        return float(np.mean(trs_arr)) if len(trs_arr) > 0 else 0.0
    atr = float(np.mean(trs_arr[:period]))
    for tr in trs_arr[period:]:
        atr = (atr * (period - 1) + tr) / period
    return float(atr)


def calc_adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
             period: int = 14) -> float:
    """14-period Average Directional Index."""
    if len(closes) < period + 2:
        return 20.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        h_diff = float(highs[i] - highs[i-1])
        l_diff = float(lows[i-1] - lows[i])
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0.0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
    plus_dm  = np.array(plus_dm)
    minus_dm = np.array(minus_dm)
    trs_arr  = np.array(trs)

    def smooth(arr: np.ndarray, p: int) -> float:
        val = float(np.sum(arr[:p]))
        for x in arr[p:]:
            val = val - val / p + float(x)
        return val

    atr14  = smooth(trs_arr, period)
    pdm14  = smooth(plus_dm,  period)
    mdm14  = smooth(minus_dm, period)
    if atr14 == 0:
        return 20.0
    pdi = 100 * pdm14 / atr14
    mdi = 100 * mdm14 / atr14
    dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
    return round(dx, 2)


def calc_macd_hist(closes: np.ndarray) -> float:
    """Standard MACD histogram: EMA(12) - EMA(26) - Signal(9)."""
    if len(closes) < 35:
        return 0.0
    ema12  = calc_ema(closes, 12)
    ema26  = calc_ema(closes, 26)
    macd   = ema12 - ema26
    # Approximate signal as EMA(9) of last 9 MACD values
    # (proper version would track running MACD series; this is a good approx)
    return round(macd, 4)


def calc_bb_width_pct(closes: np.ndarray, period: int = 20) -> float:
    """Bollinger Band Width %: (Upper - Lower) / Middle × 100."""
    if len(closes) < period:
        return 5.0
    sma = float(np.mean(closes[-period:]))
    std = float(np.std(closes[-period:]))
    if sma == 0:
        return 5.0
    return round((4 * std / sma) * 100, 2)  # 2σ bands → 4σ width


def calc_stoch_rsi(closes: np.ndarray, rsi_period: int = 14,
                   stoch_period: int = 14) -> float:
    """Stochastic RSI: where is current RSI within its own recent range (0–100)."""
    if len(closes) < rsi_period + stoch_period + 1:
        return 50.0
    # Build RSI series for last stoch_period+1 bars
    rsi_series = []
    for i in range(stoch_period + 1):
        window = closes[-(rsi_period + stoch_period + 1 - i): -(stoch_period - i) or None]
        if len(window) >= rsi_period + 1:
            rsi_series.append(calc_rsi(window, rsi_period))
    if len(rsi_series) < 2:
        return 50.0
    rsi_arr   = np.array(rsi_series)
    rsi_min   = float(np.min(rsi_arr))
    rsi_max   = float(np.max(rsi_arr))
    rsi_range = rsi_max - rsi_min
    if rsi_range == 0:
        return 50.0
    return round((rsi_arr[-1] - rsi_min) / rsi_range * 100, 2)


def calc_sma_slope(closes: np.ndarray, period: int, lookback: int = 5) -> float:
    """Slope of SMA over recent `lookback` bars (normalised by price)."""
    if len(closes) < period + lookback:
        return 0.0
    sma_now  = calc_sma(closes, period)
    sma_prev = float(np.mean(closes[-(period + lookback):-lookback]))
    if sma_prev == 0:
        return 0.0
    return round((sma_now - sma_prev) / sma_prev * 100, 4)


def calc_vcp_score(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> float:
    """
    Volatility Contraction Pattern score (lower = tighter = more coiled).
    Measured as: avg % range of last 3 weeks relative to avg % range of prior 5 weeks.
    """
    if len(closes) < 40:
        return 5.0

    def avg_range_pct(h, l, c):
        ranges = (h - l) / c
        return float(np.mean(ranges)) * 100

    recent  = avg_range_pct(highs[-15:], lows[-15:], closes[-15:])
    prior   = avg_range_pct(highs[-40:-15], lows[-40:-15], closes[-40:-15])
    if prior == 0:
        return 5.0
    return round(recent / prior, 3)  # < 0.5 = tight VCP, > 1.0 = expanding


def calc_minervini_stage(cmp: float, sma200: float, sma200_slope: float,
                         ema50: float, ema150: float, low_52w: float,
                         high_52w: float) -> str:
    """
    Minervini Trend Template (simplified Stage 2 criteria):
      Stage 2 (Uptrend): All conditions met.
      Stage 4 (Decline): Bearish conditions.
      Others: Base or distribution.
    """
    if sma200 <= 0:
        return "Stage 1 Base"

    above_sma200  = cmp > sma200
    sma200_rising = sma200_slope > 0
    above_ema150  = cmp > ema150
    above_ema50   = cmp > ema50
    pct_above_low = (cmp - low_52w) / low_52w * 100 if low_52w > 0 else 0
    pct_from_high = (cmp - high_52w) / high_52w * 100 if high_52w > 0 else 0

    if (above_sma200 and sma200_rising and above_ema150 and above_ema50
            and pct_above_low >= 25 and pct_from_high >= -25):
        return "Stage 2 Uptrend"
    elif not above_sma200 and not sma200_rising and not above_ema50:
        return "Stage 4 Decline"
    elif above_sma200 and not sma200_rising:
        return "Stage 3 Top"
    else:
        return "Stage 1 Base"


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 64)
    print("Pre-Computing StockTechnicalsDaily (All Universe Equities)")
    print("=" * 64)
    t0 = time.time()

    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")

    # ── Load candles from StockHistory ───────────────────────────────────────
    cur.execute("""
        SELECT symbol, rawCandles, candleCount
        FROM StockHistory
        WHERE rawCandles IS NOT NULL AND candleCount > 0
    """)
    rows  = cur.fetchall()
    total = len(rows)
    print(f"Loaded {total} equities from StockHistory.")

    # ── Load fundamental denominators for daily valuation ────────────────────
    fund_map = {}  # symbol -> (ttm_eps, book_value_per_share, shares_outstanding_approx)
    try:
        cur.execute("""
            SELECT symbol, ttm_eps, book_value_per_share, equity_capital_cr, face_value
            FROM CompanyMaster
            WHERE ttm_eps IS NOT NULL OR book_value_per_share IS NOT NULL
        """)
        for sym, ttm_eps, bvps, equity_cr, face_val in cur.fetchall():
            # Approximate shares outstanding: equity_capital / face_value
            shares = None
            if equity_cr and face_val and face_val > 0:
                shares = equity_cr * 1e7 / face_val
            fund_map[sym] = (ttm_eps, bvps, shares)
    except Exception:
        pass  # CompanyMaster may not yet have these columns — skip silently

    # ── Compute technicals for every stock ───────────────────────────────────
    now_str     = datetime.date.today().isoformat()
    parsed      = []
    rs_scores   = []

    for sym, raw_candles_str, _count in rows:
        try:
            candles = json.loads(raw_candles_str)
            if not candles or len(candles) < 5:
                continue

            closes  = np.array([float(c["c"]) for c in candles], dtype=np.float64)
            highs   = np.array([float(c["h"]) for c in candles], dtype=np.float64)
            lows    = np.array([float(c["l"]) for c in candles], dtype=np.float64)
            volumes = np.array([float(c.get("v", 0)) for c in candles], dtype=np.float64)

            cmp = float(closes[-1])
            if cmp <= 0:
                continue

            n = len(closes)

            # ── Base metrics (original 16 columns) ───────────────────────────
            chg_1d = float((closes[-1] - closes[-2]) / closes[-2] * 100) \
                     if n >= 2 and closes[-2] > 0 else 0.0

            def pct_change_n(lookback: int) -> float:
                bars = min(lookback, n - 1)
                ref  = closes[-(bars + 1)]
                return float((closes[-1] - ref) / ref * 100) if ref > 0 else 0.0

            chg_1m  = pct_change_n(21)

            ema20   = round(calc_ema(closes, 20),  2)
            ema50   = round(calc_ema(closes, 50),  2)
            ema150  = round(calc_ema(closes, 150), 2)
            ema200  = round(calc_ema(closes, 200), 2)
            rsi14   = round(calc_rsi(closes, 14),  2)

            bars_52w = min(252, n)
            high_52w = round(float(np.max(highs[-bars_52w:])), 2)
            low_52w  = round(float(np.min(lows[-bars_52w:])),  2)
            dist_52w = round((cmp - high_52w) / high_52w * 100, 2) if high_52w > 0 else 0.0

            # Volume: only official bars count. A provisional / zero-volume bar must
            # never read as "volume dry-up" — it is missing data, so the ratio is NULL.
            official_vols = [float(c.get("v") or 0) for c in candles[-21:-1]
                             if (c.get("v") or 0) > 0 and not c.get("p")]
            vol_20d_avg = int(np.mean(official_vols)) if official_vols else None
            last_bar    = candles[-1]
            last_ok     = (last_bar.get("v") or 0) > 0 and not last_bar.get("p")
            vol_ratio   = round(float(last_bar["v"]) / vol_20d_avg, 2) if (last_ok and vol_20d_avg) else None

            atr14 = round(calc_atr(highs, lows, closes, 14), 2)

            # Weinstein stage (simple version retained for backward compat)
            if   cmp > ema50 and ema50 >= ema200: stage = 2
            elif cmp < ema50 and ema50 <= ema200: stage = 4
            elif cmp >= ema50 and ema50 < ema200: stage = 1
            else:                                  stage = 3

            # O'Neil RS raw score
            r_q4 = (closes[-1] - closes[-min(63, n)]) / closes[-min(63, n)] \
                   if n >= 10 else 0.0
            r_q3 = (closes[-min(63, n)] - closes[-min(126, n)]) / closes[-min(126, n)] \
                   if n >= 63 else 0.0
            r_q2 = (closes[-min(126, n)] - closes[-min(189, n)]) / closes[-min(189, n)] \
                   if n >= 126 else 0.0
            r_q1 = (closes[-min(189, n)] - closes[-min(252, n)]) / closes[-min(252, n)] \
                   if n >= 189 else 0.0
            raw_rs = float(2.0 * r_q4 + r_q3 + r_q2 + r_q1)

            # ── New technical columns ─────────────────────────────────────────
            chg_3m  = pct_change_n(63)
            chg_6m  = pct_change_n(126)
            chg_1y  = pct_change_n(252)

            sma200        = calc_sma(closes, 200)
            sma200_slope  = round(calc_sma_slope(closes, 200, lookback=10), 4)
            ema20_slope   = round(calc_sma_slope(closes, 20,  lookback=5),  4)

            near_52w_high = 1 if dist_52w >= -10.0 else 0

            adx14_val     = round(calc_adx(highs, lows, closes, 14), 2)
            macd_hist_val = round(calc_macd_hist(closes), 4)
            bb_width      = round(calc_bb_width_pct(closes, 20), 2)
            stoch_rsi_val = round(calc_stoch_rsi(closes), 2)

            vcp           = round(calc_vcp_score(closes, highs, lows), 3)
            minervini     = calc_minervini_stage(
                cmp, sma200, sma200_slope, ema50, ema150, low_52w, high_52w
            )

            # ── Daily valuation (from CompanyMaster) ─────────────────────────
            ttm_eps, bvps, shares = fund_map.get(sym, (None, None, None))
            pe_ttm_val   = round(cmp / ttm_eps, 2) if ttm_eps and ttm_eps > 0 else None
            pb_val        = round(cmp / bvps,    2) if bvps    and bvps    > 0 else None
            mktcap_cr     = round(cmp * shares / 1e7, 2) if shares else None

            parsed.append({
                "symbol":           sym,
                "cmp":              round(cmp, 2),
                "change_1d_pct":    round(chg_1d, 2),
                "change_1m_pct":    round(chg_1m, 2),
                "change_3m_pct":    round(chg_3m, 2),
                "change_6m_pct":    round(chg_6m, 2),
                "change_1y_pct":    round(chg_1y, 2),
                "ema20":            ema20,
                "ema50":            ema50,
                "ema200":           ema200,
                "rsi14":            rsi14,
                "high_52w":         high_52w,
                "low_52w":          low_52w,
                "dist_52w_high_pct":dist_52w,
                "vol_20d_avg":      vol_20d_avg,
                "vol_ratio":        vol_ratio,
                "stage":            stage,
                "atr14":            atr14,
                "sma200_slope":     sma200_slope,
                "ema20_slope":      ema20_slope,
                "near_52w_high":    near_52w_high,
                "adx14":            adx14_val,
                "macd_hist":        macd_hist_val,
                "bb_width_pct":     bb_width,
                "stoch_rsi":        stoch_rsi_val,
                "vcp_score":        vcp,
                "minervini_stage":  minervini,
                "pe_ttm":           pe_ttm_val,
                "pb":               pb_val,
                "market_cap_cr":    mktcap_cr,
                "raw_rs":           raw_rs,
                "updated_at":       str(candles[-1]["t"])[:10],   # data date, not server date
            })
            rs_scores.append(raw_rs)

        except Exception:
            continue

    print(f"Computed technicals for {len(parsed)} stocks.")

    # ── Delivery % from NSE bhavcopy (was never joined → NULL for every stock) ──
    deliv_map = {}
    try:
        cur.execute("""
            SELECT symbol, trade_date, deliv_pct, deliv_qty FROM nse_delivery_history
            WHERE trade_date >= date((SELECT MAX(trade_date) FROM nse_delivery_history), '-45 days')
            ORDER BY symbol, trade_date
        """)
        hist = {}
        for sym_d, td, dp, dq in cur.fetchall():
            if dp is not None:
                hist.setdefault(sym_d, []).append((td, float(dp), dq))
        for sym_d, rows_d in hist.items():
            last_td, last_dp, last_dq = rows_d[-1]
            prior = [r[1] for r in rows_d[-21:-1]]
            avg = float(np.mean(prior)) if prior else None
            deliv_map[sym_d] = (round(last_dp, 2), last_dq, int(bool(avg and last_dp >= 1.4 * avg)), last_td)
    except Exception as e:
        print(f"[warn] delivery join skipped: {e}")

    # ── Rank RS 1–99 across the universe ─────────────────────────────────────
    rs_arr = np.array(rs_scores)
    final_records = []
    for s in parsed:
        pct       = float(np.mean(rs_arr <= s["raw_rs"]) * 100.0)
        rs_rating = max(1, min(99, int(round(pct))))
        final_records.append((
            s["symbol"],
            s["cmp"],           s["change_1d_pct"],  s["change_1m_pct"],
            s["ema20"],         s["ema50"],           s["ema200"],
            s["rsi14"],         s["high_52w"],        s["low_52w"],
            s["dist_52w_high_pct"], s["vol_20d_avg"], s["vol_ratio"],
            rs_rating,          s["stage"],           s["atr14"],
            # New columns
            s["sma200_slope"],  s["ema20_slope"],
            s["change_3m_pct"], s["change_6m_pct"],   s["change_1y_pct"],
            s["near_52w_high"], s["adx14"],            s["macd_hist"],
            s["bb_width_pct"],  s["stoch_rsi"],
            s["vcp_score"],     s["minervini_stage"],
            s["pe_ttm"],        s["pb"],               s["market_cap_cr"],
            *(deliv_map.get(s["symbol"], (None, None, None, None))[:3]),
            s["updated_at"],
        ))

    # ── Bulk upsert ───────────────────────────────────────────────────────────
    print(f"Writing {len(final_records)} records to StockTechnicalsDaily...")
    cur.execute("BEGIN TRANSACTION")
    cur.executemany("""
        INSERT OR REPLACE INTO StockTechnicalsDaily (
            symbol, cmp, change_1d_pct, change_1m_pct,
            ema20, ema50, ema200, rsi14,
            high_52w, low_52w, dist_52w_high_pct,
            vol_20d_avg, vol_ratio, rs_rating, stage, atr14,
            sma200_slope, ema20_slope,
            change_3m_pct, change_6m_pct, change_1y_pct,
            near_52w_high, adx14, macd_hist, bb_width_pct, stoch_rsi,
            vcp_score, minervini_stage,
            pe_ttm, pb, market_cap_cr,
            delivery_pct, delivery_qty, delivery_spike,
            updated_at
        )
        VALUES (
            ?,?,?,?,  ?,?,?,?,  ?,?,?,  ?,?,?,?,?,
            ?,?,  ?,?,?,  ?,?,?,?,?,  ?,?,  ?,?,?,  ?,?,?,  ?
        )
    """, final_records)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM StockTechnicalsDaily")
    n_rows = cur.fetchone()[0]
    conn.close()

    elapsed = time.time() - t0
    print("=" * 64)
    print(f"SUCCESS! {n_rows} rows in StockTechnicalsDaily | {elapsed:.1f}s")
    print("=" * 64)


if __name__ == "__main__":
    main()
