"""
ops_system.py — /api/admin/sys/*: the owner's control room (the /rahul page).

Machine health with history, systemd services (status / restart), every scheduled job (cron + the trading scheduler:
when it runs next, when it last ran, did it work), how fresh each dataset is, log tails, and run-now for safe jobs.

Security: every route needs a verified Firebase ID token whose verified email is on the allow-list
(collectors/admin/auth.py). Anyone else, signed in or not, gets a plain 404 so the page does not look like it exists.
Nothing here takes a command from the caller: services, jobs and log files come from fixed allow-lists below.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import ops_track
from collectors.admin import auth as _auth

IST = ZoneInfo("Asia/Kolkata")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("QS_DATA_DIR", os.path.join(ROOT, "Data"))
LOGS = os.environ.get("QS_LOG_DIR", os.path.join(ROOT, "logs"))
SCHED_LOGS = os.path.join(DATA, "logs")
LAB_DIR = os.environ.get("QSLAB_DIR", "/opt/quantsync-lab")
LAB_CFG = os.environ.get("QSLAB_CONFIG", os.path.join(LAB_DIR, "config.yaml"))
METRICS_DB = os.environ.get("QS_OPS_METRICS_DB", os.path.join(DATA, "ops_metrics.db"))
CERT = os.environ.get("QS_CERT", "/etc/letsencrypt/live/quantsync-api.duckdns.org/fullchain.pem")
OLLAMA = os.environ.get("QS_OLLAMA", "http://127.0.0.1:11434")

router = APIRouter(prefix="/api/admin/sys", tags=["ops-system"])


# ───────────────────────────── auth: owner only, everyone else sees 404 ─────────────────────────────
def owner(request: Request) -> Dict[str, Any]:
    ip = request.client.host if request.client else "?"
    try:
        _auth._throttle(ip)
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer "):
            raise _auth.AuthError("no token")
        claims = _auth.verify(h[7:].strip())
    except Exception:  # noqa: BLE001 - every failure looks the same from outside
        _auth._fails[ip].append(time.time())
        raise HTTPException(404, "Not found")
    claims["_ip"] = ip
    return claims


def _audit(adm: Dict[str, Any], action: str, detail: Any) -> None:
    try:
        from collectors.core import db as cdb
        with cdb.session() as c:
            cdb.audit(c, adm.get("email", "?"), action, detail, adm.get("_ip", ""))
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────────── small helpers ─────────────────────────────
def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _cal():
    try:
        from common import market_calendar as mc
        return mc
    except Exception:  # noqa: BLE001
        return None


def market_open(now: Optional[dt.datetime] = None) -> bool:
    mc = _cal()
    if mc:
        return bool(mc.is_market_open(now))
    n = now or now_ist()
    return n.weekday() < 5 and dt.time(9, 15) <= n.time() < dt.time(15, 30)


def trading_day(d: Optional[dt.date] = None) -> bool:
    mc = _cal()
    d = d or now_ist().date()
    return bool(mc.is_trading_day(d)) if mc else d.weekday() < 5


def expected_session(now: Optional[dt.datetime] = None) -> dt.date:
    """The trading day whose end-of-day data should exist by now (today only after 16:00, else the one before)."""
    now = now or now_ist()
    d = now.date()
    if trading_day(d) and now.time() >= dt.time(16, 0):
        return d
    mc = _cal()
    if mc:
        return mc.previous_trading_day(d)
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def to_epoch(v: Any) -> Optional[float]:
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s[:26] if "%f" in fmt else s[:19 if len(s) > 10 else 10], fmt).replace(tzinfo=IST).timestamp()
        except ValueError:
            continue
    return None


def mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _db_one(path: str, sql: str, args: tuple = ()) -> Optional[tuple]:
    con = sqlite3.connect(path, timeout=3)
    try:
        con.execute("PRAGMA query_only=1")
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def _db_all(path: str, sql: str, args: tuple = ()) -> List[tuple]:
    con = sqlite3.connect(path, timeout=3)
    try:
        con.execute("PRAGMA query_only=1")
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _yaml_value(key: str, default: str) -> str:
    try:
        for line in open(LAB_CFG, encoding="utf-8"):
            m = re.match(rf"^{re.escape(key)}\s*:\s*(.+?)\s*(#.*)?$", line)
            if m:
                return m.group(1).strip().strip("'\"")
    except OSError:
        pass
    return default


def paths() -> Dict[str, str]:
    return {
        "master": os.environ.get("QS_MASTER_DB", os.path.join(DATA, "quantsync_master.db")),
        "dna": os.environ.get("QS_DNA_DB", os.path.join(DATA, "stock_dna_cache.db")),
        "history": os.environ.get("QS_HIST_DB", os.path.join(DATA, "stock_history.db")),
        "intel": os.environ.get("QS_INTEL_DB", os.path.join(DATA, "intel.db")),
        "collectors": os.environ.get("QS_COLLECTOR_DB", os.path.join(DATA, "collectors.db")),
        "fundamentals": os.environ.get("QS_FUND_DB", os.path.join(DATA, "fundamental_cache.db")),
        "lab": _yaml_value("db_path", os.path.join(LAB_DIR, "db", "lab.db")),
        "labdata": _yaml_value("data_dir", os.path.join(LAB_DIR, "data")),
    }


_SECRET = re.compile(r"(bot\d{6,}:[\w-]{20,}|(?i:bearer)\s+[\w.\-]{20,}|(?i:(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)(?:(?i:bearer)\s+)?\S{6,})")


def redact(text: str) -> str:
    return _SECRET.sub("«hidden»", text)


# ───────────────────────────── machine metrics (kept for 8 days, sampled every 30 s) ─────────────────────────────
_SCHEMA_M = """CREATE TABLE IF NOT EXISTS m(ts INTEGER PRIMARY KEY, cpu REAL, mem REAL, mem_mb REAL, swap_mb REAL, load1 REAL, disk REAL);"""
_sampler = {"started": False, "last": None, "prev": None}
RANGES = {"1h": (3600, 30), "6h": (21600, 180), "24h": (86400, 600), "7d": (604800, 3600)}


def _cpu_counts() -> Optional[Tuple[float, float]]:
    try:
        f = open("/proc/stat").readline().split()[1:]
        v = [float(x) for x in f]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return idle, sum(v)
    except (OSError, ValueError, IndexError):
        return None


def _meminfo() -> Dict[str, float]:
    out: Dict[str, float] = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, rest = line.partition(":")
            out[k] = float(rest.split()[0]) / 1024.0      # MB
    except (OSError, ValueError):
        pass
    return out


def _snapshot(prev: Optional[Tuple[float, float]]) -> Dict[str, Any]:
    cur = _cpu_counts()
    cpu = None
    if cur and prev and cur[1] > prev[1]:
        cpu = max(0.0, min(100.0, 100.0 * (1 - (cur[0] - prev[0]) / (cur[1] - prev[1]))))
    mi = _meminfo()
    total, avail = mi.get("MemTotal", 0.0), mi.get("MemAvailable", 0.0)
    used = max(0.0, total - avail)
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    try:
        du = shutil.disk_usage("/")
        disk = 100.0 * du.used / du.total
    except OSError:
        disk = None
    return {"cpu": cpu, "mem": (100.0 * used / total) if total else None, "mem_mb": used, "mem_total_mb": total,
            "mem_free_mb": avail, "swap_mb": max(0.0, mi.get("SwapTotal", 0.0) - mi.get("SwapFree", 0.0)),
            "swap_total_mb": mi.get("SwapTotal", 0.0), "load1": load1, "disk": disk, "_cpu_counts": cur}


def _mcon() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(METRICS_DB), exist_ok=True)
    con = sqlite3.connect(METRICS_DB, timeout=5)
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    con.executescript(_SCHEMA_M)
    return con


def _store_sample(s: Dict[str, Any], ts: Optional[int] = None) -> None:
    ts = int(ts or time.time()) // 30 * 30
    con = _mcon()
    try:
        con.execute("INSERT OR IGNORE INTO m VALUES (?,?,?,?,?,?,?)",
                    (ts, s["cpu"], s["mem"], s["mem_mb"], s["swap_mb"], s["load1"], s["disk"]))
        if ts % 3600 < 30:
            con.execute("DELETE FROM m WHERE ts < ?", (ts - 8 * 86400,))
        con.commit()
    finally:
        con.close()


def _sampler_loop() -> None:
    prev = _cpu_counts()
    while True:
        time.sleep(30)
        try:
            s = _snapshot(prev)
            prev = s.pop("_cpu_counts")
            _sampler["last"] = {**s, "ts": time.time()}
            _store_sample(s)
        except Exception:  # noqa: BLE001 - keep sampling
            pass


def start_sampler() -> None:
    if _sampler["started"] or not sys.platform.startswith("linux"):
        return
    _sampler["started"] = True
    threading.Thread(target=_sampler_loop, name="ops-sampler", daemon=True).start()


def metrics_series(rng: str) -> Dict[str, Any]:
    span, bucket = RANGES.get(rng, RANGES["6h"])
    since = int(time.time()) - span
    con = _mcon()
    try:
        rows = con.execute(
            "SELECT (ts/?)*? AS t, AVG(cpu), MAX(cpu), MAX(mem), MAX(mem_mb), MAX(swap_mb), AVG(load1), MAX(disk) FROM m WHERE ts>=? GROUP BY t ORDER BY t",
            (bucket, bucket, since)).fetchall()
    finally:
        con.close()
    pts = [{"t": r[0], "cpu": r[1], "cpu_max": r[2], "mem": r[3], "mem_mb": r[4], "swap_mb": r[5], "load": r[6], "disk": r[7]} for r in rows]
    peak_mem = max((p["mem_mb"] or 0 for p in pts), default=None)
    peak_cpu = max((p["cpu_max"] or 0 for p in pts), default=None)
    return {"range": rng if rng in RANGES else "6h", "bucket": bucket, "points": pts, "peakMemMb": peak_mem, "peakCpu": peak_cpu}


def machine_now() -> Dict[str, Any]:
    last = _sampler.get("last")
    if last and time.time() - last["ts"] < 90:
        s = dict(last)
    else:
        a = _cpu_counts(); time.sleep(0.25)
        s = _snapshot(a); s.pop("_cpu_counts", None)
    try:
        up = float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        up = None
    try:
        cores = os.cpu_count()
    except Exception:  # noqa: BLE001
        cores = None
    try:
        du = shutil.disk_usage("/")
        s["disk_total_gb"], s["disk_used_gb"] = du.total / 1e9, du.used / 1e9
    except OSError:
        pass
    s.pop("ts", None)
    s.pop("_cpu_counts", None)
    s["uptime_s"], s["cores"] = up, cores
    return s


# ───────────────────────────── services (systemd) ─────────────────────────────
SERVICES: Dict[str, Dict[str, Any]] = {
    "api":         {"unit": "quantsync-api",         "label": "Website API",          "about": "Serves the website, Lab and intel. Restart drops requests for about 3 seconds.", "actions": ["restart"], "expect": True},
    "scheduler":   {"unit": "quantsync-scheduler",   "label": "Trading scheduler",    "about": "Runs the engines: price sync, 15:15 decisions, exits, end-of-day.", "actions": ["restart", "stop", "start"], "expect": True, "critical": True},
    "worker-fast": {"unit": "quantsync-worker-fast", "label": "Lab worker (fast)",    "about": "Backtests and screens queue.", "actions": ["restart", "stop", "start"], "expect": True},
    "worker-ai":   {"unit": "quantsync-worker-ai",   "label": "Lab worker (AI)",      "about": "AI check training jobs. Uses up to 900 MB.", "actions": ["restart", "stop", "start"], "expect": True},
    "collectors":  {"unit": "quantsync_collectors",  "label": "Data collectors",      "about": "Listing, results, shareholding collectors.", "actions": ["restart", "stop", "start"], "expect": False},
    "cron":        {"unit": "cron",                  "label": "Cron",                 "about": "Runs the timed jobs listed under Jobs.", "actions": ["restart"], "expect": True},
    "nginx":       {"unit": "nginx",                 "label": "Web gateway (nginx)",  "about": "HTTPS in front of the API.", "actions": ["restart"], "expect": True},
    "ssh":         {"unit": "ssh",                   "label": "SSH",                  "about": "Your way in. Never restarted from here.", "actions": [], "expect": True},
}


def _run(cmd: List[str], timeout: int = 10) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _rss_mb(pid: int) -> Optional[float]:
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        pass
    return None


def service_status(key: str) -> Dict[str, Any]:
    spec = SERVICES[key]
    code, out = _run(["systemctl", "show", spec["unit"], "--no-pager",
                      "-p", "LoadState,ActiveState,SubState,MainPID,ActiveEnterTimestampMonotonic,MemoryCurrent,NRestarts,Result"])
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    base = {"key": key, "unit": spec["unit"], "label": spec["label"], "about": spec["about"], "actions": spec["actions"],
            "expect": spec["expect"], "critical": bool(spec.get("critical"))}
    if code != 0 or kv.get("LoadState") in (None, "not-found"):
        return {**base, "state": "missing", "installed": False}
    state, sub = kv.get("ActiveState", "unknown"), kv.get("SubState", "")
    pid = int(kv.get("MainPID") or 0)
    mem = None
    mc = kv.get("MemoryCurrent", "")
    if mc.isdigit() and int(mc) < 2 ** 60:
        mem = int(mc) / 1048576.0
    elif pid:
        mem = _rss_mb(pid)
    up = None
    try:
        mono = int(kv.get("ActiveEnterTimestampMonotonic") or 0)
        if mono and state == "active":
            up = float(open("/proc/uptime").read().split()[0]) - mono / 1e6
    except (OSError, ValueError):
        pass
    return {**base, "installed": True, "state": state, "sub": sub, "pid": pid or None, "mem_mb": mem, "uptime_s": up,
            "restarts": int(kv["NRestarts"]) if (kv.get("NRestarts") or "").isdigit() else None, "result": kv.get("Result")}


def all_services() -> List[Dict[str, Any]]:
    out = [service_status(k) for k in SERVICES]
    return [s for s in out if s.get("installed") or s["expect"]]


# ───────────────────────────── cron + scheduler timetable ─────────────────────────────
_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MACROS = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}


def _field(spec: str, lo: int, hi: int, dow: bool = False) -> set:
    vals: set = set()
    for part in spec.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = max(1, int(s))
        if part in ("*", "?", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = (_DOW.get(x, None) if dow and x in _DOW else int(x)), (_DOW.get(y, None) if dow and y in _DOW else int(y))
        else:
            a = _DOW[part] if dow and part in _DOW else int(part)
            b = hi if step > 1 else a
        vals.update(range(a, b + 1, step))
    if dow:
        vals = {0 if v == 7 else v for v in vals}
    return {v for v in vals if lo <= v <= hi}


def cron_parse(expr: str) -> Dict[str, Any]:
    expr = _MACROS.get(expr.strip(), expr.strip())
    f = expr.split()
    if len(f) != 5:
        raise ValueError("cron needs 5 fields")
    return {"min": _field(f[0], 0, 59), "hour": _field(f[1], 0, 23), "dom": _field(f[2], 1, 31), "mon": _field(f[3], 1, 12),
            "dow": _field(f[4], 0, 6, True), "dom_star": f[2] in ("*", "?"), "dow_star": f[4] in ("*", "?")}


def _day_matches(p: Dict[str, Any], d: dt.date) -> bool:
    if d.month not in p["mon"]:
        return False
    dom_ok, dow_ok = d.day in p["dom"], ((d.weekday() + 1) % 7) in p["dow"]
    if p["dom_star"] and p["dow_star"]:
        return True
    if p["dom_star"]:
        return dow_ok
    if p["dow_star"]:
        return dom_ok
    return dom_ok or dow_ok


def cron_next(expr: str, after: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    if expr.strip().startswith("@reboot"):
        return None
    p = cron_parse(expr)
    after = (after or now_ist()).astimezone(IST)
    hours, mins = sorted(p["hour"]), sorted(p["min"])
    day = after.date()
    for off in range(0, 400):
        d = day + dt.timedelta(days=off)
        if not _day_matches(p, d):
            continue
        for h in hours:
            for m in mins:
                cand = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
                if cand > after:
                    return cand
    return None


def _hh(vals: set) -> str:
    return ", ".join(f"{v:02d}" for v in sorted(vals))


def humanize(expr: str) -> str:
    e = expr.strip()
    if e == "@reboot":
        return "at every reboot"
    try:
        f = _MACROS.get(e, e).split()
        mi, ho, dom, mon, dow = f
        p = cron_parse(e)
    except Exception:  # noqa: BLE001
        return e
    days = "" if p["dow_star"] and p["dom_star"] else ""
    if not p["dow_star"]:
        s = sorted(p["dow"])
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        days = "Mon–Fri" if s == [1, 2, 3, 4, 5] else (names[s[0]] if len(s) == 1 else ",".join(names[i] for i in s))
    elif not p["dom_star"]:
        s = sorted(p["dom"])
        days = "day " + ",".join(str(i) for i in s) + " of the month"
    if mi.startswith("*/") and ho == "*":
        when = f"every {mi[2:]} min"
    elif mi.startswith("*/"):
        when = f"every {mi[2:]} min, {ho.replace('-', '–')}h"
    elif ho.startswith("*/"):
        when = f"every {ho[2:]} h at :{int(mi):02d}" if mi.isdigit() else f"every {ho[2:]} h"
    elif ho == "*" and mi.isdigit():
        when = f"hourly at :{int(mi):02d}"
    elif "-" in ho and mi.isdigit():
        when = f"hourly at :{int(mi):02d}, {ho.replace('-', '–')}h"
    elif mi.isdigit() and all(x.isdigit() for x in ho.split(",")):
        when = " & ".join(f"{int(h):02d}:{int(mi):02d}" for h in ho.split(","))
    else:
        when = f"{mi} {ho}"
    return f"{when}{' · ' + days if days else ''}"


# What each cron line is, in words (first matching pattern wins). run = key in RUNNABLE.
_LABELS: List[Tuple[str, str, str, Optional[str]]] = [
    (r"duck\.sh", "DuckDNS address update", "Infra", None),
    (r"sync_trendlyne", "Fundamentals sync (Trendlyne)", "Data", "trendlyne"),
    (r"build_screener_snapshot", "Screener snapshot", "Xray", "screener"),
    (r"eod_pipeline_orchestrator", "End-of-day pipeline", "Trading", "sched-eod"),
    (r"eod_freshness_watchdog", "Data freshness watchdog", "Data", "watchdog"),
    (r"resolve_outcomes", "Signal outcomes", "Xray", "resolve"),
    (r"build_historical_breakout_ledger", "Weekly breakout ledger + ML gates", "AI", None),
    (r"corporate_actions", "Corporate actions (splits, bonus)", "Data", "corp-actions"),
    (r"intel\.run (\w+)", "News · {1}", "News", "intel-{1}"),
    (r"lab\.cli ([\w-]+)(?: (--[\w-]+))?", "Lab · {1} {2}", "Lab", "lab-{1}{2}"),
    (r"start_quantsync", "Start everything at reboot", "Infra", None),
    (r"home_automation", "Home automation", "Other", None),
    (r"news_aggregator", "News aggregator", "News", None),
]
_cron_cache: Dict[str, Any] = {"t": 0.0, "v": []}


def _classify(cmd: str) -> Tuple[str, str, Optional[str]]:
    for pat, label, group, run in _LABELS:
        m = re.search(pat, cmd)
        if m:
            g = [x or "" for x in m.groups()]
            fmt = lambda s: s.replace("{1}", g[0] if len(g) > 0 else "").replace("{2}", g[1] if len(g) > 1 else "").strip()  # noqa: E731
            r = fmt(run) if run else None
            return fmt(label), group, (r.replace("--", "") if r else None)
    m = re.search(r"([\w./-]+\.(?:py|sh))", cmd)
    return (m.group(1).split("/")[-1] if m else cmd[:40]), "Other", None


def _logfile_of(cmd: str) -> Optional[str]:
    m = re.search(r">>?\s*(\S+\.log)", cmd)
    if not m:
        return None
    p = m.group(1)
    cwd = re.search(r"cd\s+(\S+)", cmd)
    if not p.startswith("/"):
        p = os.path.join(cwd.group(1) if cwd else ROOT, p)
    return p


def cron_jobs() -> List[Dict[str, Any]]:
    if time.time() - _cron_cache["t"] < 45:
        return _cron_cache["v"]
    code, out = _run(["crontab", "-l"])
    jobs: List[Dict[str, Any]] = []
    if code == 0:
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            paused = line.startswith("#")
            body = line.lstrip("#").strip() if paused else line
            m = re.match(r"^(@\w+|(?:[\d*/,\-a-zA-Z?]+\s+){5})\s*(.*)$", body)
            if not m or re.match(r"^\w+=", body):
                continue
            expr, cmd = m.group(1).strip(), m.group(2).strip()
            if paused and not re.search(r"\.(py|sh)\b|python|flock|cd ", cmd):
                continue
            try:
                nxt = None if paused else cron_next(expr)
                cron_parse(expr) if not expr.startswith("@reboot") else None
            except Exception:  # noqa: BLE001
                continue
            label, group, run = _classify(cmd)
            jobs.append({"kind": "cron", "id": f"cron:{len(jobs)}", "label": label, "group": group, "run": run,
                         "schedule": humanize(expr), "expr": expr, "paused": paused,
                         "next": nxt.timestamp() if nxt else None, "log": _logfile_of(cmd)})
    _cron_cache.update(t=time.time(), v=jobs)
    return jobs


# The trading scheduler's own timetable. Keep in step with scheduler.setup_schedules (a test checks every name exists).
def _ltp_slots() -> List[str]:
    return [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5) if not (h == 9 and m < 15) and not (h == 15 and m > 25)]


SCHED_JOBS: List[Dict[str, Any]] = [
    {"name": "job_dna_batch_warmcache", "label": "DNA warm-cache (xray)", "times": ["03:00"], "group": "Xray"},
    {"name": "job_resolve_screener_outcomes", "label": "Screener outcomes", "times": ["04:00"], "group": "Xray"},
    {"name": "job_freshness_watchdog", "label": "Data freshness check", "times": ["09:00", "16:35"], "group": "Data", "run": "watchdog"},
    {"name": "job_ltp_sync", "label": "Price sync + stop/target checks", "times": _ltp_slots(), "group": "Trading", "critical": True, "every": "every 5 min 09:15–15:25"},
    {"name": "job_market_sweep", "label": "Pre-close quote sweep", "times": ["14:30", "15:00", "15:10"], "group": "Trading", "run": "sched-sweep"},
    {"name": "job_decision_1515", "label": "Decision cycle (exits, scans, orders)", "times": ["15:15"], "group": "Trading", "critical": True},
    {"name": "job_1535_candle_sync", "label": "Official end-of-day candles", "times": ["15:35"], "group": "Data", "run": "sched-sync"},
    {"name": "job_finalize_1545", "label": "NAV settlement + Telegram recap", "times": ["15:45"], "group": "Trading", "critical": True},
    {"name": "job_master_eod_pipeline", "label": "End-of-day pipeline", "times": ["15:55"], "group": "Trading", "run": "sched-eod"},
    {"name": "job_fetch_nse_delivery_data", "label": "NSE delivery % file", "times": ["18:30"], "group": "Data", "run": "sched-delivery"},
    {"name": "job_history_sync", "label": "Weekly candle history audit", "times": ["10:00"], "weekday": 5, "group": "Data"},
]


def _sched_next(job: Dict[str, Any], after: dt.datetime) -> Optional[dt.datetime]:
    for off in range(0, 9):
        d = after.date() + dt.timedelta(days=off)
        if job.get("weekday") is not None and d.weekday() != job["weekday"]:
            continue
        if job.get("weekday") is None and not trading_day(d) and job["group"] == "Trading":
            continue
        for t in sorted(job["times"]):
            h, m = map(int, t.split(":"))
            c = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
            if c > after:
                return c
    return None


def _runs_for(job: str, source: str = "scheduler") -> Dict[str, Any]:
    try:
        con = ops_track.connect()
        try:
            last = con.execute("SELECT * FROM runs WHERE job=? AND source=? ORDER BY id DESC LIMIT 1", (job, source)).fetchone()
            today0 = now_ist().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            n = con.execute("SELECT COUNT(*), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) FROM runs WHERE job=? AND source=? AND started>=?", (job, source, today0)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return {}
    if not last:
        return {"runsToday": 0}
    return {"lastStart": last["started"], "lastEnd": last["finished"], "ok": None if last["finished"] is None else bool(last["ok"]),
            "note": last["note"], "runsToday": n[0] or 0, "failedToday": n[1] or 0,
            "durationS": (last["finished"] - last["started"]) if last["finished"] else None}


def scheduler_jobs() -> List[Dict[str, Any]]:
    now = now_ist()
    out = []
    for j in SCHED_JOBS:
        nxt = _sched_next(j, now)
        when = j.get("every") or (" & ".join(j["times"]) + (" · Saturdays" if j.get("weekday") == 5 else " · trading days"))
        out.append({"kind": "scheduler", "id": j["name"], "label": j["label"], "group": j["group"], "run": j.get("run"),
                    "schedule": when, "critical": bool(j.get("critical")), "next": nxt.timestamp() if nxt else None, **_runs_for(j["name"])})
    return out


def _file_run(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    mt = mtime(path)
    return {"lastOutput": mt} if mt else {}


def _intel_runs() -> Dict[str, Dict[str, Any]]:
    try:
        rows = _db_all(paths()["intel"], "SELECT job, started, finished, ok, added, note FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY job)")
    except Exception:  # noqa: BLE001
        return {}
    return {r[0]: {"lastStart": r[1], "lastEnd": r[2], "ok": bool(r[3]) if r[3] is not None else None, "note": f"added {r[4]}" + (f" · {r[5]}" if r[5] else "")} for r in rows}


def timetable() -> Dict[str, Any]:
    intel = _intel_runs()
    jobs = scheduler_jobs()
    for c in cron_jobs():
        extra: Dict[str, Any] = _file_run(c["log"])
        m = re.match(r"News · (\w+)", c["label"])
        if m and m.group(1) in intel:
            extra.update(intel[m.group(1)])
        elif c.get("run") and c["run"] in RUNNABLE:
            extra.update({k: v for k, v in _runs_for(c["run"], "manual").items() if k in ("lastStart", "lastEnd", "ok", "note")} if not extra.get("lastStart") else {})
        jobs.append({**c, **extra})
    return {"jobs": jobs, "now": time.time(), "marketOpen": market_open(), "tradingDay": trading_day()}


# ───────────────────────────── run-now (fixed allow-list; trading jobs are deliberately absent) ─────────────────────────────
def _py() -> str:
    p = os.path.join(ROOT, "venv", "bin", "python3")
    return p if os.path.exists(p) else sys.executable


def _labpy() -> str:
    p = os.path.join(LAB_DIR, "venv", "bin", "python")
    return p if os.path.exists(p) else sys.executable


def _app(*args: str) -> Dict[str, Any]:
    return {"cwd": ROOT, "cmd": [_py(), *args], "env": {}}


def _intel(job: str) -> Dict[str, Any]:
    return _app("-m", "intel.run", job)


def _lab(*args: str) -> Dict[str, Any]:
    return {"cwd": LAB_DIR, "cmd": [_labpy(), "-m", "lab.cli", *args], "env": {"QSLAB_CONFIG": LAB_CFG}}


RUNNABLE: Dict[str, Dict[str, Any]] = {
    "watchdog": {"label": "Data freshness check + heal", **_app("eod_freshness_watchdog.py", "--heal")},
    "sched-eod": {"label": "End-of-day pipeline", "cwd": ROOT, "cmd": ["flock", "-n", "/tmp/quantsync_eod_pipeline.lock", _py(), "pipelines/eod_pipeline_orchestrator.py"], "env": {}, "heavy": True},
    "sched-sweep": {"label": "Pre-close quote sweep", **_app("scheduler.py", "--now", "sweep")},
    "sched-sync": {"label": "Official end-of-day candles", **_app("scheduler.py", "--now", "sync"), "heavy": True},
    "sched-delivery": {"label": "NSE delivery % file", **_app("scheduler.py", "--now", "delivery")},
    "sched-dna": {"label": "DNA (xray) refresh", **_app("scheduler.py", "--now", "dna_refresh"), "heavy": True},
    "screener": {"label": "Screener snapshot", **_app("build_screener_snapshot.py"), "heavy": True},
    "resolve": {"label": "Signal outcomes", **_app("resolve_outcomes.py")},
    "corp-actions": {"label": "Corporate actions", **_app("common/corporate_actions.py", "--days-back", "30")},
    "trendlyne": {"label": "Fundamentals sync (Trendlyne)", "cwd": ROOT, "cmd": ["/bin/bash", os.path.join(ROOT, "scripts", "sync_trendlyne.sh")], "env": {}},
    "lab-cook": {"label": "Lab daily cook", **_lab("cook"), "heavy": True},
    "lab-board": {"label": "Lab screens board", **_lab("board")},
    "lab-universe": {"label": "Lab universe re-rank", **_lab("universe")},
    "lab-library": {"label": "Lab strategy library refresh", **_lab("library"), "heavy": True},
    "lab-ai-check-stale": {"label": "Lab AI check (stale ones)", **_lab("ai-check", "--stale"), "heavy": True},
    "lab-ai-check-rescore": {"label": "Lab AI check (rescore)", **_lab("ai-check", "--rescore"), "heavy": True},
}
for _j in ("filings", "meetings", "insider", "flows", "world", "markets", "home", "fields", "morning", "evening"):
    RUNNABLE[f"intel-{_j}"] = {"label": f"News · {_j}", **_intel(_j)}

_running: Dict[str, Any] = {}
_run_lock = threading.Lock()


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_manual() -> List[Dict[str, Any]]:
    try:
        con = ops_track.connect()
        try:
            rows = con.execute("SELECT * FROM runs WHERE source='manual' AND finished IS NULL AND started > ?", (time.time() - 6 * 3600,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []
    return [{"job": r["job"], "pid": r["pid"], "started": r["started"]} for r in rows if _alive(r["pid"])]


def start_run(key: str, who: str, force: bool = False) -> Dict[str, Any]:
    spec = RUNNABLE.get(key)
    if not spec:
        return {"ok": False, "error": "That job cannot be run from here."}
    with _run_lock:
        if any(r["job"] == key for r in running_manual()):
            return {"ok": False, "error": "Already running."}
        mi = _meminfo()
        if spec.get("heavy") and mi.get("MemAvailable", 9e9) < 900 and not force:
            return {"ok": False, "error": f"Only {mi.get('MemAvailable', 0):.0f} MB RAM free. Heavy jobs need 900 MB. Wait or force."}
        if spec.get("heavy") and market_open() and not force:
            return {"ok": False, "error": "Market is open. Heavy jobs wait until after 15:30 (or force)."}
        os.makedirs(LOGS, exist_ok=True)
        log = os.path.join(LOGS, f"ops_manual_{key}.log")
        env = {**os.environ, **spec.get("env", {})}
        try:
            fh = open(log, "a", encoding="utf-8")
            fh.write(f"\n===== {now_ist():%Y-%m-%d %H:%M:%S} IST · started by {who} · {' '.join(spec['cmd'])}\n")
            fh.flush()
            proc = subprocess.Popen(spec["cmd"], cwd=spec["cwd"], env=env, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return {"ok": False, "error": f"Could not start: {e}"}
        rid = ops_track.start(key, "manual", pid=proc.pid, who=who)

        def _wait() -> None:
            rc = proc.wait()
            try:
                fh.write(f"===== finished with exit code {rc}\n"); fh.close()
            except Exception:  # noqa: BLE001
                pass
            ops_track.finish(rid, rc == 0, f"exit code {rc}")
        threading.Thread(target=_wait, daemon=True).start()
    return {"ok": True, "pid": proc.pid, "log": f"ops_manual_{key}.log"}


def runs_history(limit: int = 40) -> List[Dict[str, Any]]:
    try:
        con = ops_track.connect()
        try:
            rows = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return []
    return [{"job": r["job"], "source": r["source"], "start": r["started"], "end": r["finished"], "ok": None if r["finished"] is None else bool(r["ok"]),
             "note": r["note"], "who": r["who"]} for r in rows]


# ───────────────────────────── data freshness ─────────────────────────────
def _item(key: str, label: str, group: str, *, at: Any = None, on: Optional[str] = None, count: Optional[int] = None,
          detail: str = "", status: str = "ok", expect: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "group": group, "at": to_epoch(at), "on": on, "count": count, "detail": detail, "status": status, "expect": expect}


def _by_date(last: Optional[str], expected: dt.date) -> str:
    if not last:
        return "unknown"
    return "ok" if str(last)[:10] >= expected.isoformat() else "stale"


def _by_age(at: Optional[float], hours: float) -> str:
    if at is None:
        return "unknown"
    return "ok" if time.time() - at <= hours * 3600 else "stale"


def _chk_candles() -> Dict[str, Any]:
    P = paths(); exp = expected_session()
    row = _db_one(P["history"], "SELECT rawCandles FROM StockHistory WHERE symbol='RELIANCE'")
    last = None
    if row and row[0]:
        cs = json.loads(row[0]); last = str(cs[-1].get("t", ""))[:10] if cs else None
    n = (_db_one(P["history"], "SELECT COUNT(*) FROM StockHistory") or [0])[0]
    return _item("candles", "Daily candles (all stocks)", "Market data", at=mtime(P["history"]), on=last, count=n,
                 detail=f"{n} stocks · newest candle {last}", status=_by_date(last, exp), expect=f"candle for {exp}")


def _chk_ltp() -> Dict[str, Any]:
    mt = mtime(os.path.join(DATA, "current_ltp.json"))
    st = _by_age(mt, 0.2) if market_open() else ("ok" if mt else "unknown")
    return _item("ltp", "Live prices cache", "Market data", at=mt, detail="refreshed every 5 min in market hours", status=st, expect="under 10 min old while market is open")


def _chk_delivery() -> Dict[str, Any]:
    P = paths(); last = None
    for k in ("history", "master", "dna"):
        try:
            r = _db_one(P[k], "SELECT MAX(trade_date), COUNT(DISTINCT symbol) FROM nse_delivery_history")
            if r and r[0]:
                last = r; break
        except Exception:  # noqa: BLE001
            continue
    if not last:
        return _item("delivery", "NSE delivery %", "Market data", status="unknown", detail="table not found")
    return _item("delivery", "NSE delivery %", "Market data", on=last[0], count=last[1], detail=f"{last[1]} stocks", status=_by_date(last[0], expected_session() - dt.timedelta(days=0)), expect="after 18:30 each trading day")


def _chk_fundamentals() -> Dict[str, Any]:
    mt = mtime(paths()["fundamentals"])
    return _item("fundamentals", "Fundamentals (Trendlyne)", "Market data", at=mt, detail="synced every 2 hours", status=_by_age(mt, 6), expect="under 6 h old")


def _chk_dna() -> Dict[str, Any]:
    P = paths(); exp = expected_session()
    r = _db_one(P["dna"], "SELECT COUNT(*), MAX(report_date), MAX(updated_at) FROM stock_dna_reports")
    return _item("dna", "Xray page (stock DNA reports)", "Xray & screener", at=r[2], on=r[1], count=r[0], detail=f"{r[0]} stocks · report date {r[1]}", status=_by_date(r[1], exp), expect=f"reports for {exp}")


def _chk_screener() -> Dict[str, Any]:
    P = paths(); exp = expected_session()
    r = _db_one(P["dna"], "SELECT MAX(scan_date), MAX(scan_ts) FROM stock_screener_rows")
    n = (_db_one(P["dna"], "SELECT COUNT(*) FROM stock_screener_rows WHERE scan_date=?", (r[0],)) or [0])[0] if r and r[0] else 0
    st = _by_date(r[0] if r else None, exp)
    if market_open() and r and r[1]:
        st = _by_age(to_epoch(r[1]), 1.6)
    return _item("screener", "Screener rows", "Xray & screener", at=r[1] if r else None, on=r[0] if r else None, count=n, detail=f"{n} rows on {r[0] if r else '?'}", status=st, expect="hourly in market hours, daily after close")


def _chk_trades() -> Dict[str, Any]:
    P = paths()
    a = (_db_one(P["master"], "SELECT COUNT(*) FROM active_trades") or [0])[0]
    c = _db_one(P["master"], "SELECT COUNT(*), MAX(exit_date) FROM closed_trades")
    nav = _db_one(P["master"], "SELECT MAX(date) FROM daily_nav")
    exp = expected_session()
    return _item("trades", "Trade book & NAV", "Trading", at=mtime(P["master"]), on=nav[0] if nav else None, count=a,
                 detail=f"{a} open · {c[0]} closed · last exit {c[1]} · NAV for {nav[0] if nav else '?'}", status=_by_date(nav[0] if nav else None, exp), expect=f"NAV for {exp}")


def _chk_events() -> Dict[str, Any]:
    P = paths()
    r = _db_one(P["intel"], "SELECT MAX(ts), COUNT(*) FROM events")
    day = (now_ist() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    n = (_db_one(P["intel"], "SELECT COUNT(*) FROM events WHERE ts>=?", (day,)) or [0])[0]
    at = to_epoch(r[0]) if r else None
    return _item("events", "Filings & news events", "News", at=at, count=r[1] if r else None, detail=f"{n} in the last 24 h · {r[1] if r else 0} total",
                 status=_by_age(at, 3 if market_open() else 30), expect="new filings every few minutes on trading days")


def _chk_markets() -> Dict[str, Any]:
    P = paths()
    r = _db_one(P["intel"], "SELECT MAX(as_of), COUNT(*) FROM markets")
    at = to_epoch(r[0]) if r else None
    return _item("markets", "Market strip (indices, global)", "News", at=at, count=r[1] if r else None, detail=f"{r[1] if r else 0} indices", status=_by_age(at, 1 if market_open() else 30), expect="every 15 min")


def _chk_home() -> Dict[str, Any]:
    r = _db_one(paths()["intel"], "SELECT built_at FROM news_home WHERE id=1")
    at = to_epoch(r[0]) if r else None
    return _item("news_home", "News Today page", "News", at=at, detail="built every 15 min", status=_by_age(at, 1 if market_open() else 30), expect="every 15 min")


def _chk_briefs() -> Dict[str, Any]:
    r = _db_one(paths()["intel"], "SELECT kind, day, created_at FROM briefs ORDER BY created_at DESC LIMIT 1")
    return _item("briefs", "Morning / evening brief", "News", at=r[2] if r else None, on=r[1] if r else None, detail=f"latest: {r[0]} for {r[1]}" if r else "none yet", status=_by_age(r[2] if r else None, 30), expect="daily 07:00 and 17:00")


def _chk_lab_board() -> Dict[str, Any]:
    r = _db_one(paths()["lab"], "SELECT built_at FROM board_cache WHERE id=1")
    n = (_db_one(paths()["lab"], "SELECT COUNT(*) FROM screen_results") or [0])[0]
    return _item("lab_board", "Lab screens board", "Lab", at=r[0] if r else None, count=n, detail=f"{n} screens", status=_by_age(r[0] if r else None, 2 if market_open() else 30), expect="every 30 min")


def _chk_lab_cook() -> Dict[str, Any]:
    d = os.path.join(paths()["labdata"], "features", "tf=1D")
    newest = 0.0
    try:
        with os.scandir(d) as it:
            for e in it:
                newest = max(newest, e.stat().st_mtime)
    except OSError:
        pass
    return _item("lab_cook", "Lab feature store (daily cook)", "Lab", at=newest or None, detail="built after close, 19:30", status=_by_age(newest or None, 30), expect="every trading day 19:30")


def _chk_lab_universe() -> Dict[str, Any]:
    r = _db_one(paths()["lab"], "SELECT MAX(updated_at), SUM(free_backtest) FROM universe")
    return _item("lab_universe", "Lab universe (free backtest stocks)", "Lab", at=r[0] if r else None, count=r[1] if r else None, detail=f"{r[1] or 0} free stocks", status=_by_age(r[0] if r else None, 24 * 35), expect="monthly")


def _chk_lab_backtests() -> Dict[str, Any]:
    r = _db_one(paths()["lab"], "SELECT MAX(created_at), COUNT(*) FROM backtest_runs")
    s = (_db_one(paths()["lab"], "SELECT COUNT(*) FROM strategies WHERE archived=0") or [0])[0]
    return _item("lab_backtests", "Backtests", "Lab", at=r[0] if r else None, count=r[1] if r else None, detail=f"{r[1] or 0} runs · {s} strategies", status="ok" if r and r[0] else "unknown", expect="on demand")


def _chk_lab_ai() -> Dict[str, Any]:
    P = paths()["lab"]
    r = _db_one(P, "SELECT MAX(finished_at), COUNT(*) FROM ai_enhancements WHERE status='done'")
    v = _db_all(P, "SELECT COALESCE(verdict,status), COUNT(*) FROM ai_enhancements GROUP BY 1 ORDER BY 2 DESC")
    last_age = (time.time() - r[0]) / 86400 if r and r[0] else None
    return _item("lab_ai", "AI check (last run)", "AI", at=r[0] if r else None, count=r[1] if r else None,
                 detail=" · ".join(f"{k} {n}" for k, n in v[:6]) or "no runs", status="unknown" if last_age is None else ("ok" if last_age <= 8 else "stale"), expect="weekly rescore, monthly retrain")


def _chk_lab_queue() -> Dict[str, Any]:
    P = paths()["lab"]
    rows = dict(_db_all(P, "SELECT status, COUNT(*) FROM jobs GROUP BY status"))
    old = _db_one(P, "SELECT MIN(created_at) FROM jobs WHERE status='queued'")
    last = _db_one(P, "SELECT MAX(finished_at) FROM jobs WHERE status IN ('done','failed')")
    age = (time.time() - old[0]) if old and old[0] else 0
    st = "stale" if age > 1800 and not rows.get("running") else "ok"
    return _item("lab_queue", "Lab job queue", "Lab", at=last[0] if last else None, count=rows.get("queued", 0),
                 detail=f"{rows.get('queued', 0)} queued · {rows.get('running', 0)} running · {rows.get('failed', 0)} failed ever", status=st, expect="queued jobs start within minutes")


def _chk_lab_live() -> Dict[str, Any]:
    r = _db_one(paths()["lab"], "SELECT MAX(COALESCE(exit_date, entry_date)), COUNT(*), SUM(is_open) FROM live_trades")
    return _item("lab_live", "Lab live signals", "Lab", on=r[0] if r else None, count=r[1] if r else None, detail=f"{r[2] or 0} open of {r[1] or 0}", status="ok", expect="updates after each close")


def _chk_ollama() -> Dict[str, Any]:
    o = ollama_status()
    mt = mtime(os.path.join(LOGS, "intel_reader.log"))
    return _item("ollama", "AI model on the phone (Ollama)", "AI", at=mt, detail=("reachable · " + ", ".join(o.get("models", [])[:3]) + f" · {o.get('ms')} ms") if o["up"] else "NOT reachable (phone asleep or tunnel down)",
                 status="ok" if o["up"] else "stale", expect="tunnel up")


CHECKS = [_chk_candles, _chk_ltp, _chk_delivery, _chk_fundamentals, _chk_dna, _chk_screener, _chk_trades, _chk_events, _chk_markets, _chk_home, _chk_briefs,
          _chk_lab_board, _chk_lab_cook, _chk_lab_universe, _chk_lab_backtests, _chk_lab_ai, _chk_lab_queue, _chk_lab_live, _chk_ollama]
_data_cache: Dict[str, Any] = {"t": 0.0, "v": None}


def data_freshness(force: bool = False) -> Dict[str, Any]:
    if not force and _data_cache["v"] and time.time() - _data_cache["t"] < 20:
        return _data_cache["v"]
    items = []
    for fn in CHECKS:
        try:
            items.append(fn())
        except Exception as e:  # noqa: BLE001
            nm = fn.__name__.replace("_chk_", "")
            items.append(_item(nm, nm.replace("_", " ").title(), "Other", status="unknown", detail=f"could not read: {str(e)[:90]}"))
    P = paths()
    files = []
    for k in ("master", "dna", "history", "intel", "lab", "collectors", "fundamentals"):
        p = P[k]
        try:
            st = os.stat(p); files.append({"key": k, "name": os.path.basename(p), "mb": st.st_size / 1048576.0, "at": st.st_mtime})
        except OSError:
            files.append({"key": k, "name": os.path.basename(p), "mb": None, "at": None})
    out = {"items": items, "files": files, "expectedSession": expected_session().isoformat(), "now": time.time()}
    _data_cache.update(t=time.time(), v=out)
    return out


# ───────────────────────────── ollama, certificate, trading ─────────────────────────────
def ollama_status() -> Dict[str, Any]:
    import urllib.request
    t0 = time.time()
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode())
        return {"up": True, "ms": int((time.time() - t0) * 1000), "models": [m.get("name") for m in data.get("models", [])]}
    except Exception:  # noqa: BLE001
        return {"up": False, "models": []}


def cert_status() -> Dict[str, Any]:
    try:
        from cryptography import x509
        c = x509.load_pem_x509_certificate(open(CERT, "rb").read())
        end = c.not_valid_after_utc if hasattr(c, "not_valid_after_utc") else c.not_valid_after.replace(tzinfo=dt.timezone.utc)
        return {"expires": end.timestamp(), "daysLeft": (end - dt.datetime.now(dt.timezone.utc)).days}
    except Exception:  # noqa: BLE001
        return {"expires": None, "daysLeft": None}


def trading_snapshot() -> Dict[str, Any]:
    P = paths()["master"]
    out: Dict[str, Any] = {"open": [], "recentClosed": [], "nav": None}
    try:
        con = sqlite3.connect(P, timeout=3); con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=1")
        try:
            out["open"] = [dict(r) for r in con.execute("SELECT symbol, strategy_id, engine_version, entry_date, entry_price, current_stop, target_price, shares, current_ltp, unrealized_pnl FROM active_trades ORDER BY entry_date DESC")]
            out["recentClosed"] = [dict(r) for r in con.execute("SELECT symbol, strategy_id, engine_version, entry_date, exit_date, entry_price, exit_price, net_pnl FROM closed_trades ORDER BY exit_date DESC, rowid DESC LIMIT 8")]
            n = con.execute("SELECT date, engine_version, total_nav, drawdown_pct, active_positions_count FROM daily_nav ORDER BY date DESC LIMIT 1").fetchone()
            out["nav"] = dict(n) if n else None
            out["counts"] = {"open": len(out["open"]), "closed": con.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]}
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:160]
    return out


# ───────────────────────────── alerts ─────────────────────────────
def compute_alerts(machine: Dict[str, Any], services: List[Dict[str, Any]], data: Dict[str, Any], tt: Dict[str, Any], cert: Dict[str, Any]) -> List[Dict[str, str]]:
    A: List[Dict[str, str]] = []
    for s in services:
        if s.get("expect") and s["state"] != "active":
            A.append({"level": "bad", "text": f"{s['label']} is {s['state']}", "tab": "control"})
        elif (s.get("restarts") or 0) >= 5 and (s.get("uptime_s") or 1e9) < 3600:
            A.append({"level": "warn", "text": f"{s['label']} restarted {s['restarts']} times", "tab": "control"})
    if (machine.get("mem") or 0) >= 88:
        A.append({"level": "bad" if machine["mem"] >= 94 else "warn", "text": f"RAM {machine['mem']:.0f}% used ({machine['mem_mb']:.0f} MB)", "tab": "now"})
    if (machine.get("disk") or 0) >= 85:
        A.append({"level": "warn", "text": f"Disk {machine['disk']:.0f}% full", "tab": "now"})
    if cert.get("daysLeft") is not None and cert["daysLeft"] < 14:
        A.append({"level": "bad" if cert["daysLeft"] < 5 else "warn", "text": f"HTTPS certificate expires in {cert['daysLeft']} days", "tab": "data"})
    for it in data["items"]:
        if it["status"] == "stale":
            A.append({"level": "warn", "text": f"{it['label']}: {it['detail'] or 'out of date'}", "tab": "data"})
    ltp = next((j for j in tt["jobs"] if j["id"] == "job_ltp_sync"), None)
    if tt["marketOpen"] and ltp and (ltp.get("lastStart") is None or time.time() - ltp["lastStart"] > 600):
        A.append({"level": "bad", "text": "Price sync has not run in the last 10 minutes while the market is open", "tab": "jobs"})
    for j in tt["jobs"]:
        if j.get("ok") is False and (j.get("lastStart") or 0) > time.time() - 86400:
            A.append({"level": "warn", "text": f"{j['label']} failed: {(j.get('note') or '')[:90]}", "tab": "jobs"})
    return A


# ───────────────────────────── routes ─────────────────────────────
@router.get("/me")
def me(adm=Depends(owner)):
    return {"ok": True, "email": adm["email"], "now": time.time(), "tz": "Asia/Kolkata"}


@router.get("/live")
def live(adm=Depends(owner)):
    m = machine_now()
    return {"now": time.time(), "machine": m, "services": all_services(), "marketOpen": market_open()}


@router.get("/overview")
def overview(adm=Depends(owner)):
    m = machine_now(); sv = all_services(); data = data_freshness(); tt = timetable(); cert = cert_status()
    alerts = compute_alerts(m, sv, data, tt, cert)
    jobs = tt["jobs"]
    upcoming = sorted([j for j in jobs if j.get("next")], key=lambda j: j["next"])[:6]
    recent = sorted([j for j in jobs if j.get("lastStart") or j.get("lastOutput")], key=lambda j: -(j.get("lastEnd") or j.get("lastStart") or j.get("lastOutput") or 0))[:8]
    st = [i["status"] for i in data["items"]]
    return {"now": time.time(), "marketOpen": tt["marketOpen"], "tradingDay": tt["tradingDay"], "machine": m, "services": sv, "alerts": alerts,
            "upcoming": upcoming, "recent": recent, "cert": cert, "ollama": ollama_status(),
            "dataSummary": {"ok": st.count("ok"), "stale": st.count("stale"), "unknown": st.count("unknown"), "total": len(st)},
            "trading": {k: v for k, v in trading_snapshot().items() if k in ("counts", "nav")}, "expectedSession": data["expectedSession"]}


@router.get("/metrics")
def metrics(range: str = Query("6h"), adm=Depends(owner)):
    return metrics_series(range)


@router.get("/schedule")
def schedule(adm=Depends(owner)):
    tt = timetable()
    return {**tt, "runs": runs_history(), "running": running_manual(), "runnable": {k: {"label": v["label"], "heavy": bool(v.get("heavy"))} for k, v in RUNNABLE.items()}}


@router.get("/data")
def data(refresh: bool = False, adm=Depends(owner)):
    return data_freshness(force=refresh)


@router.get("/trading")
def trading(adm=Depends(owner)):
    return trading_snapshot()


@router.get("/logs")
def logs_index(adm=Depends(owner)):
    out = []
    for d, tag in ((SCHED_LOGS, "scheduler"), (LOGS, "logs")):
        try:
            for e in os.scandir(d):
                if e.is_file() and e.name.endswith(".log"):
                    st = e.stat(); out.append({"name": e.name, "kb": st.st_size / 1024.0, "at": st.st_mtime, "dir": tag})
        except OSError:
            pass
    seen, uniq = set(), []
    for o in sorted(out, key=lambda x: -x["at"]):
        if o["name"] not in seen:
            seen.add(o["name"]); uniq.append(o)
    units = [{"name": f"journal:{s['unit']}", "label": s["label"]} for s in SERVICES.values() if s["unit"] != "ssh"]
    return {"files": uniq, "journals": units}


_LOG_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,80}\.log$")


def _tail(path: str, lines: int, q: str = "") -> List[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END); size = f.tell()
        f.seek(max(0, size - 400_000))
        text = f.read().decode("utf-8", "replace")
    rows = text.splitlines()
    if q:
        ql = q.lower(); rows = [r for r in rows if ql in r.lower()]
    return [redact(r)[:600] for r in rows[-lines:]]


@router.get("/logs/read")
def logs_read(name: str, lines: int = 200, q: str = "", adm=Depends(owner)):
    lines = max(10, min(lines, 600))
    if name.startswith("journal:"):
        unit = name[8:]
        if unit not in {s["unit"] for s in SERVICES.values()}:
            raise HTTPException(404, "Not found")
        code, out = _run(["journalctl", "-u", unit, "-n", str(lines * 3 if q else lines), "--no-pager", "-o", "short-iso"], timeout=15)
        rows = out.splitlines()
        if q:
            rows = [r for r in rows if q.lower() in r.lower()][-lines:]
        return {"name": name, "lines": [redact(r)[:600] for r in rows]}
    if not _LOG_NAME.match(name):
        raise HTTPException(404, "Not found")
    for d in (SCHED_LOGS, LOGS):
        p = os.path.realpath(os.path.join(d, name))
        if os.path.dirname(p) == os.path.realpath(d) and os.path.isfile(p):
            return {"name": name, "lines": _tail(p, lines, q), "at": mtime(p)}
    raise HTTPException(404, "Not found")


@router.post("/service/{key}/{action}")
def service_action(key: str, action: str, confirm: bool = False, adm=Depends(owner)):
    spec = SERVICES.get(key)
    if not spec or action not in spec["actions"]:
        raise HTTPException(400, "That action is not allowed.")
    if spec.get("critical") and market_open() and not confirm:
        return {"ok": False, "needsConfirm": True, "error": "Market is open. Restarting the trading scheduler now can miss a price check or the 15:15 decision. Confirm to go ahead."}
    _audit(adm, f"service_{action}", {"service": spec["unit"]})
    if key == "api":      # we are the API: answer first, restart a moment later
        subprocess.Popen(["/bin/sh", "-c", f"sleep 2; systemctl {action} {spec['unit']}"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "note": "Restarting in 2 seconds. The page reconnects by itself."}
    code, out = _run(["systemctl", action, spec["unit"]], timeout=30)
    time.sleep(0.6)
    return {"ok": code == 0, "error": None if code == 0 else out.strip()[-300:], "service": service_status(key)}


@router.post("/run/{key}")
def run_now(key: str, force: bool = False, adm=Depends(owner)):
    _audit(adm, "run_job", {"job": key, "force": force})
    r = start_run(key, adm["email"], force=force)
    if not r["ok"] and r.get("error") == "That job cannot be run from here.":
        raise HTTPException(400, r["error"])
    return r
