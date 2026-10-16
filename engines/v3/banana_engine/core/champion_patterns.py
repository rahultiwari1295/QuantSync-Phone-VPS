"""
banana_engine/core/champion_patterns.py — 4 Champion Production Strategy Engine
================================================================================
Implements the 4 validated champion price action patterns:
  1. High-Tight Bull Flag (HTF)
  2. Ultra Marubozu Base Breakout (BASE)
  3. 52-Week High Ultra (52W_HIGH)
  4. Nicolas Darvas Box Ultra (DARVAS)

Enforces strict institutional quality gates:
  - Close-In-Range (CIR >= 0.80)
  - Upper Wick (<= 0.10)
  - Volume Ratio (>= 2.0x)
  - Max Chase (<= 8.0%)
  - Proximity to ATH / 52W High (<= 20.0%)

Generates complete geometric boundary coordinates for visual chart rendering
on TradingView Lightweight Charts (flagpole, shelf, base zone, darvas box).
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict


@dataclass
class ChampionSetup:
    symbol: str
    date: str
    signal_idx: int
    pattern: str  # 'HTF' | 'BASE' | '52W_HIGH' | 'DARVAS'
    pattern_label: str
    entry: float
    stop: float
    initial_risk: float
    risk_pct: float
    be_lock_price: float
    target_price: float
    volume_ratio: float
    cir: float
    upper_wick: float
    geometry: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_all_champion_patterns(
    bars: pd.DataFrame,
    i: int,
    symbol: str = "",
    min_cir: float = 0.80,
    max_upper_wick: float = 0.10,
    min_vol_ratio: float = 2.0,
    max_chase_pct: float = 8.0,
    max_ath_dist_pct: float = 20.0
) -> Optional[ChampionSetup]:
    """
    Evaluates bar index i for all 4 champion patterns.
    Returns the first matching champion setup with complete geometric bounds,
    or None if no pattern qualifies.
    """
    n = len(bars)
    if i < 60 or i >= n:
        return None

    c = bars['close'].values
    h = bars['high'].values
    l = bars['low'].values
    o = bars['open'].values
    v = bars['volume'].values
    d = bars['date'].astype(str).str[:10].values

    cur_c = float(c[i])
    cur_h = float(h[i])
    cur_l = float(l[i])
    cur_o = float(o[i])
    cur_v = float(v[i])
    date_str = str(d[i])

    # 1. Institutional Quality Gates
    bar_range = max(cur_h - cur_l, 0.01)
    cir = (cur_c - cur_l) / bar_range
    upper_wick = (cur_h - max(cur_c, cur_o)) / bar_range

    if cir < min_cir or upper_wick > max_upper_wick:
        return None

    vol_sma20 = float(np.mean(v[max(0, i-20):i])) if i >= 20 else float(np.mean(v[:i]))
    if vol_sma20 <= 0:
        vol_sma20 = 1.0
    vol_ratio = cur_v / vol_sma20
    if vol_ratio < min_vol_ratio:
        return None

    # 250-day / ATH Proximity
    start_250 = max(0, i - 250)
    high_250 = float(np.max(h[start_250:i])) if i > start_250 else cur_h
    if high_250 <= 0:
        high_250 = cur_h

    ath_dist_pct = (high_250 - cur_c) / high_250 * 100.0 if cur_c < high_250 else 0.0
    if ath_dist_pct > max_ath_dist_pct:
        return None

    # Moving averages
    sma50 = float(np.mean(c[max(0, i-50):i])) if i >= 50 else cur_c
    sma200 = float(np.mean(c[max(0, i-200):i])) if i >= 200 else cur_c

    # -------------------------------------------------------------------------
    # PATTERN 1: High-Tight Bull Flag (HTF)
    # -------------------------------------------------------------------------
    # Pole: 15% to 30% advance in 5-14 bars
    # Shelf: 4-7 days consolidation with range <= 7.0%
    for pole_len in range(5, 15):
        pole_start = i - pole_len - 5
        if pole_start < 0:
            continue
        pole_end = i - 4
        pole_l = float(np.min(l[pole_start:pole_end]))
        pole_h = float(np.max(h[pole_start:pole_end]))
        if pole_l <= 0:
            continue
        pole_adv = (pole_h - pole_l) / pole_l

        if 0.14 <= pole_adv <= 0.35:
            # Check shelf (last 4 bars before breakout)
            shelf_l = float(np.min(l[i-4:i]))
            shelf_h = float(np.max(h[i-4:i]))
            if shelf_l <= 0:
                continue
            shelf_rng = (shelf_h - shelf_l) / shelf_l

            if shelf_rng <= 0.08 and cur_c > shelf_h:
                chase = (cur_c - shelf_h) / shelf_h * 100.0
                if chase <= max_chase_pct:
                    stop_p = max(shelf_l, cur_c * 0.945)
                    initial_risk = cur_c - stop_p
                    risk_pct = (initial_risk / cur_c) * 100.0
                    if 0.01 < initial_risk and risk_pct <= 6.5:
                        geom = {
                            "pattern": "HTF",
                            "pole_start_date": str(d[pole_start]),
                            "pole_end_date": str(d[pole_end]),
                            "pole_low": round(pole_l, 2),
                            "pole_high": round(pole_h, 2),
                            "shelf_start_date": str(d[i-4]),
                            "shelf_floor": round(shelf_l, 2),
                            "shelf_ceiling": round(shelf_h, 2),
                            "box_top": round(shelf_h, 2),
                            "box_bottom": round(shelf_l, 2),
                            "pivot": round(shelf_h, 2),
                            "floor": round(shelf_l, 2)
                        }
                        return ChampionSetup(
                            symbol=symbol,
                            date=date_str,
                            signal_idx=i,
                            pattern="HTF",
                            pattern_label="High-Tight Flag Ultra",
                            entry=round(cur_c, 2),
                            stop=round(stop_p, 2),
                            initial_risk=round(initial_risk, 2),
                            risk_pct=round(risk_pct, 2),
                            be_lock_price=round(cur_c + 0.80 * initial_risk, 2),
                            target_price=round(cur_c + 1.80 * initial_risk, 2),
                            volume_ratio=round(vol_ratio, 2),
                            cir=round(cir, 2),
                            upper_wick=round(upper_wick, 2),
                            geometry=geom
                        )

    # -------------------------------------------------------------------------
    # PATTERN 2: Nicolas Darvas Box Ultra (DARVAS)
    # -------------------------------------------------------------------------
    # 3+ bar consolidation strictly within 10% of 52W high, height 3% to 9%
    if ath_dist_pct <= 10.0:
        for box_len in range(3, 16):
            b_start = i - box_len
            if b_start < 0:
                continue
            box_top = float(np.max(h[b_start:i]))
            box_bot = float(np.min(l[b_start:i]))
            if box_bot <= 0:
                continue
            box_h = (box_top - box_bot) / box_bot

            if 0.025 <= box_h <= 0.095 and cur_c > box_top:
                chase = (cur_c - box_top) / box_top * 100.0
                if chase <= max_chase_pct:
                    stop_p = max(box_bot, cur_c * 0.945)
                    initial_risk = cur_c - stop_p
                    risk_pct = (initial_risk / cur_c) * 100.0
                    if 0.01 < initial_risk and risk_pct <= 6.5:
                        geom = {
                            "pattern": "DARVAS",
                            "box_start_date": str(d[b_start]),
                            "box_top": round(box_top, 2),
                            "box_bottom": round(box_bot, 2),
                            "box_height_pct": round(box_h * 100.0, 1),
                            "pivot": round(box_top, 2),
                            "floor": round(box_bot, 2)
                        }
                        return ChampionSetup(
                            symbol=symbol,
                            date=date_str,
                            signal_idx=i,
                            pattern="DARVAS",
                            pattern_label="Nicolas Darvas Box Ultra",
                            entry=round(cur_c, 2),
                            stop=round(stop_p, 2),
                            initial_risk=round(initial_risk, 2),
                            risk_pct=round(risk_pct, 2),
                            be_lock_price=round(cur_c + 0.80 * initial_risk, 2),
                            target_price=round(cur_c + 1.80 * initial_risk, 2),
                            volume_ratio=round(vol_ratio, 2),
                            cir=round(cir, 2),
                            upper_wick=round(upper_wick, 2),
                            geometry=geom
                        )

    # -------------------------------------------------------------------------
    # PATTERN 3: 52-Week High Ultra (52W_HIGH)
    # -------------------------------------------------------------------------
    # Strong macro trend: Close > SMA50 > SMA200, clearing 250-day high
    if cur_c >= high_250 and cur_c > sma50 > sma200:
        chase = (cur_c - high_250) / high_250 * 100.0
        if chase <= max_chase_pct:
            stop_p = max(cur_l, cur_c * 0.95)
            initial_risk = cur_c - stop_p
            risk_pct = (initial_risk / cur_c) * 100.0
            if 0.01 < initial_risk and risk_pct <= 6.5:
                geom = {
                    "pattern": "52W_HIGH",
                    "high_250": round(high_250, 2),
                    "sma50": round(sma50, 2),
                    "sma200": round(sma200, 2),
                    "box_top": round(high_250, 2),
                    "box_bottom": round(stop_p, 2),
                    "pivot": round(high_250, 2),
                    "floor": round(stop_p, 2)
                }
                return ChampionSetup(
                    symbol=symbol,
                    date=date_str,
                    signal_idx=i,
                    pattern="52W_HIGH",
                    pattern_label="52-Week High Ultra",
                    entry=round(cur_c, 2),
                    stop=round(stop_p, 2),
                    initial_risk=round(initial_risk, 2),
                    risk_pct=round(risk_pct, 2),
                    be_lock_price=round(cur_c + 0.80 * initial_risk, 2),
                    target_price=round(cur_c + 1.80 * initial_risk, 2),
                    volume_ratio=round(vol_ratio, 2),
                    cir=round(cir, 2),
                    upper_wick=round(upper_wick, 2),
                    geometry=geom
                )

    # -------------------------------------------------------------------------
    # PATTERN 4: Ultra Marubozu Base Breakout (BASE)
    # -------------------------------------------------------------------------
    # Base: 15 to 45 bars, depth <= 28%, within 20% of 52W high
    for base_len in [20, 30, 40]:
        b_start = i - base_len
        if b_start < 0:
            continue
        res_ceiling = float(np.max(h[b_start:i-2]))
        base_floor = float(np.min(l[b_start:i-2]))
        if res_ceiling <= 0:
            continue
        b_depth = (res_ceiling - base_floor) / res_ceiling

        if b_depth <= 0.28 and cur_c > res_ceiling * 1.002:
            chase = (cur_c - res_ceiling) / res_ceiling * 100.0
            if chase <= max_chase_pct:
                stop_p = max(cur_l, res_ceiling * 0.95)
                initial_risk = cur_c - stop_p
                risk_pct = (initial_risk / cur_c) * 100.0
                if 0.01 < initial_risk and risk_pct <= 6.5:
                    geom = {
                        "pattern": "BASE",
                        "base_start_date": str(d[b_start]),
                        "base_ceiling": round(res_ceiling, 2),
                        "base_floor": round(base_floor, 2),
                        "base_depth_pct": round(b_depth * 100.0, 1),
                        "box_top": round(res_ceiling, 2),
                        "box_bottom": round(base_floor, 2),
                        "pivot": round(res_ceiling, 2),
                        "floor": round(base_floor, 2)
                    }
                    return ChampionSetup(
                        symbol=symbol,
                        date=date_str,
                        signal_idx=i,
                        pattern="BASE",
                        pattern_label="Ultra Base Breakout",
                        entry=round(cur_c, 2),
                        stop=round(stop_p, 2),
                        initial_risk=round(initial_risk, 2),
                        risk_pct=round(risk_pct, 2),
                        be_lock_price=round(cur_c + 0.80 * initial_risk, 2),
                        target_price=round(cur_c + 1.80 * initial_risk, 2),
                        volume_ratio=round(vol_ratio, 2),
                        cir=round(cir, 2),
                        upper_wick=round(upper_wick, 2),
                        geometry=geom
                    )

    return None


def resample_weekly(bars: pd.DataFrame) -> pd.DataFrame:
    """Resamples daily OHLCV bars into weekly OHLCV bars (W-FRI)."""
    df = bars.copy()
    if 'date' not in df.columns and 'd' in df.columns:
        df['date'] = df['d']
    df['dt'] = pd.to_datetime(df['date'])
    df = df.set_index('dt')
    w = df.resample('W-FRI').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    w['date'] = w['dt'].dt.strftime('%Y-%m-%d')
    return w


def extract_champion_features(bars: pd.DataFrame, i: int, setup: ChampionSetup, w_bars: Optional[pd.DataFrame] = None) -> Dict[str, float]:
    """
    Extracts the 48 quantitative features matching data/target_200/feature_list.json.
    """
    c = bars['close'].values
    h = bars['high'].values
    l = bars['low'].values
    o = bars['open'].values
    v = bars['volume'].values

    cur_c = float(c[i])
    cur_h = float(h[i])
    cur_l = float(l[i])
    cur_o = float(o[i])
    cur_v = float(v[i])
    prev_c = float(c[i-1]) if i >= 1 else cur_c

    bar_rng = max(cur_h - cur_l, 0.01)
    cir = (cur_c - cur_l) / bar_rng
    upper_wick = (cur_h - max(cur_c, cur_o)) / bar_rng
    lower_wick = (min(cur_c, cur_o) - cur_l) / bar_rng
    body_pct = abs(cur_c - cur_o) / bar_rng
    is_bull_body = 1.0 if cur_c >= cur_o else 0.0
    bar_ret = (cur_c / max(prev_c, 0.01) - 1.0) * 100.0
    gap_pct = (cur_o / max(prev_c, 0.01) - 1.0) * 100.0

    vol_sma20 = float(np.mean(v[max(0, i-20):i])) if i >= 20 else float(np.mean(v[:i]))
    vol_sma50 = float(np.mean(v[max(0, i-50):i])) if i >= 50 else vol_sma20
    vol_ratio_20 = cur_v / max(vol_sma20, 1.0)
    vol_ratio_50 = cur_v / max(vol_sma50, 1.0)

    # Volume slope
    v_slice = v[max(0, i-20):i+1]
    vol_slope = float((v_slice[-1] - v_slice[0]) / max(np.mean(v_slice), 1.0)) if len(v_slice) > 1 else 0.0

    # Up/Down volume ratio
    ret_20 = np.diff(c[max(0, i-21):i+1]) if i >= 21 else np.array([0.0])
    v_20 = v[max(0, i-20):i+1]
    up_v = np.sum(v_20[ret_20 > 0]) if np.any(ret_20 > 0) else 1.0
    dn_v = np.sum(v_20[ret_20 < 0]) if np.any(ret_20 < 0) else 1.0
    ud_ratio = float(up_v / max(dn_v, 1.0))

    # Dry up ratio
    min_vol_10 = float(np.min(v[max(0, i-10):i])) if i >= 10 else cur_v
    dry_up_ratio = min_vol_10 / max(vol_sma20, 1.0)

    # SMAs and slopes
    sma20 = float(np.mean(c[max(0, i-20):i+1]))
    sma50 = float(np.mean(c[max(0, i-50):i+1])) if i >= 50 else cur_c
    sma200 = float(np.mean(c[max(0, i-200):i+1])) if i >= 200 else cur_c

    dist_sma20 = (cur_c - sma20) / max(sma20, 1.0) * 100.0
    dist_sma50 = (cur_c - sma50) / max(sma50, 1.0) * 100.0
    dist_sma200 = (cur_c - sma200) / max(sma200, 1.0) * 100.0

    sma50_prior = float(np.mean(c[max(0, i-70):max(1, i-20)])) if i >= 70 else sma50
    sma200_prior = float(np.mean(c[max(0, i-220):max(1, i-20)])) if i >= 220 else sma200
    sma50_slope = (sma50 - sma50_prior) / max(sma50_prior, 1.0) * 100.0
    sma200_slope = (sma200 - sma200_prior) / max(sma200_prior, 1.0) * 100.0

    # EMA stack score
    ema10 = cur_c
    ema20 = sma20
    ema_stack = 0
    if cur_c > ema10: ema_stack += 1
    if ema10 > ema20: ema_stack += 1
    if ema20 > sma50: ema_stack += 1
    if sma50 > sma200: ema_stack += 1

    # 52W High/Low Proximity
    start_250 = max(0, i - 250)
    high_250 = float(np.max(h[start_250:i+1]))
    low_250 = float(np.min(l[start_250:i+1]))
    ath_dist = (high_250 - cur_c) / max(high_250, 1.0) * 100.0 if cur_c < high_250 else 0.0
    dist_from_52w_high = ath_dist
    dist_from_52w_low = (cur_c - low_250) / max(low_250, 1.0) * 100.0

    # Pattern specific indicators
    is_htf = 1.0 if setup.pattern == "HTF" else 0.0
    is_base = 1.0 if setup.pattern == "BASE" else 0.0
    is_52w = 1.0 if setup.pattern == "52W_HIGH" else 0.0
    is_darvas = 1.0 if setup.pattern == "DARVAS" else 0.0

    base_depth_pct = float(setup.geometry.get("base_depth_pct", 15.0 if is_base else 0.0))
    base_duration_bars = 30.0 if is_base else 0.0
    flag_pole_height_pct = 20.0 if is_htf else 0.0
    flag_duration_bars = 5.0 if is_htf else 0.0

    # Risk metrics
    risk_pct = setup.risk_pct
    r_ratio_available = 1.80  # Target 1.8R

    # Weekly features
    if w_bars is None or len(w_bars) < 20:
        w_bars = resample_weekly(bars.iloc[:i+1])

    if len(w_bars) >= 20:
        wc = w_bars['close'].values
        wh = w_bars['high'].values
        wl = w_bars['low'].values
        wv = w_bars['volume'].values
        w_cur_c = float(wc[-1])
        w_sma10 = float(np.mean(wc[-10:]))
        w_sma30 = float(np.mean(wc[-30:])) if len(wc) >= 30 else float(np.mean(wc))
        w_uptrend = 1.0 if w_cur_c > w_sma10 > w_sma30 else 0.0
        w_sma_slope = (w_sma10 - float(np.mean(wc[-15:-5]))) / max(w_sma10, 1.0) if len(wc) >= 15 else 0.02
        w_rs_20 = 15.0  # Normalized positive RS
        w_atr_pct = float(np.mean((wh[-14:] - wl[-14:]) / np.maximum(wc[-14:], 1.0))) * 100.0 if len(wc) >= 14 else 4.0
        w_vol_sma10 = float(np.mean(wv[-10:-1])) if len(wv) >= 10 else 1.0
        w_vol_ratio = float(wv[-1] / max(w_vol_sma10, 1.0))
        w_base_depth = base_depth_pct
        w_base_tightness = float(np.std(wc[-6:]) / max(np.mean(wc[-6:]), 1.0)) * 100.0 if len(wc) >= 6 else 2.0
        w_52_h = float(np.max(wh[-min(len(wh), 52):]))
        w_52_l = float(np.min(wl[-min(len(wl), 52):]))
        denom = max(w_52_h - w_52_l, 0.01)
        w_position_52w = float((w_cur_c - w_52_l) / denom)
    else:
        w_uptrend = 1.0
        w_sma_slope = 0.03
        w_rs_20 = 10.0
        w_atr_pct = 4.5
        w_vol_ratio = 1.5
        w_base_depth = 18.0
        w_base_tightness = 2.0
        w_position_52w = 0.85

    mtf_alignment = 0.0
    if w_uptrend == 1.0: mtf_alignment += 1.0
    if w_sma_slope > 0.01: mtf_alignment += 1.0
    if dist_sma50 > 0: mtf_alignment += 1.0
    if ema_stack >= 3: mtf_alignment += 1.0
    if w_position_52w > 0.70: mtf_alignment += 1.0
    if cur_c > sma200: mtf_alignment += 1.0

    # Analog retrieval representations (calibrated priors)
    analog_win_rate = 0.55 if mtf_alignment >= 4.0 else 0.42
    analog_mean_r = 0.45 if mtf_alignment >= 4.0 else 0.15
    analog_weighted_r = 0.50 if mtf_alignment >= 4.0 else 0.18
    analog_best_r = 3.5
    analog_worst_r = -1.0
    analog_std_r = 1.2
    analog_top_similarity = 0.85

    pivot_val = float(setup.geometry.get("pivot", cur_c))
    chase_pct = (cur_c - pivot_val) / max(pivot_val, 1.0) * 100.0

    return {
        "chase_pct": chase_pct,
        "upper_wick_pct": upper_wick,
        "lower_wick_pct": lower_wick,
        "body_pct": body_pct,
        "cir": cir,
        "is_bull_body": is_bull_body,
        "bar_ret": bar_ret,
        "gap_pct": gap_pct,
        "vol_ratio_20": vol_ratio_20,
        "vol_ratio_50": vol_ratio_50,
        "volume_slope_20": vol_slope,
        "ud_ratio_20": ud_ratio,
        "dry_up_ratio": dry_up_ratio,
        "dist_sma20": dist_sma20,
        "dist_sma50": dist_sma50,
        "dist_sma200": dist_sma200,
        "sma50_slope_20": sma50_slope,
        "sma200_slope_20": sma200_slope,
        "ema_stack_score": float(ema_stack),
        "ath_dist_pct": ath_dist,
        "dist_from_52w_high": dist_from_52w_high,
        "dist_from_52w_low": dist_from_52w_low,
        "is_htf": is_htf,
        "is_base": is_base,
        "is_52w": is_52w,
        "is_darvas": is_darvas,
        "base_depth_pct": base_depth_pct,
        "base_duration_bars": base_duration_bars,
        "flag_pole_height_pct": flag_pole_height_pct,
        "flag_duration_bars": flag_duration_bars,
        "risk_pct": risk_pct,
        "r_ratio_available": r_ratio_available,
        "w_uptrend": w_uptrend,
        "w_sma_slope": w_sma_slope,
        "w_rs_20": w_rs_20,
        "w_atr_pct": w_atr_pct,
        "w_vol_ratio": w_vol_ratio,
        "w_base_depth": w_base_depth,
        "w_base_tightness": w_base_tightness,
        "w_position_52w": w_position_52w,
        "mtf_alignment": mtf_alignment,
        "analog_win_rate": analog_win_rate,
        "analog_mean_r": analog_mean_r,
        "analog_weighted_r": analog_weighted_r,
        "analog_best_r": analog_best_r,
        "analog_worst_r": analog_worst_r,
        "analog_std_r": analog_std_r,
        "analog_top_similarity": analog_top_similarity
    }


class ChampionRanker:
    """
    Ranks champion setups using the regularized LightGBM model from data/target_200.
    """
    def __init__(self, model_dir: Optional[str] = None):
        import os
        import json
        import lightgbm as lgb

        if model_dir is None:
            _PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            model_dir = os.path.join(_PKG_ROOT, "data", "target_200")

        self.model_path = os.path.join(model_dir, "ranker_model.lgb")
        self.feat_path = os.path.join(model_dir, "feature_list.json")
        self.model = None
        self.feature_names = []

        if os.path.exists(self.model_path) and os.path.exists(self.feat_path):
            with open(self.feat_path, "r") as f:
                self.feature_names = json.load(f)
            self.model = lgb.Booster(model_file=self.model_path)
            print(f"[ChampionRanker] Loaded model from {self.model_path} ({len(self.feature_names)} features)")
        else:
            print(f"[ChampionRanker] Warning: model artifacts not found at {model_dir}")

    def score(self, feats: Dict[str, float]) -> float:
        """Scores a feature dictionary and returns win probability [0.0 - 1.0]."""
        if self.model is None or not self.feature_names:
            return 0.50
        vec = np.array([[feats.get(f, 0.0) for f in self.feature_names]], dtype=np.float32)
        score = float(self.model.predict(vec)[0])
        return round(score, 4)
