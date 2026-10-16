"""news — NSE corporate announcements + board meeting calendar. Every 15 min in market hours."""
from __future__ import annotations

from datetime import date, timedelta

from ..core import db, parsers
from ..core.fetcher import Fetcher, FetchSpec
from ..core.pipeline import JobRun, promote_announcements, promote_meetings
from ._common import NSE_API, d, hist_conn, nse_attempts, universe

NAME = "news"


def run(run: JobRun, fetcher: Fetcher, **kw) -> None:
    today = date.today()
    with db.session() as conn, hist_conn() as hist:
        known = set(universe(hist))
        url = f"{NSE_API}/corporate-announcements?index=equities&from_date={d(today - timedelta(days=2))}&to_date={d(today)}"
        res = fetcher.fetch(FetchSpec(NAME, "nse_announcements", str(today), nse_attempts(url),
                                      accept=lambda x: None if isinstance(x, list) else "expected a list"))
        run.log(f"announcements: {res.tried}")
        if res.ok:
            promote_announcements(conn, run, parsers.parse_nse_announcements(res.data), "NSE_ANNOUNCEMENTS", known)
        else:
            run.status = "PARTIAL"
        if kw.get("meetings", True):
            url = f"{NSE_API}/corporate-board-meetings?index=equities&from_date={d(today)}&to_date={d(today + timedelta(days=60))}"
            res = fetcher.fetch(FetchSpec(NAME, "nse_board_meetings", str(today), nse_attempts(url),
                                          accept=lambda x: None if isinstance(x, list) else "expected a list"))
            run.log(f"board meetings: {res.tried}")
            if res.ok:
                promote_meetings(conn, hist, run, parsers.parse_nse_board_meetings(res.data), "NSE_BOARD_MEETINGS", known)
            else:
                run.status = "PARTIAL"
        conn.commit()
        hist.commit()
