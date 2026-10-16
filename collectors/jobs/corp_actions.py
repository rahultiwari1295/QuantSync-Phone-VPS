"""corp_actions — dividends, splits, bonus from NSE. Daily 07:30 (last 30 days) and --history for backfill."""
from __future__ import annotations

from datetime import date, timedelta

from ..core import db, parsers
from ..core.fetcher import Fetcher, FetchSpec
from ..core.pipeline import JobRun, listing_dates, promote_actions
from ._common import NSE_API, BSE_API, d, hist_conn, nse_attempts

NAME = "corp_actions"


def run(run: JobRun, fetcher: Fetcher, days_back: int = 30, from_date: str = "", **kw) -> None:
    to = date.today() + timedelta(days=60)
    frm = date.fromisoformat(from_date) if from_date else date.today() - timedelta(days=int(days_back))
    # NSE limits wide windows: walk in 6-month chunks
    chunks, cur = [], frm
    while cur < to:
        nxt = min(cur + timedelta(days=180), to)
        chunks.append((cur, nxt))
        cur = nxt + timedelta(days=1)
    with db.session() as conn, hist_conn() as hist:
        listing = listing_dates(conn)
        for a, b in chunks:
            url = f"{NSE_API}/corporates-corporateActions?index=equities&from_date={d(a)}&to_date={d(b)}"
            spec = FetchSpec(NAME, "nse_corp_actions", f"{a}:{b}", nse_attempts(url),
                             accept=lambda x: None if isinstance(x, list) else "expected a list")
            res = fetcher.fetch(spec)
            run.log(f"{a}..{b}: {res.tried}")
            if not res.ok:
                run.status = "PARTIAL"
                run.rejected += 1
                continue
            rows = parsers.parse_nse_corporate_actions(res.data)
            promote_actions(conn, hist, run, rows, listing, "NSE_CORP_ACTION")
            conn.commit()
            hist.commit()
