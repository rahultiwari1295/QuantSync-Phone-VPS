"""
QuantSync Daily V2 Technical Primitives & Audited Market Indicators
-------------------------------------------------------------------
Audited technical primitives including standard Wilder ADX(14), RSI(14), ATR(14),
Bollinger Bands, Keltner Squeeze, and Real Point-In-Time Nifty Market Features.
"""

import math
import numpy as np
from typing import Dict, List, Any


def compute_ema(values: List[float], span: int) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    alpha = 2.0 / (span + 1.0)
    ema = [0.0] * n
    ema[0] = values[0]
    for i in range(1, n):
        ema[i] = (values[i] * alpha) + (ema[i - 1] * (1.0 - alpha))
    return ema


def compute_sma(values: List[float], span: int) -> List[float]:
    n = len(values)
    if n == 0:
        return []
    sma = [0.0] * n
    running_sum = 0.0
    for i in range(n):
        running_sum += values[i]
        if i >= span:
            running_sum -= values[i - span]
            sma[i] = running_sum / span
        else:
            sma[i] = running_sum / (i + 1)
    return sma


def compute_wilder_adx(candles: List[Dict[str, Any]], period: int = 14) -> Dict[str, List[float]]:
    n = len(candles)
    if n < period + 1:
        return {
            'adx': [0.0] * n,
            'plus_di': [0.0] * n,
            'minus_di': [0.0] * n,
            'dx': [0.0] * n
        }

    highs = [float(c['h']) for c in candles]
    lows = [float(c['l']) for c in candles]
    closes = [float(c['c']) for c in candles]

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hpc, lpc)

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        else:
            plus_dm[i] = 0.0

        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
        else:
            minus_dm[i] = 0.0

    atr_smoothed = [0.0] * n
    plus_dm_smoothed = [0.0] * n
    minus_dm_smoothed = [0.0] * n

    atr_smoothed[period] = sum(tr[1:period + 1])
    plus_dm_smoothed[period] = sum(plus_dm[1:period + 1])
    minus_dm_smoothed[period] = sum(minus_dm[1:period + 1])

    for i in range(period + 1, n):
        atr_smoothed[i] = atr_smoothed[i - 1] - (atr_smoothed[i - 1] / period) + tr[i]
        plus_dm_smoothed[i] = plus_dm_smoothed[i - 1] - (plus_dm_smoothed[i - 1] / period) + plus_dm[i]
        minus_dm_smoothed[i] = minus_dm_smoothed[i - 1] - (minus_dm_smoothed[i - 1] / period) + minus_dm[i]

    plus_di = [0.0] * n
    minus_di = [0.0] * n
    dx = [0.0] * n

    for i in range(period, n):
        den = max(1e-4, atr_smoothed[i])
        p_di = (plus_dm_smoothed[i] / den) * 100.0
        m_di = (minus_dm_smoothed[i] / den) * 100.0
        plus_di[i] = p_di
        minus_di[i] = m_di

        di_sum = p_di + m_di
        if di_sum > 1e-4:
            dx[i] = (abs(p_di - m_di) / di_sum) * 100.0
        else:
            dx[i] = 0.0

    adx = [0.0] * n
    adx_start = 2 * period
    if n > adx_start:
        adx[adx_start] = sum(dx[period:adx_start + 1]) / float(period + 1)
        for i in range(adx_start + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / float(period)

    return {
        'adx': adx,
        'plus_di': plus_di,
        'minus_di': minus_di,
        'dx': dx
    }


def compute_rsi(closes: List[float], period: int = 14) -> List[float]:
    n = len(closes)
    if n < period + 1:
        return [50.0] * n

    rsi = [50.0] * n
    gains = [0.0] * n
    losses = [0.0] * n

    for i in range(1, n):
        delta = closes[i] - closes[i - 1]
        if delta > 0:
            gains[i] = delta
        else:
            losses[i] = -delta

    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss <= 1e-4:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def compute_atr(candles: List[Dict[str, Any]], period: int = 14) -> List[float]:
    n = len(candles)
    if n == 0:
        return []
    tr = [0.0] * n
    highs = [float(c['h']) for c in candles]
    lows = [float(c['l']) for c in candles]
    closes = [float(c['c']) for c in candles]

    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        hl = highs[i] - lows[i]
        hpc = abs(highs[i] - closes[i - 1])
        lpc = abs(lows[i] - closes[i - 1])
        tr[i] = max(hl, hpc, lpc)

    atr = [0.0] * n
    if n < period:
        return tr

    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def calculate_stock_indicators(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(candles)
    closes = [float(c['c']) for c in candles]
    highs = [float(c['h']) for c in candles]
    lows = [float(c['l']) for c in candles]
    vols = [float(c['v']) for c in candles]

    ema10 = compute_ema(closes, 10)
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    ema100 = compute_ema(closes, 100)
    ema200 = compute_ema(closes, 200)

    sma20 = compute_sma(closes, 20)
    sma50 = compute_sma(closes, 50)
    sma200 = compute_sma(closes, 200)

    atr14 = compute_atr(candles, 14)
    rsi14 = compute_rsi(closes, 14)
    adx_dict = compute_wilder_adx(candles, 14)

    vol_sma20 = compute_sma(vols, 20)
    vol_sma50 = compute_sma(vols, 50)

    # Bollinger Bands (20, 2.0)
    bb_upper = [0.0] * n
    bb_lower = [0.0] * n
    for i in range(19, n):
        win = closes[i - 19:i + 1]
        std = np.std(win)
        mean = sma20[i]
        bb_upper[i] = mean + (2.0 * std)
        bb_lower[i] = mean - (2.0 * std)

    # Keltner Channels (20, 1.5 * ATR)
    kc_upper = [0.0] * n
    kc_lower = [0.0] * n
    squeeze = [0] * n
    for i in range(19, n):
        kc_upper[i] = sma20[i] + (1.5 * atr14[i])
        kc_lower[i] = sma20[i] - (1.5 * atr14[i])
        if bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i]:
            squeeze[i] = 1

    return {
        'ema10': ema10, 'ema20': ema20, 'ema50': ema50, 'ema100': ema100, 'ema200': ema200,
        'sma20': sma20, 'sma50': sma50, 'sma200': sma200,
        'atr14': atr14, 'rsi14': rsi14,
        'adx14': adx_dict['adx'], 'plus_di': adx_dict['plus_di'], 'minus_di': adx_dict['minus_di'],
        'vol_sma20': vol_sma20, 'vol_sma50': vol_sma50,
        'bb_upper': bb_upper, 'bb_lower': bb_lower,
        'kc_upper': kc_upper, 'kc_lower': kc_lower,
        'squeeze': squeeze
    }


def compute_nifty_real_market_features(nifty_candles: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    n = len(nifty_candles)
    if n == 0:
        return {}

    closes = [float(c['c']) for c in nifty_candles]
    ema20 = compute_ema(closes, 20)
    ema50 = compute_ema(closes, 50)
    ema100 = compute_ema(closes, 100)
    ema200 = compute_ema(closes, 200)

    rsi14 = compute_rsi(closes, 14)
    atr14 = compute_atr(nifty_candles, 14)

    market_features = {}
    for i in range(n):
        d = nifty_candles[i]['t'][:10]
        c = closes[i]

        e20_v = ema20[i]
        e50_v = ema50[i]
        e100_v = ema100[i]
        e200_v = ema200[i]

        slope5 = (e50_v - ema50[i - 5]) / max(1e-4, ema50[i - 5]) * 100.0 if i >= 5 else 0.0
        ret20 = (c - closes[i - 20]) / max(1e-4, closes[i - 20]) * 100.0 if i >= 20 else 0.0

        market_features[d] = {
            'nifty_close': c,
            'nifty_above_ema200': 1.0 if c > e200_v else 0.0,
            'nifty_above_ema50': 1.0 if c > e50_v else 0.0,
            'nifty_above_ema20': 1.0 if c > e20_v else 0.0,
            'nifty_dist_ema50_pct': round((c - e50_v) / max(1e-4, e50_v) * 100.0, 2),
            'nifty_dist_ema200_pct': round((c - e200_v) / max(1e-4, e200_v) * 100.0, 2),
            'nifty_rsi14': round(rsi14[i], 2),
            'nifty_atr14_pct': round(atr14[i] / max(1e-4, c) * 100.0, 2),
            'nifty_ema50_slope5': round(slope5, 2),
            'nifty_ret20d': round(ret20, 2)
        }

    return market_features
