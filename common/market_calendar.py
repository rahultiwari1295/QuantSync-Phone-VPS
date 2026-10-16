"""
common/market_calendar.py — single source of truth for NSE trading days.

Every job (market sweep, candle sync, settlement, EOD pipeline, screener) must ask
this module whether a date is a trading session. Before this existed the system only
checked Monday–Friday, which created a fake candle for ~2,000 stocks on the
2 Oct 2026 holiday and ran settlement on it.

Holidays 2021 → Sep 2026 were verified against the candle store (weekdays with no
candles for any large-cap). Remaining 2026 dates are from the NSE circular.
Add future years here, or point NSE_HOLIDAYS_FILE at a JSON list of ISO dates.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from typing import List, Optional, Union

logger = logging.getLogger("market_calendar")

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
SESSION_OPEN = _dt.time(9, 15)
SESSION_CLOSE = _dt.time(15, 30)

HOLIDAY_LIST: List[str] = [
    # 2018-2020
    "2018-12-25",
    "2019-03-04", "2019-03-21", "2019-04-17", "2019-04-19", "2019-04-29", "2019-05-01",
    "2019-06-05", "2019-08-12", "2019-08-15", "2019-09-02", "2019-09-10", "2019-10-02",
    "2019-10-08", "2019-10-21", "2019-10-28", "2019-11-12", "2019-12-25",
    "2020-02-21", "2020-03-10", "2020-04-02", "2020-04-06", "2020-04-10", "2020-04-14",
    "2020-05-01", "2020-05-25", "2020-10-02", "2020-11-16", "2020-11-30", "2020-12-25",
    # 2021
    "2021-01-26", "2021-03-11", "2021-03-29",
    "2021-04-02", "2021-04-14", "2021-04-21", "2021-05-13", "2021-07-21", "2021-08-19",
    "2021-09-10", "2021-10-15", "2021-11-05", "2021-11-19",
    # 2022
    "2022-01-26", "2022-03-01", "2022-03-18", "2022-04-14", "2022-04-15", "2022-05-03",
    "2022-08-09", "2022-08-15", "2022-08-31", "2022-10-05", "2022-10-26", "2022-11-08",
    # 2023
    "2023-01-26", "2023-03-07", "2023-03-30", "2023-04-04", "2023-04-07", "2023-04-14",
    "2023-05-01", "2023-06-29", "2023-08-15", "2023-09-19", "2023-10-02", "2023-10-24",
    "2023-11-14", "2023-11-27", "2023-12-25",
    # 2024
    "2024-01-22", "2024-01-26", "2024-03-08", "2024-03-25", "2024-03-29", "2024-04-11",
    "2024-04-17", "2024-05-01", "2024-05-20", "2024-06-17", "2024-07-17", "2024-08-15",
    "2024-10-02", "2024-11-15", "2024-11-20", "2024-12-25",
    # 2025
    "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10", "2025-04-14", "2025-04-18",
    "2025-05-01", "2025-08-15", "2025-08-27", "2025-10-02", "2025-10-22", "2025-11-05",
    "2025-12-25",
    # 2026
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03",
    "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
    "2026-10-20", "2026-11-10", "2026-11-24", "2026-12-25",
]

# Weekend sessions that did trade (Muhurat, Budget-day and NSE special/DR sessions).
SPECIAL_SESSIONS = {
    "2019-10-27", "2020-02-01", "2020-11-14", "2023-11-12", "2024-01-20", "2024-03-02",
    "2024-05-18", "2025-02-01", "2026-02-01",
}

_HOLIDAYS = set(HOLIDAY_LIST)

_extra = os.environ.get("NSE_HOLIDAYS_FILE")
if _extra and os.path.exists(_extra):
    try:
        with open(_extra) as fh:
            _HOLIDAYS.update(str(x)[:10] for x in json.load(fh))
    except Exception as exc:  # pragma: no cover
        logger.warning(f"Could not load NSE_HOLIDAYS_FILE={_extra}: {exc}")

DateLike = Union[_dt.date, _dt.datetime, str, None]


def ist_now() -> _dt.datetime:
    """Timezone-aware current time in IST (independent of server timezone)."""
    return _dt.datetime.now(IST)


def _to_date(d: DateLike) -> _dt.date:
    if d is None:
        return ist_now().date()
    if isinstance(d, _dt.datetime):
        if d.tzinfo is not None:
            d = d.astimezone(IST)
        return d.date()
    if isinstance(d, _dt.date):
        return d
    return _dt.date.fromisoformat(str(d)[:10])


def is_holiday(d: DateLike = None) -> bool:
    return _to_date(d).isoformat() in _HOLIDAYS


def is_trading_day(d: DateLike = None) -> bool:
    """True for Mon–Fri sessions that are not NSE holidays."""
    day = _to_date(d)
    iso = day.isoformat()
    if iso in SPECIAL_SESSIONS:
        return True
    return day.weekday() < 5 and iso not in _HOLIDAYS


def previous_trading_day(d: DateLike = None) -> _dt.date:
    day = _to_date(d) - _dt.timedelta(days=1)
    while not is_trading_day(day):
        day -= _dt.timedelta(days=1)
    return day


def next_trading_day(d: DateLike = None) -> _dt.date:
    day = _to_date(d) + _dt.timedelta(days=1)
    while not is_trading_day(day):
        day += _dt.timedelta(days=1)
    return day


def last_completed_session(now: Optional[_dt.datetime] = None) -> _dt.date:
    """Most recent session whose official close (15:30 IST) has passed."""
    now = (now or ist_now())
    now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    today = now.date()
    if is_trading_day(today) and now.time() >= SESSION_CLOSE:
        return today
    return previous_trading_day(today)


def trading_days_between(start: DateLike, end: DateLike) -> List[_dt.date]:
    a, b = _to_date(start), _to_date(end)
    out: List[_dt.date] = []
    while a <= b:
        if is_trading_day(a):
            out.append(a)
        a += _dt.timedelta(days=1)
    return out


def recent_trading_days(n: int, until: DateLike = None) -> List[_dt.date]:
    """The last n trading days up to and including `until` (if it is a session)."""
    day = _to_date(until)
    out: List[_dt.date] = []
    if not is_trading_day(day):
        day = previous_trading_day(day)
    while len(out) < n:
        out.append(day)
        day = previous_trading_day(day)
    return sorted(out)


def is_market_open(now: Optional[_dt.datetime] = None) -> bool:
    now = (now or ist_now())
    now = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return is_trading_day(now.date()) and SESSION_OPEN <= now.time() < SESSION_CLOSE
