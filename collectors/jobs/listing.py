"""listing — NSE equity master (listing dates, ISIN, face value). Daily 07:30."""
from __future__ import annotations

from ..core import db, parsers
from ..core.fetcher import Fetcher, FetchSpec, Attempt
from ..core.pipeline import JobRun, promote_listing
from ._common import NSE_ARCH, hist_conn, universe

NAME = "listing"


def run(run: JobRun, fetcher: Fetcher, **kw) -> None:
    url = f"{NSE_ARCH}/content/equities/EQUITY_L.csv"
    spec = FetchSpec(NAME, "nse_equity_list", "all",
                     [Attempt("direct", "nse", url), Attempt("chromium", "nse", url), Attempt("relay", "generic")],
                     expect="csv", accept=lambda t: None if "SYMBOL" in t[:200].upper() else "not the equity list")
    res = fetcher.fetch(spec)
    run.log(f"fetch: {res.tried}")
    if not res.ok:
        run.status = "FAILED"
        run.log(f"no route worked: {res.error}")
        return
    rows = parsers.parse_equity_list_csv(res.data)
    run.log(f"parsed {len(rows)} listings")
    if len(rows) < 1500:                      # NSE lists ~2,100 equities; far fewer means a truncated/blocked file
        run.status = "FAILED"
        run.log("too few rows — refusing to update")
        return
    with db.session() as conn, hist_conn() as hist:
        promote_listing(conn, hist, rows, "NSE_EQUITY_LIST", run, universe=set(universe(hist)))
        hist.commit()
