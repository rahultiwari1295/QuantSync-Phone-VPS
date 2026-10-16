"""Routes for the redesigned QuantSync app.

Registered from server.py with two lines at the very end of the file:

    import app_routes
    app_routes.register(app, APP_DIR)

Adds
  GET /api/app/xray-batch?symbols=A,B,C   X-Ray score, name, sector and price for up to 60 stocks in one call
  GET /api/app/summary                    how many stocks are covered and when the X-Ray data was last refreshed
  GET /api/app/market                     market health from every X-Ray report: share of stocks in uptrends, verdict counts,
                                          each sector's health with its strongest stocks, and the highest scores overall
  /app/...                                the static web app (web/app in this folder)

Reads only from Data/stock_dna_cache.db (read-only connection). Nothing here writes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("quantsync.app_routes")
SYM = re.compile(r"^[A-Z0-9&-]{1,20}$")


def _ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def xray_rows(cache_db: str, symbols: list[str]) -> dict:
    out: dict = {}
    if not symbols or not os.path.exists(cache_db):
        return out
    con = _ro(cache_db)
    try:
        q = ",".join("?" * len(symbols))
        for r in con.execute(f"SELECT symbol, health_score, stance, archetype, report_json, updated_at FROM stock_dna_reports WHERE symbol IN ({q})", symbols):
            try:
                rep = json.loads(r["report_json"] or "{}")
            except Exception:  # noqa: BLE001
                rep = {}
            m = rep.get("current_metrics") or {}
            u = rep.get("ui_report") or {}
            score = _num(u.get("dna_health_score")) or _num(r["health_score"])
            out[r["symbol"]] = {
                "symbol": r["symbol"], "name": rep.get("company_name") or r["symbol"], "sector": rep.get("sector"),
                "score": round(score) if score is not None else None, "stance": r["stance"], "archetype": r["archetype"],
                "title": u.get("action_title") or rep.get("action_title"),
                "price": _num(m.get("price")), "ema200": _num(m.get("ema200")), "w52_high": _num(m.get("w52_high")),
                "w52_low": _num(m.get("w52_low")), "dist_52w_pct": _num(m.get("dist_52w_pct")), "updated_at": r["updated_at"],
            }
        try:  # fill name/sector/price from the screener table where the report lacks them
            for r in con.execute(f"SELECT symbol, company_name, sector, curr_price, dna_score FROM stock_screener_rows WHERE symbol IN ({q})", symbols):
                o = out.setdefault(r["symbol"], {"symbol": r["symbol"]})
                o.setdefault("name", r["company_name"]); o["name"] = o.get("name") or r["company_name"] or r["symbol"]
                o["sector"] = o.get("sector") or r["sector"]
                o["price"] = o.get("price") if o.get("price") is not None else _num(r["curr_price"])
                if o.get("score") is None and _num(r["dna_score"]) is not None:
                    o["score"] = round(_num(r["dna_score"]))
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return out


_MKT: dict = {"key": None, "t": 0.0, "data": None}
_MKT_LOCK = threading.Lock()
_PATHS = ("$.sector", "$.company_name", "$.current_metrics.price", "$.current_metrics.ema200", "$.current_metrics.ema50",
          "$.ui_report.dna_health_score", "$.ui_report.action_title", "$.current_metrics.dist_52w_pct")


def _market_rows(con: sqlite3.Connection) -> list[dict]:
    """One JSON parse per report: json_extract with several paths returns them together as an array."""
    q = "SELECT symbol, health_score, stance, json_extract(report_json, " + ", ".join(f"'{p}'" for p in _PATHS) + ") AS j FROM stock_dna_reports"
    rows = []
    for r in con.execute(q):
        try:
            v = json.loads(r["j"] or "[]")
        except Exception:  # noqa: BLE001
            continue
        if len(v) != len(_PATHS):
            continue
        sector, name, price, e200, e50, score, title, d52 = v
        score = _num(score) if _num(score) is not None else _num(r["health_score"])
        rows.append({"symbol": r["symbol"], "name": name or r["symbol"], "sector": sector, "price": _num(price), "ema200": _num(e200), "ema50": _num(e50),
                     "score": score, "stance": (r["stance"] or "").upper(), "title": title, "dist_52w_pct": _num(d52)})
    return rows


def market_snapshot(cache_db: str) -> dict:
    con = _ro(cache_db)
    try:
        key = con.execute("SELECT count(*), max(updated_at) FROM stock_dna_reports").fetchone()
        key = (key[0], key[1])
        with _MKT_LOCK:
            if _MKT["key"] == key and _MKT["data"] is not None and time.time() - _MKT["t"] < 6 * 3600:
                return _MKT["data"]
        rows = _market_rows(con)
        try:  # reports from before the sector fix say "NSE Equities": use the screener's sector instead
            sec = {r["symbol"]: r["sector"] for r in con.execute("SELECT symbol, sector FROM stock_screener_rows WHERE sector IS NOT NULL AND sector != ''")}
        except sqlite3.Error:
            sec = {}
    finally:
        con.close()
    for r in rows:
        if not r["sector"] or r["sector"] == "NSE Equities":
            r["sector"] = sec.get(r["symbol"]) or r["sector"]
    priced = [r for r in rows if r["price"] is not None]
    pct = lambda part, whole: round(100.0 * part / whole, 1) if whole else None  # noqa: E731
    above200 = [r for r in priced if r["ema200"] is not None]
    above50 = [r for r in priced if r["ema50"] is not None]
    scored = [r for r in rows if r["score"] is not None]
    stances = {"BULLISH": 0, "NEUTRAL": 0, "BEARISH": 0}
    for r in rows:
        if r["stance"] in stances:
            stances[r["stance"]] += 1
    groups: dict = {}
    for r in rows:
        s = r["sector"]
        if s and s != "NSE Equities":
            groups.setdefault(s, []).append(r)
    sectors = []
    for name, g in groups.items():
        if len(g) < 5:
            continue
        gp = [r for r in g if r["price"] is not None and r["ema200"] is not None]
        gs = [r for r in g if r["score"] is not None]
        top = sorted(gs, key=lambda r: -r["score"])[:3]
        sectors.append({"name": name, "n": len(g),
                        "avg_score": round(sum(r["score"] for r in gs) / len(gs), 1) if gs else None,
                        "above200_pct": pct(sum(1 for r in gp if r["price"] > r["ema200"]), len(gp)),
                        "bullish_pct": pct(sum(1 for r in g if r["stance"] == "BULLISH"), len(g)),
                        "bearish_pct": pct(sum(1 for r in g if r["stance"] == "BEARISH"), len(g)),
                        "leaders": [{"symbol": r["symbol"], "name": r["name"], "score": round(r["score"])} for r in top]})
    sectors.sort(key=lambda s: -(s["above200_pct"] or 0))
    leaders = sorted([r for r in scored if r["price"] is not None], key=lambda r: -r["score"])[:12]
    data = {
        "as_of": key[1], "stocks": len(rows),
        "above200_pct": pct(sum(1 for r in above200 if r["price"] > r["ema200"]), len(above200)),
        "above50_pct": pct(sum(1 for r in above50 if r["price"] > r["ema50"]), len(above50)),
        "avg_score": round(sum(r["score"] for r in scored) / len(scored), 1) if scored else None,
        "stances": stances,
        "score_bands": {"strong": sum(1 for r in scored if r["score"] >= 70), "mixed": sum(1 for r in scored if 45 <= r["score"] < 70), "weak": sum(1 for r in scored if r["score"] < 45)},
        "sectors": sectors,
        "leaders": [{**{k: r[k] for k in ("symbol", "name", "sector", "stance", "price", "title", "dist_52w_pct")}, "score": round(r["score"])} for r in leaders],
    }
    with _MKT_LOCK:
        _MKT.update(key=key, t=time.time(), data=data)
    return data


def register(app: FastAPI, app_dir: str) -> None:
    cache_db = os.path.join(app_dir, "Data", "stock_dna_cache.db")
    web = os.path.join(app_dir, "web", "app")

    @app.get("/api/app/xray-batch")
    def xray_batch(symbols: str = ""):
        want = []
        for s in symbols.split(","):
            s = s.strip().upper()
            if s and SYM.match(s) and s not in want:
                want.append(s)
        if not want:
            raise HTTPException(400, "Pass at least one symbol.")
        try:
            return {"items": xray_rows(cache_db, want[:60])}
        except sqlite3.Error as e:
            log.warning("xray-batch failed: %s", e)
            raise HTTPException(503, "X-Ray data is being refreshed. Try again shortly.")

    @app.get("/api/app/summary")
    def summary():
        if not os.path.exists(cache_db):
            return {"stocks": None, "as_of": None}
        con = _ro(cache_db)
        try:
            n, as_of = con.execute("SELECT count(*), max(updated_at) FROM stock_dna_reports").fetchone()
            return {"stocks": n, "as_of": as_of}
        except sqlite3.Error:
            return {"stocks": None, "as_of": None}
        finally:
            con.close()

    @app.get("/api/app/market")
    def market():
        if not os.path.exists(cache_db):
            raise HTTPException(503, "X-Ray data is not available yet.")
        try:
            return market_snapshot(cache_db)
        except sqlite3.Error as e:
            log.warning("market snapshot failed: %s", e)
            raise HTTPException(503, "X-Ray data is being refreshed. Try again shortly.")

    def _warm():  # build the first snapshot in the background so the first visitor does not wait
        try:
            if os.path.exists(cache_db):
                market_snapshot(cache_db)
        except Exception as e:  # noqa: BLE001
            log.warning("market warm-up failed: %s", e)
    threading.Thread(target=_warm, daemon=True).start()

    if os.path.isdir(web):
        @app.get("/app", include_in_schema=False)
        def app_root():
            return RedirectResponse("/app/")
        app.mount("/app", StaticFiles(directory=web, html=True), name="web_app")
    else:
        log.warning("web/app not found at %s; the app UI is not served", web)
