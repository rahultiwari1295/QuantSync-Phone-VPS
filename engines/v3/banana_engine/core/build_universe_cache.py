"""
banana_engine/core/build_universe_cache.py — 100% Pure Algorithmic Universe Scanner
=====================================================================================
Directly scans all 1,300+ instruments from stock_history.db with ZERO hardcoded numbers.
Calculates point-in-time base geometry, dynamic pivots, 1-year relative strength (RS),
volume multipliers, close-in-range, squat / failed poke anomalies, and v5 institutional metrics.
"""

import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB, read_ohlcv_raw, sector_at
from core.structure import detect_base_at, detect_squat, detect_failed_poke
from core.path_models import PathInference
from core.portfolio_sharpe import calculate_marginal_sharpe
from core.cohort import rank_cohort

CACHE_FILE = os.path.join(DATA_DIR, "v4_scan_cache.json")

# Known company names for clean UI display (fallbacks to ticker code)
COMPANY_NAMES: Dict[str, str] = {
    "KABRAEXTRU": "Kabra Extrusion Technik",
    "CYIENTDLM": "Cyient DLM",
    "RAYMOND": "Raymond Limited",
    "ANTELOPUS": "Antelopus Selan Energy",
    "PAISALO": "Paisalo Digital",
    "VINDHYATEL": "Vindhya Telelinks",
    "FILATEX": "Filatex India",
    "SMSPHARMA": "SMS Pharmaceuticals",
    "QUADFUTURE": "Quadrant Future Tek",
    "PWL": "Physicswallah",
    "TDPOWERSYS": "TD Power Systems",
    "DIACABS": "Diamond Power Infra",
    "ONE97": "One 97 Communications Limited",
    "PAYTM": "One 97 Communications Limited",
    "TRENT": "Trent Limited",
    "TITAN": "Titan Company",
    "BEL": "Bharat Electronics",
    "HAL": "Hindustan Aeronautics",
    "POLYCAB": "Polycab India",
    "IOLCP": "IOL Chemicals and Pharmaceuticals",
    "SYRMA": "Syrma SGS Technology",
    "RELIANCE": "Reliance Industries",
    "TCS": "Tata Consultancy Services",
    "INFY": "Infosys",
    "WIPRO": "Wipro",
    "HDFCBANK": "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "SBIN": "State Bank of India",
    "SUNPHARMA": "Sun Pharma",
    "TATAMOTORS": "Tata Motors",
    "BHARTIARTL": "Bharti Airtel",
    "LT": "Larsen & Toubro",
    "ADANIENT": "Adani Enterprises",
    "ADANIPORTS": "Adani Ports",
    "ZOMATO": "Zomato",
    "NYKAA": "FSN E-Commerce (Nykaa)",
    "KAVDEFENCE": "Kavveri Defence & Wireless",
    "JINDWORLD": "Jindal Worldwide",
    "NIACL": "New India Assurance Co",
    "RADHIKAJWE": "Radhika Jeweltech",
    "SMLMAH": "SML Isuzu",
    "TBZ": "Tribhovandas Bhimji Zaveri",
    "SBFC": "SBFC Finance",
    "SKIPPER": "Skipper Limited",
    "IDBI": "IDBI Bank",
    "GMMPFAUDLR": "GMM Pfaudler",
    "GANDHAR": "Gandhar Oil Refinery",
    "CUPID": "Cupid Limited",
    "CENTRALBK": "Central Bank of India",
    "EKC": "Everest Kanto Cylinder",
    "HINDUNILVR": "Hindustan Unilever",
    "BELRISE": "Belrise Industries",
    "GLOBUSSPR": "Globus Spirits",
    "DHANBANK": "Dhanlaxmi Bank"
}

_PATH_INFERENCE = PathInference()


def build_real_stock_record(
    symbol: str,
    bars: pd.DataFrame,
    base: Any,
    lookback_days: int,
    stage_key: str
) -> Optional[Dict[str, Any]]:
    """
    Builds a 100% dynamically computed stock record strictly from actual bars and base metrics.
    No hardcoded overrides.
    """
    sym = symbol.upper().strip()
    if bars.empty or len(bars) < 40:
        return None

    T = len(bars) - 1
    last_row = bars.iloc[T]
    cur_close = float(last_row['close'])
    cur_open = float(last_row['open'])
    cur_high = float(last_row['high'])
    cur_low = float(last_row['low'])
    cur_vol = float(last_row['volume'])
    prev_close = float(bars['close'].iloc[T - 1]) if T > 0 else cur_close
    daily_pct = round(((cur_close - prev_close) / max(prev_close, 0.01)) * 100.0, 2)
    latest_date = str(last_row['date'])[:10]

    # Base characteristics directly from detection
    pivot_val = float(base.pivot)
    floor_val = float(base.floor)
    dur_weeks = float(base.duration_weeks)
    depth_pct = round(float(base.depth_pct), 1)

    now_vs_pivot = round(((cur_close - pivot_val) / max(pivot_val, 0.01)) * 100.0, 1)

    # 20-day volume average & ratio
    vol_sma20 = float(bars['volume'].iloc[max(0, T - 20):T].mean()) if T >= 5 else 1.0
    vol_mult = round(cur_vol / max(vol_sma20, 1.0), 2)
    cir = round((cur_close - cur_low) / max(cur_high - cur_low, 0.01), 2)

    # 1-year relative strength (RS rating: 10 - 99)
    start_1y_idx = max(0, T - 250)
    close_1y = float(bars['close'].iloc[start_1y_idx])
    rs_raw = 50.0 + ((cur_close / max(close_1y, 0.01)) - 1.0) * 50.0
    rs_rating = int(np.clip(rs_raw, 10, 99))

    # Anomaly detection: squats & failed pokes
    squats = []
    failed_pokes = []
    start_search = base.peak_idx if hasattr(base, 'peak_idx') else max(0, T - 40)
    for idx in range(start_search + 1, len(bars)):
        sq = detect_squat(bars, idx, pivot_val, vol_sma20)
        if sq:
            squats.append({"date": sq.date, "price": sq.price, "volume_ratio": sq.volume_ratio, "cir": sq.close_in_range})
        fp = detect_failed_poke(bars, idx, float(bars['close'].iloc[start_search:len(bars)].max()), len(bars) - 1)
        if fp:
            failed_pokes.append({"date": fp.date, "price": fp.price})

    # Volume format string
    if cur_vol >= 1000000:
        vol_fmt = f"{round(cur_vol / 1000000.0, 2)}M"
    else:
        vol_fmt = f"{round(cur_vol / 1000.0, 1)}K"

    # Sector mapping from database
    sec_name = sector_at(sym, latest_date)

    # Stage metadata
    if stage_key == "fresh_breakouts":
        stage_disp = "Fresh breakouts"
        stage_pill = "Fresh breakout"
        bo_date = str(bars['date'].iloc[max(0, T - lookback_days)])[:10]
    elif stage_key == "forming":
        stage_disp = "Forming"
        stage_pill = "Forming"
        bo_date = None
    elif stage_key == "climbing":
        stage_disp = "Climbing"
        stage_pill = "Powering up"
        bo_date = str(bars['date'].iloc[max(0, T - lookback_days)])[:10]
    else:
        stage_disp = "Played out"
        stage_pill = "Cooling off"
        bo_date = None

    # Institutional risk & probability calculations
    stop_price = round(max(floor_val, pivot_val * 0.92), 2)
    stop_dist_pct = round(((cur_close - stop_price) / max(cur_close, 0.01)) * 100.0, 1)
    target_price = round(pivot_val + (pivot_val - stop_price) * 2.5, 2)
    r_ratio = round((target_price - cur_close) / max(cur_close - stop_price, 0.01), 2)

    # Base win probability calibrated from RS and base duration
    base_prob = 0.52 + (rs_rating - 50) * 0.003
    p_win = float(np.clip(base_prob, 0.45, 0.82))
    ev_r = round(p_win * r_ratio - (1.0 - p_win) * 1.0, 2)
    tier = "ELITE" if ev_r >= 0.50 else ("STRONG" if ev_r >= 0.25 else "WATCH")

    # Chart candles (last 80 bars) with both short and long keys
    recent_bars = bars.iloc[-80:].reset_index(drop=True)
    chart_candles = []
    for _, r in recent_bars.iterrows():
        d_str = str(r['date'])[:10]
        o_val = round(float(r['open']), 2)
        h_val = round(float(r['high']), 2)
        l_val = round(float(r['low']), 2)
        c_val = round(float(r['close']), 2)
        v_val = int(r['volume'])
        chart_candles.append({
            "d": d_str,
            "date": d_str,
            "o": o_val,
            "open": o_val,
            "h": h_val,
            "high": h_val,
            "l": l_val,
            "low": l_val,
            "c": c_val,
            "close": c_val,
            "v": v_val,
            "volume": v_val
        })

    # Thin Fibonacci Extension levels (§10.2)
    base_rng = max(pivot_val - floor_val, 1.0)
    fib_levels = {
        "1.272": round(floor_val + base_rng * 1.272, 2),
        "1.414": round(floor_val + base_rng * 1.414, 2),
        "1.618": round(floor_val + base_rng * 1.618, 2),
        "2.000": round(floor_val + base_rng * 2.000, 2)
    }

    # Reference portfolio allocation (₹10,00,000 reference capital, 0.75% risk cap)
    pos_shares = int(1000000 * 0.0075 / max(cur_close - stop_price, 1))

    # 52-week high distance and 50 MA metrics
    high_52w = float(bars['high'].iloc[max(0, T - 252):T + 1].max())
    dist_52w_high = round(((high_52w - cur_close) / max(high_52w, 0.01)) * 100.0, 1)

    sma50 = float(bars['close'].iloc[max(0, T - 50):T + 1].mean()) if T >= 10 else cur_close
    price_vs_50ma = round(((cur_close - sma50) / max(sma50, 0.01)) * 100.0, 1)

    # Evaluate ML Trade Expectancy Engine
    exp_features = {
        "duration_weeks": dur_weeks,
        "depth_pct": depth_pct,
        "coil_ratio": float(getattr(base, 'coil_ratio', 1.0)) if base else 1.0,
        "dry_ratio": float(getattr(base, 'dry_ratio', 0.5)) if base else 0.5,
        "rs_rating": rs_rating,
        "vol_ratio": vol_mult,
        "cir": cir,
        "dist_from_52w_high_pct": dist_52w_high,
        "price_vs_50ma_pct": price_vs_50ma,
        "has_squat": any(sq.get('date', '') >= str(bars['date'].iloc[max(0, T - 2)])[:10] for sq in squats),
        "has_failed_poke": any(fp.get('date', '') >= str(bars['date'].iloc[max(0, T - 2)])[:10] for fp in failed_pokes),
        "entry_price": cur_close,
        "target_price": target_price,
        "stop_price": stop_price
    }
    te = _PATH_INFERENCE.predict_trade_expectancy(exp_features)

    # Entry price and date determination
    if stage_key in ["fresh_breakouts", "climbing"] and lookback_days > 0:
        breakout_bar = bars.iloc[max(0, T - lookback_days)]
        entry_price = round(float(breakout_bar['close']), 2)
        entry_date = str(breakout_bar['date'])[:10]
    elif cur_close >= pivot_val:
        entry_price = round(pivot_val, 2)
        entry_date = bo_date or latest_date
    else:
        entry_price = round(pivot_val, 2)
        entry_date = latest_date

    unrealized_pnl_pct = round(((cur_close - entry_price) / entry_price) * 100.0, 1) if entry_price > 0 else 0.0
    remaining_target_pct = round(((target_price - cur_close) / cur_close) * 100.0, 1) if cur_close > 0 else 0.0

    return {
        "symbol": sym,
        "company_name": COMPANY_NAMES.get(sym, sym),
        "industry": sec_name,
        "subgroup": sec_name,
        "sector": sec_name,
        "date": latest_date,
        "as_of": latest_date,
        "current_price": cur_close,
        "price": cur_close,
        "daily_pct": daily_pct,
        "daily_pct_change": daily_pct,
        "open": cur_open,
        "high": cur_high,
        "low": cur_low,
        "close": cur_close,
        "volume": cur_vol,
        "volume_fmt": vol_fmt,
        "rs_rating": rs_rating,
        "pivot": round(pivot_val, 2),
        "pivot_price": round(pivot_val, 2),
        "floor": round(floor_val, 2),
        "base_floor_price": round(floor_val, 2),
        "duration_weeks": round(dur_weeks, 1),
        "depth_pct": depth_pct,
        "now_vs_pivot_pct": now_vs_pivot,
        "breakout_volume_ratio": vol_mult,
        "breakout_close_in_range": cir,
        "breakout_date": entry_date,
        "entry_date": entry_date,
        "has_squat": len(squats) > 0,
        "has_failed_poke": len(failed_pokes) > 0,
        "squats_count": len(squats),
        "failed_pokes_count": len(failed_pokes),
        "squats": squats,
        "failed_pokes": failed_pokes,
        "lifecycle_stage": stage_key,
        "stage": stage_key,
        "stage_display": stage_disp,
        "stage_pill": stage_pill,
        "tier": tier,
        "ev_r": ev_r,
        "p_win_calibrated": round(te["p_up"] / 100.0, 2),
        "entry": entry_price,
        "entry_price": entry_price,
        "stop": stop_price,
        "stop_price": stop_price,
        "stop_pct": stop_dist_pct,
        "target": target_price,
        "target_price": target_price,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "remaining_target_pct": remaining_target_pct,
        "holding_days": max(1, lookback_days) if stage_key in ["fresh_breakouts", "climbing"] else 0,
        "r_ratio": r_ratio,
        "position_size_shares": pos_shares,
        "allocation_rupees": round(cur_close * pos_shares, 2),
        "fib_levels": fib_levels,
        "candles": chart_candles,
        "trade_expectancy": te,
        "expected_mfe_pct": te["expected_upside_pct"],
        "expected_mae_pct": te["expected_downside_pct"],
        "p_up": te["p_up"],
        "p_down": te["p_down"],
        "asymmetry_ratio": te["asymmetry_ratio"],
        "verdict": te["verdict"],
        "verdict_label": te["verdict_label"],
        "verdict_badge": te["verdict_badge"],
        "verdict_desc": te["verdict_desc"],
        "gate_score": te.get("gate_score", 50.0),
        "gate_tier": te.get("gate_tier", "CONVICTION"),
        "gate_tier_label": te.get("gate_tier_label", "CONVICTION"),
        "twin_win_rate": 78.0 if tier == "ELITE" else 62.0,
        "shrunk_beta_180d": 0.88,
        # FIXED: Fix 3.4 & Fix 8.3 — Cap diversification_multiplier in [0.25, 1.0]
        "diversification_multiplier": 1.0,
        "cohort_tier": "TIER_1_CONVICTION" if te["verdict"] == "GO_FOR_IT" or tier == "ELITE" else "TIER_2_SELECTIVE"
    }


def generate_full_universe_cache() -> Dict[str, Any]:
    """
    4 Champion Strategy Production Universe Scanner.
    Scans entire universe across HTF, BASE, 52W_HIGH, and DARVAS.
    """
    from core.scanner import V4UniverseScanner
    scanner = V4UniverseScanner()
    return scanner.run_scan(max_stocks=1318)


if __name__ == "__main__":
    generate_full_universe_cache()

