"""
angel_auth.py — Backward-compatibility shim for legacy QuantSync imports.
========================================================================
Angel One authentication and broker functions were unified into common.broker.
This file provides backward-compatibility for legacy callers and language servers.
"""

from common.broker import (
    get_session,
    fetch_holdings,
    fetch_holdings as fetch_angel_holdings,
    fetch_batch_quotes,
    _HOLDING_URL,
    _CANDLE_URL,
    _MARKET_URL,
)

# Legacy alias
fresh_angel_login = get_session

__all__ = [
    "get_session",
    "fresh_angel_login",
    "fetch_holdings",
    "fetch_angel_holdings",
    "fetch_batch_quotes",
    "_HOLDING_URL",
    "_CANDLE_URL",
    "_MARKET_URL",
]
