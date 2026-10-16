import os
import sys

sys.path.insert(0, "/root/quantsync")

from daily_processor import process_daily
from daily_processor_v3 import process_daily_v3

def print_result(label, date_str, res):
    exits = res.get("exits", [])
    entries = res.get("entries", [])
    print(f"\n[{label} | {date_str}] Exits: {len(exits)} | Entries: {len(entries)}")
    for ex in exits:
        sym = ex.get("symbol")
        p = ex.get("exit_price")
        r = ex.get("exit_reason")
        pnl = ex.get("net_pnl", 0.0)
        print(f"  🔴 EXIT:  {sym} | Price: ₹{p} | Reason: {r} | Net PnL: ₹{pnl:,.2f}")
    for en in entries:
        sym = en.get("symbol")
        st = en.get("strategy_name") or en.get("strategy_id") or "Breakout"
        p = en.get("entry_price")
        stop = en.get("current_stop")
        tgt = en.get("target_price")
        print(f"  🟢 ENTRY: {sym} | Strategy: {st} | Price: ₹{p} | Stop: ₹{stop} | Target: ₹{tgt}")

print("\n" + "=" * 70)
print("  >>> DAY 1: 2026-10-15 (THURSDAY) REPLAY <<<")
print("=" * 70)
res_v2_15 = process_daily("2026-10-15", dry_run=True, scan_buys=True)
print_result("QuantSync-V2", "2026-10-15", res_v2_15)

res_v3_15 = process_daily_v3("2026-10-15", dry_run=True, scan_buys=True)
print_result("QuantSync-V3", "2026-10-15", res_v3_15)

print("\n" + "=" * 70)
print("  >>> DAY 2: 2026-10-16 (FRIDAY) REPLAY <<<")
print("=" * 70)
res_v2_16 = process_daily("2026-10-16", dry_run=True, scan_buys=True)
print_result("QuantSync-V2", "2026-10-16", res_v2_16)

res_v3_16 = process_daily_v3("2026-10-16", dry_run=True, scan_buys=True)
print_result("QuantSync-V3", "2026-10-16", res_v3_16)

print("=" * 70 + "\n")
