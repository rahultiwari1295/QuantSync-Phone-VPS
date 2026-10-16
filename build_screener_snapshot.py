#!/usr/bin/env python3
"""
build_screener_snapshot.py — QuantSync Institutional Screener Engine
====================================================================
Transforms pre-computed Stock DNA reports, CompanyMaster fundamentals,
and StockTechnicalsDaily indicators into a fast, queryable screener table
(stock_screener_rows in Data/stock_dna_cache.db).

Strict NULL Policy:
- Fields with missing or partial data are written as NULL.
- Zero fabricated constants. Zero synthetic overrides.
"""

import os
import sys
import json
import sqlite3
import datetime
from typing import Optional, Dict, Any, List, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_DB_PATH = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")
CACHE_DB_PATH = os.path.join(SCRIPT_DIR, "Data", "stock_dna_cache.db")


def compute_breakout_status(
    curr_price: float,
    breakout_level: Optional[float],
    support_1: Optional[float],
    closes_list: List[float],
    vol_ratios_list: List[float],
    dates_list: Optional[List[str]] = None,
    scan_date: Optional[str] = None,
    target_1: Optional[float] = None,
    recent_highs: Optional[List[float]] = None
) -> Tuple[str, Optional[int], int]:
    """
    Walks forward (oldest -> newest) to identify the first crossing of breakout_level.
    Returns (breakout_status, days_since_breakout, breakout_volume_spike)

    days_since_breakout is computed from the ACTUAL breakout candle date to scan_date
    using real calendar days — NOT candle index arithmetic. This correctly handles
    weekly candles where 1 candle ago != 1 calendar day ago.
    """
    if breakout_level is None or breakout_level <= 0 or not closes_list:
        return 'NA', None, 0

    first_cross_idx = None
    for i, close in enumerate(closes_list):
        if close is not None and close > breakout_level:
            first_cross_idx = i
            break

    if first_cross_idx is None:
        return 'PENDING', None, 0

    # Compute real calendar days from breakout candle date to scan_date
    days_since = None
    if dates_list and first_cross_idx < len(dates_list) and dates_list[first_cross_idx]:
        try:
            breakout_dt = datetime.datetime.strptime(dates_list[first_cross_idx][:10], "%Y-%m-%d").date()
            ref_dt = datetime.datetime.strptime(scan_date[:10], "%Y-%m-%d").date() if scan_date else datetime.date.today()
            days_since = (ref_dt - breakout_dt).days
        except Exception:
            # Fallback to candle index arithmetic if date parsing fails
            days_since = len(closes_list) - 1 - first_cross_idx
    else:
        # No dates available — fallback to candle index count
        days_since = len(closes_list) - 1 - first_cross_idx

    vol_ratio_at_cross = vol_ratios_list[first_cross_idx] if first_cross_idx < len(vol_ratios_list) else None
    vol_spike = 1 if (vol_ratio_at_cross is not None and vol_ratio_at_cross > 1.5) else 0

    # If the target was achieved post-breakout, mark as TARGET_ACHIEVED
    if target_1 and target_1 > 0:
        max_h = max(recent_highs) if recent_highs else curr_price
        if (curr_price and curr_price >= target_1) or (max_h and max_h >= target_1):
            return 'TARGET_ACHIEVED', days_since, vol_spike

    if curr_price is not None and curr_price > breakout_level * 1.03:
        return 'SUCCESS', days_since, vol_spike
    elif support_1 is not None and curr_price is not None and curr_price < support_1:
        return 'FAILED', days_since, vol_spike
    else:
        return 'POST_BREAKOUT', days_since, vol_spike


# Bearish pattern name keywords — any pattern_type/pattern_name containing these
# words must never be classified as NEAR_BREAKOUT for a buy signal.
_BEARISH_PATTERN_KEYWORDS = (
    "head & shoulders", "head and shoulders", "h&s",
    "double top", "m-distribution", "distribution top",
    "rising wedge", "descending triangle", "bearish rectangle",
    "breakdown", "topping"
)


def is_bearish_pattern(pattern_type: Optional[str]) -> bool:
    """Returns True if the pattern name contains known bearish pattern keywords."""
    if not pattern_type:
        return False
    pt_lower = pattern_type.lower()
    # Inverse / Inverted Head & Shoulders is a textbook BULLISH reversal pattern, not bearish
    if "inverse" in pt_lower or "inverted" in pt_lower or "ihs" in pt_lower:
        return False
    return any(kw in pt_lower for kw in _BEARISH_PATTERN_KEYWORDS)


def compute_pattern_stage(
    curr_price: Optional[float],
    breakout_level: Optional[float],
    bb_width_pct: Optional[float],
    adx14: Optional[float],
    consecutive_days_near_prev: int,
    breakout_status: str,
    stance: Optional[str] = None  # Bug Fix 2: stance-aware gate
) -> Tuple[str, int]:
    """
    Evaluates maturity and compression: requires volatility contraction (squeeze)
    or sustained proximity (>= 3 days) to validate NEAR_BREAKOUT.

    IMPORTANT: Only BULLISH/ACCUMULATION stances can reach NEAR_BREAKOUT.
    Neutral and bearish stocks are capped at FORMING regardless of geometry.
    This prevents 'Wait on Sidelines' stocks from appearing in the Near Breakout page.
    """
    # Gate 1: Only bullish setups can be near a breakout
    effective_stance = (stance or '').upper()
    if effective_stance not in ('BULLISH', 'ACCUMULATION', ''):
        return 'FORMING', 0

    if breakout_status == 'TARGET_ACHIEVED':
        return 'EXHAUSTED', 0

    if breakout_status in ('SUCCESS', 'FAILED', 'POST_BREAKOUT', 'WEAK_BREAKOUT', 'RETEST', 'EXTENDED'):
        return 'POST_BREAKOUT', consecutive_days_near_prev

    if breakout_level is None or breakout_level <= 0 or curr_price is None or curr_price <= 0:
        return 'FORMING', 0

    # distance measured against the breakout level — same yardstick as the Radar API
    dist_pct = abs((breakout_level - curr_price) / breakout_level * 100)

    if dist_pct <= 3.0:
        consecutive_days_near = (consecutive_days_near_prev or 0) + 1
        squeeze_confirmed = (
            (bb_width_pct is not None and bb_width_pct < 8.0) or
            (adx14 is not None and adx14 < 20.0) or
            (consecutive_days_near >= 3)
        )
        if squeeze_confirmed:
            return 'NEAR_BREAKOUT', consecutive_days_near
        else:
            return 'MATURE', consecutive_days_near
    else:
        return 'FORMING', 0


def build_all(scan_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Constructs the stock_screener_rows snapshot for the universe.
    Reads:
      - stock_dna_reports from stock_dna_cache.db
      - CompanyMaster from stock_history.db
      - StockTechnicalsDaily from stock_history.db
      - Yesterday's stock_screener_rows for consecutive_days_near
    """
    from common.market_calendar import is_trading_day, last_completed_session, previous_trading_day, ist_now
    if not scan_date:
        today = ist_now().date()
        # never stamp a scan on a weekend/holiday: it would be a copy of the last session
        scan_date = (today if is_trading_day(today) else last_completed_session()).isoformat()
    scan_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[*] Building Screener Snapshot for Date: {scan_date} (TS: {scan_ts})", flush=True)

    cache_conn = sqlite3.connect(CACHE_DB_PATH)
    cache_cur = cache_conn.cursor()

    hist_conn = sqlite3.connect(HIST_DB_PATH)
    hist_cur = hist_conn.cursor()

    # 1. Load CompanyMaster lookup map
    hist_cur.execute("""
        SELECT symbol, company_name, sector, industry, mcap_tier, is_psu, is_fno,
               market_cap_cr, pe_ratio, industry_pe, pb_ratio, roce_pct, roe_pct,
               opm_pct, dividend_yield_pct, piotroski_score, promoter_holding_pct,
               fii_holding_pct, sales_cagr_3y, profit_cagr_3y, debt_to_equity
        FROM CompanyMaster
    """)
    cm_map = {}
    for r in hist_cur.fetchall():
        cm_map[r[0]] = {
            "company_name": r[1],
            "sector": r[2],
            "industry": r[3],
            "mcap_tier": r[4],
            "is_psu": r[5] or 0,
            "is_fno": r[6] or 0,
            "market_cap_cr": r[7],
            "pe_ratio": r[8],
            "industry_pe": r[9],
            "pb_ratio": r[10],
            "roce_pct": r[11],
            "roe_pct": r[12],
            "opm_pct": r[13],
            "dividend_yield_pct": r[14],
            "piotroski_score": r[15],
            "promoter_holding_pct": r[16],
            "fii_holding_pct": r[17],
            "sales_cagr_3y": r[18],
            "profit_cagr_3y": r[19],
            "debt_to_equity": r[20]
        }

    # 2. Load StockTechnicalsDaily lookup map
    hist_cur.execute("""
        SELECT symbol, ema20, ema50, ema200, rsi14, atr14, vol_ratio,
               adx14, macd_hist, bb_width_pct, stoch_rsi, minervini_stage, vcp_score, cmp
        FROM StockTechnicalsDaily
    """)
    std_map = {}
    for r in hist_cur.fetchall():
        std_map[r[0]] = {
            "ema20": r[1],
            "ema50": r[2],
            "ema200": r[3],
            "rsi14": r[4],
            "atr14": r[5],
            "vol_ratio": r[6],
            "adx14": r[7],
            "macd_hist": r[8],
            "bb_width_pct": r[9],
            "stoch_rsi": r[10],
            "minervini_stage": r[11],
            "vcp_score": r[12],
            "cmp": r[13]
        }
    hist_conn.close()

    # 3. Load yesterday's consecutive_days_near lookup map
    yesterday_date = previous_trading_day(scan_date).isoformat()   # previous SESSION, not calendar day
    cache_cur.execute("SELECT symbol, consecutive_days_near FROM stock_screener_rows WHERE scan_date = ?", (yesterday_date,))
    prev_near_map = dict(cache_cur.fetchall())

    # 4. Fetch all cached DNA reports
    cache_cur.execute("SELECT symbol, health_score, stance, action, report_json FROM stock_dna_reports WHERE report_json IS NOT NULL")
    cached_reports = cache_cur.fetchall()

    print(f"[*] Found {len(cached_reports)} cached DNA reports. Processing screener records...", flush=True)

    rows_to_upsert = []
    skipped_count = 0
    quarantine_rows = []
    trade_rows = []
    try:
        from common import trade_rules as _trade_rules
        _edge = _trade_rules.load_edge()
        _raw_conn = sqlite3.connect(f"file:{HIST_DB_PATH}?mode=ro", uri=True)
    except Exception:
        _trade_rules, _edge, _raw_conn = None, {}, None

    for sym, health_score, stance, action, r_json_str in cached_reports:
        try:
            rep = json.loads(r_json_str)
        except Exception:
            skipped_count += 1
            continue

        cm = cm_map.get(sym, {})
        std = std_map.get(sym, {})
        ui_rep = rep.get("ui_report", {})
        tc = ui_rep.get("trade_execution_card", {})
        curr_m = rep.get("current_metrics", {})
        _flags = curr_m.get("data_flags") or {}
        _gaps = _flags.get("unexplained_gaps_1y") or []
        _note = None
        if _gaps:
            _note = f"Unverified price jump on {_gaps[-1].get('date')} (possible unadjusted split/bonus)"
        elif _flags.get("zero_volume_20d", 0) >= 5 or _flags.get("provisional_last_bar"):
            _note = "Official exchange volume pending"
        elif _flags.get("stale"):
            _note = "Price data not current"
        quarantine_rows.append((1 if _note else 0, _note, sym))
        solv = rep.get("solvency", {})
        cd = rep.get("chart_directives", {})
        candles = rep.get("chart_candles", [])

        # Price & Range (prioritize fresh price from DNA report latest candle, fallback to StockTechnicalsDaily)
        curr_price = curr_m.get("price") if (curr_m.get("price") is not None and curr_m.get("price") > 0) else std.get("cmp")
        w52_high = curr_m.get("w52_high")
        w52_low = curr_m.get("w52_low")
        dist_from_52w_high_pct = curr_m.get("dist_52w_pct")
        ath = curr_m.get("ath")
        dist_from_ath_pct = curr_m.get("dist_ath_pct")
        candle_count = curr_m.get("candle_count")

        # EMAs & Trend (prioritize StockTechnicalsDaily, fallback to current_metrics)
        ema20 = std.get("ema20") if std.get("ema20") is not None else curr_m.get("ema20")
        ema50 = std.get("ema50") if std.get("ema50") is not None else curr_m.get("ema50")
        ema200 = std.get("ema200") if std.get("ema200") is not None else curr_m.get("ema200")

        price_vs_ema20_pct = None
        if curr_price is not None and ema20 is not None and ema20 > 0:
            price_vs_ema20_pct = round(((curr_price - ema20) / ema20) * 100, 2)

        price_vs_ema50_pct = None
        if curr_price is not None and ema50 is not None and ema50 > 0:
            price_vs_ema50_pct = round(((curr_price - ema50) / ema50) * 100, 2)

        price_vs_ema200_pct = None
        if curr_price is not None and ema200 is not None and ema200 > 0:
            price_vs_ema200_pct = round(((curr_price - ema200) / ema200) * 100, 2)

        # EMA Alignment
        ema_alignment = 'MIXED'
        if curr_price and ema20 and ema50 and ema200:
            if curr_price > ema20 > ema50 > ema200:
                ema_alignment = 'BULL_STACK'
            elif curr_price < ema20 < ema50 < ema200:
                ema_alignment = 'BEAR_STACK'

        # Momentum
        rsi = std.get("rsi14") if std.get("rsi14") is not None else curr_m.get("rsi")
        rsi_zone = 'NEUTRAL'
        if rsi is not None:
            if rsi < 35:
                rsi_zone = 'OVERSOLD'
            elif rsi > 70:
                rsi_zone = 'OVERBOUGHT'

        atr = std.get("atr14") if std.get("atr14") is not None else curr_m.get("atr")
        atr_pct = None
        if atr is not None and curr_price is not None and curr_price > 0:
            atr_pct = round((atr / curr_price) * 100, 2)

        volume_ratio_20d = std.get("vol_ratio") if std.get("vol_ratio") is not None else curr_m.get("volume_mult")
        # Ensure robust, non-zero volume ratio using candle volume history if latest tick is 0
        if volume_ratio_20d is None or volume_ratio_20d <= 0.05:
            c_vols = [float(c.get("volume") or c.get("v") or 0) for c in candles if float(c.get("volume") or c.get("v") or 0) > 0]
            if len(c_vols) >= 5:
                base_vols = c_vols[-21:-1] if len(c_vols) >= 21 else c_vols[:-1]
                avg_20 = sum(base_vols) / len(base_vols) if base_vols else 1.0
                if avg_20 > 0:
                    volume_ratio_20d = round(float(c_vols[-1] / avg_20), 2)
            if volume_ratio_20d is None or volume_ratio_20d <= 0.05:
                volume_ratio_20d = 2.0

        adx14 = std.get("adx14")
        macd_hist = std.get("macd_hist")
        bb_width_pct = std.get("bb_width_pct")
        stoch_rsi = std.get("stoch_rsi")
        minervini_stage = std.get("minervini_stage")
        vcp_score = std.get("vcp_score")

        # Pattern identification from the DNA verdict. Trendline labels are
        # levels (for example, "Major Structural Resistance"), not patterns;
        # using their last label here made every card appear to be that label.
        trendlines = cd.get("trendlines", [])
        pattern_type = (
            ui_rep.get("pro_analysis", {}).get("chart_pattern_detected")
            or ui_rep.get("dna_archetype")
            or "Consolidation Range"
        )
        support_1 = None
        support_2 = None
        resistance_1 = None
        resistance_2 = None
        target_1 = tc.get("target_1")
        target_2 = tc.get("target_2")
        stop_loss = tc.get("stop_loss")

        for tl in trendlines:
            tl_type = tl.get("type", "").lower()
            p2_price = tl.get("p2", {}).get("price")
            if tl_type in ("support", "floor", "lower"):
                if support_1 is None:
                    support_1 = p2_price
                else:
                    support_2 = p2_price
            elif tl_type in ("resistance", "neckline", "ceiling", "upper", "pivot"):
                if resistance_1 is None:
                    resistance_1 = p2_price
                else:
                    resistance_2 = p2_price

        # Check horizontal levels from chart directives if available
        horiz_levels = cd.get("horizontal_levels", [])
        for hl in horiz_levels:
            hl_label = (hl.get("label") or "").lower()
            hl_price = hl.get("price")
            if any(k in hl_label for k in ("breakout", "neckline", "ceiling", "pivot", "resistance")):
                if resistance_1 is None and hl_price:
                    resistance_1 = hl_price
            elif "target" in hl_label and target_1 is None and hl_price:
                target_1 = hl_price
            elif any(k in hl_label for k in ("invalidation", "stop", "floor", "support")) and stop_loss is None and hl_price:
                stop_loss = hl_price

        # Invariant: Breakout level must NEVER default to target_1!
        breakout_level = resistance_1 or tc.get("breakout_pivot") or curr_price
        breakout_direction = 'BULLISH' if (stance or '').upper() in ('BULLISH', 'ACCUMULATION') else ('BEARISH' if (stance or '').upper() in ('BEARISH', 'DISTRIBUTION') else 'NONE')

        # --- Bug Fix 1 & 3: Bearish pattern name detection —
        # If the pattern name contains bearish keywords, force stance to BEARISH
        # regardless of what the regime logic previously decided. This prevents
        # a true H&S or Double Top from appearing as a BULLISH near-breakout setup.
        if is_bearish_pattern(pattern_type) and (stance or '').upper() != 'BEARISH':
            stance = 'BEARISH'
            breakout_direction = 'BEARISH'

        # Recent candles for breakout forward-walk & target tracking
        recent_candles = candles[-20:] if candles else []
        rc_with_close = [c for c in recent_candles if c.get("close") is not None]
        closes_list = [c.get("close") for c in rc_with_close]
        vol_ratios_list = [c.get("vol_ratio") or 1.0 for c in rc_with_close]
        dates_list = [c.get("time") for c in rc_with_close]  # ISO date strings e.g. '2026-09-22'
        recent_highs = [c.get("high") or c.get("h") or 0.0 for c in recent_candles]
        max_recent_high = max(recent_highs) if recent_highs else (curr_price or 0.0)

        # Check if target was already achieved before modifying targets
        target_achieved = False
        if target_1 and curr_price and (curr_price >= target_1 or max_recent_high >= target_1):
            target_achieved = True

        # --- Mathematical Target & Stop Loss Enforcement ---
        calc_atr = atr if (atr and atr > 0) else (curr_price * 0.04 if curr_price else 1.0)
        is_bullish = (stance or '').upper() in ('BULLISH', 'ACCUMULATION') or str(action or '').startswith(('BUY', 'STRONG_BUY'))

        if curr_price and curr_price > 0:
            if is_bullish and not target_achieved:
                min_target = max(breakout_level or curr_price, curr_price) * 1.05
                t1_is_insufficient = (
                    target_1 is None
                    or target_1 <= curr_price
                    or target_1 < min_target
                )
                if t1_is_insufficient:
                    if target_2 and target_2 > curr_price and target_2 >= min_target:
                        target_1 = target_2
                    else:
                        target_1 = round(max(breakout_level or curr_price, curr_price) + max(2.0 * calc_atr, curr_price * 0.10), 2)

                # Stop loss must strictly be below curr_price
                if stop_loss is None or stop_loss >= curr_price:
                    stop_loss = round(min(breakout_level or curr_price, curr_price) - max(1.5 * calc_atr, curr_price * 0.06), 2)
            else:
                # Target achieved or defensive setup: preserve original target, ensure stop_loss is below
                if stop_loss is None or stop_loss >= curr_price:
                    stop_loss = round(curr_price - max(1.5 * calc_atr, curr_price * 0.05), 2)

        # One tested definition of breakout / retest / failed (common.trade_rules) replaces "first close above the level".
        trade_call = None
        _level_for_rules = resistance_1 or tc.get("breakout_pivot")
        if _trade_rules and _raw_conn and _level_for_rules and breakout_direction != 'BEARISH':
            try:
                _r = _raw_conn.execute("SELECT rawCandles FROM StockHistory WHERE symbol=?", (sym,)).fetchone()
                if _r and _r[0]:
                    trade_call = _trade_rules.evaluate_candles(json.loads(_r[0]), float(_level_for_rules), "D", _edge)
            except Exception:
                trade_call = None

        breakout_status, days_since_breakout, breakout_vol_spike = compute_breakout_status(
            curr_price=curr_price,
            breakout_level=breakout_level,
            support_1=support_1,
            closes_list=closes_list,
            vol_ratios_list=vol_ratios_list,
            dates_list=dates_list,
            scan_date=scan_date,
            target_1=target_1,
            recent_highs=recent_highs
        )

        if not _level_for_rules:
            # no real resistance level: the old code used the current price as the "level" and invented statuses from it
            breakout_status, days_since_breakout, breakout_vol_spike = 'NA', None, 0
        if trade_call and trade_call.get("state") not in (None, "NA"):
            breakout_status, _d = _trade_rules.legacy_breakout_status(trade_call)
            if _d is not None:
                days_since_breakout = _d
            if trade_call.get("state") in ("NEAR", "ATTEMPT"):
                breakout_status = 'PENDING'
        trade_rows.append((trade_call.get("state") if trade_call else None, trade_call.get("grade") if trade_call else None,
                           trade_call.get("action") if trade_call else None,
                           json.dumps(trade_call, default=str)[:6000] if trade_call else None, sym))

        if breakout_status == 'TARGET_ACHIEVED':
            stance = 'NEUTRAL'
            action = 'TARGET_ACHIEVED'
            action_title = 'Target Achieved · Profit Booked'

        # Gate: Bearish stocks or explicit AVOID recommendations must never be tagged as SUCCESS breakouts
        if (stance or '').upper() == 'BEARISH' or str(action or '').startswith('AVOID') or str(action or '').startswith('STRICT_AVOID'):
            if breakout_status == 'SUCCESS':
                breakout_status = 'FAILED'

        prev_days_near = prev_near_map.get(sym, 0)
        pattern_stage, consecutive_days_near = compute_pattern_stage(
            curr_price=curr_price,
            breakout_level=breakout_level,
            bb_width_pct=bb_width_pct,
            adx14=adx14,
            consecutive_days_near_prev=prev_days_near,
            breakout_status=breakout_status,
            stance=stance  # Bug Fix 2: pass stance so neutral/bearish stocks stay FORMING
        )

        # A validated BUY carries its own plan (stop = level - 1 ATR, target = 2R). Use it everywhere on the row so the
        # card, the stock page and the ledger never show three different stops for the same trade.
        if trade_call and trade_call.get("action") == "BUY" and trade_call.get("plan"):
            _pl = trade_call["plan"]
            stop_loss, target_1, target_2 = _pl["stop"], _pl["target"], None

        # Fibonacci Levels from chart directives
        fib_382 = None
        fib_500 = None
        fib_618 = None
        for hl in cd.get("horizontal_levels", []):
            lbl = hl.get("label", "").lower()
            if "0.382" in lbl or "38.2" in lbl:
                fib_382 = hl.get("price")
            elif "0.50" in lbl or "50%" in lbl:
                fib_500 = hl.get("price")
            elif "0.618" in lbl or "golden" in lbl or "61.8" in lbl:
                fib_618 = hl.get("price")

        # Risk / Reward Ratio (strictly calculated from live entry, target, and stop loss)
        risk_reward_ratio = None
        if curr_price and stop_loss and target_1 and curr_price > stop_loss and target_1 > curr_price:
            risk = curr_price - stop_loss
            reward = target_1 - curr_price
            if risk > 0 and reward > 0:
                risk_reward_ratio = round(reward / risk, 2)
        elif tc.get("risk_to_reward_ratio"):
            rr_str = str(tc.get("risk_to_reward_ratio") or "")
            if ":" in rr_str:
                try:
                    parts = [float(p.strip()) for p in rr_str.split(":")]
                    if parts[0] > 0 and len(parts) > 1:
                        risk_reward_ratio = round(parts[1] / parts[0], 2)
                except Exception:
                    pass

        # Entry Zone
        entry_zone_low = support_1
        entry_zone_high = curr_price
        if trade_call and trade_call.get("action") == "BUY" and trade_call.get("plan"):
            entry_zone_low, entry_zone_high = trade_call.get("level"), trade_call["plan"]["entry_ref"]

        # Solvency
        solvency_score = solv.get("score")
        solvency_grade = solv.get("grade")

        # Compile tuple matching stock_screener_rows column order
        row = (
            sym,
            cm.get("company_name") or rep.get("company_name"),
            cm.get("sector") or rep.get("sector"),
            cm.get("industry"),
            cm.get("mcap_tier"),
            cm.get("is_psu", 0),
            cm.get("is_fno", 0),
            scan_date,
            scan_ts,
            curr_price,
            w52_high,
            w52_low,
            dist_from_52w_high_pct,
            ath,
            dist_from_ath_pct,
            candle_count,
            ema20,
            ema50,
            ema200,
            price_vs_ema20_pct,
            price_vs_ema50_pct,
            price_vs_ema200_pct,
            ema_alignment,
            rsi,
            rsi_zone,
            atr,
            atr_pct,
            volume_ratio_20d,
            adx14,
            macd_hist,
            bb_width_pct,
            stoch_rsi,
            minervini_stage,
            vcp_score,
            pattern_type,
            pattern_stage,
            consecutive_days_near,
            breakout_level,
            breakout_direction,
            breakout_status,
            days_since_breakout,
            breakout_vol_spike,
            support_1,
            support_2,
            resistance_1,
            resistance_2,
            fib_382,
            fib_500,
            fib_618,
            health_score or ui_rep.get("dna_health_score"),
            (stance or ui_rep.get("stance", "NEUTRAL")).upper(),
            action or tc.get("action"),
            tc.get("action_title") or ui_rep.get("action_title"),
            stop_loss,
            target_1,
            target_2,
            risk_reward_ratio,
            entry_zone_low,
            entry_zone_high,
            solvency_score,
            solvency_grade,
            cm.get("debt_to_equity"),
            cm.get("market_cap_cr"),
            cm.get("pe_ratio"),
            cm.get("industry_pe"),
            cm.get("pb_ratio"),
            cm.get("roce_pct"),
            cm.get("roe_pct"),
            cm.get("opm_pct"),
            cm.get("dividend_yield_pct"),
            cm.get("piotroski_score"),
            cm.get("promoter_holding_pct"),
            cm.get("fii_holding_pct"),
            cm.get("sales_cagr_3y"),
            cm.get("profit_cagr_3y"),
            None,  # resolved_outcome
            None,  # resolved_date
            None   # resolve_lookback_days
        )
        rows_to_upsert.append(row)

    # 5. Atomic Batch Upsert
    cache_cur.executemany("""
        INSERT OR REPLACE INTO stock_screener_rows (
            symbol, company_name, sector, industry, mcap_tier, is_psu, is_fno,
            scan_date, scan_ts, curr_price, w52_high, w52_low, dist_from_52w_high_pct,
            ath, dist_from_ath_pct, candle_count, ema20, ema50, ema200,
            price_vs_ema20_pct, price_vs_ema50_pct, price_vs_ema200_pct,
            ema_alignment, rsi, rsi_zone, atr, atr_pct, volume_ratio_20d,
            adx14, macd_hist, bb_width_pct, stoch_rsi, minervini_stage, vcp_score,
            pattern_type, pattern_stage, consecutive_days_near, breakout_level,
            breakout_direction, breakout_status, days_since_breakout, breakout_volume_spike,
            support_1, support_2, resistance_1, resistance_2, fib_382, fib_500, fib_618,
            dna_score, stance, action, action_title, stop_loss, target_1, target_2,
            risk_reward_ratio, entry_zone_low, entry_zone_high, solvency_score,
            solvency_grade, debt_to_equity, market_cap_cr, pe_ratio, industry_pe,
            pb_ratio, roce_pct, roe_pct, opm_pct, div_yield_pct, piotroski_score,
            promoter_holding_pct, fii_holding_pct, sales_cagr_3y, profit_cagr_3y,
            resolved_outcome, resolved_date, resolve_lookback_days
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, rows_to_upsert)

    _cols = {r[1] for r in cache_cur.execute("PRAGMA table_info(stock_screener_rows)")}
    if "data_quarantine" not in _cols:
        cache_cur.execute("ALTER TABLE stock_screener_rows ADD COLUMN data_quarantine INTEGER DEFAULT 0")
    if "data_note" not in _cols:
        cache_cur.execute("ALTER TABLE stock_screener_rows ADD COLUMN data_note TEXT")
    cache_cur.executemany(f"UPDATE stock_screener_rows SET data_quarantine=?, data_note=? WHERE symbol=? AND scan_date='{scan_date}'",
                          quarantine_rows)
    for _col, _typ in (("trade_state", "TEXT"), ("trade_grade", "TEXT"), ("trade_action", "TEXT"), ("trade_json", "TEXT")):
        if _col not in _cols:
            cache_cur.execute(f"ALTER TABLE stock_screener_rows ADD COLUMN {_col} {_typ}")
    cache_cur.executemany(f"UPDATE stock_screener_rows SET trade_state=?, trade_grade=?, trade_action=?, trade_json=? "
                          f"WHERE symbol=? AND scan_date='{scan_date}'", trade_rows)
    if _raw_conn:
        _raw_conn.close()

    cache_conn.commit()
    cache_conn.close()

    print(f"[✓] Screener Snapshot Complete: {len(rows_to_upsert)} rows saved for {scan_date} (skipped: {skipped_count})", flush=True)
    return {
        "success": True,
        "scan_date": scan_date,
        "rows_saved": len(rows_to_upsert),
        "skipped": skipped_count
    }


def resolve_outcomes(lookback_days: int = 10) -> Dict[str, Any]:
    """
    Evaluates older screener recommendations against today's prices to establish
    transparent accuracy metrics.
    """
    today = datetime.date.today().isoformat()
    target_date = (datetime.date.today() - datetime.timedelta(days=lookback_days)).isoformat()

    conn = sqlite3.connect(CACHE_DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        SELECT symbol, breakout_level, support_1, pattern_stage
        FROM stock_screener_rows
        WHERE scan_date = ? AND resolved_outcome IS NULL
          AND pattern_stage IN ('NEAR_BREAKOUT', 'MATURE', 'POST_BREAKOUT')
    """, (target_date,))
    historical_signals = cur.fetchall()

    resolved_count = 0
    success_count = 0
    failed_count = 0
    inconclusive_count = 0

    for sym, bl, sup, stage in historical_signals:
        cur.execute("""
            SELECT curr_price FROM stock_screener_rows
            WHERE symbol = ? AND scan_date = ?
        """, (sym, today))
        curr_row = cur.fetchone()

        if not curr_row or curr_row[0] is None:
            outcome = 'INCONCLUSIVE'
            inconclusive_count += 1
        elif bl and curr_row[0] > bl * 1.03:
            outcome = 'SUCCESS'
            success_count += 1
        elif sup and curr_row[0] < sup:
            outcome = 'FAILED'
            failed_count += 1
        else:
            outcome = 'INCONCLUSIVE'
            inconclusive_count += 1

        cur.execute("""
            UPDATE stock_screener_rows
            SET resolved_outcome = ?, resolved_date = ?, resolve_lookback_days = ?
            WHERE symbol = ? AND scan_date = ?
        """, (outcome, today, lookback_days, sym, target_date))
        resolved_count += 1

    conn.commit()
    conn.close()

    print(f"[✓] Resolved {resolved_count} signals from {target_date}: {success_count} Success, {failed_count} Failed, {inconclusive_count} Inconclusive", flush=True)
    return {
        "success": True,
        "target_date": target_date,
        "resolved_count": resolved_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "inconclusive_count": inconclusive_count
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="QuantSync Screener Snapshot Builder")
    parser.add_argument("--date", type=str, default=None, help="Target scan date (YYYY-MM-DD)")
    parser.add_argument("--resolve", action="store_true", help="Run outcome resolution job")
    parser.add_argument("--lookback", type=int, default=10, help="Lookback days for resolution")
    args = parser.parse_args()

    if args.resolve:
        resolve_outcomes(lookback_days=args.lookback)
    else:
        build_all(scan_date=args.date)
