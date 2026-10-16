#!/usr/bin/env python3
"""
scripts/repair_candles.py — one-shot repair of the candle store.

Fixes found in the Oct 2026 audit:
  * bars dated on NSE holidays / non-sessions (e.g. 2 Oct 2026 ghost bars)   → dropped
  * zero / negative prices                                                     → dropped
  * high < max(open, close) or low > min(open, close)                          → envelope repaired
  * zero-volume sweep snapshots since --suspect-from (24 Sep 2026)             → marked provisional
    so the next official sync (or --refetch now) replaces them with exchange candles
  * duplicate dates                                                            → last one kept

Every changed symbol is backed up to StockHistory_backup first.

Usage on the VPS:
  ./venv/bin/python3 scripts/repair_candles.py --dry-run
  ./venv/bin/python3 scripts/repair_candles.py --refetch      # repair + pull official candles from 24 Sep
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.market_calendar import is_trading_day  # noqa: E402
from common.data_quality import repair_bar, bar_is_coherent  # noqa: E402


def clean_candles(cs, suspect_from, stats, examples, symbol):
    by_date = {}
    for bar in cs:
        try:
            t = str(bar["t"])[:10]
        except Exception:
            stats["dropped_malformed"] += 1
            continue
        if not is_trading_day(t):
            stats["dropped_non_session"] += 1
            if len(examples) < 20:
                examples.append([symbol, t])
            continue
        try:
            if min(float(bar[k]) for k in ("o", "h", "l", "c")) <= 0:
                stats["dropped_nonpositive"] += 1
                continue
        except Exception:
            stats["dropped_malformed"] += 1
            continue
        bar = dict(bar)
        bar["t"] = t
        if not bar_is_coherent(bar):
            bar = repair_bar(bar)
            stats["repaired_ohlc"] += 1
        if t >= suspect_from and not (bar.get("v") or 0) and not bar.get("p"):
            bar["p"] = 1
            stats["marked_provisional"] += 1
        if t in by_date:
            stats["deduped"] += 1
        by_date[t] = bar
    return [by_date[d] for d in sorted(by_date)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(ROOT, "Data", "stock_history.db"))
    ap.add_argument("--suspect-from", default="2026-09-24")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refetch", action="store_true", help="pull official candles from --suspect-from afterwards")
    ap.add_argument("--symbols", help="comma separated subset")
    args = ap.parse_args(argv)

    stats = collections.Counter()
    examples: list = []
    only = {s.strip().upper() for s in args.symbols.split(",")} if args.symbols else None
    now = dt.datetime.now().isoformat(timespec="seconds")

    conn = sqlite3.connect(args.db)
    if not args.dry_run:
        conn.execute("CREATE TABLE IF NOT EXISTS StockHistory_backup(symbol TEXT, rawCandles TEXT, backed_up_at TEXT, reason TEXT)")

    rows = conn.execute("SELECT symbol, rawCandles FROM StockHistory").fetchall()
    for i, (sym, raw) in enumerate(rows, 1):
        if only and sym not in only:
            continue
        stats["symbols_scanned"] += 1
        try:
            cs = json.loads(raw) if raw else []
        except Exception:
            stats["bad_json"] += 1
            continue
        cleaned = clean_candles(cs, args.suspect_from, stats, examples, sym)
        if cleaned == cs:
            continue
        stats["symbols_changed"] += 1
        if not args.dry_run:
            conn.execute("INSERT INTO StockHistory_backup VALUES (?,?,?,?)", (sym, raw, now, "repair_candles"))
            conn.execute("UPDATE StockHistory SET rawCandles=?, candleCount=? WHERE symbol=?",
                         (json.dumps(cleaned), len(cleaned), sym))
            if stats["symbols_changed"] % 200 == 0:
                conn.commit()
    conn.commit()
    conn.close()

    out = dict(stats)
    out["dropped_examples"] = examples
    out["dry_run"] = args.dry_run

    if args.refetch and not args.dry_run:
        from common.candle_sync import sync_candles_incremental
        out["refetch"] = sync_candles_incremental(refresh_from=args.suspect_from, rebuild_bundles_after=True)
        from common.data_quality import assess_session
        from common.market_calendar import last_completed_session
        out["quality_after"] = assess_session(last_completed_session().isoformat(), args.db)

    print(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":
    main()
