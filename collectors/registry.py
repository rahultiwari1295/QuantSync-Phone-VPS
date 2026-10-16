"""
collectors/registry.py — every job, its schedule and what it does. One place for the daemon, CLI and ops page.

Schedule kinds (all times IST, days = ISO weekdays 1=Mon..7=Sun):
  {"at": "07:30", "days": [1..5]}                       once a day at/after that time
  {"every_min": 15, "window": ["09:00","18:00"], "days": [1..5]}
  {"every_min": 120, "window": [...], "days": [...], "season": True}   only during results season
  None                                                    manual / queue only
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

WEEKDAYS = [1, 2, 3, 4, 5]

JOBS: Dict[str, Dict[str, Any]] = {
    "listing": {"module": "listing", "args": {}, "schedule": {"at": "07:20", "days": WEEKDAYS}, "timeout_min": 20,
                "label": "Listing master", "about": "NSE equity list: listing date, ISIN, face value"},
    "corp_actions": {"module": "corp_actions", "args": {"days_back": 30}, "schedule": {"at": "07:35", "days": WEEKDAYS}, "timeout_min": 30,
                     "label": "Corporate actions", "about": "Dividends, splits, bonus (NSE), last 30 days"},
    "corp_actions_history": {"module": "corp_actions", "args": {"from_date": "2018-01-01"}, "schedule": None, "timeout_min": 120,
                             "label": "Corporate actions backfill", "about": "One-off history back to 2018"},
    "news": {"module": "news", "args": {}, "schedule": {"every_min": 15, "window": ["09:00", "18:00"], "days": WEEKDAYS}, "timeout_min": 10,
             "label": "News & announcements", "about": "NSE announcements + board meeting calendar"},
    "results_from_cache": {"module": "results", "args": {"mode": "from_cache"}, "schedule": None, "timeout_min": 60,
                           "label": "Re-validate results cache", "about": "Re-check existing screener results against the listing gate and arithmetic rules"},
    "results_daily": {"module": "results", "args": {"mode": "daily"}, "schedule": {"at": "19:00", "days": WEEKDAYS}, "timeout_min": 90,
                      "label": "Results (daily)", "about": "Results filed in the last 3 days + stocks with none"},
    "results_season": {"module": "results", "args": {"mode": "daily"}, "schedule": {"every_min": 120, "window": ["10:00", "22:00"], "days": WEEKDAYS, "season": True},
                       "timeout_min": 90, "label": "Results (season)", "about": "Every 2 hours during results season"},
    "results_weekly": {"module": "results", "args": {"mode": "weekly"}, "schedule": {"at": "03:00", "days": [7]}, "timeout_min": 240,
                       "label": "Results (weekly sweep)", "about": "Every stale or missing stock"},
    "shareholding": {"module": "shareholding", "args": {}, "schedule": {"at": "04:30", "days": [7]}, "timeout_min": 30,
                     "label": "Shareholding", "about": "Promoter / FII / DII / pledge, validated"},
    "ratios": {"module": "ratios_job", "args": {}, "schedule": {"at": "19:45", "days": WEEKDAYS}, "timeout_min": 20,
               "label": "Ratios", "about": "P/E, P/B, ROE, ROCE, D/E from validated results + our price"},
    "status": {"module": "status", "args": {}, "schedule": {"at": "20:00", "days": WEEKDAYS}, "timeout_min": 10,
               "label": "Coverage status", "about": "Per-stock freshness of every dataset"},
    "probe": {"module": "probe", "args": {}, "schedule": None, "timeout_min": 10,
              "label": "Route probe", "about": "Try every route on every endpoint; keeps raw samples"},
}

# Results seasons: the weeks after each quarter closes (Indian filing deadline = 45 days).
SEASONS = ((1, 15, 2, 20), (4, 15, 6, 5), (7, 15, 8, 20), (10, 15, 11, 20))


def in_season(d: date) -> bool:
    for m1, d1, m2, d2 in SEASONS:
        if date(d.year, m1, d1) <= d <= date(d.year, m2, d2):
            return True
    return False


def _t(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def is_due(sched: Optional[Dict[str, Any]], now: datetime, last_started: Optional[datetime]) -> bool:
    if not sched:
        return False
    if now.isoweekday() not in sched.get("days", WEEKDAYS):
        return False
    if sched.get("season") and not in_season(now.date()):
        return False
    mins = now.hour * 60 + now.minute
    if "at" in sched:
        due_at = datetime.combine(now.date(), datetime.min.time()) + timedelta(minutes=_t(sched["at"]))
        return now >= due_at and (last_started is None or last_started < due_at)
    lo, hi = sched["window"]
    if not (_t(lo) <= mins <= _t(hi)):
        return False
    return last_started is None or (now - last_started) >= timedelta(minutes=sched["every_min"])


def next_run(sched: Optional[Dict[str, Any]], now: datetime, last_started: Optional[datetime]) -> Optional[str]:
    """Human-friendly next run time (looks ahead up to 8 days, minute steps are coarse on purpose)."""
    if not sched:
        return None
    t = now
    for _ in range(8 * 24 * 4):
        t += timedelta(minutes=15)
        if is_due(sched, t, last_started):
            return t.strftime("%Y-%m-%d %H:%M")
    return None
