#!/usr/bin/env python3
"""
eod_pipeline_orchestrator.py
============================
Master End-of-Day (EOD) Unified Ingestion & Processing Pipeline for QuantSync.

Enforces a strict, deterministic sequence with zero cross-cron clashing:
  Step 1: 🕯️  Official EOD Candle Sync (Angel One SmartAPI via common.candle_sync)
  Step 2: 📊  Authoritative Daily Settlement (V2 & V3 Daily Processors: Mark-to-Market, Exits, Entries)
  Step 3: 📐  Daily Technicals Engine (StockTechnicalsDaily precomputation)
  Step 4: 🧬  Universe DNA Evaluation (evaluate_universe_dna.py with worker memory capping)
  Step 5: 🎯  Screener Rows & Breakouts (build_screener_snapshot.py)
  Step 6: 🚀  V3 Pattern Candidates & Breakout Portal Sync (sync_v3_pattern_candidates)
  Step 7: 💾  Materialize Screener Breakouts Snapshot (generate_screener_snapshot)
  Step 8: 🔄  Invalidate In-Memory Caches & Hot-Reload WebSocket Fallback
  Step 9: 🛡️  Comprehensive Freshness Audit Verification (eod_freshness_watchdog)

Eliminates:
  - Stale prices on Pattern, Breakout, DNA, and Active Trade cards.
  - Race conditions between crontab and scheduler.py.
  - Out-of-memory (OOM) kills via sequential memory-safe subprocess execution.
  - Hardcoded/demo seeds in production.

Usage:
  ./venv/bin/python3 eod_pipeline_orchestrator.py [--date YYYY-MM-DD] [--force] [--skip-step N]
"""

import sys, os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for _sub in ("common", "core", "api", "pipelines", "engines", "collectors", "intel"):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


import os
import sys
import time
import fcntl
import json
import logging
import argparse
import datetime
import subprocess
import sqlite3
from typing import Dict, Any, Optional

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_ROOT = SCRIPT_DIR
sys.path.insert(0, PROD_ROOT)

LOG_DIR = os.path.join(PROD_ROOT, "logs")
DATA_DIR = os.path.join(PROD_ROOT, "Data")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [EOD_Orchestrator] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "eod_pipeline_orchestrator.log"))
    ]
)
logger = logging.getLogger("EOD_Orchestrator")

LOCK_FILE = "/tmp/quantsync_eod_pipeline.lock"


def ist_now() -> datetime.datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def is_trading_day(dt: datetime.datetime) -> bool:
    """NSE session check (weekends AND exchange holidays)."""
    from common.market_calendar import is_trading_day as _is_session
    return _is_session(dt.date() if isinstance(dt, datetime.datetime) else dt)


class EODPipelineOrchestrator:
    def __init__(self, target_date: Optional[str] = None, force: bool = False):
        self.now_dt = ist_now()
        self.target_date = target_date or self.now_dt.strftime("%Y-%m-%d")
        self.force = force
        self.report: Dict[str, Any] = {
            "target_date": self.target_date,
            "started_at": ist_now().isoformat(),
            "status": "RUNNING",
            "steps": {},
            "errors": []
        }

    def acquire_lock(self):
        self.lock_fd = open(LOCK_FILE, "w")
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_fd.write(f"{os.getpid()}\n{ist_now().isoformat()}\n")
            self.lock_fd.flush()
        except BlockingIOError:
            logger.error("❌ Another EOD Pipeline instance is already running! Exiting to prevent race conditions.")
            sys.exit(1)

    def release_lock(self):
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.lock_fd.close()
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception as e:
            logger.warning(f"Error releasing lock file: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1: Candle Sync
    # ──────────────────────────────────────────────────────────────────────────
    def step_1_candle_sync(self) -> bool:
        logger.info(f"═══ [STEP 1/9] Official EOD Candle Sync for {self.target_date} ═══")
        t0 = time.time()
        try:
            from common.candle_sync import sync_candles_incremental
            from common.data_quality import assess_session
            res = sync_candles_incremental(days_back=5, force_all=False, rebuild_bundles_after=True,
                                           rate_limit_delay=0.36, cutoff_date=self.target_date, refresh_sessions=3)
            quality = assess_session(self.target_date)
            if quality["status"] != "OK":
                logger.warning(f"Official coverage {quality['official_coverage']:.1%} after sync — retrying once with forced refresh of {self.target_date}")
                time.sleep(60)
                res = sync_candles_incremental(days_back=5, force_all=False, rebuild_bundles_after=True,
                                               rate_limit_delay=0.4, cutoff_date=self.target_date,
                                               refresh_from=self.target_date)
                quality = assess_session(self.target_date)
            # corporate actions: adjust split/bonus history before anything computes on it
            try:
                from common.corporate_actions import sync_and_apply
                ca_res = sync_and_apply(days_back=30)
                ca_res.pop("detail", None)
                logger.info(f"Corporate actions: {ca_res}")
            except Exception as ce:
                ca_res = {"error": str(ce)}
                logger.warning(f"Corporate action sync failed (continuing with unadjusted gaps quarantined): {ce}")
            elapsed = time.time() - t0
            self.data_quality = quality
            ok = quality["status"] == "OK"
            logger.info(f"{'✅' if ok else '⚠️'} Step 1 ({elapsed:.1f}s): {quality['official_bars']}/{quality['universe']} official bars "
                        f"({quality['official_coverage']:.1%}) → {quality['status']}")
            self.report["steps"]["step_1_candle_sync"] = {
                "success": ok,
                "elapsed_sec": round(elapsed, 1),
                "candles_updated": res.get("updated_symbols", 0),
                "failed_symbols": res.get("failed_symbols", 0),
                "data_quality": quality,
                "corporate_actions": ca_res,
            }
            if not ok:
                self.report["errors"].append(
                    f"Step 1: only {quality['official_coverage']:.1%} official candles for {self.target_date} ({quality['status']})")
            return ok
        except Exception as e:
            logger.error(f"❌ Step 1 Failed: {e}", exc_info=True)
            self.data_quality = {"status": "FAILED", "error": str(e), "session": self.target_date}
            self.report["steps"]["step_1_candle_sync"] = {"success": False, "error": str(e)}
            self.report["errors"].append(f"Step 1: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2: Multi-Engine Daily Settlement (Exits, Entries, MTM)
    # ──────────────────────────────────────────────────────────────────────────
    def step_2_daily_settlement(self) -> bool:
        if getattr(self, "data_quality", {}).get("status") == "FAILED":
            logger.error("⛔ Step 2 blocked: candle data FAILED quality gate — no entries/exits on unverified prices.")
            self.report["steps"]["step_2_settlement"] = {"success": False, "blocked": "data_quality_failed"}
            self.report["errors"].append("Step 2: blocked by data quality gate")
            return False
        logger.info(f"═══ [STEP 2/9] Authoritative Daily Settlement (V2 & V3) for {self.target_date} ═══")
        t0 = time.time()
        v2_res, v3_res = None, None
        try:
            from daily_processor import process_daily
            v2_res = process_daily(date_str=self.target_date, dry_run=False, scan_buys=True)
            logger.info(f"✅ V2 Settlement complete: {v2_res}")
        except Exception as e:
            logger.error(f"❌ Error in V2 Settlement: {e}", exc_info=True)
            self.report["errors"].append(f"Step 2 V2: {e}")

        try:
            from daily_processor_v3 import process_daily_v3
            v3_res = process_daily_v3(date_str=self.target_date, dry_run=False, scan_buys=True)
            logger.info(f"✅ V3 Settlement complete: {v3_res}")
        except Exception as e:
            logger.error(f"❌ Error in V3 Settlement: {e}", exc_info=True)
            self.report["errors"].append(f"Step 2 V3: {e}")

        elapsed = time.time() - t0
        self.report["steps"]["step_2_settlement"] = {
            "success": True,
            "elapsed_sec": round(elapsed, 1),
            "v2_result": v2_res,
            "v3_result": v3_res
        }
        return True

    # ──────────────────────────────────────────────────────────────────────────
    # Step 3: Compute Daily Technicals
    # ──────────────────────────────────────────────────────────────────────────
    def step_3_compute_technicals(self) -> bool:
        logger.info(f"═══ [STEP 3/9] Pre-Computing Daily Technicals for {self.target_date} ═══")
        t0 = time.time()
        script_path = os.path.join(PROD_ROOT, "scripts", "compute_daily_technicals.py")
        python_bin = os.path.join(PROD_ROOT, "venv", "bin", "python3")
        if not os.path.exists(python_bin):
            python_bin = sys.executable

        try:
            proc = subprocess.run([python_bin, script_path], capture_output=True, text=True, timeout=120)
            elapsed = time.time() - t0
            if proc.returncode != 0:
                logger.error(f"❌ Step 3 Failed (exit code {proc.returncode}): {proc.stderr}")
                self.report["steps"]["step_3_technicals"] = {"success": False, "error": proc.stderr}
                self.report["errors"].append(f"Step 3: {proc.stderr[:200]}")
                return False

            hist_db = os.path.join(DATA_DIR, "stock_history.db")
            conn = sqlite3.connect(hist_db)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM StockTechnicalsDaily")
            total_tech = cur.fetchone()[0]
            conn.close()

            logger.info(f"✅ Step 3 Complete ({elapsed:.1f}s): {total_tech} rows active in StockTechnicalsDaily.")
            self.report["steps"]["step_3_technicals"] = {
                "success": True,
                "elapsed_sec": round(elapsed, 1),
                "total_rows": total_tech
            }
            return True
        except Exception as e:
            logger.error(f"❌ Step 3 Failed: {e}", exc_info=True)
            self.report["steps"]["step_3_technicals"] = {"success": False, "error": str(e)}
            self.report["errors"].append(f"Step 3: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 4: Evaluate Universe DNA
    # ──────────────────────────────────────────────────────────────────────────
    def step_4_evaluate_dna(self) -> bool:
        logger.info(f"═══ [STEP 4/9] Universe DNA Multi-Process Evaluation for {self.target_date} ═══")
        t0 = time.time()
        try:
            import evaluate_universe_dna
            # Force refresh to guarantee today's prices and patterns are analyzed (workers=2 to prevent OOM)
            evaluate_universe_dna.run_multiprocess_universe_evaluation(workers=2, skip_cached=False)
            elapsed = time.time() - t0

            cache_db = os.path.join(DATA_DIR, "stock_dna_cache.db")
            conn = sqlite3.connect(cache_db)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM stock_dna_reports WHERE report_json IS NOT NULL")
            total_reports = cur.fetchone()[0]
            conn.close()

            logger.info(f"✅ Step 4 Complete ({elapsed:.1f}s): {total_reports} reports cached in stock_dna_reports.")
            self.report["steps"]["step_4_dna"] = {
                "success": True,
                "elapsed_sec": round(elapsed, 1),
                "total_reports": total_reports
            }
            return True
        except Exception as e:
            logger.error(f"❌ Step 4 Failed: {e}", exc_info=True)
            self.report["steps"]["step_4_dna"] = {"success": False, "error": str(e)}
            self.report["errors"].append(f"Step 4: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 5: Build Screener Snapshot Rows
    # ──────────────────────────────────────────────────────────────────────────
    def step_5_build_screener(self) -> bool:
        logger.info(f"═══ [STEP 5/9] Building Screener Rows for {self.target_date} ═══")
        t0 = time.time()
        try:
            import build_screener_snapshot
            res = build_screener_snapshot.build_all(scan_date=self.target_date)
            elapsed = time.time() - t0

            cache_db = os.path.join(DATA_DIR, "stock_dna_cache.db")
            conn = sqlite3.connect(cache_db)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM stock_screener_rows WHERE scan_date = ?", (self.target_date,))
            total_rows = cur.fetchone()[0]
            conn.close()

            logger.info(f"✅ Step 5 Complete ({elapsed:.1f}s): {total_rows} rows materialized in stock_screener_rows for {self.target_date}.")
            self.report["steps"]["step_5_screener"] = {
                "success": True,
                "elapsed_sec": round(elapsed, 1),
                "total_rows": total_rows
            }
            return True
        except Exception as e:
            logger.error(f"❌ Step 5 Failed: {e}", exc_info=True)
            self.report["steps"]["step_5_screener"] = {"success": False, "error": str(e)}
            self.report["errors"].append(f"Step 5: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 6: Sync V3 Pattern Candidates & Breakout Portal
    # ──────────────────────────────────────────────────────────────────────────
    def step_6_sync_patterns(self) -> bool:
        logger.info(f"═══ [STEP 6/9] Synchronizing V3 Pattern Candidates for {self.target_date} ═══")
        t0 = time.time()
        try:
            import snapshot_manager
            count = snapshot_manager.sync_v3_pattern_candidates()
            elapsed = time.time() - t0

            master_db = os.path.join(DATA_DIR, "quantsync_master.db")
            conn = sqlite3.connect(master_db)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM v3_pattern_candidates WHERE date = ?", (self.target_date,))
            pat_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM v3_breakout_portal WHERE date = ?", (self.target_date,))
            portal_count = cur.fetchone()[0]
            conn.close()

            logger.info(f"✅ Step 6 Complete ({elapsed:.1f}s): {pat_count} pattern setups & {portal_count} portal candidates for {self.target_date}.")
            self.report["steps"]["step_6_patterns"] = {
                "success": True,
                "elapsed_sec": round(elapsed, 1),
                "pattern_count": pat_count,
                "portal_count": portal_count
            }
            return True
        except Exception as e:
            logger.error(f"❌ Step 6 Failed: {e}", exc_info=True)
            self.report["steps"]["step_6_patterns"] = {"success": False, "error": str(e)}
            self.report["errors"].append(f"Step 6: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 7: Materialize Screener Breakouts Snapshot
    # ──────────────────────────────────────────────────────────────────────────
    def step_7_materialize_snapshot(self) -> bool:
        logger.info(f"═══ [STEP 7/9] Materializing Screener Breakouts Snapshot ═══")
        t0 = time.time()
        try:
            import snapshot_manager
            res = snapshot_manager.generate_screener_snapshot()
            elapsed = time.time() - t0
            logger.info(f"✅ Step 7 Complete ({elapsed:.1f}s): screener_breakouts_v2 snapshot generated.")
            self.report["steps"]["step_7_snapshot"] = {
                "success": bool(res),
                "elapsed_sec": round(elapsed, 1)
            }
            return True
        except Exception as e:
            logger.error(f"❌ Step 7 Failed: {e}", exc_info=True)
            self.report["steps"]["step_7_snapshot"] = {"success": False, "error": str(e)}
            self.report["errors"].append(f"Step 7: {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 8: Hot-Reload Server Cache & Live WebSocket Fallback Stream
    # ──────────────────────────────────────────────────────────────────────────
    def step_7b_base_rates(self) -> bool:
        """Refresh measured base rates used by DNA 'History check' lines (≈30 s)."""
        try:
            from common.base_rates import build
            res = build(step=3)
            self.report["steps"]["step_7b_base_rates"] = {"success": True, **res}
            return True
        except Exception as e:
            logger.warning(f"Base-rate refresh failed (DNA keeps yesterday's table): {e}")
            self.report["steps"]["step_7b_base_rates"] = {"success": False, "error": str(e)}
            return False

    def step_8_hot_reload(self) -> bool:
        logger.info("═══ [STEP 8/9] Busting Server Memory Caches & Hot-Reloading WebSocket Stream ═══")
        t0 = time.time()
        try:
            # 1. Invalidate REST API in-memory snapshot caches
            subprocess.run(
                ["curl", "-sk", "-X", "POST", "https://localhost:8443/api/cache/bust"],
                capture_output=True,
                text=True,
                timeout=10
            )

            # 2. Invalidate Market Stream monitored universe
            try:
                import market_stream
                market_stream.stream_manager.refresh_monitored_universe()
                logger.info("✅ Live Market Stream Manager refreshed fallback universe.")
            except Exception as e:
                logger.warning(f"Note: Could not refresh stream in-process: {e}")

            elapsed = time.time() - t0
            logger.info(f"✅ Step 8 Complete ({elapsed:.1f}s): Caches cleared and WebSocket fallbacks reloaded.")
            self.report["steps"]["step_8_hot_reload"] = {
                "success": True,
                "elapsed_sec": round(elapsed, 1)
            }
            return True
        except Exception as e:
            logger.error(f"❌ Step 8 Failed: {e}", exc_info=True)
            self.report["steps"]["step_8_hot_reload"] = {"success": False, "error": str(e)}
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 9: Audit Verification Watchdog
    # ──────────────────────────────────────────────────────────────────────────
    def step_9_audit_watchdog(self) -> bool:
        logger.info(f"═══ [STEP 9/9] Running Full Database Freshness Audit for {self.target_date} ═══")
        t0 = time.time()
        try:
            import eod_freshness_watchdog
            audit_res = eod_freshness_watchdog.audit_system_freshness(target_date=self.target_date)
            elapsed = time.time() - t0

            is_healthy = audit_res.get("all_fresh", False)
            status_str = "HEALTHY" if is_healthy else "STALE_DETECTED"
            logger.info(f"{'✅' if is_healthy else '⚠️'} Audit Status: {status_str} in {elapsed:.1f}s")
            
            self.report["steps"]["step_9_audit"] = {
                "success": is_healthy,
                "elapsed_sec": round(elapsed, 1),
                "audit_summary": audit_res
            }

            # Save master audit proof
            audit_file = os.path.join(DATA_DIR, f"eod_audit_proof_{self.target_date}.json")
            with open(audit_file, "w") as f:
                json.dump(self.report, f, indent=2)
            logger.info(f"📜 Authoritative Audit Proof persisted to: {audit_file}")

            return is_healthy
        except Exception as e:
            logger.error(f"❌ Step 9 Failed: {e}", exc_info=True)
            self.report["steps"]["step_9_audit"] = {"success": False, "error": str(e)}
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Master Execution
    # ──────────────────────────────────────────────────────────────────────────
    def run(self, skip_steps=None) -> Dict[str, Any]:
        skip_steps = skip_steps or []
        logger.info("═" * 80)
        logger.info(f"🚀 STARTING QUANTSYNC MASTER EOD PIPELINE FOR: {self.target_date}")
        logger.info(f"Current Time: {ist_now().strftime('%Y-%m-%d %H:%M:%S IST')} | Force={self.force}")
        logger.info("═" * 80)

        if not self.force and not is_trading_day(self.now_dt):
            logger.info(f"Non-trading day ({self.target_date}) — Market closed. Pipeline skipped.")
            self.report["status"] = "SKIPPED_WEEKEND"
            return self.report

        self.acquire_lock()
        t_start = time.time()
        try:
            if 1 not in skip_steps:
                self.step_1_candle_sync()
            if 2 not in skip_steps:
                self.step_2_daily_settlement()
            if 3 not in skip_steps:
                self.step_3_compute_technicals()
            if 4 not in skip_steps:
                self.step_4_evaluate_dna()
            if 5 not in skip_steps:
                self.step_5_build_screener()
            if 6 not in skip_steps:
                self.step_6_sync_patterns()
            if 7 not in skip_steps:
                self.step_7_materialize_snapshot()
            if 7 not in skip_steps:
                self.step_7b_base_rates()
            if 8 not in skip_steps:
                self.step_8_hot_reload()
            if 9 not in skip_steps:
                self.step_9_audit_watchdog()

            total_elapsed = time.time() - t_start
            self.report["total_elapsed_sec"] = round(total_elapsed, 1)
            self.report["status"] = "COMPLETED" if not self.report["errors"] else "COMPLETED_WITH_WARNINGS"
            self.report["finished_at"] = ist_now().isoformat()
            try:
                from common.data_quality import write_status
                write_status({
                    "session": self.target_date,
                    "status": getattr(self, "data_quality", {}).get("status", "UNKNOWN"),
                    "data_quality": getattr(self, "data_quality", {}),
                    "pipeline_status": self.report["status"],
                    "failed_steps": [k for k, v in self.report["steps"].items() if not v.get("success")],
                    "finished_at": self.report["finished_at"],
                })
            except Exception as se:
                logger.warning(f"Could not write data_status.json: {se}")

            if self.report["errors"]:
                try:
                    from common.notifier import notify_system_error
                    notify_system_error(
                        job_name="Master EOD Pipeline",
                        error_msg="; ".join(self.report["errors"][:3]),
                        target_date=self.target_date,
                        details=f"Completed with errors. Steps: {[k for k, v in self.report['steps'].items() if not v.get('success')]}"
                    )
                except Exception as ne:
                    logger.warning(f"Could not dispatch error notification: {ne}")

            logger.info("═" * 80)
            logger.info(f"🎉 MASTER EOD PIPELINE FINISHED IN {total_elapsed:.1f}s | STATUS: {self.report['status']}")
            logger.info("═" * 80)
            return self.report
        except Exception as e:
            logger.error(f"💥 Unhandled crash in Master EOD Pipeline: {e}", exc_info=True)
            self.report["status"] = "CRASHED"
            self.report["errors"].append(str(e))
            try:
                from common.notifier import notify_system_error
                notify_system_error(
                    job_name="Master EOD Pipeline",
                    error_msg=f"Pipeline crashed: {e}",
                    target_date=self.target_date
                )
            except Exception as ne:
                logger.warning(f"Could not dispatch crash notification: {ne}")
            raise
        finally:
            self.release_lock()


def main():
    parser = argparse.ArgumentParser(description="QuantSync Master EOD Ingestion & Processing Pipeline Orchestrator")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD (defaults to today)")
    parser.add_argument("--force", action="store_true", help="Force execution even on non-trading days/weekends")
    parser.add_argument("--skip-step", type=int, action="append", default=[], help="Skip specific step numbers (1-9)")
    args = parser.parse_args()

    orchestrator = EODPipelineOrchestrator(target_date=args.date, force=args.force)
    res = orchestrator.run(skip_steps=args.skip_step)
    if res.get("status") in ["COMPLETED", "SKIPPED_WEEKEND"]:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
