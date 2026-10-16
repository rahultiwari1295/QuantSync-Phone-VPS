#!/usr/bin/env python3
"""
scripts/run_v3_2026_complete.py — Run QuantSync-V3 for Complete 2026 Data
========================================================================
Executes full chronological backtest of QuantSync-V3 governed by the
September 18th Frozen Quality Guards baseline across 2026-01-01 to 2026-09-18.
"""

import os
import sys
import json
import time
import datetime
import pandas as pd
import numpy as np

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from engines.v3.engine import V3TradingEngine


def main():
    print("═" * 84)
    print(" 🚀 QUANTSYNC-V3: COMPLETE 2026 CHRONOLOGICAL REPLAY (TILL TODAY)")
    print(" Baseline: September 18th Frozen Quality Guards Snapshot")
    print(" Execution Window: 2026-01-01 -> 2026-09-18")
    print("═" * 84, flush=True)

    t0 = time.time()
    engine = V3TradingEngine()
    engine.load_universe()
    load_time = time.time() - t0
    print(f"✅ Loaded {len(engine._universe_daily)} symbols into universe in {load_time:.2f}s.\n")

    start_date = "2026-01-01"
    end_date = "2026-09-18"

    t_run_start = time.time()
    res = engine.run(start_date=start_date, end_date=end_date, is_live=False)
    run_elapsed = time.time() - t_run_start

    # Export reports
    reports_dir = os.path.join(_PROD_ROOT, "reports")
    os.makedirs(reports_dir, exist_ok=True)
    engine.export_reports(output_dir=reports_dir, prefix="v3_2026_complete")

    nav_df = pd.DataFrame(engine.daily_nav_history)
    if not nav_df.empty:
        nav_df.to_csv(os.path.join(reports_dir, "v3_2026_complete_nav_history.csv"), index=False)

    # Detailed statistics
    closed = engine.closed_trades
    n_closed = len(closed)
    wins = [t for t in closed if t.get("net_pnl", 0) > 0]
    losses = [t for t in closed if t.get("net_pnl", 0) <= 0]
    gross_w = sum(t.get("net_pnl", 0) for t in wins)
    gross_l = abs(sum(t.get("net_pnl", 0) for t in losses))
    tot_friction = sum(t.get("total_friction", 0) for t in closed)
    net_pnl = sum(t.get("net_pnl", 0) for t in closed)
    wr = (len(wins) / n_closed * 100.0) if n_closed else 0.0
    pf = (gross_w / max(1e-4, gross_l)) if gross_l else 99.0
    avg_win = (gross_w / len(wins)) if wins else 0.0
    avg_loss = (gross_l / len(losses)) if losses else 0.0
    win_loss_ratio = (avg_win / max(1e-4, avg_loss)) if avg_loss else 0.0

    # Max Drawdown calculation
    max_dd_pct = 0.0
    if not nav_df.empty and "total_nav" in nav_df:
        nav_series = nav_df["total_nav"].values
        peak = np.maximum.accumulate(nav_series)
        dd = (peak - nav_series) / peak * 100.0
        max_dd_pct = float(np.max(dd))

    # Pattern breakdown
    pattern_counts = {}
    for t in closed:
        pid = t.get("strategy_id", "Unknown")
        pattern_counts[pid] = pattern_counts.get(pid, 0) + 1

    final_nav = engine.daily_nav_history[-1]["total_nav"] if engine.daily_nav_history else engine.initial_capital
    tot_return_pct = round((final_nav - engine.initial_capital) / engine.initial_capital * 100.0, 2)

    print("\n" + "═" * 84)
    print(" 📊 QUANTSYNC-V3 PERFORMANCE SUMMARY (2026 YTD COMPLETE)")
    print("═" * 84)
    print(f"  • Starting Capital:          ₹{engine.initial_capital:>12,.2f}")
    print(f"  • Ending NAV:                ₹{final_nav:>12,.2f}")
    print(f"  • Total Net Realized Return: {tot_return_pct:>12.2f}%")
    print(f"  • Net Realized PnL:          ₹{net_pnl:>12,.2f}")
    print(f"  • Total Friction (Costs):    ₹{tot_friction:>12,.2f}")
    print(f"  • Max Peak-to-Trough DD:     {max_dd_pct:>12.2f}%")
    print("─" * 84)
    print(f"  • Total Closed Trades:       {n_closed:>12d}")
    print(f"  • Winning Trades:            {len(wins):>12d} ({wr:.1f}%)")
    print(f"  • Losing Trades:             {len(losses):>12d} ({100.0 - wr:.1f}%)")
    print(f"  • Profit Factor:             {pf:>12.2f}")
    print(f"  • Win / Loss Ratio:          {win_loss_ratio:>12.2f}")
    print(f"  • Average Win:               ₹{avg_win:>12,.2f}")
    print(f"  • Average Loss:              ₹{avg_loss:>12,.2f}")
    print(f"  • Currently Open Positions:  {len(engine.active_positions):>12d}")
    print("─" * 84)
    print("  Pattern Distribution:")
    for pat, count in sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    - {pat:<16}: {count:>4d} trades")
    print("─" * 84)
    print(f"  • Replay Execution Time:     {run_elapsed:.2f}s ({len(engine.daily_nav_history)} market sessions)")
    print(f"  • Reports Saved To:          {reports_dir}/v3_2026_complete_closed_trades.csv")
    print("═" * 84 + "\n")

    # Output JSON summary for machine verification
    summary_out = {
        "status": "SUCCESS",
        "engine": "QuantSync-V3",
        "baseline": "state_20260918_v6_quality_guards",
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": engine.initial_capital,
        "final_nav": final_nav,
        "total_return_pct": tot_return_pct,
        "net_pnl": round(net_pnl, 2),
        "total_friction": round(tot_friction, 2),
        "trades_closed": n_closed,
        "win_rate": round(wr, 2),
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "active_open": len(engine.active_positions),
        "patterns": pattern_counts
    }
    with open(os.path.join(reports_dir, "v3_2026_complete_summary.json"), "w") as f:
        json.dump(summary_out, f, indent=2)


if __name__ == "__main__":
    main()
