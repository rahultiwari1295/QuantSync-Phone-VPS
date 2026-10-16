#!/usr/bin/env python3
"""
api_stress_test.py — Concurrent Stress & Load Benchmark for QuantSync API
========================================================================
Simulates realistic multi-user concurrency (10, 25, 50, 100 concurrent users)
testing both Vercel Edge Proxy and Direct VPS backend endpoints.

Usage:
    python3 api_stress_test.py --concurrency 10 --requests 50
    python3 api_stress_test.py --concurrency 50 --requests 200 --target vercel
    python3 api_stress_test.py --concurrency 50 --requests 100 --target vps
"""

import argparse
import json
import ssl
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import statistics

TARGETS = {
    "vercel": "https://rsitradingplatform.vercel.app/api",
    "vps": "https://quantsync-api.duckdns.org:8443/api"
}

ENDPOINTS = [
    ("/terminalData?version=v2", "GET", 0.50),     # 50% of traffic
    ("/signals?version=v2", "GET", 0.20),          # 20% of traffic
    ("/screener?breakout_status=SUCCESS&limit=5", "GET", 0.20), # 20% of traffic
    ("/intel/pulse", "GET", 0.10)                  # 10% of traffic
]

def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def run_single_request(base_url: str, endpoint: str, user_id: int, ssl_ctx: ssl.SSLContext):
    url = f"{base_url}{endpoint}"
    headers = {
        "Accept-Encoding": "gzip",
        "User-Agent": f"QuantSyncStressBot/2.0 (User-{user_id})",
        "Cache-Control": "no-cache"
    }
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12, context=ssl_ctx) as resp:
            content = resp.read()
            lat_ms = (time.time() - t0) * 1000
            return {
                "endpoint": endpoint,
                "status": resp.status,
                "latency_ms": lat_ms,
                "bytes": len(content),
                "error": None
            }
    except Exception as e:
        lat_ms = (time.time() - t0) * 1000
        return {
            "endpoint": endpoint,
            "status": 0,
            "latency_ms": lat_ms,
            "bytes": 0,
            "error": str(e)
        }

def run_benchmark(target: str, concurrency: int, total_requests: int):
    base_url = TARGETS.get(target, TARGETS["vercel"])
    ssl_ctx = make_ssl_context()
    
    print("\n" + "=" * 65)
    print(f"🚀 QuantSync Stress Test Starting")
    print(f"   Target:       {target.upper()} ({base_url})")
    print(f"   Concurrency:  {concurrency} simultaneous users")
    print(f"   Total Req:    {total_requests} requests")
    print("=" * 65 + "\n")

    # Distribute requests according to endpoint weights
    tasks = []
    for i in range(total_requests):
        # Deterministic weighting
        pct = (i % 10) / 10.0
        if pct < 0.5:
            ep = "/terminalData?version=v2"
        elif pct < 0.7:
            ep = "/signals?version=v2"
        elif pct < 0.9:
            ep = "/screener?breakout_status=SUCCESS&limit=5"
        else:
            ep = "/intel/pulse"
        tasks.append((ep, i % concurrency))

    t_wall_start = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_single_request, base_url, ep, uid, ssl_ctx) for ep, uid in tasks]
        for f in as_completed(futures):
            results.append(f.result())

    t_wall_total = time.time() - t_wall_start

    # Metrics computation
    successes = [r for r in results if r["status"] == 200]
    failures = [r for r in results if r["status"] != 200]
    latencies = [r["latency_ms"] for r in successes]

    print("\n" + "—" * 65)
    print("📊 BENCHMARK RESULTS")
    print("—" * 65)
    print(f"Total Completed:      {len(results)} requests in {round(t_wall_total, 2)}s")
    print(f"Successful (HTTP 200):{len(successes)} ({round(len(successes)/len(results)*100, 1)}%)")
    print(f"Failed / Timed out:   {len(failures)}")
    print(f"Throughput (RPS):     {round(len(successes) / t_wall_total, 1)} req/sec")

    if latencies:
        latencies.sort()
        n = len(latencies)
        p50 = statistics.median(latencies)
        p95 = latencies[int(n * 0.95)] if n > 1 else latencies[0]
        p99 = latencies[int(n * 0.99)] if n > 1 else latencies[0]

        print("\n⏱️  LATENCY PERCENTILES (Round-Trip):")
        print(f"   Fastest (Min):      {round(min(latencies), 1)} ms")
        print(f"   p50 (Median):       {round(p50, 1)} ms")
        print(f"   p95 (95% of users): {round(p95, 1)} ms")
        print(f"   p99 (Slowest 1%):   {round(p99, 1)} ms")
        print(f"   Max:                {round(max(latencies), 1)} ms")

    if failures:
        print("\n⚠️ ERRORS DETECTED:")
        for f in failures[:5]:
            print(f"   - {f['endpoint']} -> {f['error']}")

    print("—" * 65 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QuantSync API Stress Test")
    parser.add_argument("--concurrency", type=int, default=10, help="Simultaneous users (e.g. 10, 50)")
    parser.add_argument("--requests", type=int, default=50, help="Total requests to fire")
    parser.add_argument("--target", choices=["vercel", "vps"], default="vercel", help="Test target")
    args = parser.parse_args()

    run_benchmark(args.target, args.concurrency, args.requests)
