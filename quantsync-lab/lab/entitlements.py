"""What each plan gets, in one place. Pricing is off for now (everyone is on `free`); turning it on later means setting
users.plan = 'pro' for paying people, and the checks below start to differ. No payment code lives here.

Heavy work (past tests, strategy tracking) runs on the liquid universe for everyone today (see universe.py). The `universe`
entry records what a paid plan will get: all stocks."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

PLANS = {
    "free": {"label": "Free", "universe": "free", "universe_size": 500, "scans_per_hour": 60, "strategies": 25, "watchlist": 20,
             "backtests_per_hour": 10, "ai_jobs_per_day": 5, "refresh": "daily"},
    "pro": {"label": "Pro", "universe": "all", "universe_size": None, "scans_per_hour": 600, "strategies": 100, "watchlist": 200,
            "backtests_per_hour": 60, "ai_jobs_per_day": 50, "refresh": "intraday"},
}


def plan_of(con, uid: str | None) -> str:
    if not uid:
        return "free"
    r = con.execute("SELECT plan FROM users WHERE uid=?", (uid,)).fetchone()
    return r[0] if r and r[0] in PLANS else "free"


def limits(plan: str) -> dict:
    return PLANS.get(plan, PLANS["free"])


_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def client_key(request: Request, uid: str | None) -> str:
    if uid:
        return "u:" + uid
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
    return "ip:" + ip


def hit(key: str, per_hour: int, what: str = "requests") -> None:
    """Sliding one-hour window. Raises 429 with a plain message when the person has used their share."""
    now = time.time()
    with _lock:
        q = _hits[key]
        while q and q[0] < now - 3600:
            q.popleft()
        if len(q) >= per_hour:
            wait = int((q[0] + 3600 - now) / 60) + 1
            raise HTTPException(429, f"You have used your {per_hour} custom {what} for this hour. Try again in about {wait} min. Ready-made screens are not limited.")
        q.append(now)
        if len(_hits) > 5000:  # keep memory flat
            for k in [k for k, v in _hits.items() if not v or v[-1] < now - 3600][:2500]:
                _hits.pop(k, None)
