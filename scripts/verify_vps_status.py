import urllib.request
import ssl
import json
import time

ctx = ssl._create_unverified_context()
base = "https://127.0.0.1:8443"

def get(path):
    t0 = time.time()
    res = urllib.request.urlopen(base + path, context=ctx)
    elapsed = round((time.time() - t0) * 1000, 1)
    status = res.status
    body = res.read()
    return status, body, elapsed

print("=== 1. CHECKING STATIC PAGES ===")
for page in ["/screener.html", "/stock-detail.html", "/dna-mobile-story.html"]:
    status, body, ms = get(page)
    print(f"  {page}: {status} OK ({len(body)} bytes, {ms}ms)")

print("\n=== 2. CHECKING API ENDPOINTS ===")
# Stock detail
status, body, ms = get("/api/stock/detail/NOCIL")
d = json.loads(body)
print(f"  /api/stock/detail/NOCIL: {status} OK ({ms}ms)")
print(f"    price={d.get('curr_price')}, change_1d={d.get('change_1d_pct')}%, 52W=({d.get('w52_low')}, {d.get('w52_high')})")

# Accuracy
status, body, ms = get("/api/screener/accuracy?days=90")
d = json.loads(body)
print(f"  /api/screener/accuracy: {status} OK ({ms}ms)")
print(f"    win_rate={d.get('win_rate_pct')}%, total_audited={d.get('total_resolved_signals')}, W={d.get('winners')}, L={d.get('losers')}")

# Breakouts
status, body, ms = get("/api/screener/breakouts")
d = json.loads(body)
print(f"  /api/screener/breakouts: {status} OK ({ms}ms)")
print(f"    near={len(d.get('near_breakout',[]))}, success={len(d.get('success',[]))}, retest={len(d.get('retest',[]))}")

# DNA Report
status, body, ms = get("/api/dna/report/NOCIL")
d = json.loads(body)
print(f"  /api/dna/report/NOCIL: {status} OK ({ms}ms)")
print(f"    is_fresh={d.get('is_fresh')}, updated_at={d.get('cache_updated_at')}")
