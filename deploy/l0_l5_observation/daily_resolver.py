"""
deploy/l0_l5_observation/daily_resolver.py
==========================================
Run after market close each trading day.
1. Resolves any L0 or L5 predictions that reached their horizon.
2. Prints daily stats.
3. Appends summary to OBSERVATION_LOG.md
"""
import os
import sys
import sqlite3
from datetime import date
from pathlib import Path

# Paths
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PKG_ROOT = os.path.join(_REPO_ROOT, "QuantSync_Lines", "banana_engine")
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.paper_logger import resolve_pending_outcomes as l0_resolve
from deploy.l0_l5_observation.shadow_logger import (
    resolve_pending_outcomes as l5_resolve,
    get_shadow_stats as l5_stats,
)

LOG = Path(_REPO_ROOT) / 'deploy' / 'l0_l5_observation' / 'OBSERVATION_LOG.md'


def get_l0_stats():
    l0_db = Path(_PKG_ROOT) / 'data' / 'paper_trading.db'
    if not l0_db.exists():
        l0_db = Path(_REPO_ROOT) / 'QuantSync_Lines' / 'banana_engine' / 'data' / 'paper_trading.db'
    if l0_db.exists():
        conn = sqlite3.connect(l0_db)
        row = conn.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END),
                   AVG(CASE WHEN resolved = 1 THEN realized_r END),
                   SUM(CASE WHEN resolved = 1 AND realized_r > 0 THEN 1 ELSE 0 END) * 1.0
                       / NULLIF(SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END), 0)
            FROM paper_predictions
        """).fetchone()
        conn.close()
        return {
            'total': int(row[0] or 0),
            'resolved': int(row[1] or 0),
            'mean_r': float(row[2] or 0.0),
            'win_rate': float(row[3] or 0.0),
        }
    return {'total': 0, 'resolved': 0, 'mean_r': 0.0, 'win_rate': 0.0}


def main():
    today = date.today()
    l0_res = l0_resolve(horizon=20)
    l0_resolved = l0_res.get('resolved_count', 0) if isinstance(l0_res, dict) else l0_res
    l5_resolved = l5_resolve(horizon=40)
    ls0 = get_l0_stats()
    ls5 = l5_stats()

    entry = f"""
## {today.isoformat()}

**Resolved today:** L0={l0_resolved}, L5={l5_resolved}

| System | Total | Resolved | Win Rate | Mean R |
|---|---|---|---|---|
| L0 (production) | {ls0['total']} | {ls0['resolved']} | {ls0['win_rate']*100:.1f}% | {ls0['mean_r']:.3f} |
| L5 (shadow) | {ls5['total']} | {ls5['resolved']} | {ls5['win_rate']*100:.1f}% | {ls5['mean_r']:.3f} |
"""
    with open(LOG, 'a') as f:
        f.write(entry)
    print(entry)


if __name__ == '__main__':
    main()
