"""
collectors/core/fetcher.py — fetch with a fallback chain and per-route health.

A FetchSpec lists attempts in preferred order, e.g.
    direct:nse -> chromium:nse -> direct:bse -> relay
The first attempt that returns a usable body wins. Every try is recorded in
raw_fetch (hash, status, route) and in source_health. A route that fails 3 times
in a row is paused for an hour (admin can resume/force it from the ops page).

Block detection: 401/403/429/5xx, an HTML page where JSON was expected, an empty
body, or "Access Denied" text all count as failures — never as data.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import db

PAUSE_AFTER_FAILS = 3
PAUSE_SECONDS = 3600
MIN_INTERVAL = {"nse": 1.2, "bse": 0.8, "screener": 1.8, "generic": 0.5}
RELAY_MAX_AGE_HOURS = 36

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "nse": {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/"},
    "bse": {"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Origin": "https://www.bseindia.com",
            "Referer": "https://www.bseindia.com/"},
    "screener": {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
    "generic": {"User-Agent": UA},
}
WARM_URL = {"nse": "https://www.nseindia.com/", "bse": "https://www.bseindia.com/"}


@dataclass
class Attempt:
    route: str            # direct | chromium | relay
    site: str             # nse | bse | screener | generic
    url: str = ""

    @property
    def name(self) -> str:
        return self.route if self.route == "relay" else f"{self.route}:{self.site}"


@dataclass
class FetchSpec:
    job: str
    source: str           # logical dataset source, e.g. "nse_corp_actions"
    key: str              # symbol or date key
    attempts: List[Attempt]
    expect: str = "json"  # json | text | csv
    accept: Optional[Callable[[Any], Optional[str]]] = None   # parsed-body sanity check -> reason or None


@dataclass
class FetchResult:
    ok: bool
    route: str = ""
    status: int = 0
    body: bytes = b""
    data: Any = None
    error: str = ""
    tried: List[str] = field(default_factory=list)


def looks_blocked(status: int, ctype: str, body: bytes, expect: str) -> Optional[str]:
    if status in (401, 403, 429) or status >= 500:
        return f"http {status}"
    if status != 200:
        return f"http {status}"
    if not body or len(body) < 2:
        return "empty body"
    head = body[:400].lstrip().lower()
    if b"access denied" in head or b"request unsuccessful" in head or b"akamai" in head[:200]:
        return "blocked page"
    if expect == "json" and (head.startswith(b"<!doctype") or head.startswith(b"<html")):
        return "html instead of json"
    return None


class _RateLimiter:
    def __init__(self):
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, site: str):
        gap = MIN_INTERVAL.get(site, 0.5)
        with self._lock:
            delay = self._last.get(site, 0) + gap + random.uniform(0, 0.4) - time.time()
            if delay > 0:
                time.sleep(delay)
            self._last[site] = time.time()


_limiter = _RateLimiter()


# ── real transports ─────────────────────────────────────────────────────────
_sessions: Dict[str, Any] = {}


def _direct(a: Attempt, spec: FetchSpec) -> Tuple[int, str, bytes]:
    import requests
    s = _sessions.get(a.site)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS.get(a.site, HEADERS["generic"]))
        _sessions[a.site] = s
        if a.site in WARM_URL:
            try:
                s.get(WARM_URL[a.site], timeout=15)
            except Exception:
                pass
    r = s.get(a.url, timeout=25)
    if r.status_code in (401, 403) and a.site in WARM_URL:        # cookie expired: re-warm once
        s.get(WARM_URL[a.site], timeout=15)
        r = s.get(a.url, timeout=25)
    return r.status_code, r.headers.get("content-type", ""), r.content


class ChromiumPool:
    """One shared headless browser. Opens the site like a visitor (passes the bot check),
    then calls the data URL through the same cookie jar. Recycled every N requests to cap RAM."""
    RECYCLE_AFTER = 200

    def __init__(self):
        self._pw = self._browser = None
        self._ctx: Dict[str, Any] = {}
        self._count = 0
        self._lock = threading.RLock()

    def _start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-http2"])
        self._ctx, self._count = {}, 0

    def close(self):
        with self._lock:
            self._close_nolock()

    def _close_nolock(self):
        for c in self._ctx.values():
            try:
                c.close()
            except Exception:
                pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = None
        self._ctx = {}

    def get(self, a: Attempt) -> Tuple[int, str, bytes]:
        with self._lock:
            if self._browser is None or self._count >= self.RECYCLE_AFTER:
                self._close_nolock()
                self._start()
            ctx = self._ctx.get(a.site)
            if ctx is None:
                ctx = self._browser.new_context(user_agent=UA, locale="en-IN")
                page = ctx.new_page()
                try:
                    page.goto(WARM_URL.get(a.site, a.url), wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(1500)
                finally:
                    page.close()
                self._ctx[a.site] = ctx
            self._count += 1
            resp = ctx.request.get(a.url, headers=HEADERS.get(a.site, {}), timeout=30000)
            return resp.status, resp.headers.get("content-type", ""), resp.body()


_pool = ChromiumPool()


def _chromium(a: Attempt, spec: FetchSpec) -> Tuple[int, str, bytes]:
    return _pool.get(a)


def _relay(a: Attempt, spec: FetchSpec) -> Tuple[int, str, bytes]:
    """Use the newest body a home-network relay pushed for this (source, key)."""
    with db.session() as c:
        row = c.execute("SELECT id, status, body, ts FROM relay_inbox WHERE source=? AND key=? ORDER BY id DESC LIMIT 1",
                        (spec.source, spec.key)).fetchone()
        if not row:
            return 404, "", b""
        age_h = (time.time() - time.mktime(time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%S"))) / 3600
        if age_h > RELAY_MAX_AGE_HOURS:
            return 404, "", b""
        c.execute("UPDATE relay_inbox SET processed=1 WHERE id=?", (row["id"],))
        return int(row["status"] or 200), "application/json", bytes(row["body"])


DEFAULT_TRANSPORTS = {"direct": _direct, "chromium": _chromium, "relay": _relay}


# ── health ──────────────────────────────────────────────────────────────────
def _health_row(conn, route: str):
    conn.execute("INSERT OR IGNORE INTO source_health(route) VALUES (?)", (route,))
    return conn.execute("SELECT * FROM source_health WHERE route=?", (route,)).fetchone()


def route_usable(conn, route: str) -> bool:
    h = _health_row(conn, route)
    if h["forced_off"]:
        return False
    return (h["paused_until"] or 0) <= time.time()


def record(conn, route: str, ok: bool, error: str = "") -> None:
    _health_row(conn, route)
    day = time.strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO source_daily(day,route) VALUES (?,?)", (day, route))
    if ok:
        conn.execute("UPDATE source_health SET ok_count=ok_count+1, consecutive_fail=0, last_ok=?, paused_until=0 WHERE route=?",
                     (db.now_iso(), route))
        conn.execute("UPDATE source_daily SET ok=ok+1 WHERE day=? AND route=?", (day, route))
    else:
        is_route_fail = not (error and "http 404" in error)
        conn.execute("UPDATE source_health SET fail_count=fail_count+1, "
                     "consecutive_fail=consecutive_fail + ?, last_fail=?, last_error=? WHERE route=?",
                     (1 if is_route_fail else 0, db.now_iso(), error[:300], route))
        conn.execute("UPDATE source_daily SET fail=fail+1 WHERE day=? AND route=?", (day, route))
        h = conn.execute("SELECT consecutive_fail FROM source_health WHERE route=?", (route,)).fetchone()
        if h["consecutive_fail"] >= PAUSE_AFTER_FAILS:
            conn.execute("UPDATE source_health SET paused_until=? WHERE route=?", (time.time() + PAUSE_SECONDS, route))


class Fetcher:
    def __init__(self, transports: Optional[Dict[str, Callable]] = None, sleep: bool = True, keep_body: bool = True):
        self.transports = dict(DEFAULT_TRANSPORTS)
        if transports:
            self.transports.update(transports)
        self.sleep = sleep
        self.keep_body = keep_body

    def fetch(self, spec: FetchSpec) -> FetchResult:
        res = FetchResult(ok=False)
        with db.session() as conn:
            for a in spec.attempts:
                if not route_usable(conn, a.name):
                    res.tried.append(f"{a.name}:paused")
                    continue
                fn = self.transports.get(a.route)
                if fn is None:
                    continue
                conn.commit()                       # release the write lock before a transport opens its own connection
                if self.sleep and a.route != "relay":
                    _limiter.wait(a.site)
                status, ctype, body, err = 0, "", b"", ""
                try:
                    status, ctype, body = fn(a, spec)
                    err = looks_blocked(status, ctype, body, spec.expect) or ""
                except Exception as e:                                    # network/browser failure
                    err = f"{type(e).__name__}: {e}"[:300]
                data = None
                if not err:
                    try:
                        data = json.loads(body) if spec.expect == "json" else body.decode("utf-8", "replace")
                        if spec.accept:
                            err = spec.accept(data) or ""
                    except ValueError as e:
                        err = f"bad json: {e}"[:200]
                h = hashlib.sha256(body or b"").hexdigest()
                conn.execute("INSERT INTO raw_fetch(job,source,route,url,key,status,ok,content_hash,bytes,body,fetched_at,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (spec.job, spec.source, a.name, a.url, spec.key, status, 0 if err else 1, h, len(body or b""),
                              (body if (self.keep_body and not err and len(body) < 400_000) else None), db.now_iso(), err or None))
                record(conn, a.name, not err, err)
                conn.commit()
                res.tried.append(a.name + (f":fail({err})" if err else ":ok"))
                if not err:
                    res.ok, res.route, res.status, res.body, res.data = True, a.name, status, body, data
                    return res
                res.error = err
        return res


def unchanged_since_last(source: str, key: str, content_hash: str) -> bool:
    """True if the newest OK fetch for this (source, key) has the same hash — skip reprocessing."""
    with db.session() as c:
        r = c.execute("SELECT content_hash FROM raw_fetch WHERE source=? AND key=? AND ok=1 ORDER BY id DESC LIMIT 1 OFFSET 1",
                      (source, key)).fetchone()
    return bool(r and r["content_hash"] == content_hash)
