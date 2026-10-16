"""
QuantSync Daily V2 Strategy Evaluators (9 Surviving Core Strategies)
-------------------------------------------------------------------
Faithfully implements the exact Weekly -> Daily strategy translations from the frozen research sweep.
Strictly generates RAW candidates without embedded ML logic.
"""

import numpy as np
from typing import Dict, List, Any, Optional


def eval_breakout(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                  lookback_high: int = 20, trend_ema_span: int = 200, vol_mult: float = 1.2,
                  dist_52w_max: float = 8.0) -> Optional[Dict[str, Any]]:
    if idx < max(lookback_high, 60):
        return None
    c = float(candles[idx]['c'])
    h = float(candles[idx]['h'])
    o = float(candles[idx]['o'])
    v = float(candles[idx]['v'])

    ema_trend = float(ind['ema200'][idx]) if 'ema200' in ind else c
    if c <= ema_trend:
        return None

    past_high = max(float(x['h']) for x in candles[idx-lookback_high:idx])
    if c <= past_high:
        return None

    v_avg20 = float(np.mean([float(x['v']) for x in candles[idx-20:idx]])) if idx >= 20 else v
    vol_ratio = v / max(1.0, v_avg20)
    if vol_ratio < vol_mult:
        return None

    high_252 = max(float(x['h']) for x in candles[max(0, idx-252):idx+1])
    dist_52w = (high_252 - c) / c * 100.0
    if dist_52w > dist_52w_max:
        return None

    # Strategy-Specific Raw Variables
    pivot_dist = (c - past_high) / past_high * 100.0
    consol_range = (past_high - min(float(x['l']) for x in candles[idx-lookback_high:idx])) / past_high * 100.0
    atr14 = float(ind['atr14'][idx]) if 'atr14' in ind else (c * 0.02)
    atr_comp = (atr14 / c * 100.0)
    ema20 = float(ind['ema20'][idx]) if 'ema20' in ind else c
    ema50 = float(ind['ema50'][idx]) if 'ema50' in ind else c
    ema_alignment = 1.0 if (c > ema20 > ema50 > ema_trend) else 0.0

    return {
        'symbol': sym,
        'strategy_id': 'Breakout',
        'strategy_family': 'MOMENTUM_BREAKOUT',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'pivot_dist': round(pivot_dist, 2),
            'breakout_magnitude': round((c - o) / max(1e-4, o) * 100.0, 2),
            'consol_range': round(consol_range, 2),
            'atr_compression': round(atr_comp, 2),
            'dist_from_52w_high': round(dist_52w, 2),
            'breakout_vol_ratio': round(vol_ratio, 2),
            'ema_alignment': ema_alignment
        }
    }


def eval_alpha(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
               nifty_map: Dict[str, Dict[str, Any]], rs_lookback: int = 60,
               min_rs_pct: float = 8.0) -> Optional[Dict[str, Any]]:
    if idx < max(rs_lookback, 60):
        return None
    c = float(candles[idx]['c'])
    c_prev = float(candles[idx-rs_lookback]['c'])
    stock_ret = (c - c_prev) / c_prev * 100.0

    d = candles[idx]['t'][:10]
    d_prev = candles[idx-rs_lookback]['t'][:10]

    nifty_curr = nifty_map.get(d, {}).get('nifty_close', None)
    nifty_past = nifty_map.get(d_prev, {}).get('nifty_close', None)
    if nifty_curr is None or nifty_past is None:
        nifty_ret = 0.0
    else:
        nifty_ret = (nifty_curr - nifty_past) / nifty_past * 100.0

    excess_rs = stock_ret - nifty_ret
    if excess_rs < min_rs_pct:
        return None

    ema50 = float(ind['ema50'][idx]) if 'ema50' in ind else c
    if c <= ema50:
        return None

    # Strategy-Specific Raw Variables
    c_prev20 = float(candles[idx-20]['c']) if idx >= 20 else c
    stock_ret20 = (c - c_prev20) / c_prev20 * 100.0
    c_prev120 = float(candles[idx-120]['c']) if idx >= 120 else c
    stock_ret120 = (c - c_prev120) / c_prev120 * 100.0

    return {
        'symbol': sym,
        'strategy_id': 'Alpha',
        'strategy_family': 'RELATIVE_STRENGTH',
        'date': d,
        'entry_price': c,
        'raw_measurements': {
            'rs_20d': round(stock_ret20, 2),
            'rs_60d': round(excess_rs, 2),
            'rs_120d': round(stock_ret120, 2),
            'rs_slope': round(stock_ret20 - (excess_rs / 3.0), 2),
            'benchmark_ret': round(nifty_ret, 2),
            'stock_ret': round(stock_ret, 2)
        }
    }


def eval_snapback(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                  ema_span: int = 50, rsi_min: float = 35.0, rsi_max: float = 48.0) -> Optional[Dict[str, Any]]:
    if idx < 60:
        return None
    c = float(candles[idx]['c'])
    l = float(candles[idx]['l'])
    h = float(candles[idx]['h'])
    ema = float(ind[f'ema{ema_span}'][idx]) if f'ema{ema_span}' in ind else float(ind['ema50'][idx])
    rsi = float(ind['rsi14'][idx]) if 'rsi14' in ind else 50.0

    # Touched or within 1.5% of EMA
    dist_ema = (c - ema) / ema * 100.0
    if not (-2.0 <= dist_ema <= 2.5):
        return None
    if not (rsi_min <= rsi <= rsi_max):
        return None

    sma200 = float(ind['sma200'][idx]) if 'sma200' in ind else c
    if c <= sma200:
        return None

    # Strategy-Specific Raw Variables
    past_h20 = max(float(x['h']) for x in candles[idx-20:idx])
    pullback_depth = (past_h20 - l) / past_h20 * 100.0
    rsi_prev3 = float(ind['rsi14'][idx-3]) if idx >= 3 else rsi
    rsi_recovery = rsi - rsi_prev3

    return {
        'symbol': sym,
        'strategy_id': 'Snapback',
        'strategy_family': 'MEAN_REVERSION',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'dist_from_ema': round(dist_ema, 2),
            'pullback_depth': round(pullback_depth, 2),
            'rsi14': round(rsi, 2),
            'rsi_recovery': round(rsi_recovery, 2),
            'trend_strength': round((c - sma200) / sma200 * 100.0, 2)
        }
    }


def eval_decouple(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                  nifty_map: Dict[str, Dict[str, Any]], lookback: int = 10) -> Optional[Dict[str, Any]]:
    if idx < max(lookback, 60):
        return None
    c = float(candles[idx]['c'])
    c_prev = float(candles[idx-lookback]['c'])
    stock_ret = (c - c_prev) / c_prev * 100.0

    d = candles[idx]['t'][:10]
    d_prev = candles[idx-lookback]['t'][:10]

    n_curr = nifty_map.get(d, {}).get('nifty_close', None)
    n_past = nifty_map.get(d_prev, {}).get('nifty_close', None)
    if n_curr is None or n_past is None:
        return None
    nifty_ret = (n_curr - n_past) / n_past * 100.0

    # Decouple: Nifty down (< -1.0%) but Stock positive (> +2.0%)
    if not (nifty_ret <= -1.0 and stock_ret >= 2.0):
        return None

    return {
        'symbol': sym,
        'strategy_id': 'Decouple',
        'strategy_family': 'DIVERGENCE',
        'date': d,
        'entry_price': c,
        'raw_measurements': {
            'stock_ret_10d': round(stock_ret, 2),
            'nifty_ret_10d': round(nifty_ret, 2),
            'excess_divergence': round(stock_ret - nifty_ret, 2),
            'divergence_magnitude': round(abs(stock_ret) + abs(nifty_ret), 2)
        }
    }


def eval_carter_squeeze(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                        min_sqz_bars: int = 4) -> Optional[Dict[str, Any]]:
    if idx < 60:
        return None
    sqz = ind.get('squeeze', [])
    if len(sqz) <= idx:
        return None

    # Squeeze fired today (was on for past N bars, now turning off or expanding)
    sqz_past = sqz[max(0, idx-min_sqz_bars):idx]
    if sum(sqz_past) < min_sqz_bars:
        return None

    c = float(candles[idx]['c'])
    o = float(candles[idx]['o'])
    ema20 = float(ind['ema20'][idx]) if 'ema20' in ind else c
    if c <= ema20:
        return None

    bb_u = float(ind['bb_upper'][idx])
    bb_l = float(ind['bb_lower'][idx])
    bb_w = (bb_u - bb_l) / c * 100.0

    return {
        'symbol': sym,
        'strategy_id': 'CarterSqueeze',
        'strategy_family': 'VOLATILITY_EXPANSION',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'squeeze_duration': sum(sqz[max(0, idx-20):idx]),
            'bb_width_pct': round(bb_w, 2),
            'momentum_1d': round((c - o) / max(1e-4, o) * 100.0, 2),
            'expansion_magnitude': round((c - ema20) / ema20 * 100.0, 2)
        }
    }


def eval_wyckoff_spring(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                        support_lookback: int = 50) -> Optional[Dict[str, Any]]:
    if idx < max(support_lookback, 60):
        return None
    c = float(candles[idx]['c'])
    l = float(candles[idx]['l'])
    o = float(candles[idx]['o'])
    v = float(candles[idx]['v'])

    sup_level = min(float(x['l']) for x in candles[idx-support_lookback:idx-2])
    if sup_level <= 0:
        return None
    # Low pierced below support, but close reclaimed above support strongly
    if not (l < sup_level and c > sup_level and c >= o):
        return None

    v_avg20 = float(np.mean([float(x['v']) for x in candles[idx-20:idx]])) if idx >= 20 else v
    spring_depth = (sup_level - l) / max(1e-4, sup_level) * 100.0
    reclaim_mag = (c - sup_level) / max(1e-4, sup_level) * 100.0

    return {
        'symbol': sym,
        'strategy_id': 'WyckoffSpring',
        'strategy_family': 'ACCUMULATION_SPRING',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'test_depth_pct': round(spring_depth, 2),
            'spring_volume_ratio': round(v / max(1.0, v_avg20), 2),
            'reclaim_magnitude_pct': round(reclaim_mag, 2),
            'dist_to_support': round(abs(c - sup_level) / max(1e-4, c) * 100.0, 2)
        }
    }


def eval_turtle_soup(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                     low_lookback: int = 20) -> Optional[Dict[str, Any]]:
    if idx < max(low_lookback, 60):
        return None
    c = float(candles[idx]['c'])
    l = float(candles[idx]['l'])
    o = float(candles[idx]['o'])
    h = float(candles[idx]['h'])

    past_low = min(float(x['l']) for x in candles[idx-low_lookback:idx])
    if past_low <= 0:
        return None
    if not (l < past_low and c > past_low and c >= o):
        return None

    atr14 = float(ind['atr14'][idx]) if 'atr14' in ind else (c * 0.02)
    undercut = (past_low - l) / max(1e-4, past_low) * 100.0
    candle_size = (h - l) / max(1e-4, atr14)

    return {
        'symbol': sym,
        'strategy_id': 'TurtleSoup',
        'strategy_family': 'LIQUIDITY_SWEEP',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'undercut_depth_pct': round(undercut, 2),
            'reclaim_candle_atr': round(candle_size, 2),
            'reclaim_close_loc': round((c - l) / max(1e-2, h - l), 2),
            'prior_low_distance': round((c - past_low) / max(1e-4, past_low) * 100.0, 2)
        }
    }


def eval_dip_buyer(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                   dip_pct_min: float = 8.0, dist_200_max: float = 6.0) -> Optional[Dict[str, Any]]:
    if idx < 60:
        return None
    c = float(candles[idx]['c'])
    l = float(candles[idx]['l'])
    o = float(candles[idx]['o'])

    # Retraced >= 8% from 50-day high
    past_h50 = max(float(x['h']) for x in candles[idx-50:idx+1])
    dip_depth = (past_h50 - l) / past_h50 * 100.0
    if dip_depth < dip_pct_min:
        return None

    # Within 6% of 200 SMA
    sma200 = float(ind['sma200'][idx]) if 'sma200' in ind else c
    dist_200 = abs(c - sma200) / sma200 * 100.0
    if dist_200 > dist_200_max:
        return None

    # Rebound candle
    if c < o:
        return None

    return {
        'symbol': sym,
        'strategy_id': 'DipBuyer',
        'strategy_family': 'PULLBACK_VALUE',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'dip_depth_pct': round(dip_depth, 2),
            'dist_to_sma200': round(dist_200, 2),
            'rebound_strength': round((c - o) / max(1e-4, o) * 100.0, 2),
            'proximity_to_low': round((c - l) / max(1e-4, l) * 100.0, 2)
        }
    }


def eval_nr7(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    if idx < 60:
        return None
    today_range = float(candles[idx]['h']) - float(candles[idx]['l'])
    prev_6_ranges = [float(candles[i]['h']) - float(candles[i]['l']) for i in range(idx-6, idx)]

    if today_range >= min(prev_6_ranges):
        return None

    c = float(candles[idx]['c'])
    ema50 = float(ind['ema50'][idx]) if 'ema50' in ind else c
    if c <= ema50:
        return None

    avg_range = float(np.mean(prev_6_ranges))
    range_ratio = today_range / max(1e-4, avg_range)

    return {
        'symbol': sym,
        'strategy_id': 'NR7',
        'strategy_family': 'VOLATILITY_COMPRESSION',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'today_range': round(today_range, 2),
            'avg_6d_range': round(avg_range, 2),
            'range_compression_ratio': round(range_ratio, 2),
            'dist_from_ema50': round((c - ema50) / ema50 * 100.0, 2)
        }
    }


def eval_hyper_coil(sym: str, candles: List[Dict[str, Any]], ind: Dict[str, Any], idx: int,
                    coil_bars: int = 5, max_range_pct: float = 3.5) -> Optional[Dict[str, Any]]:
    if idx < 60:
        return None
    recent = candles[max(0, idx-coil_bars+1):idx+1]
    h_coil = max(float(x['h']) for x in recent)
    l_coil = min(float(x['l']) for x in recent)
    c = float(candles[idx]['c'])

    if l_coil <= 0:
        return None
    coil_span = (h_coil - l_coil) / max(1e-4, l_coil) * 100.0
    if coil_span > max_range_pct:
        return None

    ema50 = float(ind['ema50'][idx]) if 'ema50' in ind else c
    if c <= ema50:
        return None

    return {
        'symbol': sym,
        'strategy_id': 'HyperCoil',
        'strategy_family': 'TIGHT_COIL',
        'date': candles[idx]['t'][:10],
        'entry_price': c,
        'raw_measurements': {
            'coil_span_pct': round(coil_span, 2),
            'dist_from_ema50': round((c - ema50) / ema50 * 100.0, 2)
        }
    }


def evaluate_all_daily_v2_strategies(
    sym: str,
    candles: List[Dict[str, Any]],
    ind: Dict[str, Any],
    idx: int,
    nifty_map: Dict[str, Any],
    include_hypercoil: bool = False
) -> List[Dict[str, Any]]:
    """
    Evaluates the core strategies for a given stock at bar `idx`.
    Returns all generated raw candidate dictionaries.
    """
    candidates = []

    s_brk = eval_breakout(sym, candles, ind, idx)
    if s_brk: candidates.append(s_brk)

    s_alp = eval_alpha(sym, candles, ind, idx, nifty_map)
    if s_alp: candidates.append(s_alp)

    s_snp = eval_snapback(sym, candles, ind, idx)
    if s_snp: candidates.append(s_snp)

    s_dec = eval_decouple(sym, candles, ind, idx, nifty_map)
    if s_dec: candidates.append(s_dec)

    s_sqz = eval_carter_squeeze(sym, candles, ind, idx)
    if s_sqz: candidates.append(s_sqz)

    s_wyc = eval_wyckoff_spring(sym, candles, ind, idx)
    if s_wyc: candidates.append(s_wyc)

    s_trt = eval_turtle_soup(sym, candles, ind, idx)
    if s_trt: candidates.append(s_trt)

    s_dip = eval_dip_buyer(sym, candles, ind, idx)
    if s_dip: candidates.append(s_dip)

    s_nr7 = eval_nr7(sym, candles, ind, idx)
    if s_nr7: candidates.append(s_nr7)

    if include_hypercoil:
        s_hyp = eval_hyper_coil(sym, candles, ind, idx)
        if s_hyp: candidates.append(s_hyp)

    return candidates
