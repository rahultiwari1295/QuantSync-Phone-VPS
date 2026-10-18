"""QuantSync's five price-action breakout signals as point-in-time boolean columns.

Ported from the live engine's signal modules (engines/v2/signals/p1,p2,p4,p5,p9). Every value at bar t uses only
bars <= t. Thresholds are the engine's own; where the engine picks a *stop* it is only used for its own risk filter
(the Lab simulator applies the strategy's exit rules instead).
Daily bars only (weekly bars get NaN)."""
from __future__ import annotations

import numpy as np
import pandas as pd

SIGNALS = {
    "sig_rs_leader": "RS leader breakout",
    "sig_stage2_base": "Stage-2 base breakout",
    "sig_power_gap": "Power gap",
    "sig_apex_base": "Apex base breakout",
    "sig_high_tight_flag": "High tight flag",
    "sig_v3_htf": "v3 High-tight flag Ultra",
    "sig_v3_darvas": "v3 Darvas box Ultra",
    "sig_v3_52wh": "v3 52-week high Ultra",
    "sig_v3_base": "v3 Ultra base breakout",
    "sig_v3_rs_refined": "v3 RS leader refined",
    "sig_v4_f3": "v4 F3 Moonbag pattern",
}

try:
    from classical_pattern_dna import ClassicalPatternAnalystDNA
except ImportError:
    try:
        import sys
        from pathlib import Path
        for p in (Path(__file__).resolve().parent.parent.parent, Path.cwd().parent, Path.cwd()):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        from classical_pattern_dna import ClassicalPatternAnalystDNA
    except Exception:
        ClassicalPatternAnalystDNA = None

WIN_PATTERNS = {
    "FALLING_WEDGE": 1.0,
    "DOUBLE_BOTTOM": 2.0,
    "INVERSE_HEAD_AND_SHOULDERS": 3.0,
    "SYMMETRICAL_TRIANGLE": 4.0,
    "BULLISH_RECTANGLE": 5.0,
}


def _find_base_start(highs, lows, closes, T, min_depth=0.05, max_depth=0.35, min_bars=15, max_lookback=250):
    for peak in range(T - min_bars, max(0, T - max_lookback), -1):
        pivot = highs[peak]
        if pivot < np.max(highs[max(0, peak - 10):min(T, peak + 11)]):
            continue
        depth = (pivot - float(np.min(lows[peak:T]))) / max(pivot, 0.01)
        if not (min_depth <= depth <= max_depth):
            continue
        if np.any(closes[peak + 1:T] > pivot * 1.015):
            continue
        return peak
    return None


def add_signals(f: pd.DataFrame, tf: str) -> None:
    """Adds sig_* columns to f in place. f needs open/high/low/close/volume, sma_*, vol_sma_20, hh_20, atr_14."""
    n = len(f)
    if tf != "1D":
        for k in SIGNALS:
            f[k] = np.nan
        f["p1_risk_pct"] = np.nan
        f["v3_risk_pct"] = np.nan
        f["v3_conviction"] = np.nan
        f["v4_pattern"] = np.nan
        return
    o, h, l, c, v = (f[k].to_numpy("float64") for k in ("open", "high", "low", "close", "volume"))
    pc = np.r_[np.nan, c[:-1]]
    idx = np.arange(n)
    rng = h - l
    with np.errstate(all="ignore"):
        ok_rng = rng > 0
        cir = np.where(ok_rng, (c - l) / rng, np.nan)
        uw = np.where(ok_rng, (h - np.maximum(c, o)) / rng, np.nan)
        turnover = c * v
        vs20 = f["vol_sma_20"].to_numpy("float64")
        vr_incl = v / np.where(vs20 > 0, vs20, np.nan)
        vprior = pd.Series(v).shift(1).rolling(20, min_periods=20).mean().to_numpy()
        vr_prior = np.round(v / np.where(vprior > 0, vprior, np.nan), 2)   # the engine rounds this ratio to 2 dp before comparing
        s50, s150, s200 = (f[f"sma_{k}"].to_numpy("float64") for k in (50, 150, 200))
        s200 = np.where(np.isnan(s200), s50, s200)   # the engine falls back to the 50-bar average on young charts
        hh20 = f["hh_20"].to_numpy("float64")
        atr = f["atr_14"].to_numpy("float64")
        hs, ls = pd.Series(h), pd.Series(l)
        hh40, ll40 = hs.shift(1).rolling(40, min_periods=40).max().to_numpy(), ls.shift(1).rolling(40, min_periods=40).min().to_numpy()
        hh60, ll60 = hs.shift(1).rolling(60, min_periods=60).max().to_numpy(), ls.shift(1).rolling(60, min_periods=60).min().to_numpy()

        # P1 RS leader: 20-bar breakout on 2x volume, strong close, not stretched
        base40 = (hh40 - ll40) / hh40 * 100
        p1 = ((idx >= 60) & (c >= 20) & (turnover >= 5e6) & (c > o) & (cir >= 0.70) & (uw <= 0.12) & (c >= hh20)
              & ((c - hh20) / hh20 * 100 <= 3.5) & (vr_prior >= 2.0) & ((c / s200 - 1) * 100 <= 55) & (c > s50) & (base40 <= 28))
        p1_risk_pct = np.round((c - np.maximum(l * 0.99, c * 0.94)) / c * 100.0, 2)

        # P9 Weinstein stage-2 base breakout
        s150_prev = np.r_[np.full(20, np.nan), s150[:-20]] if n > 20 else np.full(n, np.nan)
        depth60 = (hh60 - ll60) / hh60 * 100
        stop9 = np.round(np.maximum.reduce([hh60 * 0.97, c - 1.5 * atr, c * 0.93]), 2)   # the engine rounds stops to paise
        risk9 = (c - stop9) / c * 100
        p9 = ((idx >= 150) & (c >= 20) & (c >= s150) & ~(s150 < s150_prev * 0.99) & (depth60 >= 10) & (depth60 <= 35)
              & (c >= hh60) & (c > o) & (cir >= 0.65) & (uw <= 0.20) & (vr_incl >= 1.8) & (turnover >= 2e7) & (risk9 >= 1.5) & (risk9 <= 7.0))
        # P4 power earnings gap
        gap = (o - pc) / pc * 100
        gsize = o - pc
        fill = np.where(gsize > 0, (o - l) / gsize * 100, 100.0)
        stop4 = np.round(np.maximum(l * 0.99, pc + 0.35 * gsize), 2)
        risk4 = (c - stop4) / c * 100
        p4 = ((idx >= 40) & (c >= 20) & (gap >= 3.5) & (cir >= 0.70) & (uw <= 0.15) & (fill <= 40) & (vr_incl >= 3.0)
              & (turnover >= 5e7) & (risk4 >= 2.0) & (risk4 <= 8.0))

    # P2 apex base and P5 high tight flag need pattern search: run the engine's own logic on the few bars that pass cheap filters
    p2 = np.zeros(n, bool)
    p5 = np.zeros(n, bool)
    cand2 = np.flatnonzero((idx >= 40) & (c >= 20) & (c > o) & (turnover >= 1e7) & (cir >= 0.85) & (uw <= 0.10) & (vr_prior >= 2.2))
    for i in cand2:
        pk = _find_base_start(h, l, c, i)
        if pk is None:
            continue
        pivot, floor = h[pk], float(np.min(l[pk:i + 1]))
        depth, weeks = (pivot - floor) / max(pivot, 0.01) * 100, (i - pk) / 5.0
        if not (4.0 <= weeks <= 80.0 and 5.0 <= depth <= 35.0) or c[i] < pivot * 0.995 or (c[i] - pivot) / pivot * 100 > 3.0:
            continue
        s200i = float(np.mean(c[max(0, i - 199):i + 1])) if i >= 199 else float(np.mean(c[max(0, i - 49):i + 1]))
        if (c[i] - s200i) / s200i * 100 > 55:
            continue
        ath = float(np.max(h[:i + 1]))
        if (ath - c[i]) / ath * 100 > 35:
            continue
        stop = round(max(l[i], floor, round(c[i] * 0.955, 2)), 2)
        if stop >= c[i] * 0.985:
            stop = round(c[i] * 0.955, 2)
        if (c[i] - stop) / c[i] * 100 > 5.5:
            continue
        p2[i] = True
    cand5 = np.flatnonzero((idx >= 70) & (c >= 20) & (c > o) & (turnover >= 2e7) & (cir >= 0.65) & (uw <= 0.20) & (vr_incl >= 1.6))
    for i in cand5:
        s = max(0, i - 25)
        pk = s + int(np.argmax(h[s:i]))
        if not (8 <= i - pk <= 25):
            continue
        ps = max(0, pk - 40)
        if pk <= ps:
            continue
        low0 = float(np.min(l[ps:pk]))
        if low0 <= 0 or (h[pk] - low0) / low0 * 100 < 70.0:
            continue
        flag_low = float(np.min(l[pk:i]))
        if (h[pk] - flag_low) / h[pk] * 100 > 20.0 or c[i] < h[pk] * 0.99:
            continue
        risk = (c[i] - round(max(flag_low * 0.99, c[i] * 0.935), 2)) / c[i] * 100
        if 2.0 <= risk <= 7.5:
            p5[i] = True

    # -------------------------------------------------------------------------
    # v3 Champion Patterns: HTF -> Darvas -> 52W High -> Base (evaluated in order)
    # -------------------------------------------------------------------------
    v3_htf = np.zeros(n, bool)
    v3_darvas = np.zeros(n, bool)
    v3_52wh = np.zeros(n, bool)
    v3_base = np.zeros(n, bool)
    v3_risk_pct = np.full(n, np.nan, dtype="float64")
    v3_conviction = np.full(n, np.nan, dtype="float64")

    # Cheap pre-filter for v3: min 60 bars, green, CIR >= 0.85, wick <= 0.10, vol >= 2.5x prior
    cand_v3 = np.flatnonzero((idx >= 60) & (c > o) & (cir >= 0.85) & (uw <= 0.10) & (vr_prior >= 2.5))
    for i in cand_v3:
        high_250 = float(np.max(h[max(0, i - 250):i])) if i > 0 else h[i]
        if high_250 <= 0:
            high_250 = h[i]
        ath_dist_pct = (high_250 - c[i]) / high_250 * 100.0 if c[i] < high_250 else 0.0
        if ath_dist_pct > 20.0:
            continue

        matched = None
        stop_p = None

        # 1. HTF Ultra
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
                shelf_l = float(np.min(l[i - 4:i]))
                shelf_h = float(np.max(h[i - 4:i]))
                if shelf_l <= 0:
                    continue
                shelf_rng = (shelf_h - shelf_l) / shelf_l
                if shelf_rng <= 0.08 and c[i] > shelf_h:
                    chase = (c[i] - shelf_h) / shelf_h * 100.0
                    if chase <= 8.0:
                        st = max(shelf_l, c[i] * 0.945)
                        init_risk = c[i] - st
                        rp = (init_risk / c[i]) * 100.0
                        if 0.01 < init_risk and rp <= 6.5:
                            matched = "HTF"
                            stop_p = st
                            break

        # 2. Darvas Box Ultra
        if matched is None and ath_dist_pct <= 10.0:
            for box_len in range(3, 16):
                b_start = i - box_len
                if b_start < 0:
                    continue
                box_top = float(np.max(h[b_start:i]))
                box_bot = float(np.min(l[b_start:i]))
                if box_bot <= 0:
                    continue
                box_h = (box_top - box_bot) / box_bot
                if 0.025 <= box_h <= 0.095 and c[i] > box_top:
                    chase = (c[i] - box_top) / box_top * 100.0
                    if chase <= 8.0:
                        st = max(box_bot, c[i] * 0.945)
                        init_risk = c[i] - st
                        rp = (init_risk / c[i]) * 100.0
                        if 0.01 < init_risk and rp <= 6.5:
                            matched = "DARVAS"
                            stop_p = st
                            break

        # 3. 52-Week High Ultra
        if matched is None and c[i] >= high_250:
            sma50_i = float(np.mean(c[max(0, i - 50):i])) if i >= 50 else c[i]
            sma200_i = float(np.mean(c[max(0, i - 200):i])) if i >= 200 else c[i]
            if c[i] > sma50_i > sma200_i:
                chase = (c[i] - high_250) / high_250 * 100.0
                if chase <= 8.0:
                    st = max(l[i], c[i] * 0.95)
                    init_risk = c[i] - st
                    rp = (init_risk / c[i]) * 100.0
                    if 0.01 < init_risk and rp <= 6.5:
                        matched = "52WH"
                        stop_p = st

        # 4. Ultra Base Breakout
        if matched is None:
            for base_len in (20, 30, 40):
                b_start = i - base_len
                if b_start < 0:
                    continue
                res_ceiling = float(np.max(h[b_start:i - 2]))
                base_floor = float(np.min(l[b_start:i - 2]))
                if res_ceiling <= 0:
                    continue
                b_depth = (res_ceiling - base_floor) / res_ceiling
                if b_depth <= 0.28 and c[i] > res_ceiling * 1.002:
                    chase = (c[i] - res_ceiling) / res_ceiling * 100.0
                    if chase <= 8.0:
                        st = max(l[i], res_ceiling * 0.95)
                        init_risk = c[i] - st
                        rp = (init_risk / c[i]) * 100.0
                        if 0.01 < init_risk and rp <= 6.5:
                            matched = "BASE"
                            stop_p = st
                            break

        if matched is not None:
            risk_pct_val = round((c[i] - stop_p) / c[i] * 100.0, 2)
            v3_risk_pct[i] = risk_pct_val
            cir_val = cir[i]
            uw_val = uw[i]
            vr_val = vr_prior[i]
            score_candle = (cir_val * 20.0) + ((1.0 - min(uw_val, 1.0)) * 20.0)
            score_vol = min(vr_val / 5.0, 1.0) * 25.0
            score_ath = max(0.0, (20.0 - ath_dist_pct) / 20.0) * 20.0
            ret60_val = (c[i] / c[i - 60] - 1.0) * 100.0 if i >= 60 else 0.0
            score_trend = max(0.0, min((ret60_val - 15.0) / 30.0, 1.0)) * 15.0
            v3_conviction[i] = round(score_candle + score_vol + score_ath + score_trend, 1)

            if matched == "HTF":
                v3_htf[i] = True
            elif matched == "DARVAS":
                v3_darvas[i] = True
            elif matched == "52WH":
                v3_52wh[i] = True
            elif matched == "BASE":
                v3_base[i] = True

    # -------------------------------------------------------------------------
    # v3 RS Leader Refined
    # -------------------------------------------------------------------------
    s_ema50 = f["ema_50"].to_numpy("float64") if "ema_50" in f else s50
    s_ema200 = f["ema_200"].to_numpy("float64") if "ema_200" in f else s200
    chase_20 = np.where(hh20 > 0, (c - hh20) / hh20 * 100.0, 0.0)
    dist_sma200_val = np.where(s200 > 0, (c / s200 - 1.0) * 100.0, 0.0)
    c60 = pd.Series(c).shift(60).to_numpy("float64")
    stock_ret60_arr = np.where(c60 > 0, (c / c60 - 1.0) * 100.0, 0.0)
    c_prev_arr = np.r_[np.nan, c[:-1]]
    bar_ret_arr = np.where(c_prev_arr > 0, (c / c_prev_arr - 1.0) * 100.0, 0.0)
    rs60_pct_val = f["rs60_pct"].to_numpy("float64") if "rs60_pct" in f.columns else np.zeros(n, dtype="float64")

    v3_rs_refined = (
        (idx >= 60) & (c >= 40.0) & (turnover >= 1.5e7) & (turnover <= 2.5e9) &
        (c > o) & (bar_ret_arr >= 4.0) & (cir >= 0.75) & (uw <= 0.20) &
        (vr_prior >= 2.50) & (c > s_ema50) & (s_ema50 > s_ema200) &
        (c >= hh20) & (chase_20 >= 1.5) & (chase_20 <= 8.0) &
        ~((chase_20 <= 1.0) & (uw > 0.18)) &
        ~((chase_20 <= 1.0) & (vr_prior < 2.0)) &
        ~((dist_sma200_val > 30.0) & (uw > 0.20)) &
        (stock_ret60_arr >= 15.0) &
        (rs60_pct_val >= 95.0)
    )

    # -------------------------------------------------------------------------
    # v4 F3 Classical Pattern + Stage 2
    # -------------------------------------------------------------------------
    v4_f3 = np.zeros(n, bool)
    v4_pattern = np.zeros(n, dtype="float64")
    above_200_50 = f["above_ema200_50"].to_numpy("float64") if "above_ema200_50" in f.columns else np.zeros(n)
    dist_e50 = f["dist_ema_50_pct"].to_numpy("float64") if "dist_ema_50_pct" in f.columns else np.zeros(n)
    dist_e200 = f["dist_ema_200_pct"].to_numpy("float64") if "dist_ema_200_pct" in f.columns else np.zeros(n)

    cand_v4 = np.flatnonzero(
        (idx >= 50) & (above_200_50 >= 40) & (dist_e50 <= 12.0) & (dist_e200 <= 15.0) & (cir >= 0.65) & (uw <= 0.28)
    )
    if ClassicalPatternAnalystDNA is not None and len(cand_v4) > 0:
        try:
            analyst = ClassicalPatternAnalystDNA()
            for i in cand_v4:
                sub_df = f.iloc[max(0, i - 119):i + 1][["date", "open", "high", "low", "close", "volume"]]
                pat = analyst.analyze(sub_df)
                if pat and pat.pattern_type in WIN_PATTERNS:
                    v4_f3[i] = True
                    v4_pattern[i] = WIN_PATTERNS[pat.pattern_type]
        except Exception:
            pass

    # Assign all signals to dataframe
    sig_arrays = [
        ("sig_rs_leader", p1),
        ("sig_stage2_base", p9),
        ("sig_power_gap", p4),
        ("sig_apex_base", p2),
        ("sig_high_tight_flag", p5),
        ("sig_v3_htf", v3_htf),
        ("sig_v3_darvas", v3_darvas),
        ("sig_v3_52wh", v3_52wh),
        ("sig_v3_base", v3_base),
        ("sig_v3_rs_refined", v3_rs_refined),
        ("sig_v4_f3", v4_f3),
    ]
    for k, arr in sig_arrays:
        f[k] = arr.astype("float64")

    f["p1_risk_pct"] = p1_risk_pct.astype("float64")
    f["v3_risk_pct"] = v3_risk_pct
    f["v3_conviction"] = v3_conviction
    f["v4_pattern"] = v4_pattern

    early = np.arange(n) < 40
    for k in SIGNALS:
        f.loc[early, k] = np.nan

