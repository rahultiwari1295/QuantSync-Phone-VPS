#!/usr/bin/env python3
"""
common/candle_sync.py — Authoritative Angel One EOD Candle Synchronization
==========================================================================
Provides robust, rate-limited incremental and backfill synchronization of
daily candles from Angel One SmartAPI into StockHistory (stock_history.db).

Features:
- Incremental sync: Only queries missing trading dates per symbol.
- Prioritization: Index benchmarks (NIFTY, NIFTY100) & active universe first.
- Rate-limiting: Strictly bounds to ~3 requests/sec to avoid API throttling.
- Session auto-refresh on 403 / 429 / JWT expiry.
- Automatic bundle rebuild for V2 & V3 engines post-sync.
"""

import os
import sys
import time
import json
import sqlite3
import datetime
import logging
from typing import Dict, List, Any, Optional

_COMMON_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_ROOT = os.path.dirname(_COMMON_DIR)
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from common.broker import get_session, _CANDLE_URL, _post

logger = logging.getLogger("ProdVPS.CandleSync")

DB_PATH = os.path.join(_PROD_ROOT, "Data", "stock_history.db")
BUNDLE_V2_DATA = os.path.join(_PROD_ROOT, "engines", "v2", "data", "v2_bundles.pkl")
BUNDLE_V2_ROOT = os.path.join(_PROD_ROOT, "Data", "v2_2026_bundles.pkl")


def get_ist_now() -> datetime.datetime:
    utc = datetime.datetime.utcnow()
    return utc + datetime.timedelta(hours=5, minutes=30)


def get_target_candle_date() -> str:
    """Most recent NSE session whose official close has passed (holiday-aware)."""
    from common.market_calendar import last_completed_session
    return last_completed_session().isoformat()


def fetch_symbol_candles(
    symbol_token: str,
    exchange: str = "NSE",
    from_date: str = "",
    to_date: str = "",
    max_retries: int = 3
) -> List[List[Any]]:
    token = get_session()
    payload = {
        "exchange": exchange,
        "symboltoken": str(symbol_token),
        "interval": "ONE_DAY",
        "fromdate": from_date,
        "todate": to_date
    }

    for attempt in range(1, max_retries + 1):
        resp = _post(_CANDLE_URL, payload, token)
        if resp.get("status") and resp.get("data"):
            return resp["data"]

        msg = str(resp.get("message", "")).lower()
        if "jwt" in msg or "session" in msg or "unauthorized" in msg or resp.get("errorcode") in ("AG8001", "AG8002"):
            token = get_session(force=True)
        elif "rate" in msg or "exceeding" in msg or "access denied" in msg:
            time.sleep(3.0 * attempt)

        if attempt < max_retries:
            time.sleep(1.0 * attempt)

    return []


def rebuild_engine_bundles(verbose: bool = True) -> int:
    try:
        from engines.v2.core.indicators import compute_indicators
        from engines.v2.core.timeframe_engine import aggregate_candles, compute_bollinger_keltner_squeeze, compute_donchian
    except ImportError:
        logger.info("Upgraded QuantSync-V2 queries stock_history.db directly; legacy bundles not required.")
        return 0

    t0 = time.time()
    target_bundle = BUNDLE_V2_DATA if os.path.exists(BUNDLE_V2_DATA) else BUNDLE_V2_ROOT
    if not os.path.exists(target_bundle):
        logger.warning(f"Cannot rebuild bundles: {target_bundle} not found.")
        return 0

    import pickle
    with open(target_bundle, "rb") as f:
        old_data = pickle.load(f)

    target_syms = list(old_data.keys()) if isinstance(old_data, dict) else []
    if not target_syms:
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    new_bundles: Dict[str, Dict[str, Any]] = {}
    success_count = 0

    for sym in target_syms:
        cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (sym,))
        row = cur.fetchone()
        if not row or not row[0]:
            continue

        try:
            full_candles = json.loads(row[0])
            c_2024 = [c for c in full_candles if str(c.get("t", ""))[:10] >= "2024-01-01"]
            if len(c_2024) < 50:
                continue

            ind1d = compute_indicators(c_2024)
            if not ind1d or len(ind1d.get("ema20", [])) < len(c_2024):
                continue

            b2d = aggregate_candles(c_2024, 2)
            ind2d = compute_indicators(b2d) if len(b2d) >= 30 else {}
            sq2d = compute_bollinger_keltner_squeeze(b2d) if len(b2d) >= 30 else {}
            highs_2d = [float(b["h"]) for b in b2d]
            lows_2d = [float(b["l"]) for b in b2d]
            don2d = compute_donchian(highs_2d, lows_2d, 20) if len(b2d) >= 30 else {}

            b3d = aggregate_candles(c_2024, 3)
            ind3d = compute_indicators(b3d) if len(b3d) >= 20 else {}

            d_to_idx = {c["t"][:10]: k for k, c in enumerate(c_2024)}
            d_to_c = {c["t"][:10]: c for c in c_2024}

            new_bundles[sym] = {
                "candles": c_2024,
                "ind1d": ind1d,
                "b2d": b2d,
                "ind2d": ind2d,
                "sq2d": sq2d,
                "don2d": don2d,
                "b3d": b3d,
                "ind3d": ind3d,
                "date_to_idx": d_to_idx,
                "date_to_candle": d_to_c
            }
            success_count += 1
        except Exception as e:
            logger.debug(f"Error computing bundle for {sym}: {e}")

    conn.close()

    for save_path in [BUNDLE_V2_DATA, BUNDLE_V2_ROOT]:
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "wb") as f:
                pickle.dump(new_bundles, f, protocol=pickle.HIGHEST_PROTOCOL)
            if verbose:
                logger.info(f"Saved updated bundle ({success_count} symbols) to {save_path}")
        except Exception as e:
            logger.error(f"Failed saving bundle to {save_path}: {e}")

    logger.info(f"Bundle rebuild complete in {time.time()-t0:.2f}s ({success_count}/{len(target_syms)} symbols).")
    return success_count


def merge_official_candles(
    existing: List[Dict[str, Any]],
    raw_new: List[List[Any]],
    target_cutoff: str,
) -> List[Dict[str, Any]]:
    """
    Merge official broker candles into an existing candle list.

    Rules (the 24 Sep 2026 incident came from breaking these):
      * Official candles always overwrite whatever is stored for that date,
        including provisional intraday snapshots written by the market sweep.
      * Bars dated on a non-trading day (holiday ghosts) are dropped.
      * Nothing after target_cutoff is stored as history.
    """
    from common.market_calendar import is_trading_day
    by_date: Dict[str, Dict[str, Any]] = {}
    for c in existing:
        d = str(c.get("t", ""))[:10]
        if d and is_trading_day(d):
            by_date[d] = c
    for c in raw_new or []:
        d = str(c[0])[:10]
        if d > target_cutoff or not is_trading_day(d):
            continue
        o, h, l, cl = (round(float(x), 2) for x in c[1:5])
        if min(o, h, l, cl) <= 0:
            continue
        by_date[d] = {
            "t": d,
            "o": o,
            "h": max(h, o, cl),
            "l": min(l, o, cl),
            "c": cl,
            "v": int(c[5]),
        }
    return [by_date[d] for d in sorted(by_date)]


def _needs_refresh(candles: List[Dict[str, Any]], refresh_dates: List[str]) -> bool:
    """True when any of the recent sessions is missing, provisional, or has no volume."""
    by_date = {str(c.get("t", ""))[:10]: c for c in candles[-15:]}
    for d in refresh_dates:
        c = by_date.get(d)
        if c is None or c.get("p") or not c.get("v"):
            return True
    return False


def sync_candles_incremental(
    days_back: int = 30,
    force_all: bool = False,
    rebuild_bundles_after: bool = True,
    rate_limit_delay: float = 0.35,
    cutoff_date: Optional[str] = None,
    refresh_sessions: int = 3,
    refresh_from: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pull official daily candles from Angel One and make them the stored history.

    refresh_sessions: the last N sessions are always re-checked; a symbol is
        refetched if any of them is missing, provisional (sweep snapshot) or has
        zero volume. This is what guarantees the official close replaces the
        14:30-15:10 sweep bar.
    refresh_from: ISO date; force a refetch of every symbol from this date
        (used by scripts/repair_candles.py to heal a corrupted window).
    """
    from common.market_calendar import recent_trading_days

    t0 = time.time()
    target_cutoff = cutoff_date or get_target_candle_date()
    to_date_str = f"{target_cutoff} 15:30"
    refresh_dates = [d.isoformat() for d in recent_trading_days(refresh_sessions, target_cutoff)]

    logger.info(f"Candle sync: cutoff={target_cutoff}, re-check sessions={refresh_dates}, refresh_from={refresh_from}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT symbol, token, rawCandles, isV3Eligible
        FROM StockHistory
        WHERE token IS NOT NULL AND token != ''
    """)
    records = cur.fetchall()

    indices = {"NIFTY", "NIFTY100", "BANKNIFTY", "NIFTYIT", "NIFTYMIDCAP100", "NIFTYNXT50"}
    p1 = [r for r in records if r[0] in indices]
    p2 = [r for r in records if r[0] not in indices and r[3] == 1]
    p3 = [r for r in records if r[0] not in indices and r[3] != 1]
    sorted_records = p1 + p2 + p3

    total = len(sorted_records)
    updated_count = skipped_count = failed_count = 0
    failed_symbols: List[str] = []

    for idx, (sym, tok, raw_json, _) in enumerate(sorted_records, 1):
        try:
            existing = json.loads(raw_json) if raw_json else []
        except Exception:
            existing = []

        if refresh_from:
            from_day = refresh_from
        elif not force_all and existing and not _needs_refresh(existing, refresh_dates):
            skipped_count += 1
            continue
        elif existing:
            # earliest of: first refresh session, or the day after the last stored bar
            last_date = str(existing[-1].get("t", ""))[:10]
            from_day = min(refresh_dates[0], last_date)
        else:
            from_day = (datetime.date.fromisoformat(target_cutoff) - datetime.timedelta(days=days_back)).isoformat()

        raw_new = fetch_symbol_candles(tok, exchange="NSE", from_date=f"{from_day} 09:15", to_date=to_date_str)
        time.sleep(rate_limit_delay)

        if not raw_new:
            failed_count += 1
            failed_symbols.append(sym)
            continue

        merged = merge_official_candles(existing, raw_new, target_cutoff)
        if len(merged) > 2000:
            merged = merged[-2000:]

        cur.execute(
            "UPDATE StockHistory SET rawCandles = ?, candleCount = ? WHERE symbol = ?",
            (json.dumps(merged), len(merged), sym)
        )
        updated_count += 1

        if updated_count % 25 == 0 or idx == total:
            conn.commit()
            logger.info(f"[{idx:4d}/{total:4d}] updated={updated_count} current={skipped_count} failed={failed_count} ({(time.time()-t0)/60:.1f}m)")

    conn.commit()
    conn.close()

    total_time = round(time.time() - t0, 2)
    logger.info(f"Candle sync complete in {total_time}s ({updated_count} updated, {skipped_count} current, {failed_count} failed).")

    bundles_rebuilt = 0
    if rebuild_bundles_after and updated_count > 0:
        bundles_rebuilt = rebuild_engine_bundles(verbose=True)

    return {
        "status": "SUCCESS" if failed_count <= max(25, total * 0.02) else "DEGRADED",
        "target_cutoff": target_cutoff,
        "total_symbols": total,
        "updated_symbols": updated_count,
        "skipped_symbols": skipped_count,
        "failed_symbols": failed_count,
        "failed_symbol_list": failed_symbols[:200],
        "bundles_rebuilt": bundles_rebuilt,
        "elapsed_seconds": total_time
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import argparse
    ap = argparse.ArgumentParser(description="Official EOD candle sync")
    ap.add_argument("--force", action="store_true", help="refetch every symbol")
    ap.add_argument("--refresh-from", help="force refetch of all symbols from this ISO date")
    args = ap.parse_args()
    print(json.dumps(sync_candles_incremental(force_all=args.force, refresh_from=args.refresh_from), indent=2))
