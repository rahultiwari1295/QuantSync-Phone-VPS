#!/usr/bin/env python3
"""
QuantSync — High-Performance Multiprocess Universe DNA Evaluator
Leverages Python multiprocessing to batch-compute Stock DNA reports,
2-panel light/dark analyst charts, and institutional reasoning matrices
across all 1,318 universe stocks.
"""

import os
import sys
import json
import time
import datetime
import sqlite3
from collections import Counter
from multiprocessing import Pool, cpu_count
import dna_engine

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_DB = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")
CACHE_DB = os.path.join(SCRIPT_DIR, "Data", "stock_dna_cache.db")
REPORT_OUT = os.path.join(SCRIPT_DIR, "Data", "universe_dna_evaluation.json")

def process_single_stock(sym):
    try:
        rep = dna_engine.calculate_dna_report(sym, force_refresh=True, generate_charts=False)
        ui_rep = rep.get("ui_report", {})
        trade_card = ui_rep.get("trade_execution_card", {})
        return {
            "success": True,
            "symbol": sym,
            "stance": ui_rep.get("stance", "NEUTRAL"),
            "action": trade_card.get("action", "UNKNOWN"),
            "action_title": trade_card.get("action_title", ""),
            "archetype": ui_rep.get("dna_archetype", "UNKNOWN"),
            "health_score": ui_rep.get("dna_health_score", 50),
            "price": rep.get("current_metrics", {}).get("price", 0),
            "why_buy_count": len(ui_rep.get("why_buy_reasons", [])),
            "why_avoid_count": len(ui_rep.get("why_avoid_reasons", []))
        }
    except Exception as e:
        return {
            "success": False,
            "symbol": sym,
            "error": str(e)
        }

def run_multiprocess_universe_evaluation(workers=None, limit=None, skip_cached=True):
    dna_engine.init_cache_db()
    
    conn = sqlite3.connect(HIST_DB)
    cur = conn.cursor()
    cur.execute("SELECT symbol FROM StockHistory ORDER BY candleCount DESC, symbol ASC")
    symbols = [r[0] for r in cur.fetchall()]
    conn.close()

    total_universe = len(symbols)
    if skip_cached:
        # Determine cutoff time: the most recent market close (15:45 IST)
        # Weekday: if now is after 15:45 IST, cutoff is today 15:45 IST.
        # If now is before 15:45 IST (e.g. 03:00 AM batch), cutoff is previous trading day 15:45 IST.
        # Weekend: cutoff is Friday 15:45 IST.
        IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        now_ist = datetime.datetime.now(IST)

        if now_ist.weekday() == 5:
            last_close = (now_ist - datetime.timedelta(days=1)).replace(hour=15, minute=45, second=0, microsecond=0)
        elif now_ist.weekday() == 6:
            last_close = (now_ist - datetime.timedelta(days=2)).replace(hour=15, minute=45, second=0, microsecond=0)
        elif now_ist.hour < 15 or (now_ist.hour == 15 and now_ist.minute < 45):
            days_sub = 3 if now_ist.weekday() == 0 else 1
            last_close = (now_ist - datetime.timedelta(days=days_sub)).replace(hour=15, minute=45, second=0, microsecond=0)
        else:
            last_close = now_ist.replace(hour=15, minute=45, second=0, microsecond=0)

        cutoff_utc = last_close.astimezone(datetime.timezone.utc)
        cutoff_str = cutoff_utc.strftime("%Y-%m-%d %H:%M:%S")

        cache_conn = sqlite3.connect(CACHE_DB)
        fresh_syms = set(r[0] for r in cache_conn.cursor().execute(
            "SELECT symbol FROM stock_dna_reports WHERE report_json IS NOT NULL AND updated_at >= ?",
            (cutoff_str,)
        ).fetchall())
        cache_conn.close()
        symbols = [s for s in symbols if s not in fresh_syms]
        print(f"[*] Found {len(fresh_syms)} fresh since {cutoff_str} UTC. Remaining to process: {len(symbols)} / {total_universe}", flush=True)

    if limit:
        symbols = symbols[:limit]

    num_workers = workers or min(4, max(1, cpu_count() - 1))
    print(f"[*] Starting Universe DNA Iteration on {len(symbols)} Equities (Workers: {num_workers})", flush=True)
    
    start_time = time.time()
    stats = {
        "total_evaluated": 0,
        "successful": 0,
        "failed": 0,
        "stances": Counter(),
        "actions": Counter(),
        "archetypes": Counter(),
        "health_scores": [],
        "errors": []
    }

    completed_count = 0
    with Pool(processes=num_workers, maxtasksperchild=50) as pool:
        for res in pool.imap_unordered(process_single_stock, symbols, chunksize=4):
            completed_count += 1
            if res.get("success"):
                stats["successful"] += 1
                stats["stances"][res["stance"]] += 1
                stats["actions"][res["action"]] += 1
                stats["archetypes"][res["archetype"]] += 1
                stats["health_scores"].append(res["health_score"])
            else:
                stats["failed"] += 1
                stats["errors"].append({"symbol": res["symbol"], "error": res.get("error")})

            if completed_count % 50 == 0 or completed_count == len(symbols):
                elapsed = time.time() - start_time
                rate = completed_count / (elapsed + 1e-6)
                pct = (completed_count / len(symbols)) * 100
                rem_sec = (len(symbols) - completed_count) / (rate + 1e-6)
                print(f"[{completed_count:4}/{len(symbols)} ({pct:5.1f}%)] "
                      f"Success: {stats['successful']} | Failed: {stats['failed']} | "
                      f"Rate: {rate:4.1f} stocks/s | ETA: {rem_sec/60:.1f}m", flush=True)

    elapsed_total = time.time() - start_time
    avg_score = round(sum(stats["health_scores"]) / (len(stats["health_scores"]) + 1e-6), 1) if stats["health_scores"] else 0

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_processed": len(symbols),
        "successful": stats["successful"],
        "failed": stats["failed"],
        "elapsed_seconds": round(elapsed_total, 1),
        "stocks_per_second": round(len(symbols) / (elapsed_total + 1e-6), 2),
        "average_health_score": avg_score,
        "stance_distribution": dict(stats["stances"]),
        "action_distribution": dict(stats["actions"]),
        "archetype_distribution": dict(stats["archetypes"]),
        "errors": stats["errors"][:20]
    }

    with open(REPORT_OUT, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 80, flush=True)
    print("INSTITUTIONAL UNIVERSE ITERATION & CALIBRATION REPORT", flush=True)
    print("=" * 80, flush=True)
    print(f"Universe Equities Processed : {summary['successful']}/{summary['total_processed']}")
    print(f"Total Computation Duration  : {summary['elapsed_seconds']}s ({summary['stocks_per_second']} stocks/sec)")
    print(f"Average System Health Score : {summary['average_health_score']}/100")
    
    print("\n--- STANCE DISTRIBUTION ---", flush=True)
    for st, cnt in sorted(summary["stance_distribution"].items(), key=lambda x: -x[1]):
        pct = (cnt / (summary['successful'] or 1)) * 100
        print(f"  {st:12}: {cnt:4} ({pct:5.1f}%)", flush=True)

    print("\n--- ACTION BREAKDOWN ---", flush=True)
    for act, cnt in sorted(summary["action_distribution"].items(), key=lambda x: -x[1]):
        pct = (cnt / (summary['successful'] or 1)) * 100
        print(f"  {act:32}: {cnt:4} ({pct:5.1f}%)", flush=True)

    print(f"\n[✓] Master Evaluation written to: {REPORT_OUT}", flush=True)

    # Automatically generate screener snapshot once batch run completes
    try:
        import build_screener_snapshot
        print("\n[*] Invoking Screener Snapshot Builder...", flush=True)
        build_screener_snapshot.build_all()
    except Exception as e:
        print(f"[!] Warning: Screener snapshot build failed: {e}", file=sys.stderr, flush=True)

    # Automatically synchronize V3 Pattern Candidates and Breakout Portal
    try:
        import snapshot_manager
        print("\n[*] Synchronizing V3 Pattern Candidates & Breakout Portal...", flush=True)
        pat_cnt = snapshot_manager.sync_v3_pattern_candidates()
        print(f"[✓] V3 Pattern Candidates Synced: {pat_cnt} setups populated.", flush=True)
    except Exception as e:
        print(f"[!] Warning: V3 Pattern Candidate sync failed: {e}", file=sys.stderr, flush=True)

    # Write batch completion sentinel for server screener cache invalidation (BUG-014)
    sentinel_path = os.path.join(SCRIPT_DIR, "Data", ".screener_batch_ts")
    try:
        with open(sentinel_path, "w") as f:
            f.write(str(time.time()))
        print(f"[*] Screener batch sentinel updated: {sentinel_path}", flush=True)
    except Exception as e:
        print(f"[!] Warning: Could not write batch sentinel: {e}", file=sys.stderr)

    return summary

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="QuantSync Universe DNA Evaluator")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of stocks to process")
    parser.add_argument("--workers", type=int, default=2, help="Number of parallel worker processes (default: 2)")
    parser.add_argument("--force", action="store_true", help="Force re-evaluation of all stocks ignoring existing cache")
    args = parser.parse_args()

    run_multiprocess_universe_evaluation(
        workers=args.workers,
        limit=args.limit,
        skip_cached=not args.force
    )
