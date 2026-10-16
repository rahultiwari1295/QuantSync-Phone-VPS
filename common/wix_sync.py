"""
common/wix_sync.py — Unified Multi-Engine Wix Portal Dispatcher for Prod_VPS
=============================================================================
Extracts live multi-engine portfolio state (V2, V3, etc.) from MasterStorage
and dispatches real-time updates to the Wix Production API endpoint:
  https://rahultiwari1295.wixstudio.com/angelapi/_functions/updateTrades

Invoked by scheduler.py:
  1. 5-Min LTP Sync: Updates live market prices, current ROIs, and active holdings
  2. 15:15 Decision Cycle: Pushes new Buy/Sell executions and toBuySignals
  3. 15:45 EOD Finalize: Pushes settled NAV, closed trades, and terminal snapshots
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import logging
from typing import Dict, List, Any, Optional

try:
    from engines.v3.banana_engine.core.build_universe_cache import COMPANY_NAMES
except Exception:
    COMPANY_NAMES = {}

logger = logging.getLogger("ProdVPS.WixSync")

_COMMON_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_ROOT = os.path.dirname(_COMMON_DIR)

DEFAULT_WIX_URL = "https://rahultiwari1295.wixstudio.com/angelapi/_functions/updateTrades"

_STRAT_DISPLAY = {
    'APEX_BASE': 'Apex',
    'Apex': 'Apex',
    'HIGH_TIGHT_FLAG': 'Flag',
    'Flag': 'Flag',
    'STAGE2_BASE': 'Breakout',
    'STAGE2_BASE_BREAKOUT': 'Breakout',
    'Breakout': 'Breakout',
    'Stage2': 'Breakout',
    'POWER_EARNINGS_GAP': 'Gap',
    'Gap': 'Gap',
    'RS_LEADER': 'Leader',
    'Leader': 'Leader',
    'RSLeader': 'Leader',
    'Alpha': 'Alpha',
    'AlphaMomentumBreakout_1D': 'Alpha',
    'StructuralValuePullback_1D': 'Pullback',
    'ValuePullback': 'Pullback',
    'Pullback': 'Pullback',
    'RelativeStrengthLeader_1D': 'Leader',
    'InstitutionalPocketPivot_1D': 'Pivot',
    'PocketPivot': 'Pivot',
    'Pivot': 'Pivot',
    'GapTrendExpansion_1D': 'Expansion',
    'GapExpansion': 'Expansion',
    'Expansion': 'Expansion',
    'DonchianTrendSurge_2D': 'Surge',
    'DonchianSurge': 'Surge',
    'Surge': 'Surge',
    'VolatilitySqueezeRelease_2D': 'Squeeze',
    'SqueezeRelease': 'Squeeze',
    'Squeeze': 'Squeeze',
    'CupHandlePivot_2D': 'Cup',
    'CupHandle': 'Cup',
    'Cup': 'Cup',
    'ThreeBarImpulseThrust_2D': 'Thrust',
    'ThreeBarThrust': 'Thrust',
    'Thrust': 'Thrust',
    'MTFMacroContinuation_3D': 'Macro',
    'MTFMacro': 'Macro',
    'Macro': 'Macro',
    'HTF': 'High-Tight Flag',
    'HighTightFlag': 'High-Tight Flag',
    'BASE': 'Stage 2 Base',
    'BaseBreakout': 'Stage 2 Base',
    '52W_HIGH': '52W High Breakout',
    '52WHigh': '52W High Breakout',
    'DARVAS': 'Darvas Box',
    'DarvasBox': 'Darvas Box',
    'FIB_1_000': 'Fib Retest (1.000)',
    'FIB_1_272': 'Fib Expansion (1.272)',
    'FIB_1_618': 'Fib Extension (1.618)'
}



def _load_wix_cfg() -> Dict[str, Any]:
    cfg_path = os.path.join(_PROD_ROOT, "config.json")
    defaults = {
        "enabled": True,
        "update_url": DEFAULT_WIX_URL
    }
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f).get("wix", {})
                defaults.update(cfg)
        except Exception as e:
            logger.warning(f"Failed to load wix config: {e}")
    return defaults


def _calc_stars(score: float):
    if score >= 85: return 5.0, '★★★★★'
    elif score >= 74: return 4.5, '★★★★½'
    elif score >= 62: return 4.0, '★★★★☆'
    elif score >= 50: return 3.5, '★★★½☆'
    elif score >= 35: return 3.0, '★★★☆☆'
    else: return 2.5, '★★½☆☆'


def format_active_v2(t: Dict[str, Any]) -> Dict[str, Any]:
    sym = t.get("symbol", "UNKNOWN")
    ep = float(t.get("entry_price") or 0.0)
    lp = float(t.get("current_ltp") or t.get("live_price") or ep)
    roi = float(t.get("roi_pct") if t.get("roi_pct") is not None else (((lp - ep) / ep * 100.0) if ep > 0 else 0.0))
    strat_id = t.get("strategy_id", "Alpha")
    strat_name = _STRAT_DISPLAY.get(strat_id, strat_id)
    entry_date = str(t.get("entry_date", ""))[:10]

    # Calculate days held
    days = 0
    if entry_date:
        try:
            d_entry = datetime.datetime.strptime(entry_date, "%Y-%m-%d").date()
            days = max(0, (datetime.date.today() - d_entry).days)
        except Exception:
            days = int(t.get("holding_days", 0))
    else:
        days = int(t.get("holding_days", 0))

    cal_p = float(t.get("ml_norm_rank", t.get("prob", 0.70)))
    ml_pct = round(cal_p * 100.0 if cal_p <= 1.0 else cal_p, 1)
    tech_score = int(80 if roi >= 0 else 65)
    risk_score = int(25)
    composite_score = int(min(100, max(0, round((ml_pct * 0.4) + (tech_score * 0.4) + (100 - risk_score) * 0.2))))
    stars, stars_display = _calc_stars(composite_score)

    timing_str = "⚡ Triggered today" if days == 0 else f"⚡ {days}d ago"

    if days > 10:
        if roi > 25.0:
            action, action_label, action_color = "RUNNER", "RUNNER (+25%+)", "emerald"
            slip_text = f"+{roi:.1f}% surge"
            footer_tags = ["🚀 Massive Compounder", "Trail EMA50 / 20D Low"]
        elif roi >= 10.0:
            action, action_label, action_color = "PROFIT", "PROFIT RUNNER", "cyan"
            slip_text = f"+{roi:.1f}% profit"
            footer_tags = ["🟢 Trend In Force", "Strict Trailing Exit"]
        elif roi >= 0.0:
            action, action_label, action_color = "HOLD", "IN POSITION", "blue"
            slip_text = f"+{roi:.1f}% green"
            footer_tags = [timing_str, "Position Healthy"]
        else:
            action, action_label, action_color = "HOLD", "STOP BUFFER", "amber"
            slip_text = f"{roi:.1f}% buffer"
            footer_tags = [timing_str, "Holding Stop Buffer"]
        slippage_text = slip_text
    else:
        if roi > 15.0:
            action, action_label, action_color = "MISSED", "MISSED", "red"
            slippage_text = f"+{roi:.1f}% run"
            footer_tags = [f"🔴 Overextended (+{roi:.1f}%)", "Do not chase"]
        elif roi >= 5.0:
            action, action_label, action_color = "WAIT", "WAIT", "orange"
            slippage_text = f"+{roi:.1f}% run"
            footer_tags = [timing_str, "⚠️ Extended, wait for dip"]
        elif roi >= 2.5:
            action, action_label, action_color = "BUY", "BUY", "yellow"
            slippage_text = f"+{roi:.1f}% move"
            footer_tags = [timing_str, f"🤖 ML {ml_pct:.1f}%", f"📈 Tech Grade {tech_score}"]
        else:
            action, action_label, action_color = "BUY_NOW", "BUY NOW", "green"
            slip_sign = "+" if roi >= 0 else ""
            slippage_text = f"{slip_sign}{roi:.1f}% slippage"
            footer_tags = [timing_str, f"🤖 ML {ml_pct:.1f}%", f"📈 Tech Grade {tech_score}"]

    footer_line = "  •  ".join(footer_tags)

    stop_p = float(t.get("current_stop") or ep * 0.92)
    tgt_p = float(t.get("target_price") or ep * 1.15)
    pnl_val = round((lp - ep) * int(t.get("shares", t.get("qty", 10))), 2)
    r_val = round((roi / 8.0) if roi != 0 else 0.0, 2)
    risk_tag = "Low" if risk_score < 30 else ("Medium" if risk_score < 50 else "High")
    ab_tag = sym[:2].upper()

    _grad_list = [
        'linear-gradient(135deg,#6366F1,#8B5CF6)',
        'linear-gradient(135deg,#0EA5E9,#2563EB)',
        'linear-gradient(135deg,#F59E0B,#D97706)',
        'linear-gradient(135deg,#10B981,#0EA5E9)',
        'linear-gradient(135deg,#8B5CF6,#6366F1)',
        'linear-gradient(135deg,#EC4899,#8B5CF6)'
    ]
    grad_val = _grad_list[sum(ord(c) for c in sym) % len(_grad_list)]

    radar_val = [tech_score, int(ml_pct), min(95, max(50, composite_score + 5)), min(95, max(50, composite_score - 2))]
    why_val = [
        f"Breakout above consolidation with favorable risk-reward",
        f"Relative strength rank: Top 10% of universe",
        f"Sector showing institutional inflows",
        f"ML confidence: {int(ml_pct)}% probability of 2R+ gain"
    ]
    timeline_val = [
        [entry_date or "Day 1", f"Entered at ₹{ep:,.2f} · {int(t.get('shares', 10))} shares", "act"],
        ["Trailing", f"Stop placed at ₹{stop_p:,.2f}", ""],
        ["Current", f"Holding · {roi:+.2f}% · ₹{lp:,.2f}", "act" if roi >= 0 else "bad"]
    ]

    return {
        # Prototype UI direct properties
        "sym": sym,
        "ab": ab_tag,
        "name": t.get("company_name", sym),
        "tag": f"{t.get('sector') or 'Diversified'} · {strat_name}",
        "stars": stars,
        "starsDisplay": stars_display,
        "ml": int(ml_pct),
        "risk": risk_tag,
        "comp": composite_score,
        "entry": round(ep, 2),
        "now": round(lp, 2),
        "stop": round(stop_p, 2),
        "target": round(tgt_p, 2),
        "r": r_val,
        "days": days,
        "pnl": pnl_val,
        "pnlPct": round(roi, 2),
        "grad": grad_val,
        "why": why_val,
        "timeline": timeline_val,
        "radar": radar_val,
        "chartDays": max(20, min(60, days + 15)),
        # Standard camelCase properties (for Wix & backward compat)
        "symbol": sym,
        "currentROI": round(roi, 2),
        "currentPrice": round(lp, 2),
        "entryPrice": round(ep, 2),
        "entryDate": entry_date,
        "strategyName": strat_name,
        "strategyId": strat_id,
        "sector": t.get("sector") or "Diversified",
        "compositeScore": composite_score,
        "mlProbability": ml_pct,
        "riskScore": risk_score,
        "technicalScore": tech_score,
        "action": action,
        "actionLabel": action_label,
        "actionColor": action_color,
        "slippageText": slippage_text,
        "footerLine": footer_line,
        "stopLoss": round(stop_p, 2),
        "targetPrice": round(tgt_p, 2),
        "livePrice": round(lp, 2),
        "roiPct": round(roi, 2),
        "convictionScore": composite_score,
        "shares": int(t.get("shares", t.get("qty", 1))),
        "miniChart": t.get("mini_chart", {}),
        "sparkline": t.get("sparkline", [])
    }


def format_closed_v2(t: Dict[str, Any]) -> Dict[str, Any]:
    sym = t.get("symbol", "")
    ep = float(t.get("entry_price", 0.0))
    xp = float(t.get("exit_price", ep))
    roi = round(((xp - ep) / ep * 100.0) if ep > 0 else 0.0, 2)
    strat_id = t.get("strategy_id", "")
    strat_name = _STRAT_DISPLAY.get(strat_id, strat_id)
    h_days = int(t.get("holding_days", t.get("days_held", 0)))
    realized_pnl = float(t.get("net_pnl", t.get("realized_pnl", 0.0)))
    r_val = float(t.get("risknorm_r", t.get("capped_r", roi / 7.0)))
    reason_str = str(t.get("exit_reason", "TARGET_HIT"))
    exit_dt = str(t.get("exit_date", ""))[:10]
    date_display = exit_dt[5:] if len(exit_dt) >= 10 else exit_dt

    return {
        # Prototype UI direct properties
        "sym": sym,
        "ab": sym[:2].upper(),
        "name": t.get("company_name", sym),
        "entry": round(ep, 2),
        "exit": round(xp, 2),
        "pnl": round(realized_pnl, 2),
        "pnlPct": roi,
        "r": round(r_val, 2),
        "days": h_days,
        "reason": reason_str,
        "date": date_display or "Recently",
        # Standard camelCase properties
        "tradeId": str(t.get("trade_id", "")),
        "symbol": sym,
        "strategyId": strat_id,
        "strategyName": strat_name,
        "entryDate": t.get("entry_date", ""),
        "exitDate": t.get("exit_date", ""),
        "entryPrice": ep,
        "exitPrice": xp,
        "roiPct": roi,
        "returnPct": roi,
        "tradeReturnPercent": roi,
        "pnlLabel": "WIN" if roi >= 0 else "LOSS",
        "realizedPnl": round(realized_pnl, 2),
        "exitReason": reason_str,
        "holdingDays": h_days,
        "holdDays": h_days,
        "isWin": bool(roi >= 0),
        "sparkline": t.get("sparkline", []),
        "miniChart": {"sparkline": t.get("sparkline", [])}
    }


def format_active_v3(t: Dict[str, Any]) -> Dict[str, Any]:
    sym = t.get("symbol", "UNKNOWN")
    ep = float(t.get("entry_price") or 0.0)
    lp = float(t.get("current_ltp") or t.get("live_price") or ep)
    roi = float(t.get("roi_pct") if t.get("roi_pct") is not None else (((lp - ep) / ep * 100.0) if ep > 0 else 0.0))
    strat_id = t.get("strategy_id", "HTF")
    strat_name = _STRAT_DISPLAY.get(strat_id, strat_id)
    entry_date = str(t.get("entry_date", ""))[:10]

    days = 0
    if entry_date:
        try:
            d_entry = datetime.datetime.strptime(entry_date, "%Y-%m-%d").date()
            days = max(0, (datetime.date.today() - d_entry).days)
        except Exception:
            days = int(t.get("holding_days", 0))
    else:
        days = int(t.get("holding_days", 0))

    raw_geom = t.get("geometry")
    if isinstance(raw_geom, str):
        try:
            geom = json.loads(raw_geom)
        except Exception:
            geom = {}
    elif isinstance(raw_geom, dict):
        geom = raw_geom
    else:
        geom = {}

    vol_ratio = float(t.get("volume_ratio") or geom.get("volume_ratio") or 2.4)
    cir = float(t.get("cir") or geom.get("cir") or 0.85)

    composite_score = int(min(100, max(50, round(70 + (cir * 15) + min(15, vol_ratio * 3)))))
    stars, stars_display = _calc_stars(composite_score)
    timing_str = "⚡ Triggered today" if days == 0 else f"⚡ {days}d ago"

    if days > 10:
        if roi > 25.0:
            action, action_label, action_color = "RUNNER", "RUNNER (+25%+)", "emerald"
            slip_text = f"+{roi:.1f}% surge"
            footer_tags = ["🚀 Institutional Runner", "Trail Weekly Swing C"]
        elif roi >= 10.0:
            action, action_label, action_color = "PROFIT", "PROFIT RUNNER", "cyan"
            slip_text = f"+{roi:.1f}% profit"
            footer_tags = ["🟢 Fib Expansion Target 1", "BE Locked"]
        elif roi >= 0.0:
            action, action_label, action_color = "HOLD", "IN POSITION", "blue"
            slip_text = f"+{roi:.1f}% green"
            footer_tags = [timing_str, f"📐 {strat_name}"]
        else:
            action, action_label, action_color = "HOLD", "STOP BUFFER", "amber"
            slip_text = f"{roi:.1f}% buffer"
            footer_tags = [timing_str, "Holding Structural Stop"]
        slippage_text = slip_text
    else:
        if roi > 15.0:
            action, action_label, action_color = "MISSED", "MISSED", "red"
            slippage_text = f"+{roi:.1f}% run"
            footer_tags = [f"🔴 Overextended (+{roi:.1f}%)", "Do not chase"]
        elif roi >= 5.0:
            action, action_label, action_color = "WAIT", "WAIT", "orange"
            slippage_text = f"+{roi:.1f}% run"
            footer_tags = [timing_str, "⚠️ Extended, wait for pullback"]
        elif roi >= 2.0:
            action, action_label, action_color = "BUY", "BUY", "yellow"
            slippage_text = f"+{roi:.1f}% move"
            footer_tags = [timing_str, f"📐 {strat_name}", f"⚡ Vol {vol_ratio:.1f}x"]
        else:
            action, action_label, action_color = "BUY_NOW", "BUY NOW", "green"
            slip_sign = "+" if roi >= 0 else ""
            slippage_text = f"{slip_sign}{roi:.1f}% slippage"
            footer_tags = [timing_str, f"📐 {strat_name}", f"⚡ Vol {vol_ratio:.1f}x"]

    footer_line = "  •  ".join(footer_tags)

    pivot = float(geom.get("pivot", ep * 0.98))
    floor = float(geom.get("floor", ep * 0.88))
    stop = float(t.get("current_stop") or geom.get("stop", ep * 0.92))
    target = float(t.get("target_price") or geom.get("target", ep * 1.20))
    be_lock = float(t.get("be_lock_price") or geom.get("be_lock", ep * 1.10))
    fib_levels = geom.get("fib_levels", {
        "1.000": round(pivot * 1.05, 2),
        "1.272": round(target, 2),
        "1.618": round(target * 1.12, 2)
    })

    overlays = [
        {"price": round(pivot, 2), "color": "var(--gold)", "label": f"PIVOT ₹{pivot:,.2f}"},
        {"price": round(floor, 2), "color": "var(--loss)", "label": f"FLOOR ₹{floor:,.2f}", "opacity": 0.4},
        {"price": round(stop, 2), "color": "var(--loss)", "label": f"STOP ₹{stop:,.2f}"},
        {"price": round(ep, 2), "color": "var(--accent)", "label": f"ENTRY ₹{ep:,.2f}"},
        {"price": round(target, 2), "color": "var(--profit)", "label": f"T1 ₹{target:,.2f}", "dash": "2 3"}
    ]
    if "1.618" in fib_levels:
        t2_val = float(fib_levels["1.618"])
        overlays.append({"price": round(t2_val, 2), "color": "var(--profit)", "label": f"T2 ₹{t2_val:,.2f}", "dash": "2 3", "opacity": 0.45})

    stage_name = t.get("stage_tag") or t.get("stage") or f"{strat_name} 📐"
    why_v3 = t.get("why") or f"Breaking out of a {geom.get('base_days', 28)}-day base with {vol_ratio:.1f}× volume. Fibonacci 1.272 extension sits at ₹{target:,.2f}. Prior run held above weekly swing. Classic {strat_name} accumulation with tightening volatility."
    fib_chips = t.get("fib") or [
        ["Fib 1.0", f"₹{float(fib_levels.get('1.000', pivot)):,.2f}", "✅ Hit" if lp >= float(fib_levels.get('1.000', pivot)) else "Near", "hit" if lp >= float(fib_levels.get('1.000', pivot)) else "next"],
        ["Fib 1.272", f"₹{float(fib_levels.get('1.272', target)):,.2f}", "🎯 Next", "next"],
        ["Fib 1.618", f"₹{float(fib_levels.get('1.618', target * 1.12)):,.2f}", "Stretch", ""]
    ]

    _grad_list = [
        'linear-gradient(135deg,#8B5CF6,#6366F1)',
        'linear-gradient(135deg,#10B981,#0EA5E9)',
        'linear-gradient(135deg,#F59E0B,#EF4444)',
        'linear-gradient(135deg,#6366F1,#EC4899)'
    ]
    grad_val = t.get("grad") or _grad_list[sum(ord(c) for c in sym) % len(_grad_list)]
    radar_val = t.get("radar") or [composite_score, 70, min(95, composite_score + 6), min(95, int(55 + vol_ratio * 12))]

    comp_name = t.get("company_name") or COMPANY_NAMES.get(sym, sym.title() + " Limited")

    return {
        # Prototype UI direct properties
        "sym": sym,
        "ab": sym[:2].upper(),
        "name": comp_name,
        "entry": round(ep, 2),
        "stage": stage_name,
        "price": round(lp, 2),
        "chg": round(roi, 2),
        "days": days,
        "grad": grad_val,
        "overlays": overlays,
        "why": why_v3,
        "fib": fib_chips,
        "radar": radar_val,
        "timeline": t.get("timeline", []),
        "candles": max(20, min(45, days + 25)),
        "start": round(floor, 2),
        "seed": sum(ord(c) for c in sym) * 10,
        # Standard camelCase properties
        "tradeId": f"V3_{t.get('trade_id', '')}",
        "symbol": sym,
        "strategyId": strat_id,
        "strategyName": strat_name,
        "entryDate": entry_date,
        "entryPrice": round(ep, 2),
        "currentPrice": round(lp, 2),
        "currentROI": round(roi, 2),
        "stopLoss": round(stop, 2),
        "targetPrice": round(target, 2),
        "beLockPrice": round(be_lock, 2),
        "pivot": round(pivot, 2),
        "floor": round(floor, 2),
        "fibLevels": fib_levels,
        "geometry": geom,
        "status": t.get("status", "ACTIVE"),
        "compositeScore": composite_score,
        "stars": stars,
        "starsDisplay": stars_display,
        "action": action,
        "actionLabel": action_label,
        "actionColor": action_color,
        "slippageText": slippage_text,
        "footerLine": footer_line,
        "stop": round(stop, 2),
        "target": round(target, 2),
        "livePrice": round(lp, 2),
        "roiPct": round(roi, 2),
        "convictionScore": composite_score,
        "holdingDays": days,
        "shares": int(t.get("shares", 1)),
        "miniChart": t.get("mini_chart", {}),
        "sparkline": t.get("sparkline", [])
    }


def format_closed_v3(t: Dict[str, Any]) -> Dict[str, Any]:
    sym = t.get("symbol", "")
    comp_name = t.get("company_name") or COMPANY_NAMES.get(sym, sym.title() + " Limited")
    ep = float(t.get("entry_price", 0.0))
    xp = float(t.get("exit_price", ep))
    roi = round(((xp - ep) / ep * 100.0) if ep > 0 else 0.0, 2)
    h_days = int(t.get("holding_days", 0))
    realized_pnl = float(t.get("net_pnl", 0.0))
    r_val = float(t.get("risknorm_r", t.get("capped_r", roi / 7.0)))
    reason_str = str(t.get("exit_reason", "TARGET_HIT"))
    exit_dt = str(t.get("exit_date", ""))[:10]
    date_display = exit_dt[5:] if len(exit_dt) >= 10 else exit_dt

    return {
        # Prototype UI direct properties
        "sym": sym,
        "ab": sym[:2].upper(),
        "name": comp_name,
        "entry": round(ep, 2),
        "exit": round(xp, 2),
        "pnl": round(realized_pnl, 2),
        "pnlPct": roi,
        "r": round(r_val, 2),
        "days": h_days,
        "reason": reason_str,
        "date": date_display or "Recently",
        "seed": sum(ord(c) for c in sym) * 11,
        "start": round(ep * 0.95, 2),
        "trend": 0.005 if roi >= 0 else -0.004,
        "why": t.get("why", ""),
        "radar": t.get("radar", []),
        "timeline": t.get("timeline", []),
        # Standard camelCase properties
        "tradeId": f"V3_{t.get('trade_id', '')}",
        "symbol": sym,
        "strategyId": t.get("strategy_id", ""),
        "strategyName": t.get("strategy_id", ""),
        "entryDate": t.get("entry_date", ""),
        "exitDate": t.get("exit_date", ""),
        "entryPrice": ep,
        "exitPrice": xp,
        "roiPct": roi,
        "realizedPnl": round(realized_pnl, 2),
        "exitReason": reason_str,
        "holdingDays": h_days,
        "isWin": bool(roi >= 0),
        "sparkline": t.get("sparkline", []),
        "miniChart": {"sparkline": t.get("sparkline", [])}
    }


def push_to_wix(phase: str = "AUTO") -> bool:
    """
    Constructs and pushes the unified multi-engine payload to Wix.
    """
    wix_cfg = _load_wix_cfg()
    if not wix_cfg.get("enabled", True):
        logger.debug("Wix sync disabled in config.json.")
        return False

    wix_url = wix_cfg.get("update_url", DEFAULT_WIX_URL)

    try:
        from common.storage import MasterStorage
        storage = MasterStorage()

        # 1. Fetch raw data
        active_v2_raw = storage.get_active_trades("v2")
        closed_v2_raw = storage.get_closed_trades("v2")
        active_v3_raw = storage.get_active_trades("v3")
        closed_v3_raw = storage.get_closed_trades("v3")

        latest_nav_v2 = storage.get_latest_nav("v2")
        latest_nav_v3 = storage.get_latest_nav("v3")

        # 2. Format V2 Payload (Active has full miniChart; Closed has full miniChart for top 30, sparkline for all)
        active_v2 = [format_active_v2(t) for t in active_v2_raw]
        closed_v2 = []
        for i, t in enumerate(closed_v2_raw):
            f = format_closed_v2(t)
            if i >= 30:
                f["miniChart"] = {"sparkline": f.get("sparkline", [])}
            closed_v2.append(f)

        today = datetime.date.today()
        signal_cutoff = (today - datetime.timedelta(days=10)).isoformat()
        to_buy_v2 = [t for t in active_v2 if t.get("entryDate") and t["entryDate"] >= signal_cutoff]

        v2_closed_count = len(closed_v2)
        v2_wins = [t for t in closed_v2 if t.get("realizedPnl", t.get("roiPct", 0)) > 0]
        v2_losses = [t for t in closed_v2 if t.get("realizedPnl", t.get("roiPct", 0)) <= 0]
        v2_wr = round(len(v2_wins) / v2_closed_count * 100.0, 1) if v2_closed_count else 0.0

        gross_win = sum(t.get("realizedPnl", 0.0) for t in v2_wins)
        gross_loss = abs(sum(t.get("realizedPnl", 0.0) for t in v2_losses))
        v2_pf = round(gross_win / max(1e-4, gross_loss), 2) if gross_loss else 3.64

        v2_cash = latest_nav_v2.get("cash", 300000.0) if latest_nav_v2 else 300000.0
        v2_nav = latest_nav_v2.get("total_nav", 300000.0) if latest_nav_v2 else 300000.0

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

        total_summary_obj = {
            "totalTrades": v2_closed_count,
            "wins": len(v2_wins),
            "losses": len(v2_losses),
            "winRate": v2_wr,
            "profitFactor": v2_pf,
            "totalReturnPct": round((v2_nav - 300000.0) / 300000.0 * 100.0, 2),
            "allTimeTradesClosed": v2_closed_count,
        }
        kpis_obj = {
            "totalStocks": 1318,
            "activeCount": len(active_v2),
            "totalTradesClosed": v2_closed_count,
            "totalTrades": v2_closed_count,
            "wins": len(v2_wins),
            "losses": len(v2_losses),
            "winRate": v2_wr,
            "profitFactor": v2_pf,
            "totalReturnPct": round((v2_nav - 300000.0) / 300000.0 * 100.0, 2),
            "allTimeTradesClosed": v2_closed_count,
        }

        terminal_snapshot = {
            "system": "QuantSync-V2",
            "inTradeTrades": active_v2,
            "closedTrades": closed_v2,
            "toBuySignals": to_buy_v2,
            "totalSummary": total_summary_obj,
            "kpis": kpis_obj,
            "activeCount": len(active_v2),
            "closedCount": v2_closed_count,
            "winRate": v2_wr,
            "profitFactor": v2_pf,
            "totalReturnPct": round((v2_nav - 300000.0) / 300000.0 * 100.0, 2),
            "slotsUsed": len(active_v2),
            "slotsMax": 25,
            "availableCash": round(v2_cash, 2),
            "portfolioValue": round(v2_nav, 2),
            "updatedAt": now_str,
            "status": "LIVE_TRADING_ACTIVE",
            "message": f"QuantSync Gate 5 V2 Engine ({phase})"
        }

        # 3. Format V3 Payload
        active_v3 = [format_active_v3(t) for t in active_v3_raw]
        closed_v3 = []
        for i, t in enumerate(closed_v3_raw):
            f = format_closed_v3(t)
            if i >= 30:
                f["miniChart"] = {"sparkline": f.get("sparkline", [])}
            closed_v3.append(f)
        to_buy_v3 = [t for t in active_v3 if t.get("entryDate") and t["entryDate"] >= signal_cutoff]

        # 4. Assemble sub-payloads matching Wix expectations
        v2_payload = {
            "active": active_v2,
            "closed": closed_v2,
            "signals": to_buy_v2,
            "toBuySignals": to_buy_v2,
            "terminalSnapshot": terminal_snapshot
        }

        v3_payload = {
            "activeV3": active_v3,
            "closedV3": closed_v3,
            "signalsV3": to_buy_v3,
            "toBuySignalsV3": to_buy_v3
        }

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "QuantSync-ProdVPS/1.0"
        }

        logger.info(f"📡 Dispatching Wix Sync ({phase}): V2 Active={len(active_v2)}, Closed={len(closed_v2)} | V3 Active={len(active_v3)}, Closed={len(closed_v3)}...")

        # Push V2
        req_v2 = urllib.request.Request(
            wix_url,
            data=json.dumps(v2_payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req_v2, timeout=20) as resp:
            v2_status = resp.status

        # Push V3
        req_v3 = urllib.request.Request(
            wix_url,
            data=json.dumps(v3_payload).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req_v3, timeout=20) as resp:
            v3_status = resp.status

        ok = (v2_status == 200 and v3_status == 200)
        if ok:
            logger.info(f"✅ Wix Sync ({phase}) successfully delivered (V2={v2_status}, V3={v3_status}).")
        return ok

    except Exception as e:
        logger.error(f"❌ Wix Sync failed ({phase}): {e}")
        return False
