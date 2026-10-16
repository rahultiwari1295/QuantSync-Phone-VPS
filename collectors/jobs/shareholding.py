"""shareholding — promoter/FII/DII holding and pledge, validated (sum≈100, pledge ≤ promoter)."""
from __future__ import annotations

import json
import sqlite3

from ..core import db, parsers, validators as V
from ..core.fetcher import Fetcher
from ..core.pipeline import JobRun
from ._common import hist_conn, universe

NAME = "shareholding"


def run(run: JobRun, fetcher: Fetcher, limit: int = 0, **kw) -> None:
    c = sqlite3.connect(db.FUND_CACHE_DB, timeout=30)
    blobs = c.execute("SELECT symbol, data_json FROM stock_fundamentals").fetchall()
    c.close()
    if limit:
        blobs = blobs[: int(limit)]
    with db.session() as conn:
        for sym, js in blobs:
            run.seen += 1
            try:
                row = parsers.parse_screener_shareholding(json.loads(js))
            except ValueError:
                row = None
            if not row or not row["quarter_end"]:
                continue
            parts = {"promoter": row["promoter_pct"], "fii": row["fii_pct"], "dii": row["dii_pct"], "public": row["public_pct"]}
            why = V.holding_sum(parts, tol=1.0) or V.pledge_ok(row["promoter_pct"], row["pledged_pct"])
            if why:
                run.rejected += 1
                db.log_validation(conn, NAME, "shareholding", sym, str(row["quarter_end"]), "holding", why, "REJECT", "SCREENER", row)
                continue
            conn.execute("INSERT OR REPLACE INTO shareholding(symbol,quarter_end,promoter_pct,pledged_pct,fii_pct,dii_pct,public_pct,source,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (sym, row["quarter_end"].isoformat(), row["promoter_pct"], row["pledged_pct"], row["fii_pct"],
                          row["dii_pct"], row["public_pct"], "SCREENER", db.now_iso()))
            run.saved += 1
        conn.commit()
