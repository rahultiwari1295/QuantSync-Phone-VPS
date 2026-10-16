"""Real trades from the live QuantSync engine (same VPS) for the two flagship strategies.

AI Alpha  = engine v2 (AI PRO: Leader, Breakout, Apex, Flag, Gap)
AI Pattern = engine v3 (patterns: 52W high, HTF, Darvas, Base)

Source: the engine API on 127.0.0.1:8000 (/api/track-record, /api/active-trades, /api/v3/active-trades). Trades before the
engine's live date are its walk-forward simulation, trades after are recorded live; both are kept and labelled.
The last good response is saved to disk, so the Lab keeps showing the record if the engine API is briefly down.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path

import pandas as pd

from . import metrics
from .config import Config

log = logging.getLogger("qslab.engine")
ENGINE_OF = {"AI Alpha": "v2", "AI Pattern": "v3"}
TTL = 120
_cache: dict = {"t": 0.0, "data": None}
_lock = threading.Lock()

REASON = {"STOP_HIT": "stop", "STOP_LOSS": "stop", "TARGET_HIT": "target", "SYSTEM_TARGET_OR_TRAIL": "trail", "TIME_EXIT": "time"}


def _get(url: str, timeout: float = 6.0):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout) as r:
        return json.loads(r.read().decode())


def _file(cfg: Config) -> Path:
    return cfg.data_dir / "engine_feed.json"


def _fetch(cfg: Config) -> dict:
    base = cfg.engine_api.rstrip("/")
    tr = _get(f"{base}/api/track-record")
    active = []
    for path in ("/api/active-trades", "/api/v3/active-trades"):
        try:
            rows = _get(base + path)
            active += rows if isinstance(rows, list) else []
        except Exception:  # noqa: BLE001 - one list missing must not drop the other
            log.warning("engine %s unavailable", path)
    seen, act = set(), []
    for a in active:
        eng = a.get("engine") or ("v3" if "v3" in str(a.get("engineBadge", "")).lower() else "v2")
        k = (eng, a.get("symbol"), a.get("entryDate"))
        if k in seen or not a.get("symbol"):
            continue
        seen.add(k)
        act.append({"engine": eng, "symbol": a.get("symbol"), "strategy": a.get("strategyName"), "entry_date": a.get("entryDate"),
                    "entry_price": a.get("entryPrice"), "price": a.get("currentPrice") or a.get("livePrice"),
                    "ret_pct": a.get("currentROI", a.get("roiPct"))})
    closed = [{"engine": t.get("engine"), "symbol": t.get("symbol"), "strategy": t.get("strategy"), "entry_date": t.get("entryDate"),
               "exit_date": t.get("exitDate"), "entry_price": t.get("entryPrice"), "exit_price": t.get("exitPrice"),
               "ret_pct": t.get("returnPct"), "exit_reason": REASON.get(str(t.get("exitReason")), "rule"),
               "bars": t.get("holdingDays"), "origin": "live" if t.get("origin") == "LIVE" else "tested"}
              for t in tr.get("trades", []) if t.get("symbol") and t.get("exitDate")]
    return {"fetched_at": time.time(), "live_since": tr.get("liveSince"), "closed": closed, "active": act}


def feed(cfg: Config) -> dict | None:
    """Engine trades, refreshed at most every TTL seconds; falls back to the last saved copy."""
    with _lock:
        if _cache["data"] is not None and time.time() - _cache["t"] < TTL:
            return _cache["data"]
        try:
            d = _fetch(cfg)
            f = _file(cfg)
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(f)
        except Exception as e:  # noqa: BLE001
            log.warning("engine feed fetch failed (%s); using saved copy", e)
            d = _cache["data"]
            if d is None and _file(cfg).exists():
                d = json.loads(_file(cfg).read_text())
        _cache.update(t=time.time(), data=d)
        return d


def engine_of(strategy_row) -> str | None:
    return ENGINE_OF.get(strategy_row["name"]) if strategy_row["kind"] == "flagship" else None


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["symbol", "entry_date", "exit_date", "ret_pct", "is_open", "bars", "origin"])
    df["entry_date"] = pd.to_datetime(df["entry_date"]); df["exit_date"] = pd.to_datetime(df["exit_date"])
    df["ret_pct"] = pd.to_numeric(df["ret_pct"], errors="coerce")
    df["bars"] = pd.to_numeric(df.get("bars"), errors="coerce")
    df["is_open"] = False
    return df


def closed(cfg: Config, engine: str) -> pd.DataFrame:
    d = feed(cfg) or {"closed": []}
    return _frame([t for t in d["closed"] if t["engine"] == engine])


def active(cfg: Config, engine: str) -> list[dict]:
    d = feed(cfg) or {"active": []}
    return [a for a in d["active"] if a["engine"] == engine]


def stats(cfg: Config, engine: str) -> dict:
    """Strip on the strategy page: results recorded live by the engine, plus what is open now."""
    c = closed(cfg, engine)
    live = c[c["origin"] == "live"] if len(c) else c
    s = metrics.summarize(live)
    shown = s["trades"] >= metrics.MIN_TRADES_SHOWN
    if not shown:
        s["win_rate"] = None
    return {**s, "open": len(active(cfg, engine)), "shown": shown}


def trades(cfg: Config, engine: str, status: str, limit: int) -> list[dict]:
    today = pd.Timestamp.today().normalize()
    if status == "active":
        return [{"id": i, "symbol": a["symbol"], "signal_date": a["entry_date"], "entry_date": a["entry_date"], "entry_price": a["entry_price"],
                 "exit_date": None, "exit_price": a["price"], "exit_reason": None, "ret_pct": a["ret_pct"], "is_open": 1,
                 "age_days": (today - pd.Timestamp(a["entry_date"])).days if a["entry_date"] else None, "origin": "live", "group": a["strategy"]}
                for i, a in enumerate(sorted(active(cfg, engine), key=lambda a: a["entry_date"] or "", reverse=True))]
    d = feed(cfg) or {"closed": []}
    rows = sorted([t for t in d["closed"] if t["engine"] == engine], key=lambda t: (t["exit_date"], t["symbol"]), reverse=True)[:limit]
    return [{"id": i, "symbol": t["symbol"], "signal_date": t["entry_date"], "entry_date": t["entry_date"], "entry_price": t["entry_price"],
             "exit_date": t["exit_date"], "exit_price": t["exit_price"], "exit_reason": t["exit_reason"], "ret_pct": t["ret_pct"],
             "is_open": 0, "origin": t["origin"], "group": t["strategy"]} for i, t in enumerate(rows)]


def backtest(cfg: Config, engine: str) -> dict | None:
    """Full engine record in the same shape as a Lab past test, with a 'Live' row next to the yearly rows."""
    d = feed(cfg)
    c = closed(cfg, engine)
    if d is None or not len(c):
        return None
    years = sorted({int(y) for y in c["entry_date"].dt.year.unique()})
    summary = {"All": metrics.summarize(c), "Live": metrics.summarize(c[c["origin"] == "live"])}
    for y in years:
        summary[str(y)] = metrics.summarize(c[c["entry_date"].dt.year == y])
    rows = sorted([t for t in d["closed"] if t["engine"] == engine], key=lambda t: (t["exit_date"], t["symbol"]), reverse=True)
    trades_ = [{"symbol": t["symbol"], "signal_date": t["entry_date"], "entry_date": t["entry_date"], "entry_price": t["entry_price"],
                "exit_date": t["exit_date"], "exit_price": t["exit_price"], "exit_reason": t["exit_reason"], "ret_pct": t["ret_pct"],
                "bars": t["bars"], "is_open": False, "origin": t["origin"], "group": t["strategy"]} for t in rows]
    return {"source": "engine", "live_since": d.get("live_since"), "years": years, "summary": summary, "months": metrics.monthly_counts(c),
            "open_trades": len(active(cfg, engine)), "trades": trades_, "analytics": metrics.analytics(c), "metrics_v": metrics.METRICS_V,
            "note": f"QuantSync engine record. Before {d.get('live_since')}: walk-forward test of the same rules. From then on: recorded live."}
