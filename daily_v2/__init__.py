"""
QuantSync Daily V2 Production System
------------------------------------
Frozen baseline trading engine for NSE equities.
"""

from daily_v2.config import (
    DAILY_V2_VERSION,
    SPECIFICATION_VERSION,
    EXECUTION_CONVENTION,
    ACTIVE_STRATEGIES,
    DAILY_V2_ML_THRESHOLD,
    STRATEGY_DISPLAY_NAMES,
    TELEGRAM_CONFIG
)
from daily_v2.storage import DailyV2Storage
from daily_v2.engine import DailyV2Engine, run_daily_v2_for_date
from daily_v2.alerts import (
    send_buy_alert,
    send_exit_alert,
    send_daily_summary_alert
)
from daily_v2.terminal import (
    _build_buy_now_tile,
    get_active_wix_format,
    get_closed_wix_format,
    get_terminal_data_for_ui_v3
)

__all__ = [
    "DAILY_V2_VERSION",
    "SPECIFICATION_VERSION",
    "EXECUTION_CONVENTION",
    "ACTIVE_STRATEGIES",
    "DAILY_V2_ML_THRESHOLD",
    "STRATEGY_DISPLAY_NAMES",
    "TELEGRAM_CONFIG",
    "DailyV2Storage",
    "DailyV2Engine",
    "run_daily_v2_for_date",
    "send_buy_alert",
    "send_exit_alert",
    "send_daily_summary_alert",
    "_build_buy_now_tile",
    "get_active_wix_format",
    "get_closed_wix_format",
    "get_terminal_data_for_ui_v3"
]
