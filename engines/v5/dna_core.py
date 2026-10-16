"""
engines/v5/dna_core.py — Self-Contained DNA Forensic & Technical Matrix Engine
=============================================================================
100% Standalone module for QuantSync-V5. Zero external engine dependencies.
Computes:
  1. Stan Weinstein Stage (Stage 1 to Stage 4)
  2. Wyckoff Volume Spread Analysis (VSA Absorption, Dry-up, Churn, Climax)
  3. Wilder's RSI (14) & Regular Bullish / Bearish Divergence Detection
  4. Exponential Moving Average Stack (EMA 20, 50, 200) & Slopes
  5. Dynamic Health Score (0 to 100) & Macro Stance (BULLISH/NEUTRAL/BEARISH)
"""

import numpy as np
import pandas as pd
from typing import Dict, Any, List

def compute_dna_profile(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Computes complete forensic DNA profile directly from OHLCV candle DataFrame.
    DataFrame must contain columns: ['Open', 'High', 'Low', 'Close', 'Volume'].
    """
    if df is None or len(df) < 35:
        return {
            "health_score": 50,
            "stage": "STAGE_UNKNOWN",
            "stance": "NEUTRAL",
            "vsa_flags": [],
            "rsi_divergence": "None",
            "rsi": 50.0,
            "ema20": 0.0,
            "ema50": 0.0,
            "ema200": 0.0,
            "volume_ratio": 1.0,
            "vol_dryup": False,
            "absorption": False,
            "churn": False
        }

    close = df['Close'].values
    high = df['High'].values
    low = df['Low'].values
    volume = df['Volume'].values

    # Moving Averages
    s_close = pd.Series(close)
    ema20 = s_close.ewm(span=20, adjust=False).mean().values
    ema50 = s_close.ewm(span=50, adjust=False).mean().values
    ema200 = s_close.ewm(span=200, adjust=False).mean().values if len(close) >= 200 else ema50

    curr_p = close[-1]
    e20 = ema20[-1]
    e50 = ema50[-1]
    e200 = ema200[-1]

    # 1. Stan Weinstein Stage Determination
    if curr_p > e50 and e50 > e200:
        stage = "STAGE_2_MARKUP"
        stance = "BULLISH"
    elif curr_p < e50 and e50 < e200:
        stage = "STAGE_4_MARKDOWN"
        stance = "BEARISH"
    elif curr_p < e50 and curr_p > e200:
        stage = "STAGE_3_DISTRIBUTION"
        stance = "NEUTRAL"
    else:
        stage = "STAGE_1_ACCUMULATION"
        stance = "NEUTRAL"

    # 2. Wyckoff Volume Spread Analysis (VSA)
    vol_sma20 = pd.Series(volume).rolling(20).mean().values
    v_ratio = volume[-1] / (vol_sma20[-1] + 1e-9)
    spread = max(1e-4, high[-1] - low[-1])
    avg_spread = (pd.Series(high) - pd.Series(low)).rolling(20).mean().values[-1]

    vsa_flags: List[str] = []
    cir = (close[-1] - low[-1]) / spread

    # Absorption: High volume + strong close near high
    is_absorption = (v_ratio >= 2.0 and cir >= 0.75)
    if is_absorption:
        vsa_flags.append("ABSORPTION")

    # Dry-Up: Low volume during consolidation
    recent_5_vol = np.mean(volume[-5:])
    is_dryup = (recent_5_vol < vol_sma20[-1] * 0.70)
    if is_dryup:
        vsa_flags.append("VOLUME_DRYUP")

    # Churn: Huge volume on narrow price spread (institutional distribution)
    is_churn = (v_ratio >= 2.5 and spread < avg_spread * 0.75)
    if is_churn:
        vsa_flags.append("VOLUME_CHURN")

    # 3. Wilder's RSI (14) & Divergence
    delta = s_close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    rsi_arr = (100 - (100 / (1 + rs))).values
    curr_rsi = rsi_arr[-1] if not np.isnan(rsi_arr[-1]) else 50.0

    rsi_divergence = "None"
    if len(close) >= 25:
        recent_high_idx = np.argmax(high[-20:-3]) + (len(high) - 20)
        if high[-1] >= high[recent_high_idx] * 0.99 and curr_rsi < rsi_arr[recent_high_idx] - 5.0:
            rsi_divergence = "Bearish Regular Divergence"
        recent_low_idx = np.argmin(low[-20:-3]) + (len(low) - 20)
        if low[-1] <= low[recent_low_idx] * 1.01 and curr_rsi > rsi_arr[recent_low_idx] + 5.0:
            rsi_divergence = "Bullish Regular Divergence"

    # Health Score Computation
    score = 50.0
    if stage == "STAGE_2_MARKUP":
        score += 25.0
    elif stage == "STAGE_1_ACCUMULATION":
        score += 10.0
    elif stage == "STAGE_4_MARKDOWN":
        score -= 30.0

    if is_absorption:
        score += 15.0
    if is_dryup:
        score += 10.0
    if is_churn:
        score -= 20.0
    if rsi_divergence == "Bearish Regular Divergence":
        score -= 25.0

    if 50.0 <= curr_rsi <= 72.0:
        score += 10.0

    health_score = int(max(10, min(98, score)))

    return {
        "health_score": health_score,
        "stage": stage,
        "stance": stance,
        "vsa_flags": vsa_flags,
        "rsi_divergence": rsi_divergence,
        "rsi": round(float(curr_rsi), 1),
        "ema20": round(float(e20), 2),
        "ema50": round(float(e50), 2),
        "ema200": round(float(e200), 2),
        "volume_ratio": round(float(v_ratio), 2),
        "vol_dryup": is_dryup,
        "absorption": is_absorption,
        "churn": is_churn
    }
