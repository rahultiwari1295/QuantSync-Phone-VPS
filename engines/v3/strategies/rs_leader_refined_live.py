"""
strategies/active/strat_rs_leader_refined/strategy.py — RS Leader Refined (1D)
==============================================================================
Capsule Layer: Cross-sectional momentum leader strategy requiring relative strength
over 60 sessions vs NIFTY 100 to rank in the top 5% across the entire universe,
combined with a 20-day price resistance challenge in Stage 2 trend.
"""

from typing import Dict, Any, Optional, Tuple
import numpy as np

STRATEGY_CONFIG = {
    "id": "RelativeStrengthLeaderRefined_1D",
    "display_name": "RS Leader Refined (Apex Enhanced)",
    "family": "CROSS_SECTIONAL",
    "sleeve": "sleeve2",
    "precedence": 3,
    "min_price": 40.0,
    "min_daily_turnover": 15_000_000.0,
    "max_daily_turnover": 2_500_000_000.0,  # 250 Cr turnover cap to avoid sluggish mega caps
    "require_stage2_trend": True,
    # Core quality filters directly in strategy config
    "min_volume_ratio": 2.50,
    "min_close_in_range": 0.75,
    "max_upper_wick_pct": 0.20,
    "min_bar_return_pct": 4.0,
    "min_chase_pct": 1.5,
    "max_chase_pct": 8.0,
    "min_prior_runup_60_pct": 15.0,
    "max_ath_dist_pct": 20.0,
    "max_dist_sma200_pct": 30.0,
    "min_stop_distance_pct": 8.0,
    "exits": {
        "target_r_multiple": 2.5,
        "min_holding_days": 3,
        "max_holding_days": 60,
        "trailing_rule": "TRAILING_EMA20_LOCK",
    },
}

METADATA = STRATEGY_CONFIG


def evaluate(sym: str, candles: list, ind: dict, idx: int, **ctx) -> Optional[Dict[str, Any]]:
    if idx < 60:
        return None

    c = float(candles[idx]["c"])
    o = float(candles[idx]["o"])
    h_val = float(candles[idx]["h"])
    l_val = float(candles[idx]["l"])
    v = float(candles[idx].get("v", 0.0))
    d = candles[idx]["t"][:10]

    # 1. Liquidity & Price bounds (reject illiquid penny stocks and sluggish mega caps)
    turnover = c * v
    if c < STRATEGY_CONFIG["min_price"] or turnover < STRATEGY_CONFIG["min_daily_turnover"]:
        return None
    if turnover > STRATEGY_CONFIG.get("max_daily_turnover", 2_500_000_000.0):
        return None

    # 2. Bullish candle required (reject red/bearish body on breakout day)
    if c <= o:
        return None

    # 3. Breakout candle expansion (% return on signal day)
    prev_c = float(candles[idx - 1]["c"]) if idx > 0 else c
    if prev_c <= 0:
        return None
    bar_ret = ((c / prev_c) - 1.0) * 100.0
    if bar_ret < STRATEGY_CONFIG.get("min_bar_return_pct", 4.0):
        return None

    # 4. Close-in-range (CIR) & Upper Wick guards: Reject selling wicks and squats
    bar_range = max(h_val - l_val, 0.01)
    cir = (c - l_val) / bar_range
    if cir < STRATEGY_CONFIG.get("min_close_in_range", 0.75):
        return None

    upper_wick_pct = (h_val - max(c, o)) / bar_range
    if upper_wick_pct > STRATEGY_CONFIG.get("max_upper_wick_pct", 0.20):
        return None

    # 5. Breakout Volume Surge: Require institutional sponsorship (min 2.5x 20-day SMA)
    if idx >= 20:
        v_prior = [float(candles[k].get("v", 0.0)) for k in range(idx - 20, idx)]
        avg_v = float(np.mean(v_prior)) if v_prior else v
        vol_ratio = (v / avg_v) if avg_v > 0 else 1.0
        if vol_ratio < STRATEGY_CONFIG.get("min_volume_ratio", 2.50):
            return None
    else:
        vol_ratio = 1.0

    # 6. Trend: C > EMA50 > EMA200
    ema50 = float(ind["ema50"][idx])
    ema200 = float(ind["ema200"][idx])
    if c <= ema50 or ema50 <= ema200:
        return None

    # 7. Resistance Clearance & Chase Guard:
    # Must clear 20-day resistance with decisive clearance (1.5% to 8.0%)
    h_20 = max(float(candles[k]["h"]) for k in range(idx - 20, idx))
    if c < h_20:
        return None

    chase_pct = ((c - h_20) / h_20) * 100.0
    if chase_pct < STRATEGY_CONFIG.get("min_chase_pct", 1.5):
        return None
    if chase_pct > STRATEGY_CONFIG.get("max_chase_pct", 8.0):
        return None

    # 8. Distance above SMA200 (parabolic exhaustion check)
    dist_sma200 = ((c / ema200) - 1.0) * 100.0 if ema200 > 0 else 0.0

    # 9. Loser Traps Check
    if chase_pct <= 1.0 and upper_wick_pct > 0.18:
        return None
    if chase_pct <= 1.0 and vol_ratio < 2.0:
        return None
    if dist_sma200 > STRATEGY_CONFIG.get("max_dist_sma200_pct", 30.0) and upper_wick_pct > 0.20:
        return None

    # 10. Proximity to 52-Week High (ATH distance check)
    h_52w = max(float(candles[k]["h"]) for k in range(max(0, idx - 250), idx + 1))
    ath_dist_pct = ((h_52w - c) / h_52w) * 100.0 if h_52w > 0 else 0.0
    if ath_dist_pct > STRATEGY_CONFIG.get("max_ath_dist_pct", 20.0):
        return None

    # 11. Prior 60-Day Runup Check (institutional momentum foundation)
    c_60ago = float(candles[idx - 60]["c"])
    if c_60ago <= 0:
        return None
    stock_ret60 = ((c / c_60ago) - 1.0) * 100.0
    if stock_ret60 < STRATEGY_CONFIG.get("min_prior_runup_60_pct", 15.0):
        return None

    # 12. Relative Strength vs NIFTY 100
    nifty_map = ctx.get("nifty_map", {})
    nifty_ret60 = float(nifty_map.get(d, {}).get("ret_60d", 0.0))
    rs_60d = stock_ret60 - nifty_ret60

    # Cross-sectional check: RS_60d must be in top 5% across all stocks on this date
    all_bundles = ctx.get("all_bundles", {})
    cache = ctx.get("cache", {})
    cache_key = f"rs_95pct_{d}"

    if cache_key not in cache and all_bundles:
        date_rs = []
        for s_sym, bnd in all_bundles.items():
            k = bnd.get("date_to_idx", {}).get(d)
            if k is not None and k >= 60:
                s_c = float(bnd["candles"][k]["c"])
                s_c60 = float(bnd["candles"][k - 60]["c"])
                if s_c60 > 0:
                    r_stock = (s_c / s_c60 - 1.0) * 100.0
                    date_rs.append(r_stock - nifty_ret60)
        if date_rs:
            cache[cache_key] = float(np.percentile(date_rs, 95))
        else:
            cache[cache_key] = 999999.0

    threshold_95 = cache.get(cache_key, 999999.0)
    if rs_60d < threshold_95:
        return None

    # 13. Conviction Score Calculation (0 to 100)
    score_candle = (cir * 20.0) + ((1.0 - upper_wick_pct) * 20.0)
    score_vol = min(vol_ratio / 5.0, 1.0) * 25.0
    score_ath = max(0.0, (20.0 - ath_dist_pct) / 20.0) * 20.0
    score_trend = max(0.0, min((stock_ret60 - 15.0) / 30.0, 1.0)) * 15.0
    conviction_score = score_candle + score_vol + score_ath + score_trend

    # 14. Initial stop: structural low with minimum 8% breathing room
    min_stop_buf = STRATEGY_CONFIG.get("min_stop_distance_pct", 8.0) / 100.0
    stop = min(l_val, c * (1.0 - min_stop_buf))
    if stop >= c * 0.95:
        stop = c * (1.0 - min_stop_buf)

    risk = c - stop
    if risk <= 0:
        return None

    target = c + STRATEGY_CONFIG["exits"]["target_r_multiple"] * risk

    return {
        "symbol": sym,
        "strategy_id": STRATEGY_CONFIG["id"],
        "strategy_family": STRATEGY_CONFIG["family"],
        "sleeve": STRATEGY_CONFIG["sleeve"],
        "date": d,
        "entry_price": c,
        "structural_stop": round(stop, 2),
        "target_price": round(target, 2),
        "conviction_score": round(conviction_score, 2),
        "raw_measurements": {
            "rs_60d": round(rs_60d, 2),
            "threshold_95": round(threshold_95, 2),
            "h_20": round(h_20, 2),
            "bar_ret": round(bar_ret, 2),
            "cir": round(cir, 2),
            "upper_wick_pct": round(upper_wick_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "chase_pct": round(chase_pct, 2),
            "stock_ret60": round(stock_ret60, 2),
            "ath_dist_pct": round(ath_dist_pct, 2),
        },
    }


def evaluate_exit(trade_dict: dict, today_candle: dict, ind: dict, cur_idx: int, days_held: int = 1) -> Tuple[bool, float, str]:
    ep = float(trade_dict["entry_price"])
    stop_p = float(trade_dict.get("structural_stop", ep * 0.93))
    risk_p_sh = ep - stop_p
    bo = float(today_candle["o"])
    bh = float(today_candle["h"])
    bl = float(today_candle["l"])
    bc = float(today_candle["c"])

    # 1. Hard stop / Gap stop
    if bo <= stop_p:
        return True, bo, "GAP_STOP"
    if bl <= stop_p:
        return True, stop_p, "STOP_HIT"

    # 2. Target (+2.5R)
    if risk_p_sh > 0:
        target_p = ep + STRATEGY_CONFIG["exits"]["target_r_multiple"] * risk_p_sh
        if bo >= target_p:
            return True, bo, "GAP_TARGET"
        if bh >= target_p:
            return True, target_p, "TARGET_HIT"

    # 3. Trailing: if days_held >= 3 and C < EMA20 AND current R >= 1.5 -> exit at C
    if risk_p_sh > 0:
        current_r = (bc - ep) / risk_p_sh
        min_days = STRATEGY_CONFIG["exits"]["min_holding_days"]
        ema20 = ind.get("ema20", [])
        if days_held >= min_days and cur_idx < len(ema20) and ema20[cur_idx] > 0 and bc < ema20[cur_idx] and current_r >= 1.5:
            return True, bc, STRATEGY_CONFIG["exits"]["trailing_rule"]

    # 4. Time limit: days_held >= 60 -> exit at C
    max_days = STRATEGY_CONFIG["exits"]["max_holding_days"]
    if days_held >= max_days:
        return True, bc, f"TIME_EXPIRY_{max_days}D"

    return False, 0.0, ""
