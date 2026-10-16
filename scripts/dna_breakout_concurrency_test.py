#!/usr/bin/env python3
"""
dna_breakout_concurrency_test.py
================================
Tests concurrency capacity for:
1. DNA Page: Users searching DIFFERENT stocks, loading DNA reports, and fetching tactical charts.
2. Breakout Screen: Users loading breakouts, clicking different signals, and fetching 250-bar candlestick charts.
"""

import argparse
import random
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics

# 50 Distinct Symbols for DNA Searches & Chart Lookups
SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL", "ITC",
    "KOTAKBANK", "LT", "AXISBANK", "BAJFINANCE", "MARUTI", "ASIANPAINT", "TITAN",
    "SUNPHARMA", "WIPRO", "HCLTECH", "ADANIENT", "NTPC", "TATAMOTORS", "POWERGRID",
    "ULTRACEMCO", "JSWSTEEL", "TATASTEEL", "COALINDIA", "BAJAJFINSV", "ONGC",
    "GRASIM", "TECHM", "HINDUNILVR", "DRREDDY", "CIPLA", "APOLLOHOSP", "HEROMOTOCO",
    "DIVISLAB", "EICHERMOT", "BPCL", "BRITANNIA", "TATACONSUM", "SHRIRAMFIN",
    "INDUSINDBK", "ADANIPORTS", "BEL", "HAL", "TRENT", "VEDL", "ZOMATO", "JIOFIN", "IOC"
]

TARGETS = {
    "vercel": "https://rsitradingplatform.vercel.app/api",
    "vps": "https://quantsync-api.duckdns.org:8443/api"
}

def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def run_dna_user_flow(base_url: str, user_id: int, symbol: str, ssl_ctx: ssl.SSLContext):
    """
    Simulates 1 user on the DNA page:
    1. Search symbol
    2. Load full DNA report
    3. Load tactical dark chart image
    """
    results = []
    t_start = time.time()
    
    # Step 1: Search
    url_search = f"{base_url}/dna/search?q={symbol}"
    t0 = time.time()
    try:
        req = urllib.request.Request(url_search, headers={"Accept-Encoding": "gzip", "User-Agent": f"DNAUser-{user_id}"})
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as r:
            _ = r.read()
            results.append(("dna_search", r.status, (time.time() - t0)*1000, None))
    except Exception as e:
        results.append(("dna_search", 0, (time.time() - t0)*1000, str(e)))

    # Step 2: Report
    url_report = f"{base_url}/dna/report/{symbol}"
    t0 = time.time()
    try:
        req = urllib.request.Request(url_report, headers={"Accept-Encoding": "gzip", "User-Agent": f"DNAUser-{user_id}"})
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as r:
            _ = r.read()
            results.append(("dna_report", r.status, (time.time() - t0)*1000, None))
    except Exception as e:
        results.append(("dna_report", 0, (time.time() - t0)*1000, str(e)))

    # Step 3: Chart Image
    url_chart = f"{base_url}/dna/chart/{symbol}?view=tactical&theme=dark"
    t0 = time.time()
    try:
        req = urllib.request.Request(url_chart, headers={"User-Agent": f"DNAUser-{user_id}"})
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as r:
            _ = r.read()
            results.append(("dna_chart", r.status, (time.time() - t0)*1000, None))
    except Exception as e:
        results.append(("dna_chart", 0, (time.time() - t0)*1000, str(e)))

    total_flow_time = (time.time() - t_start) * 1000
    return {
        "user_id": user_id,
        "symbol": symbol,
        "type": "dna_flow",
        "total_time_ms": total_flow_time,
        "steps": results
    }

def run_breakout_user_flow(base_url: str, user_id: int, symbol: str, ssl_ctx: ssl.SSLContext):
    """
    Simulates 1 user on the Breakouts / Screener screen:
    1. Fetch breakouts list
    2. Click on signal -> fetch 250-bar candlestick chart
    3. Fetch stock detail metadata
    """
    results = []
    t_start = time.time()

    # Step 1: Screener breakouts
    url_screener = f"{base_url}/screener?breakout_status=SUCCESS&limit=5"
    t0 = time.time()
    try:
        req = urllib.request.Request(url_screener, headers={"Accept-Encoding": "gzip", "User-Agent": f"BreakoutUser-{user_id}"})
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as r:
            _ = r.read()
            results.append(("screener_breakouts", r.status, (time.time() - t0)*1000, None))
    except Exception as e:
        results.append(("screener_breakouts", 0, (time.time() - t0)*1000, str(e)))

    # Step 2: Fetch 250-bar Chart
    url_chart = f"{base_url}/chart/{symbol}?limit=250"
    t0 = time.time()
    try:
        req = urllib.request.Request(url_chart, headers={"Accept-Encoding": "gzip", "User-Agent": f"BreakoutUser-{user_id}"})
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as r:
            _ = r.read()
            results.append(("signal_chart_fetch", r.status, (time.time() - t0)*1000, None))
    except Exception as e:
        results.append(("signal_chart_fetch", 0, (time.time() - t0)*1000, str(e)))

    # Step 3: Stock Detail
    url_detail = f"{base_url}/stock/detail/{symbol}"
    t0 = time.time()
    try:
        req = urllib.request.Request(url_detail, headers={"Accept-Encoding": "gzip", "User-Agent": f"BreakoutUser-{user_id}"})
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as r:
            _ = r.read()
            results.append(("stock_detail", r.status, (time.time() - t0)*1000, None))
    except Exception as e:
        results.append(("stock_detail", 0, (time.time() - t0)*1000, str(e)))

    total_flow_time = (time.time() - t_start) * 1000
    return {
        "user_id": user_id,
        "symbol": symbol,
        "type": "breakout_flow",
        "total_time_ms": total_flow_time,
        "steps": results
    }

def run_concurrency_test(flow_type: str, concurrency: int, target: str):
    base_url = TARGETS.get(target, TARGETS["vercel"])
    ssl_ctx = make_ssl_context()
    
    print("\n" + "=" * 70)
    print(f"⚡ SIMULATING {concurrency} PARALLEL USERS ON {flow_type.upper()}")
    print(f"   Target:       {target.upper()} ({base_url})")
    print(f"   Unique Stocks:{min(concurrency, len(SYMBOLS))} DIFFERENT STOCKS")
    print("=" * 70)

    # Assign a unique symbol to each user
    user_symbols = [SYMBOLS[i % len(SYMBOLS)] for i in range(concurrency)]

    t_wall_start = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        if flow_type == "dna":
            futures = [
                executor.submit(run_dna_user_flow, base_url, i, user_symbols[i], ssl_ctx)
                for i in range(concurrency)
            ]
        else:
            futures = [
                executor.submit(run_breakout_user_flow, base_url, i, user_symbols[i], ssl_ctx)
                for i in range(concurrency)
            ]
        
        for f in as_completed(futures):
            results.append(f.result())

    t_wall_total = time.time() - t_wall_start

    # Flatten all step calls
    all_steps = []
    for r in results:
        all_steps.extend(r["steps"])

    step_success = [s for s in all_steps if s[1] == 200]
    step_fail = [s for s in all_steps if s[1] != 200]
    total_flow_times = [r["total_time_ms"] for r in results if all(s[1] == 200 for s in r["steps"])]

    print("\n" + "—" * 70)
    print(f"📊 {flow_type.upper()} CONCURRENCY RESULTS ({concurrency} Parallel Users)")
    print("—" * 70)
    print(f"Total User Flows Completed:  {len(results)} users in {round(t_wall_total, 2)}s")
    print(f"Total API Calls Fired:       {len(all_steps)} requests")
    print(f"Successful API Calls (200):  {len(step_success)} ({round(len(step_success)/len(all_steps)*100, 1)}%)")
    print(f"Failed API Calls:            {len(step_fail)}")
    print(f"Total System Throughput:     {round(len(all_steps) / t_wall_total, 1)} req/sec")

    if total_flow_times:
        total_flow_times.sort()
        n = len(total_flow_times)
        p50 = statistics.median(total_flow_times)
        p95 = total_flow_times[int(n * 0.95)] if n > 1 else total_flow_times[0]
        
        print("\n⏱️  FULL USER JOURNEY TIME (All 3 steps per user combined):")
        print(f"   Fastest User:             {round(min(total_flow_times), 1)} ms")
        print(f"   Median User (p50):        {round(p50, 1)} ms")
        print(f"   95th Percentile User(p95):{round(p95, 1)} ms")
        print(f"   Slowest User (Max):       {round(max(total_flow_times), 1)} ms")

    # Step breakdown
    step_types = set(s[0] for s in all_steps)
    print("\n🔍 STEP-BY-STEP BREAKDOWN:")
    for st in sorted(step_types):
        lats = [s[2] for s in all_steps if s[0] == st and s[1] == 200]
        if lats:
            print(f"   • {st:20}: Median {round(statistics.median(lats), 1):5} ms | p95 {round(sorted(lats)[int(len(lats)*0.95)], 1):5} ms")

    if step_fail:
        print("\n⚠️ FAILURES / ERRORS:")
        for sf in step_fail[:5]:
            print(f"   - Step {sf[0]} -> {sf[3]}")

    print("—" * 70 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=["dna", "breakout"], required=True, help="Flow to test")
    parser.add_argument("--concurrency", type=int, default=10, help="Parallel users")
    parser.add_argument("--target", choices=["vercel", "vps"], default="vercel", help="Target server")
    args = parser.parse_args()

    run_concurrency_test(args.flow, args.concurrency, args.target)
