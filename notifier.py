"""
common/notifier.py — Shared Multi-Engine Telegram Alerts Dispatcher
===================================================================
Dispatches real-time Telegram notifications to @rt1295_buyalert tagged by engine version:
  - Buy Alerts:          [V3-PROD] / [V2-PROD]
  - Exit Alerts:         [V3-PROD] / [V2-PROD]
  - Daily EOD Summary:   Unified multi-engine performance recap
  - System Audit Proofs: Job completion receipts
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger("ProdVPS.Notifier")

_COMMON_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_ROOT = _COMMON_DIR if os.path.exists(os.path.join(_COMMON_DIR, "config.json")) else os.path.dirname(_COMMON_DIR)


def get_tg_creds(engine_version: Optional[str] = None) -> Dict[str, Any]:
    """
    Resolves Telegram configuration with priority:
      1. Specific engine configuration in config.json -> engines.<engine_id>.telegram
      2. Master fallback configuration in config.json -> telegram
    """
    cfg_path = os.path.join(_PROD_ROOT, "config.json")
    master = {
        "enabled": True,
        "bot_token": "7591776444:AAHs2YW2oLKwmCBVJhGVRu3L5JFsmSkB6gI",
        "chat_id": "@rt1295_buyalert"
    }

    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                full_cfg = json.load(f)
            master.update(full_cfg.get("telegram", {}))

            if engine_version:
                eng_key = str(engine_version).lower()
                eng_tg = full_cfg.get("engines", {}).get(eng_key, {}).get("telegram")
                if eng_tg and isinstance(eng_tg, dict):
                    return {
                        "enabled": eng_tg.get("enabled", master.get("enabled", True)),
                        "bot_token": eng_tg.get("bot_token") or master.get("bot_token", ""),
                        "chat_id": eng_tg.get("chat_id") or master.get("chat_id", "")
                    }
        except Exception as e:
            logger.warning(f"Telegram config resolution error: {e}")

    return master


_last_send_by_chat: Dict[str, float] = {}


def send_message(
    text: str,
    engine_version: Optional[str] = None,
    parse_mode: str = "HTML",
    custom_bot_token: Optional[str] = None,
    custom_chat_id: Optional[str] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
    max_retries: int = 4
) -> bool:
    """
    Dispatches a Telegram message to the appropriate engine channel or master channel.
    Enforces a 1.05-second per-chat pacing delay and auto-retries on HTTP 429 Too Many Requests.
    """
    global _last_send_by_chat

    if custom_bot_token and custom_chat_id:
        enabled, bot_token, chat_id = True, custom_bot_token, custom_chat_id
    else:
        creds = get_tg_creds(engine_version)
        enabled = creds.get("enabled", True)
        bot_token = creds.get("bot_token", "")
        chat_id = creds.get("chat_id", "")

    if not enabled or not bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload_dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload_dict["reply_markup"] = reply_markup
    payload = json.dumps(payload_dict).encode("utf-8")

    for attempt in range(1, max_retries + 1):
        # Enforce Telegram chat rate limit of ~1 msg/sec per chat
        now = time.time()
        last_t = _last_send_by_chat.get(chat_id, 0.0)
        elapsed = now - last_t
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)

        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                _last_send_by_chat[chat_id] = time.time()
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok", False))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait_sec = 3.0
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                    wait_sec = float(err_body.get("parameters", {}).get("retry_after", 3.0))
                except Exception:
                    pass
                logger.warning(f"Telegram 429 rate limit hit for {chat_id}. Sleeping {wait_sec + 0.5:.1f}s before retry {attempt}/{max_retries}...")
                time.sleep(wait_sec + 0.5)
                continue
            else:
                logger.error(f"Telegram dispatch HTTP error {e.code} (engine={engine_version}, chat={chat_id}): {e}")
                return False
        except Exception as e:
            logger.error(f"Telegram dispatch failed (engine={engine_version}, chat={chat_id}): {e}")
            time.sleep(1.0)
            return False

    return False


_bundles_cache = None


def _get_candle_bar(sym: str, dt: str) -> Dict[str, Any]:
    global _bundles_cache
    if _bundles_cache is None:
        cache_path = os.path.join(_PROD_ROOT, "Data", "v2_2026_bundles.pkl")
        if os.path.exists(cache_path):
            try:
                import pickle
                with open(cache_path, "rb") as f:
                    _bundles_cache = pickle.load(f)
            except Exception:
                _bundles_cache = {}
        else:
            _bundles_cache = {}
    bnd = _bundles_cache.get(sym, {})
    return bnd.get("date_to_candle", {}).get(dt, {})


def send_photo(
    photo_bytes: bytes,
    engine_version: Optional[str] = None,
    caption: str = "",
    inline_button_text: Optional[str] = None,
    inline_button_url: Optional[str] = None,
    max_retries: int = 3
) -> bool:
    global _last_send_by_chat
    creds = get_tg_creds(engine_version)
    bot_token = creds.get("bot_token", "")
    chat_id = creds.get("chat_id", "")
    if not bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    # Enforce pacing
    now = time.time()
    last_t = _last_send_by_chat.get(chat_id, 0.0)
    elapsed = now - last_t
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)

    payload = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    if inline_button_text and inline_button_url:
        payload["reply_markup"] = json.dumps({
            "inline_keyboard": [
                [{"text": inline_button_text, "url": inline_button_url}]
            ]
        })

    files = {"photo": ("alert.png", photo_bytes, "image/png")}

    try:
        import requests
        resp = requests.post(url, data=payload, files=files, timeout=12)
        _last_send_by_chat[chat_id] = time.time()
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram sendPhoto failed: {e}")
        return False


STRATEGY_SINGLE_WORD: Dict[str, str] = {
    "apexbase": "Apex",
    "apex": "Apex",
    "hightightflag": "Flag",
    "hightight": "Flag",
    "flag": "Flag",
    "stage2basebreakout": "Breakout",
    "stage2base": "Breakout",
    "stage2": "Breakout",
    "breakout": "Breakout",
    "breakout1d": "Breakout",
    "stratbreakout": "Breakout",
    "powerearningsgap": "Gap",
    "gap": "Gap",
    "rsleader": "Leader",
    "leader": "Leader",
    "relativestrengthleader": "Leader",
    "relativestrengthleader1d": "Leader",
    "rsleader1d": "Leader",
    "stratrsleader": "Leader",
    "institutionalpocketpivot1d": "Pivot",
    "institutionalpocketpivot": "Pivot",
    "pocketpivot1d": "Pivot",
    "pocketpivot": "Pivot",
    "stratpocketpivot": "Pivot",
    "pivot": "Pivot",
    "structuralvaluepullback1d": "Pullback",
    "structuralvaluepullback": "Pullback",
    "valuepullback1d": "Pullback",
    "valuepullback": "Pullback",
    "stratvaluepullback": "Pullback",
    "pullback": "Pullback",
    "alpha": "Alpha",
    "alpha1d": "Alpha",
    "stratalpha": "Alpha",
    "alphamomentumbreakout1d": "Alpha",
    "alphamomentumbreakout": "Alpha",
    "alphabreakout1d": "Alpha",
    "alphabreakout": "Alpha",
    "stratalphabreakout": "Alpha",
    "cuphandlepivot2d": "Cup",
    "cuphandlepivot": "Cup",
    "cuphandle2d": "Cup",
    "cuphandle": "Cup",
    "stratcuphandle": "Cup",
    "cup": "Cup",
    "donchiantrendsurge2d": "Surge",
    "donchiantrendsurge": "Surge",
    "donchiansurge2d": "Surge",
    "donchiansurge": "Surge",
    "stratdonchiansurge": "Surge",
    "surge": "Surge",
    "gaptrendexpansion1d": "Expansion",
    "gaptrendexpansion": "Expansion",
    "gapexpansion1d": "Expansion",
    "gapexpansion": "Expansion",
    "stratgapexpansion": "Expansion",
    "expansion": "Expansion",
    "mtfmacrocontinuation3d": "Macro",
    "mtfmacrocontinuation": "Macro",
    "mtfmacro3d": "Macro",
    "mtfmacro": "Macro",
    "stratmtfmacro": "Macro",
    "macro": "Macro",
    "threebarimpulsethrust2d": "Thrust",
    "threebarimpulsethrust": "Thrust",
    "threebarthrust2d": "Thrust",
    "threebarthrust": "Thrust",
    "stratthreebarthrust": "Thrust",
    "thrust": "Thrust",
    "volatilitysqueezerelease2d": "Squeeze",
    "volatilitysqueezerelease": "Squeeze",
    "squeezerelease2d": "Squeeze",
    "squeezerelease": "Squeeze",
    "stratsqueezerelease": "Squeeze",
    "squeeze": "Squeeze",
}


def format_strategy_name(strat: str) -> str:
    import re
    if not strat:
        return "Alpha"
    key = str(strat).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    key = re.sub(r"sleeve\d+", "", key)
    if key in STRATEGY_SINGLE_WORD:
        return STRATEGY_SINGLE_WORD[key]
    for k in sorted(STRATEGY_SINGLE_WORD.keys(), key=len, reverse=True):
        if k in key or key in k:
            return STRATEGY_SINGLE_WORD[k]
    cleaned = strat.replace("_1D", "").replace("_2D", "").replace("_3D", "")
    cleaned = re.sub(r"[_\s·-]*sleeve[_\s]*\d+", "", cleaned, flags=re.IGNORECASE)
    parts = re.findall(r"[A-Za-z0-9]+", cleaned)
    return "".join(p.capitalize() for p in parts) if parts else "Alpha"


def notify_buy(trade: Dict[str, Any], engine_version: str = "v3", bar: Optional[Dict[str, Any]] = None) -> bool:
    sym = trade.get("symbol", "N/A").upper()
    strat = trade.get("strategy_id", "Alpha")
    strat_clean = format_strategy_name(strat)
    ep = float(trade.get("entry_price", 0.0))
    stop_p = float(trade.get("current_stop", trade.get("stop_price", ep * 0.92)))
    tgt_p = float(trade.get("target_price", ep + 2.5 * (ep - stop_p)))
    risk_p = max(0.01, ep - stop_p)
    risk_pct = (risk_p / ep * 100.0) if ep > 0 else 0.0
    tgt_pct = ((tgt_p - ep) / ep * 100.0) if ep > 0 else 0.0
    dt = trade.get("entry_date") or trade.get("date") or trade.get("current_date") or ""
    notify_start = os.environ.get("NOTIFY_START_DATE")
    if notify_start and dt and dt < notify_start:
        return True

    stop_str = f"₹{stop_p:,.2f}"
    tgt_str = f"₹{tgt_p:,.2f}"
    max_p_len = max(len(stop_str), len(tgt_str))
    stop_pad = stop_str.ljust(max_p_len)
    tgt_pad = tgt_str.ljust(max_p_len)

    chart_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(sym)}"

    bottom_line = f"{strat_clean} · {dt}" if dt else strat_clean
    ml_score = trade.get("ml_score")
    ml_line = f"\nML Score {float(ml_score)*100:.0f}%" if ml_score else ""

    msg = (
        f"🟢 <b>NEW BUY</b>\n\n"
        f"🚀 <a href=\"{chart_url}\"><b>{sym}</b></a> @ ₹{ep:,.2f}\n"
        f"───────────────\n"
        f"Stop     {stop_pad} (−{risk_pct:.1f}%)\n"
        f"Target  {tgt_pad} (+{tgt_pct:.1f}%){ml_line}\n"
        f"───────────────\n"
        f"{bottom_line}"
    )

    return send_message(msg, engine_version=engine_version)


def notify_exit(trade: Dict[str, Any], engine_version: str = "v3", bar: Optional[Dict[str, Any]] = None) -> bool:
    sym = trade.get("symbol", "N/A").upper()
    strat = trade.get("strategy_id", "Alpha")
    strat_clean = format_strategy_name(strat)
    ep = float(trade.get("entry_price", 0.0))
    xp = float(trade.get("exit_price", 0.0))
    ret_pct = float(trade.get("return_pct", ((xp - ep) / ep * 100.0) if ep > 0 else 0.0))
    reason = str(trade.get("exit_reason", "TARGET_HIT")).upper()
    dur = int(trade.get("holding_days", trade.get("duration", trade.get("days_held", 1))))
    entry_dt = trade.get("entry_date") or trade.get("date") or ""
    exit_dt = trade.get("exit_date") or trade.get("date") or ""
    bottom_dt = exit_dt or entry_dt
    notify_start = os.environ.get("NOTIFY_START_DATE")
    if notify_start and bottom_dt and bottom_dt < notify_start:
        return True

    chart_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(sym)}"

    dur_str = f"({dur} Day)" if dur == 1 else f"({dur} Days)"

    if "TARGET" in reason or ret_pct >= 0:
        header = "🎯 <b>EXIT — TARGET HIT</b>" if "TARGET" in reason else "🟢 <b>EXIT — PROFIT</b>"
        emoji = "🎯" if "TARGET" in reason else "💰"
        pnl_str = f"+{ret_pct:.2f}%"
    else:
        header = "🛑 <b>EXIT — STOP HIT</b>" if "STOP" in reason else "🔴 <b>EXIT — LOSS</b>"
        emoji = "🛑" if "STOP" in reason else "🔻"
        pnl_str = f"−{abs(ret_pct):.2f}%"

    ep_str = f"₹{ep:,.2f}"
    xp_str = f"₹{xp:,.2f}"
    max_p_len = max(len(ep_str), len(xp_str))
    ep_pad = ep_str.ljust(max_p_len)
    xp_pad = xp_str.ljust(max_p_len)

    if entry_dt:
        entry_str = f"Entry  {ep_pad} → {entry_dt}"
    else:
        entry_str = f"Entry  {ep_str}"

    if exit_dt:
        exit_str = f"Exit     {xp_pad} → {exit_dt}"
    else:
        exit_str = f"Exit     {xp_str}"

    bottom_dt = exit_dt or entry_dt
    bottom_line = f"{strat_clean} · {bottom_dt}" if bottom_dt else strat_clean

    msg = (
        f"{header}\n\n"
        f"{emoji} <a href=\"{chart_url}\"><b>{sym}</b></a> · <b>{pnl_str}</b> {dur_str}\n"
        f"───────────────\n"
        f"{entry_str}\n"
        f"{exit_str}\n"
        f"───────────────\n"
        f"{bottom_line}"
    )

    return send_message(msg, engine_version=engine_version)


def notify_next_day_exit(trade: Dict[str, Any], engine_version: str = "v3") -> bool:
    sym = trade.get("symbol", "N/A").upper()
    chart_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(sym)}"
    msg = (
        f"🚨 <b>EXIT ACTION REQUIRED: <a href=\"{chart_url}\"><b>NSE:{sym}</b></a></b>\n\n"
        f"• <b>Trigger:</b> {trade.get('exit_reason', 'STOP_OR_TARGET')}\n"
        f"• <b>Action:</b> <b>SELL TOMORROW AT OPEN (09:15 AM)</b>\n"
        f"• <b>Ref Price:</b> ₹{trade.get('exit_price', trade.get('current_ltp', 0.0)):.2f}\n"
        f"• <b>Entry Price:</b> ₹{trade.get('entry_price', 0.0):.2f}\n"
        f"• <b>Holding Days:</b> {trade.get('holding_days', trade.get('days_held', 0))} days\n"
        f"📈 <b><a href=\"{chart_url}\">Open {sym} on TradingView ↗</a></b>"
    )
    return send_message(msg, engine_version=engine_version)


def notify_next_day_buys(candidates: List[Dict[str, Any]], engine_version: str = "v3") -> bool:
    if not candidates:
        return True
    lines = ["📋 <b>NEXT-DAY BUY WATCHLIST (09:15 AM OPEN)</b>\n━━━━━━━━━━━━━━━━━━━"]
    for c in candidates[:10]:
        sym = str(c.get("symbol", "")).upper()
        chart_url = f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(sym)}"
        ep = float(c.get("entry_price", 0.0))
        stop_p = float(c.get("stop_price", c.get("initial_stop", 0.0)))
        lines.append(f"• <a href=\"{chart_url}\"><b>NSE:{sym}</b></a> | Entry: ₹{ep:.2f} | Stop: ₹{stop_p:.2f}")
    lines.append("━━━━━━━━━━━━━━━━━━━\n<i>Computed on today's finalized daily close.</i>")
    return send_message("\n".join(lines), engine_version=engine_version)


def notify_eod_summary(engines_summary: Dict[str, Any], date_str: str) -> bool:
    """No-op to prevent sending NAV or portfolio details to Telegram channels per user constraint."""
    return True


def notify_audit_proof(job_name: str, schedule_str: str, duration_s: float, symbols: int, orders: int, status: str = "SUCCESS") -> bool:
    status_emoji = "✅" if status == "SUCCESS" else "⚠️"
    msg = (
        f"{status_emoji} <b>SYSTEM AUDIT PROOF</b>\n\n"
        f"• <b>Job:</b> {job_name}\n"
        f"• <b>Schedule:</b> {schedule_str}\n"
        f"• <b>Status:</b> {status}\n"
        f"• <b>Symbols Checked:</b> {symbols}\n"
        f"• <b>Actions Executed:</b> {orders}\n"
        f"• <b>Execution Time:</b> {duration_s:.2f}s"
    )
    return send_message(msg, engine_version=None)
