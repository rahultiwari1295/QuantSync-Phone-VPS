"""
QuantSync Daily V2 Feature Engine (Frozen Snapshot Version)
----------------------------------------------------------
Extracts point-in-time technical and forensic features for candidate evaluation,
strictly conforming to the 53-feature schema expected by the frozen XGBoost model.
"""

from typing import Dict, List, Any, Optional
import numpy as np


FEATURE_NAMES = [
    "dist_ema10_pct",
    "dist_ema20_pct",
    "dist_ema50_pct",
    "dist_ema100_pct",
    "dist_ema200_pct",
    "dist_sma200_pct",
    "atr_dist_ema20",
    "atr_dist_ema50",
    "dist_20d_high_pct",
    "dist_50d_high_pct",
    "dist_52w_high_pct",
    "return_from_20d_low_pct",
    "return_from_50d_low_pct",
    "ret_5d",
    "ret_20d",
    "ret_60d",
    "atr14_pct",
    "rsi14",
    "adx14",
    "plus_di14",
    "minus_di14",
    "di_spread",
    "bb_width_pct",
    "squeeze_on",
    "vol_ratio_20d",
    "vol_ratio_50d",
    "body_range_ratio",
    "close_location",
    "nifty_above_ema200",
    "nifty_above_ema50",
    "nifty_above_ema20",
    "nifty_dist_ema50_pct",
    "nifty_dist_ema200_pct",
    "nifty_rsi14",
    "nifty_atr14_pct",
    "nifty_ema50_slope5",
    "nifty_ret20d",
    "strat_Alpha",
    "strat_Breakout",
    "strat_CarterSqueeze",
    "strat_Decouple",
    "strat_DipBuyer",
    "strat_HyperCoil",
    "strat_NR7",
    "strat_Snapback",
    "strat_TurtleSoup",
    "strat_WyckoffSpring",
    "di_ratio",
    "directional_purity",
    "impulse_velocity",
    "overhead_resistance_density"
]


def extract_point_in_time_features(
    sym: str,
    candles: List[Dict[str, Any]],
    ind: Dict[str, Any],
    idx: int,
    strategy_id: str,
    nifty_map: Dict[str, Any]
) -> Dict[str, float]:
    """
    Constructs the point-in-time dictionary for Daily V2 ML inference.
    """
    c = float(candles[idx]['c'])
    h = float(candles[idx]['h'])
    l = float(candles[idx]['l'])
    o = float(candles[idx]['o'])
    v = float(candles[idx]['v'])
    d = candles[idx]['t'][:10]

    e10 = float(ind['ema10'][idx]) if 'ema10' in ind else c
    e20 = float(ind['ema20'][idx]) if 'ema20' in ind else c
    e50 = float(ind['ema50'][idx]) if 'ema50' in ind else c
    e100 = float(ind['ema100'][idx]) if 'ema100' in ind else c
    e200 = float(ind['ema200'][idx]) if 'ema200' in ind else c

    s20 = float(ind['sma20'][idx]) if 'sma20' in ind else c
    s50 = float(ind['sma50'][idx]) if 'sma50' in ind else c
    s200 = float(ind['sma200'][idx]) if 'sma200' in ind else c

    atr14 = float(ind['atr14'][idx]) if 'atr14' in ind else max(1e-4, c * 0.02)
    rsi14 = float(ind['rsi14'][idx]) if 'rsi14' in ind else 50.0
    adx14 = float(ind['adx14'][idx]) if 'adx14' in ind else 20.0
    plus_di = float(ind['plus_di'][idx]) if 'plus_di' in ind else 20.0
    minus_di = float(ind['minus_di'][idx]) if 'minus_di' in ind else 20.0

    v_sma20 = float(ind['vol_sma20'][idx]) if 'vol_sma20' in ind else max(1.0, v)
    v_sma50 = float(ind['vol_sma50'][idx]) if 'vol_sma50' in ind else max(1.0, v)

    h_20 = max(float(x['h']) for x in candles[max(0, idx-20):idx+1])
    h_50 = max(float(x['h']) for x in candles[max(0, idx-50):idx+1])
    h_252 = max(float(x['h']) for x in candles[max(0, idx-252):idx+1])
    l_20 = min(float(x['l']) for x in candles[max(0, idx-20):idx+1])
    l_50 = min(float(x['l']) for x in candles[max(0, idx-50):idx+1])

    c_prev5 = float(candles[idx-5]['c']) if idx >= 5 else c
    c_prev20 = float(candles[idx-20]['c']) if idx >= 20 else c
    c_prev60 = float(candles[idx-60]['c']) if idx >= 60 else c

    ret_5d = round((c - c_prev5) / max(1e-4, c_prev5) * 100.0, 2)
    ret_20d = round((c - c_prev20) / max(1e-4, c_prev20) * 100.0, 2)
    ret_60d = round((c - c_prev60) / max(1e-4, c_prev60) * 100.0, 2)
    atr14_pct = round(atr14 / max(1e-4, c) * 100.0, 2)

    bb_upper = float(ind.get('bb_upper', [c * 1.05]*len(candles))[idx])
    bb_lower = float(ind.get('bb_lower', [c * 0.95]*len(candles))[idx])
    bb_width_pct = round((bb_upper - bb_lower) / max(1e-4, c) * 100.0, 2)
    squeeze_on = float(ind.get('squeeze', [0.0]*len(candles))[idx])

    # Forensic Features
    di_ratio = round(plus_di / max(1e-4, minus_di), 3)
    directional_purity = round(abs(plus_di - minus_di) / max(1e-4, plus_di + minus_di), 3)
    impulse_velocity = round(ret_5d / max(1e-4, atr14_pct * 5.0), 2)

    # Overhead Resistance Density: fraction of bars in last 60 sessions where high was in [0%, 10%] above c
    lookback_res_start = max(0, idx - 60)
    res_bars = 0
    total_res_bars = max(1, idx - lookback_res_start)
    for b_idx in range(lookback_res_start, idx):
        b_h = float(candles[b_idx]['h'])
        if c < b_h <= c * 1.10:
            res_bars += 1
    overhead_resistance_density = round(res_bars / float(total_res_bars), 3)

    feat = {
        'dist_ema10_pct': round((c - e10) / max(1e-4, e10) * 100.0, 2),
        'dist_ema20_pct': round((c - e20) / max(1e-4, e20) * 100.0, 2),
        'dist_ema50_pct': round((c - e50) / max(1e-4, e50) * 100.0, 2),
        'dist_ema100_pct': round((c - e100) / max(1e-4, e100) * 100.0, 2),
        'dist_ema200_pct': round((c - e200) / max(1e-4, e200) * 100.0, 2),
        'dist_sma200_pct': round((c - s200) / max(1e-4, s200) * 100.0, 2),
        'atr_dist_ema20': round((c - e20) / max(1e-4, atr14), 2),
        'atr_dist_ema50': round((c - e50) / max(1e-4, atr14), 2),
        'dist_20d_high_pct': round((h_20 - c) / max(1e-4, c) * 100.0, 2),
        'dist_50d_high_pct': round((h_50 - c) / max(1e-4, c) * 100.0, 2),
        'dist_52w_high_pct': round((h_252 - c) / max(1e-4, c) * 100.0, 2),
        'return_from_20d_low_pct': round((c - l_20) / max(1e-4, l_20) * 100.0, 2),
        'return_from_50d_low_pct': round((c - l_50) / max(1e-4, l_50) * 100.0, 2),
        'ret_5d': ret_5d,
        'ret_20d': ret_20d,
        'ret_60d': ret_60d,
        'atr14_pct': atr14_pct,
        'rsi14': round(rsi14, 2),
        'adx14': round(adx14, 2),
        'plus_di14': round(plus_di, 2),
        'minus_di14': round(minus_di, 2),
        'di_spread': round(plus_di - minus_di, 2),
        'bb_width_pct': bb_width_pct,
        'squeeze_on': squeeze_on,
        'vol_ratio_20d': round(v / max(1.0, v_sma20), 2),
        'vol_ratio_50d': round(v / max(1.0, v_sma50), 2),
        'body_range_ratio': round(abs(c - o) / max(1e-2, h - l), 2),
        'close_location': round((c - l) / max(1e-2, h - l), 2),
        'di_ratio': di_ratio,
        'directional_purity': directional_purity,
        'impulse_velocity': impulse_velocity,
        'overhead_resistance_density': overhead_resistance_density
    }

    nifty_ctx = nifty_map.get(d, {
        'nifty_above_ema200': 1.0, 'nifty_above_ema50': 1.0, 'nifty_above_ema20': 1.0,
        'nifty_dist_ema50_pct': 0.0, 'nifty_dist_ema200_pct': 0.0, 'nifty_rsi14': 50.0,
        'nifty_atr14_pct': 1.0, 'nifty_ema50_slope5': 0.0, 'nifty_ret20d': 0.0
    })
    feat.update(nifty_ctx)

    # 10 One-Hot Strategy Dummies
    strategies_list = [
        'Alpha', 'Breakout', 'CarterSqueeze', 'Decouple', 'DipBuyer',
        'HyperCoil', 'NR7', 'Snapback', 'TurtleSoup', 'WyckoffSpring'
    ]
    for s_name in strategies_list:
        feat[f"strat_{s_name}"] = 1.0 if strategy_id == s_name else 0.0

    return feat


def vector_from_features(feat_dict: Dict[str, float], feature_names_list: Optional[List[str]] = None) -> List[float]:
    """
    Returns an ordered numeric vector matching the expected feature names.
    """
    names = feature_names_list or FEATURE_NAMES
    return [float(feat_dict.get(col, 0.0)) for col in names]
