"""
scripts/daily_paper_resolver.py — Daily Paper-Trading Resolution Job
===================================================================
Resolves mature predictions that reached their 20-day horizon or experienced delisting,
computes updated performance statistics, and logs the execution summary to data/paper_trading.log.
"""

import os
import sys
from datetime import datetime

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR
from core.paper_logger import resolve_pending_outcomes, get_prediction_stats

LOG_PATH = os.path.join(DATA_DIR, "paper_trading.log")


def main():
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"=== Running Daily Paper Trading Resolver [{now_str}] ===")

    # Resolve pending outcomes
    res = resolve_pending_outcomes(horizon=20)
    stats = get_prediction_stats()

    resolved_this_run = res.get("resolved_count", 0)
    pending_count = res.get("remaining_pending", 0)
    total_preds = stats.get("total_predictions", 0)
    total_resolved = stats.get("resolved_count", 0)

    # Average realized R across all resolved predictions
    mean_r_str = "N/A"
    all_mean_r = []
    for tier, s in stats.get("per_tier", {}).items():
        if s.get("count", 0) > 0:
            all_mean_r.append(s.get("mean_r", 0.0))
    if all_mean_r:
        mean_r_str = f"{sum(all_mean_r) / len(all_mean_r):+.2f}R"

    summary_line = (
        f"[{now_str}] Resolved this run: {resolved_this_run} | "
        f"Remaining pending: {pending_count} | "
        f"Total resolved: {total_resolved}/{total_preds} | "
        f"Avg realized R: {mean_r_str}"
    )

    print(summary_line)

    # Print tier breakdown
    print("\n--- Current Tier Performance Summary ---")
    per_tier = stats.get("per_tier", {})
    if per_tier:
        for tier, t_stats in per_tier.items():
            print(f"  {tier:<10}: Count={t_stats['count']}, WinRate={t_stats['win_rate']}%, MeanR={t_stats['mean_r']:+.2f}R, Expectancy={t_stats['expectancy']:+.2f}R")
    else:
        print("  No predictions resolved yet.")

    # Append to data/paper_trading.log
    with open(LOG_PATH, "a") as f:
        f.write(summary_line + "\n")
        if per_tier:
            for tier, t_stats in per_tier.items():
                f.write(f"    {tier}: {t_stats}\n")

    print(f"\nLogged execution summary to {LOG_PATH}")


if __name__ == "__main__":
    main()
