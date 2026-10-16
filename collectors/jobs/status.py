"""status — per-symbol freshness of every dataset, for the ops page and for DNA's gating. Daily 20:00."""
from __future__ import annotations

from datetime import date, timedelta

from ..core import db
from ..core.pipeline import JobRun, freshness_state
from ._common import hist_conn, universe

NAME = "status"


def run(run: JobRun, fetcher=None, **kw) -> None:
    today = date.today()
    with db.session() as conn, hist_conn() as hist:
        syms = universe(hist)
        latest = {r[0]: r[1] for r in hist.execute("SELECT symbol, MAX(period_end_date) FROM CompanyFundamentalsHistory WHERE period_type='QUARTERLY' GROUP BY symbol")}
        listed = {r["symbol"] for r in conn.execute("SELECT symbol FROM listing")}
        sh = {r["symbol"]: r["quarter_end"] for r in conn.execute("SELECT symbol, MAX(quarter_end) quarter_end FROM shareholding GROUP BY symbol")}
        news = {r["symbol"] for r in conn.execute("SELECT DISTINCT symbol FROM announcements WHERE ts >= ?", ((today - timedelta(days=30)).isoformat(),))}
        rat = {r["symbol"]: r["quarters_used"] for r in conn.execute("SELECT symbol, quarters_used FROM ratios_daily")}
        for s in syms:
            run.seen += 1
            le = date.fromisoformat(latest[s]) if latest.get(s) else None
            db.set_status(conn, "results", s, freshness_state(le, today), latest.get(s))
            db.set_status(conn, "listing", s, "FRESH" if s in listed else "MISSING")
            q = sh.get(s)
            db.set_status(conn, "shareholding", s, freshness_state(date.fromisoformat(q), today) if q else "MISSING", q)
            db.set_status(conn, "news", s, "FRESH" if s in news else "MISSING")
            db.set_status(conn, "ratios", s, "FRESH" if rat.get(s) == 4 else "MISSING")
            run.saved += 1
        conn.commit()
