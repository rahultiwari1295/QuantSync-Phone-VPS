#!/usr/bin/env python3
"""
scheduler.py — Auto-Discovering Production Market Scheduler Daemon
===================================================================
Orchestrates the continuous 24/7 market lifecycle across all discovered trading engines
(V2, V3, V4, etc.) without requiring any code modifications when new engines are added.

Schedule (IST, Monday to Friday):
  - 09:15–15:25 (Every 5 mins): Batched LTP Sync & Stop/Target Trigger Checks
  - 14:30, 15:00, 15:10:        Pre-Close Market Data Quote Sweep (27-batch universe quote upsert)
  - 15:15:                      Main Multi-Engine Decision Cycle (Exits, Scans, Orders, Alerts)
  - 15:35:                      Official Post-Market EOD Candle Sync from Angel SmartAPI
  - 15:45:                      EOD Mark-to-Market NAV Settlement & Telegram Audit Recap
  - Saturday 10:00:             Weekly Comprehensive Candle History Audit & Backfill
"""

import os
import sys
import time

os.environ["TZ"] = "Asia/Kolkata"
if hasattr(time, "tzset"):
    time.tzset()

import json
import sqlite3
import datetime
import logging
import argparse
from typing import Optional, Dict, List, Any
import schedule

_PROD_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

for _sub in ("common", "core", "api", "pipelines", "engines", "collectors", "intel"):
    _p = os.path.join(_PROD_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engines.registry import EngineRegistry
from common.storage import MasterStorage
from common.notifier import (
    send_message, notify_buy, notify_exit, notify_eod_summary,
    notify_audit_proof, notify_next_day_exit, notify_next_day_buys,
    notify_system_error
)
import common.broker as broker

# Setup logging
log_dir = os.path.join(_PROD_ROOT, "Data", "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "scheduler.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [Scheduler] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("ProdVPS.Scheduler")


def ist_now() -> datetime.datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
        utc = datetime.datetime.utcnow()
        return utc + datetime.timedelta(hours=5, minutes=30)


def is_trading_day(dt: Optional[datetime.datetime] = None) -> bool:
    """NSE session check (weekends AND exchange holidays) via common.market_calendar."""
    from common.market_calendar import is_trading_day as _is_session
    return _is_session((dt or ist_now()).date())


def is_market_hours(dt: Optional[datetime.datetime] = None) -> bool:
    d = dt or ist_now()
    if not is_trading_day(d):
        return False
    market_open = d.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = d.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= d <= market_close


class MultiEngineScheduler:
    def __init__(self):
        self.registry = EngineRegistry()
        self.storage = MasterStorage()
        self.ltp_cache_file = os.path.join(_PROD_ROOT, "Data", "current_ltp.json")

    def job_ltp_sync(self):
        """Runs every 5 minutes during market hours: batched LTP fetch for all active positions."""
        now = ist_now()
        if not is_market_hours(now):
            return

        t0 = time.time()
        active_symbols = self.storage.get_distinct_active_symbols()
        if not active_symbols:
            logger.info("LTP Sync: No active open positions across any engine.")
            return

        logger.info(f"LTP Sync: Querying live prices for {len(active_symbols)} unique symbols: {active_symbols}")

        # Map symbols to tokens from stock_history.db
        hist_db = os.path.join(_PROD_ROOT, "Data", "stock_history.db")
        token_map = {}
        if os.path.exists(hist_db):
            try:
                conn = sqlite3.connect(f"file:{hist_db}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT symbol, token FROM symbol_metadata WHERE symbol IN ({})".format(
                    ",".join(["?"] * len(active_symbols))
                ), active_symbols)
                for r in cur.fetchall():
                    if r[1]:
                        token_map[r[0]] = str(r[1])
                missing = [s for s in active_symbols if s not in token_map]
                if missing:
                    cur.execute("SELECT symbol, token FROM StockHistory WHERE symbol IN ({})".format(
                        ",".join(["?"] * len(missing))
                    ), missing)
                    for r in cur.fetchall():
                        if r[1]:
                            token_map[r[0]] = str(r[1])
                conn.close()
            except Exception as e:
                logger.warning(f"Error resolving tokens for LTP sync: {e}")

        # Fallback to symbol tokens
        tokens = [token_map.get(s, s) for s in active_symbols]
        quotes = broker.fetch_batch_quotes(tokens, mode="LTP")
        token_to_sym = {v: k for k, v in token_map.items()}

        ltp_map = {}
        for q in quotes:
            tok = q.get("symbolToken")
            sym = token_to_sym.get(tok, tok)
            ltp = q.get("ltp", 0.0)
            if ltp > 0:
                ltp_map[sym] = ltp

        if ltp_map:
            # 1. Update master storage
            self.storage.update_active_ltps(ltp_map)

            # 2. Write to RAM/disk cache
            try:
                with open(self.ltp_cache_file, "w") as f:
                    json.dump({"updated_at": now.isoformat(), "ltp": ltp_map}, f, indent=2)
            except Exception as e:
                logger.warning(f"Could not write LTP cache: {e}")

            # 3. Dispatch updated active positions and real-time LTPs to Wix portal
            try:
                from common.wix_sync import push_to_wix
                push_to_wix(phase="5M_LTP_SYNC")
            except Exception as e:
                logger.warning(f"Wix 5m LTP dispatch error: {e}")

        logger.info(f"LTP Sync completed in {time.time()-t0:.2f}s for {len(ltp_map)} symbols.")

    def job_market_sweep(self, force: bool = False):
        """
        Pre-close quote sweep at 14:30, 15:00, 15:10 IST to update today's candle.
        Batches all ~1,319 stocks in 27 requests (50 tokens/batch) using fetch_batch_quotes(mode="OHLC").
        Upserts today's forming candle (Open, High, Low, Close=LTP, Volume) into StockHistory.
        """
        now = ist_now()
        today_str = now.strftime("%Y-%m-%d")
        if not force and not is_trading_day(now):
            logger.info(f"Non-trading day ({today_str}) — skipping market sweep.")
            return

        logger.info(f"🚀 Pre-Close Universe Market Sweep started at {now.strftime('%H:%M:%S IST')} (Date: {today_str})...")
        t0 = time.time()

        hist_db = os.path.join(_PROD_ROOT, "Data", "stock_history.db")
        if not os.path.exists(hist_db):
            logger.error(f"StockHistory DB not found at {hist_db}")
            return

        try:
            conn = sqlite3.connect(hist_db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT token, symbol FROM StockHistory WHERE token IS NOT NULL AND token != ''")
            rows = cur.fetchall()
            token_to_sym = {str(r["token"]): r["symbol"] for r in rows}
            all_tokens = list(token_to_sym.keys())

            batch_size = 50
            chunks = [all_tokens[i:i + batch_size] for i in range(0, len(all_tokens), batch_size)]
            logger.info(f"Market Sweep: Querying quotes for {len(all_tokens)} stocks in {len(chunks)} batches...")

            successful_symbols = 0
            failed_batches = 0

            for idx, chunk in enumerate(chunks, 1):
                try:
                    fetched = broker.fetch_batch_quotes(chunk, mode="FULL")  # FULL carries tradeVolume
                    if not fetched:
                        failed_batches += 1
                        time.sleep(0.35)
                        continue

                    for item in fetched:
                        tok = str(item.get("symbolToken", ""))
                        sym = token_to_sym.get(tok)
                        if not sym:
                            continue

                        open_p = float(item.get("open", 0.0))
                        high_p = float(item.get("high", 0.0))
                        low_p = float(item.get("low", 0.0))
                        ltp = float(item.get("ltp", 0.0))
                        vol = float(item.get("tradeVolume", 0.0))

                        if ltp <= 0:
                            continue

                        cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (sym,))
                        res = cur.fetchone()
                        raw_json = res[0] if res and res[0] else "[]"
                        try:
                            existing_candles = json.loads(raw_json)
                        except Exception:
                            existing_candles = []

                        if existing_candles and str(existing_candles[-1].get("t", ""))[:10] == today_str:
                            existing_candles[-1]["h"] = max(float(existing_candles[-1].get("h", 0.0)), high_p, ltp)
                            existing_candles[-1]["l"] = min(float(existing_candles[-1].get("l", ltp)), low_p, ltp) if low_p > 0 else ltp
                            existing_candles[-1]["c"] = ltp
                            existing_candles[-1]["v"] = int(vol)
                            existing_candles[-1]["p"] = 1  # provisional: official EOD sync must overwrite
                        else:
                            new_bar = {
                                "t": today_str,
                                "o": open_p if open_p > 0 else ltp,
                                "h": max(high_p, ltp),
                                "l": min(low_p, ltp) if low_p > 0 else ltp,
                                "c": ltp,
                                "v": int(vol),
                                "p": 1  # provisional: official EOD sync must overwrite
                            }
                            existing_candles.append(new_bar)
                            if len(existing_candles) > 2000:
                                existing_candles = existing_candles[-2000:]

                        cur.execute(
                            "UPDATE StockHistory SET rawCandles = ?, candleCount = ? WHERE symbol = ?",
                            (json.dumps(existing_candles), len(existing_candles), sym)
                        )
                        successful_symbols += 1

                    conn.commit()
                    time.sleep(0.35)
                except Exception as ex:
                    logger.warning(f"Error in sweep batch {idx}: {ex}")
                    failed_batches += 1
                    time.sleep(0.5)

            conn.close()
            elapsed = time.time() - t0
            logger.info(f"✅ Market Sweep completed in {elapsed:.2f}s: {successful_symbols}/{len(all_tokens)} stocks updated. Failed batches: {failed_batches}")
        except Exception as e:
            logger.error(f"Error in job_market_sweep: {e}", exc_info=True)

    def job_decision_1515(self, dry_run: bool = False, force: bool = False):
        """
        15:15 IST Pre-close replay cycle & candidate scan.
        Runs daily replay on the pre-close market sweep data (15:10 IST):
        1. Checks active positions for stop/target hits during the day and alerts immediately before close.
        2. Scans for qualifying candidate buy setups and dispatches Telegram buy alerts.
        3. Pushes updated state to Wix portal.
        """
        now = ist_now()
        today_str = now.strftime("%Y-%m-%d")

        if not force and not is_trading_day(now):
            logger.info(f"Non-trading day ({today_str}) detected — Market closed. 15:15 cycle skipped.")
            return

        logger.info(f"⚡ 15:15 Pre-Close Daily Processor triggered for date {today_str} (DryRun={dry_run})...")
        try:
            from daily_processor import process_daily
            res_v2 = process_daily(date_str=today_str, dry_run=dry_run, scan_buys=True)
            logger.info(f"✅ 15:15 V2 Daily Processor completed for {today_str}: {res_v2}")
        except Exception as e:
            logger.error(f"❌ Error during 15:15 V2 daily processor: {e}", exc_info=True)

        try:
            from daily_processor_v3 import process_daily_v3
            res_v3 = process_daily_v3(date_str=today_str, dry_run=dry_run, scan_buys=True)
            logger.info(f"✅ 15:15 V3 Daily Processor completed for {today_str}: {res_v3}")
        except Exception as e:
            logger.error(f"❌ Error during 15:15 V3 daily processor: {e}", exc_info=True)

    def job_1535_candle_sync(self, force: bool = False):
        """
        Official 15:35 IST post-market candle ingestion from Angel One SmartAPI.
        Downloads official finalized daily candles into stock_history.db and updates engine bundles.
        """
        now = ist_now()
        today_str = now.strftime("%Y-%m-%d")
        if not force and not is_trading_day(now):
            logger.info(f"Non-trading day ({today_str}) — skipping 15:35 candle sync.")
            return

        logger.info(f"🕯️ 15:35 Official EOD Candle Sync triggered for {today_str}...")
        t0 = time.time()
        try:
            from common.candle_sync import sync_candles_incremental
            res = sync_candles_incremental(days_back=5, force_all=False, rebuild_bundles_after=True, rate_limit_delay=0.36, refresh_sessions=3)
            elapsed = time.time() - t0
            logger.info(f"✅ 15:35 Candle Sync completed in {elapsed:.2f}s: {res.get('updated_symbols', 0)} updated, {res.get('skipped_symbols', 0)} skipped.")
            notify_audit_proof(
                job_name="15:35 Official EOD Candle Sync",
                schedule_str="15:35 IST (Mon-Fri)",
                duration_s=elapsed,
                symbols=res.get("updated_symbols", 0),
                orders=0,
                status="SUCCESS"
            )
        except Exception as e:
            logger.error(f"Error in 15:35 candle sync: {e}", exc_info=True)
            notify_system_error("15:35 Official EOD Candle Sync", str(e), target_date=today_str)

    def job_finalize_1545(self, force: bool = False):
        """
        End of day finalization, authoritative daily replay, and Wix sync at 15:45 IST.
        Executes the authoritative daily replay for today's date:
        1. Evaluates exits on finalized daily candle (High >= Target, Low <= Stop).
        2. Closes exited trades in master DB and dispatches Telegram exit alerts.
        3. Scans for candidate setups on finalized daily candle.
        4. Settles EOD Mark-to-Market NAV and records into daily_nav.
        5. Dispatches daily audit proof to Telegram.
        6. Synchronizes full reconciled ledger with Wix portal.
        """
        now = ist_now()
        today_str = now.strftime("%Y-%m-%d")
        logger.info(f"📊 15:45 EOD Authoritative Daily Settlement triggered for date {today_str}...")

        if not force and not is_trading_day(now):
            logger.info(f"Non-trading day ({today_str}) — skipping 15:45 finalize cycle.")
            return

        try:
            from daily_processor import process_daily
            res_v2 = process_daily(date_str=today_str, dry_run=False, scan_buys=True)
            logger.info(f"✅ 15:45 V2 Daily Settlement completed for {today_str}: {res_v2}")
        except Exception as e:
            logger.error(f"❌ Error during 15:45 V2 daily settlement: {e}", exc_info=True)
            notify_system_error("15:45 V2 Daily Settlement", str(e), target_date=today_str)

        try:
            from daily_processor_v3 import process_daily_v3
            res_v3 = process_daily_v3(date_str=today_str, dry_run=False, scan_buys=True)
            logger.info(f"✅ 15:45 V3 Daily Settlement completed for {today_str}: {res_v3}")
        except Exception as e:
            logger.error(f"❌ Error during 15:45 V3 daily settlement: {e}", exc_info=True)
            notify_system_error("15:45 V3 Daily Settlement", str(e), target_date=today_str)

    def job_dna_eod_refresh(self, force: bool = False):
        """
        16:30 IST Post-Market DNA & Screener snapshot computation.
        Refreshes technical DNA reports and screener snapshot for today's market close.
        Uses 3 worker processes to preserve VPS RAM headroom.
        """
        now = ist_now()
        today_str = now.strftime("%Y-%m-%d")
        if not force and not is_trading_day(now):
            logger.info(f"Non-trading day ({today_str}) — skipping 16:30 DNA EOD refresh.")
            return

        logger.info(f"🧬 16:30 Post-Market DNA & Screener EOD Refresh triggered for {today_str}...")
        t0 = time.time()
        try:
            import evaluate_universe_dna
            evaluate_universe_dna.run_multiprocess_universe_evaluation(workers=3)
            elapsed = time.time() - t0
            logger.info(f"✅ 16:30 Post-Market DNA & Screener EOD Refresh completed in {elapsed:.1f}s")
            notify_audit_proof(
                job_name="16:30 Post-Market DNA EOD Refresh",
                schedule_str="16:30 IST (Mon-Fri)",
                duration_s=elapsed,
                symbols=0,
                orders=0,
                status="SUCCESS"
            )
        except Exception as e:
            logger.error(f"❌ Error in 16:30 DNA EOD refresh job: {e}", exc_info=True)
            notify_system_error("16:30 DNA EOD Refresh", str(e), target_date=today_str)

    def job_master_eod_pipeline(self, force: bool = False):
        """
        Master Post-Market EOD Pipeline:
        Deterministically runs Candle Sync, Settlement, Technicals, DNA, Screener Rows, V3 Patterns,
        Snapshots, Cache Invalidation, and Freshness Audit without cross-job clashing.
        """
        now = ist_now()
        today_str = now.strftime("%Y-%m-%d")
        if not force and not is_trading_day(now):
            logger.info(f"Non-trading day ({today_str}) — skipping master EOD pipeline.")
            return

        logger.info(f"🚀 Master EOD Pipeline triggered for {today_str}...")
        try:
            from eod_pipeline_orchestrator import EODPipelineOrchestrator
            orch = EODPipelineOrchestrator(target_date=today_str, force=force)
            res = orch.run()
            logger.info(f"✅ Master EOD Pipeline completed: {res.get('status')}")
            notify_audit_proof(
                job_name="Master EOD Pipeline Orchestrator",
                schedule_str="15:55 IST (Mon-Fri)",
                duration_s=res.get("total_elapsed_sec", 0),
                symbols=0,
                orders=0,
                status=res.get("status", "SUCCESS")
            )
        except Exception as e:
            logger.error(f"❌ Error in Master EOD Pipeline: {e}", exc_info=True)
            notify_system_error("Master EOD Pipeline Orchestrator", str(e), target_date=today_str)

    def job_freshness_watchdog(self, force: bool = False):
        """
        Autonomous Data Freshness Watchdog:
        Audits all databases for stale records across StockHistory, Technicals, DNA, Screener, Patterns,
        and Active Trades. Automatically initiates recovery if stale components are found.
        """
        try:
            import eod_freshness_watchdog
            res = eod_freshness_watchdog.audit_system_freshness()
            logger.info(f"🛡️ Freshness Watchdog Status: {res.get('status')} (all_fresh={res.get('all_fresh')})")
            if not res.get("all_fresh"):
                logger.warning(f"⚠️ Stale tables detected: {res.get('stale_components')}. Initiating auto-healing...")
                notify_system_error(
                    job_name="Data Freshness Watchdog",
                    error_msg=f"Stale tables detected: {', '.join(res.get('stale_components', []))}",
                    target_date=res.get("target_trading_date"),
                    details="Market data was NOT updated. Auto-healing initiated."
                )
                eod_freshness_watchdog.auto_heal_stale_pipeline(res, target_date=res.get("target_trading_date"))
        except Exception as e:
            logger.error(f"❌ Error in Freshness Watchdog: {e}", exc_info=True)
            notify_system_error("Data Freshness Watchdog (Crash)", str(e))

    def job_history_sync(self):
        """Saturday 10:00 IST full historical candle audit and backfill."""
        logger.info("Saturday 10:00 IST History Sync triggered...")
        t0 = time.time()
        try:
            from common.candle_sync import sync_candles_incremental
            res = sync_candles_incremental(days_back=60, force_all=False, rebuild_bundles_after=True)
            elapsed = time.time() - t0
            logger.info(f"Saturday Historical Sync finished in {elapsed:.2f}s: {res.get('updated_symbols', 0)} updated.")
        except Exception as e:
            logger.error(f"Error in Saturday history sync: {e}", exc_info=True)

    def job_dna_batch_warmcache(self):
        """Overnight 03:00 IST batch computation of Stock DNA & Screener snapshot across universe stocks."""
        now = ist_now()
        yesterday = now - datetime.timedelta(days=1)
        if not is_trading_day(yesterday) and not is_trading_day(now):
            logger.info(f"Neither yesterday ({yesterday.strftime('%Y-%m-%d')}) nor today is a trading day — skipping 03:00 DNA batch.")
            return

        logger.info("🌙 03:00 IST Universe DNA Cache & Screener Snapshot job triggered...")
        t0 = time.time()
        try:
            import evaluate_universe_dna
            evaluate_universe_dna.run_multiprocess_universe_evaluation(workers=3)
            elapsed = time.time() - t0
            logger.info(f"✅ 03:00 Universe DNA Warm-cache & Screener completed in {elapsed:.1f}s")
        except Exception as e:
            logger.error(f"❌ Error in 03:00 DNA warm-cache job: {e}", exc_info=True)

    def job_resolve_screener_outcomes(self):
        """Overnight 04:00 IST outcome resolution job for historical breakout signals."""
        now = ist_now()
        yesterday = now - datetime.timedelta(days=1)
        if not is_trading_day(yesterday) and not is_trading_day(now):
            logger.info(f"Neither yesterday ({yesterday.strftime('%Y-%m-%d')}) nor today is a trading day — skipping 04:00 outcome resolution.")
            return

        logger.info("🎯 04:00 IST Screener Outcome Resolution job triggered...")
        t0 = time.time()
        try:
            import build_screener_snapshot
            res = build_screener_snapshot.resolve_outcomes(lookback_days=10)
            elapsed = time.time() - t0
            logger.info(f"✅ 04:00 Outcome Resolution completed in {elapsed:.1f}s: {res}")
        except Exception as e:
            logger.error(f"❌ Error in 04:00 outcome resolution job: {e}", exc_info=True)

    def job_fetch_nse_delivery_data(self, date_str: Optional[str] = None, force: bool = False):
        """
        Daily 18:30 IST official NSE Delivery Volume % Ingestion.
        Downloads https://archives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv
        and persists into stock_history.db table `nse_delivery_history`.
        """
        import csv
        import io
        import urllib.request

        now = ist_now()
        if not force and not date_str and not is_trading_day(now):
            logger.info("Non-trading day — skipping scheduled 18:30 NSE delivery ingestion.")
            return

        logger.info("📦 Ingesting daily official NSE Delivery % Bhavdata...")
        t0 = time.time()

        # Connect to stock_history.db
        hist_db = os.path.join(_PROD_ROOT, "Data", "stock_history.db")
        conn = sqlite3.connect(hist_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nse_delivery_history (
                symbol TEXT NOT NULL,
                series TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                close_price REAL,
                ttl_trd_qty INTEGER,
                deliv_qty INTEGER,
                deliv_pct REAL,
                turnover_lacs REAL,
                PRIMARY KEY (symbol, series, trade_date)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nse_delivery_sym_date ON nse_delivery_history (symbol, trade_date DESC)")
        conn.commit()

        # Determine target date(s)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        }

        dates_to_try = []
        if date_str:
            dates_to_try.append(date_str)
        else:
            # Try today, if fails try up to 5 preceding trading days
            for d_offset in range(0, 6):
                cand_dt = now - datetime.timedelta(days=d_offset)
                if cand_dt.weekday() < 5 or force:
                    dates_to_try.append(cand_dt.strftime("%d%m%Y"))

        downloaded_data = None
        used_date_str = None
        for d_str in dates_to_try:
            url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d_str}.csv"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        content = resp.read().decode('utf-8', errors='ignore')
                        if "SYMBOL" in content and "DELIV_PER" in content:
                            downloaded_data = content
                            used_date_str = d_str
                            break
            except Exception as e:
                logger.debug(f"NSE Bhavdata not available for {d_str}: {e}")

        if not downloaded_data:
            logger.warning(f"⚠️ Could not fetch NSE Bhavdata for target dates: {dates_to_try}")
            conn.close()
            return {"status": "error", "message": "Failed to download Bhavdata from NSE"}

        # Parse CSV
        reader = csv.DictReader(io.StringIO(downloaded_data))
        inserted_count = 0
        batch = []

        for raw_row in reader:
            row = {k.strip(): v.strip() for k, v in raw_row.items() if k}
            sym = row.get("SYMBOL")
            series = row.get("SERIES")
            date_raw = row.get("DATE1")  # e.g., '25-Sep-2026'
            if not sym or not date_raw:
                continue

            try:
                t_date = datetime.datetime.strptime(date_raw, "%d-%b-%Y").strftime("%Y-%m-%d")
            except Exception:
                t_date = f"{used_date_str[4:8]}-{used_date_str[2:4]}-{used_date_str[0:2]}"

            def _clean_float(val):
                try:
                    return float(val.replace(",", "").strip())
                except Exception:
                    return 0.0

            def _clean_int(val):
                try:
                    return int(val.replace(",", "").strip())
                except Exception:
                    return 0

            close_p = _clean_float(row.get("CLOSE_PRICE", "0"))
            ttl_qty = _clean_int(row.get("TTL_TRD_QNTY", "0"))
            deliv_qty = _clean_int(row.get("DELIV_QTY", "0"))
            deliv_pct = _clean_float(row.get("DELIV_PER", "0"))
            turnover = _clean_float(row.get("TURNOVER_LACS", "0"))

            batch.append((sym, series, t_date, close_p, ttl_qty, deliv_qty, deliv_pct, turnover))
            if len(batch) >= 1000:
                cursor.executemany("""
                    INSERT OR REPLACE INTO nse_delivery_history
                    (symbol, series, trade_date, close_price, ttl_trd_qty, deliv_qty, deliv_pct, turnover_lacs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, batch)
                inserted_count += len(batch)
                batch = []

        if batch:
            cursor.executemany("""
                INSERT OR REPLACE INTO nse_delivery_history
                (symbol, series, trade_date, close_price, ttl_trd_qty, deliv_qty, deliv_pct, turnover_lacs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, batch)
            inserted_count += len(batch)

        conn.commit()
        conn.close()

        elapsed = time.time() - t0
        logger.info(f"✅ Ingested {inserted_count} official NSE delivery records for date {used_date_str} in {elapsed:.2f}s")
        return {"status": "ok", "date": used_date_str, "records": inserted_count, "elapsed_s": round(elapsed, 2)}


def setup_schedules(sched: MultiEngineScheduler):
    try:                                   # records each run for the private /rahul page; never changes what a job does
        from ops_track import track as _t
    except Exception:                      # noqa: BLE001
        def _t(_name, fn):
            return fn
    # Overnight DNA warm-cache & Screener build
    schedule.every().day.at("03:00").do(_t("job_dna_batch_warmcache", sched.job_dna_batch_warmcache))
    # Overnight historical outcome evaluation
    schedule.every().day.at("04:00").do(_t("job_resolve_screener_outcomes", sched.job_resolve_screener_outcomes))

    # LTP sync scheduled strictly during market hours (09:15 to 15:25)
    _market_ltp_slots = []
    for _h in range(9, 16):
        for _m in range(0, 60, 5):
            if _h == 9 and _m < 15:
                continue
            if _h == 15 and _m > 25:
                continue
            _market_ltp_slots.append(f"{_h:02d}:{_m:02d}")
    for _slot in _market_ltp_slots:
        schedule.every().day.at(_slot).do(_t("job_ltp_sync", sched.job_ltp_sync))
    logger.info(f"LTP Sync scheduled for {len(_market_ltp_slots)} market-hours slots (Mon-Fri)")

    # Market sweeps
    schedule.every().day.at("14:30").do(_t("job_market_sweep", sched.job_market_sweep))
    schedule.every().day.at("15:00").do(_t("job_market_sweep", sched.job_market_sweep))
    schedule.every().day.at("15:10").do(_t("job_market_sweep", sched.job_market_sweep))

    # Decision cycle
    schedule.every().day.at("15:15").do(_t("job_decision_1515", sched.job_decision_1515))

    # Official EOD candle ingestion
    schedule.every().day.at("15:35").do(_t("job_1535_candle_sync", sched.job_1535_candle_sync))

    # EOD finalize
    schedule.every().day.at("15:45").do(_t("job_finalize_1545", sched.job_finalize_1545))

    # Master EOD Pipeline Orchestrator (15:55 IST):
    # Runs Candle Sync, Settlement, Technicals, DNA, Screener Rows, V3 Patterns, Snapshots, and Cache Invalidation
    schedule.every().day.at("15:55").do(_t("job_master_eod_pipeline", sched.job_master_eod_pipeline))

    # Freshness Watchdog (16:35 IST post-market audit & 09:00 AM IST pre-market verification):
    schedule.every().day.at("16:35").do(_t("job_freshness_watchdog", sched.job_freshness_watchdog))
    schedule.every().day.at("09:00").do(_t("job_freshness_watchdog", sched.job_freshness_watchdog))

    # Daily 18:30 IST official NSE Delivery % CSV Ingestion
    schedule.every().day.at("18:30").do(_t("job_fetch_nse_delivery_data", sched.job_fetch_nse_delivery_data))

    # Weekend history sync
    schedule.every().saturday.at("10:00").do(_t("job_history_sync", sched.job_history_sync))


def main():
    parser = argparse.ArgumentParser(description="Prod_VPS Multi-Engine Market Scheduler")
    parser.add_argument("--daemon", action="store_true", help="Run the continuous 24/7 scheduler loop")
    parser.add_argument("--now", choices=["ltp", "sweep", "decision", "sync", "finalize", "dna_refresh", "delivery", "eod_pipeline", "watchdog"], help="Execute a specific job immediately for testing")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Run decision cycle in dry-run mode")
    parser.add_argument("--force", action="store_true", default=False, help="Force execution even on non-trading days/weekends")
    args = parser.parse_args()

    sched = MultiEngineScheduler()

    if args.now:
        logger.info(f"Executing job [--now {args.now}] immediately...")
        if args.now == "ltp":
            sched.job_ltp_sync()
        elif args.now == "sweep":
            sched.job_market_sweep(force=args.force)
        elif args.now == "decision":
            sched.job_decision_1515(dry_run=args.dry_run, force=args.force)
        elif args.now == "sync":
            sched.job_1535_candle_sync(force=args.force)
        elif args.now == "finalize":
            sched.job_finalize_1545(force=args.force)
        elif args.now == "dna_refresh":
            sched.job_dna_eod_refresh(force=args.force)
        elif args.now == "delivery":
            sched.job_fetch_nse_delivery_data(force=args.force)
        elif args.now == "eod_pipeline":
            sched.job_master_eod_pipeline(force=args.force)
        elif args.now == "watchdog":
            sched.job_freshness_watchdog(force=args.force)
        return

    # Daemon mode
    print("═" * 95)
    print("QUANTSYNC PROD_VPS MULTI-ENGINE SCHEDULER DAEMON ARMED")
    print(f"Current Time: {ist_now().strftime('%Y-%m-%d %H:%M:%S IST')} | Log: {log_file}")
    print("All Schedulers & LTP Sync Armed for Active Engines")
    print("═" * 95, flush=True)

    import signal

    def handle_sig(sig, frame):
        logger.info(f"Signal {sig} received. Stopping scheduler daemon gracefully.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    setup_schedules(sched)

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler daemon stopped.")
            break
        except Exception as e:
            logger.error(f"Scheduler loop exception: {e}", exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
