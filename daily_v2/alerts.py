"""
daily_v2/alerts.py — Production Telegram Alert Dispatcher for Daily V2

Provides rich real-time Telegram alerts for:
1. BUY Alerts (Symbol, Strategy, Entry Price, ML Probability, Star Rating)
2. EXIT Alerts (Symbol, Strategy, Exit Reason, Realized Return %, Holding Days, Peak MFE)
3. Daily Cycle Summary Alerts (Total Entries, Exits, Active Positions, Top Signals)

Non-blocking & fail-safe: Never interrupts trading execution if network/Telegram API is unavailable.
"""

import os
import json
import logging
import urllib.request
import urllib.parse
from typing import Dict, List, Any, Optional

from daily_v2.config import TELEGRAM_CONFIG, STRATEGY_DISPLAY_NAMES

logger = logging.getLogger("DailyV2.Alerts")


def send_telegram_message(
    text: str,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    silent: bool = False
) -> bool:
    """Sends an HTML-formatted message to the designated Telegram chat."""
    if not TELEGRAM_CONFIG.get("enabled", True):
        logger.info(f"[Telegram Disabled] {text}")
        return False

    token = bot_token or TELEGRAM_CONFIG.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    target_chat = chat_id or TELEGRAM_CONFIG.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not target_chat:
        logger.debug(f"[Telegram Credentials Missing] Message: {text}")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": target_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent
    }

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")
        return False


def format_buy_alert(trade: Dict[str, Any]) -> str:
    """Formats rich BUY message."""
    sym = trade.get('symbol', 'UNKNOWN')
    strat_id = trade.get('strategy_id') or trade.get('strategy', 'Setup')
    strat_name = STRATEGY_DISPLAY_NAMES.get(strat_id, strat_id)
    price = float(trade.get('entry_price', 0.0))
    p_val = float(trade.get('calibrated_p', trade.get('calibrated_probability', trade.get('raw_ml_score', 0.50))))
    ml_prob = p_val * 100.0 if p_val <= 1.0 else p_val
    date = trade.get('signal_date') or trade.get('entry_date', '')
    stars_display = trade.get('stars_display', '★★★★☆')

    return (
        f"🟢 <b>BUY SIGNAL | DAILY V2 BASELINE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Stock:</b> #{sym}\n"
        f"🎯 <b>Strategy:</b> {strat_name}\n"
        f"💰 <b>Entry Price:</b> ₹{price:,.2f}\n"
        f"🤖 <b>Calibrated P:</b> {ml_prob:.1f}%\n"
        f"⭐ <b>Rating:</b> {stars_display}\n"
        f"📅 <b>Date:</b> {date}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def format_exit_alert(trade: Dict[str, Any]) -> str:
    """Formats rich EXIT message."""
    sym = trade.get('symbol', 'UNKNOWN')
    strat_id = trade.get('strategy_id') or trade.get('strategy', 'Setup')
    strat_name = STRATEGY_DISPLAY_NAMES.get(strat_id, strat_id)
    entry_p = float(trade.get('entry_price', 0.0))
    exit_p = float(trade.get('exit_price', 0.0))
    ret_pct = float(trade.get('roi_pct', trade.get('realized_return_pct', 0.0)))
    reason = trade.get('exit_reason', 'HEALTH_EXIT')
    hold_days = trade.get('holding_days', 0)
    mfe = float(trade.get('mfe_pct', 0.0))

    ret_emoji = "🟢" if ret_pct > 0 else "🔴"
    ret_sign = "+" if ret_pct > 0 else ""

    return (
        f"🔴 <b>EXIT SIGNAL | DAILY V2 BASELINE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>Stock:</b> #{sym}\n"
        f"🎯 <b>Strategy:</b> {strat_name}\n"
        f"💰 <b>Entry:</b> ₹{entry_p:,.2f} ➔ <b>Exit:</b> ₹{exit_p:,.2f}\n"
        f"{ret_emoji} <b>Realized ROI:</b> <b>{ret_sign}{ret_pct:.2f}%</b>\n"
        f"📈 <b>Peak MFE:</b> +{mfe:.1f}%\n"
        f"⏱ <b>Held:</b> {hold_days} trading days\n"
        f"🛡 <b>Exit Reason:</b> {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


def format_daily_summary_alert(summary: Dict[str, Any]) -> str:
    """Formats rich EOD cycle summary."""
    dt = summary.get('date', 'TODAY')
    entries = summary.get('new_entries_count', 0)
    exits = summary.get('exits_count', 0)
    actives = summary.get('active_count', 0)
    win_rate = summary.get('portfolio_win_rate', 0.0)
    pf = summary.get('portfolio_pf', 0.0)

    entry_syms = summary.get('entered_symbols', [])
    exit_syms = summary.get('exited_symbols', [])

    entry_str = f"➕ <b>New Buys ({entries}):</b> {', '.join(entry_syms) if entry_syms else 'None'}"
    exit_str = f"➖ <b>Exits ({exits}):</b> {', '.join(exit_syms) if exit_syms else 'None'}"

    return (
        f"📊 <b>DAILY V2 EOD CYCLE SUMMARY</b>\n"
        f"📅 <b>Date:</b> {dt}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Active Positions:</b> {actives}\n"
        f"{entry_str}\n"
        f"{exit_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Portfolio Win Rate:</b> {win_rate:.1f}%\n"
        f"📈 <b>Profit Factor:</b> {pf:.2f}x\n"
        f"⚡ <b>Engine Status:</b> ONLINE & SYNCHRONIZED"
    )


def send_buy_alert(trade: Dict[str, Any]) -> bool:
    return send_telegram_message(format_buy_alert(trade))


def send_exit_alert(trade: Dict[str, Any]) -> bool:
    return send_telegram_message(format_exit_alert(trade))


def send_daily_summary_alert(summary: Dict[str, Any]) -> bool:
    return send_telegram_message(format_daily_summary_alert(summary))
