"""probe — try every route against every endpoint once and keep the raw bodies (becomes test fixtures)."""
from __future__ import annotations

from datetime import date, timedelta

from ..core.fetcher import Attempt, Fetcher, FetchSpec
from ..core.pipeline import JobRun
from ._common import NSE_API, NSE_ARCH, d, nse_attempts

NAME = "probe"


def run(run: JobRun, fetcher: Fetcher, **kw) -> None:
    t = date.today()
    targets = {
        "equity_list": f"{NSE_ARCH}/content/equities/EQUITY_L.csv",
        "corp_actions": f"{NSE_API}/corporates-corporateActions?index=equities&from_date={d(t - timedelta(days=30))}&to_date={d(t)}",
        "announcements": f"{NSE_API}/corporate-announcements?index=equities&from_date={d(t - timedelta(days=2))}&to_date={d(t)}",
        "board_meetings": f"{NSE_API}/corporate-board-meetings?index=equities&from_date={d(t)}&to_date={d(t + timedelta(days=30))}",
        "results_sbin": f"{NSE_API}/results-comparision?symbol=SBIN",
    }
    for name, url in targets.items():
        for route in ("direct", "chromium"):
            res = fetcher.fetch(FetchSpec(NAME, f"probe_{name}", route, [Attempt(route, "nse", url)],
                                          expect="csv" if name == "equity_list" else "json"))
            run.seen += 1
            run.saved += 1 if res.ok else 0
            run.rejected += 0 if res.ok else 1
            run.log(f"{name:15s} {route:9s} {'OK ' + str(len(res.body)) + ' bytes' if res.ok else 'FAIL ' + res.error}")
