"""The News 'Today' page, written ahead of time so the app gets it in one cached request.

  python -m intel.run home      (cron every 15 minutes)

Shape (what public/app/js/views/news.js reads): mood {label, tone, line}, top[3], sections[stocks, economy, global]
each with a short paragraph and event rows. Numbers in the paragraphs are counted here from stored events; no model is
involved, so nothing in this page can be invented."""
from __future__ import annotations

import json
import time
from datetime import timedelta

from . import db

COLS = "id, symbol, company, ts, type, category, source, url, headline, summary, sentiment, impact, ai_status"


def _rows(con, sql, args=()):
    from .classify import LABEL
    out = []
    for r in con.execute(sql, args).fetchall():
        d = dict(r)
        d["label"] = LABEL.get(d.get("type"), (d.get("type") or "").replace("_", " ").title())
        out.append(d)
    return out


def _para(items: list[dict], what: str) -> str:
    n = len(items)
    if not n:
        return ""
    p = sum(1 for x in items if x["sentiment"] == "positive")
    q = sum(1 for x in items if x["sentiment"] == "negative")
    return f"{n} notable {what} items. {p} look positive and {q} negative. Biggest: {(items[0]['headline'] or '').rstrip('.')}."


def _mood(con) -> dict:
    ms = {r["name"]: dict(r) for r in con.execute("SELECT * FROM markets")}
    idx = [ms[k]["chg_pct"] for k in ("Nifty 50", "Bank Nifty") if k in ms and ms[k]["chg_pct"] is not None]
    avg = sum(idx) / len(idx) if idx else 0.0
    tone = "pos" if avg > 0.15 else "neg" if avg < -0.15 else ""
    label = "Positive" if avg > 0.6 else "Mildly positive" if avg > 0.15 else "Negative" if avg < -0.6 else "Mildly negative" if avg < -0.15 else "Mixed"
    parts = []
    if "Nifty 50" in ms and ms["Nifty 50"]["chg_pct"] is not None:
        parts.append(f"Nifty {ms['Nifty 50']['chg_pct']:+.2f}%")
    fii = con.execute("SELECT net_cr FROM flows WHERE lower(who) LIKE '%fii%' OR lower(who) LIKE '%fpi%' ORDER BY day DESC LIMIT 1").fetchone()
    if fii and fii["net_cr"] is not None:
        parts.append(f"FII net {'buying' if fii['net_cr'] > 0 else 'selling'} ₹{abs(fii['net_cr']):,.0f} Cr")
    for nm, lab in (("Brent crude", "Brent"), ("S&P 500", "S&P 500")):
        if nm in ms and ms[nm]["chg_pct"] is not None:
            parts.append(f"{lab} {ms[nm]['chg_pct']:+.1f}%")
    return {"label": label, "tone": tone, "line": " · ".join(parts) or "Waiting for market data."}


def build(con) -> dict:
    now = db.now_ist().replace(tzinfo=None)
    iso = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731
    top = _rows(con, f"SELECT {COLS} FROM events WHERE ts >= ? AND symbol IS NOT NULL ORDER BY impact DESC, ts DESC LIMIT 3", (iso(24),))
    stocks = _rows(con, f"SELECT {COLS} FROM events WHERE ts >= ? AND impact >= 40 AND category IN ('filing','insider','deal','meeting','news') ORDER BY impact DESC, ts DESC LIMIT 8", (iso(24),))
    econ = _rows(con, f"SELECT {COLS} FROM events WHERE ts >= ? AND category='macro' ORDER BY impact DESC, ts DESC LIMIT 6", (iso(48),))
    glob = _rows(con, f"SELECT {COLS} FROM events WHERE ts >= ? AND category='global' ORDER BY impact DESC, ts DESC LIMIT 6", (iso(48),))
    return {"mood": _mood(con), "top": top, "sections": [
        {"id": "stocks", "title": "Indian stocks", "paragraph": _para(stocks, "stock"), "items": stocks},
        {"id": "economy", "title": "India economy and policy", "paragraph": _para(econ, "policy"), "items": econ},
        {"id": "global", "title": "Global", "paragraph": _para(glob, "global"), "items": glob}]}


def run(log=print) -> dict:
    t0 = time.time()
    with db.session() as con:
        body = build(con)
        con.execute("INSERT OR REPLACE INTO news_home(id, built_at, body_json) VALUES(1, ?, ?)",
                    (db.now_ist().strftime("%Y-%m-%dT%H:%M:%S"), json.dumps(body, default=str)))
        db.record_run(con, "home", t0, True, len(body["top"]))
    log(f"home: mood={body['mood']['label']} top={len(body['top'])}")
    return {"mood": body["mood"]["label"], "top": len(body["top"])}
