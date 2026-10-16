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
    now = get_ist_now()
    if now.weekday() == 5:
        target = now - datetime.timedelta(days=1)
    elif now.weekday() == 6:
        target = now - datetime.timedelta(days=2)
    elif now.hour < 15 or (now.hour == 15 and now.minute < 30):
        if now.weekday() == 0:
            target = now - datetime.timedelta(days=3)
        else:
            target = now - datetime.timedelta(days=1)
    else:
        target = now

    return target.strftime("%Y-%m-%d")


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


def sync_candles_incremental(
    days_back: int = 30,
    force_all: bool = False,
    rebuild_bundles_after: bool = True,
    rate_limit_delay: float = 0.35,
    cutoff_date: Optional[str] = None
) -> Dict[str, Any]:
    t0 = time.time()
    target_cutoff = cutoff_date or get_target_candle_date()
    now = get_ist_now()
    to_date_str = now.strftime("%Y-%m-%d 15:30")

    logger.info(f"🚀 Starting Incremental Candle Sync (Target Cutoff: {target_cutoff}, ToDate: {to_date_str})...")

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
    updated_count = 0
    skipped_count = 0
    failed_count = 0

    for idx, (sym, tok, raw_json, _) in enumerate(sorted_records, 1):
        last_date = None
        existing_candles = []
        if raw_json:
            try:
                existing_candles = json.loads(raw_json)
                if existing_candles:
                    last_date = str(existing_candles[-1].get("t", ""))[:10]
            except Exception:
                existing_candles = []

        if not force_all and last_date and last_date >= target_cutoff:
            skipped_count += 1
            continue

        if last_date:
            from_date_str = f"{last_date} 09:15"
        else:
            t_back = time.time() - (days_back * 86400)
            from_date_str = time.strftime("%Y-%m-%d 09:15", time.localtime(t_back))

        raw_new = fetch_symbol_candles(tok, exchange="NSE", from_date=from_date_str, to_date=to_date_str)
        time.sleep(rate_limit_delay)

        if not raw_new:
            failed_count += 1
            continue

        existing_map = {c["t"]: c for c in existing_candles}
        for c in raw_new:
            d_str = str(c[0])[:10]
            if d_str <= target_cutoff:
                existing_map[d_str] = {
                    "t": d_str,
                    "o": round(float(c[1]), 2),
                    "h": round(float(c[2]), 2),
                    "l": round(float(c[3]), 2),
                    "c": round(float(c[4]), 2),
                    "v": int(c[5])
                }

        merged = sorted(existing_map.values(), key=lambda x: x["t"])
        if len(merged) > 2000:
            merged = merged[-2000:]

        cur.execute(
            "UPDATE StockHistory SET rawCandles = ?, candleCount = ? WHERE symbol = ?",
            (json.dumps(merged), len(merged), sym)
        )
        updated_count += 1

        if updated_count % 25 == 0 or idx == total:
            conn.commit()
            elapsed_m = (time.time() - t0) / 60.0
            pct = (idx / total) * 100
            logger.info(f"[{idx:4d}/{total:4d}] ({pct:5.1f}%) | Updated: {updated_count:4d} | Already Current: {skipped_count:4d} | Failed: {failed_count:3d} | Elapsed: {elapsed_m:.1f}m")

    conn.commit()
    conn.close()

    total_time = round(time.time() - t0, 2)
    logger.info(f"✅ Incremental Candle Sync complete in {total_time}s ({updated_count} updated, {skipped_count} skipped, {failed_count} failed).")

    bundles_rebuilt = 0
    if rebuild_bundles_after and updated_count > 0:
        bundles_rebuilt = rebuild_engine_bundles(verbose=True)

    return {
        "status": "SUCCESS",
        "target_cutoff": target_cutoff,
        "total_symbols": total,
        "updated_symbols": updated_count,
        "skipped_symbols": skipped_count,
        "failed_symbols": failed_count,
        "bundles_rebuilt": bundles_rebuilt,
        "elapsed_seconds": total_time
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    force = "--force" in sys.argv
    sync_candles_incremental(force_all=force)
