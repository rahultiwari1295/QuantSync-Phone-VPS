"""python -m intel.run <job>   (run from the VPS root, e.g. /root/quantsync)

  filings   NSE + BSE announcements            every 5 min, 08:00-20:00
  meetings  board meeting calendar             hourly
  insider   insider trades, SAST, pledges      every 30 min, 09:00-21:00
  flows     bulk/block deals, FII/DII          18:30 and 20:30
  world     global markets + news + policy     hourly, and inside the briefs
  markets   index, commodity and FX tiles + sparklines   every 15 min
  home      News Today page (mood, top stories, sections)  every 15 min
  read      filing reader (local model)        every 20 min
  fields    company + ownership facts          16:45 daily (before the Lab cook)
  morning   full sweep + morning brief         07:00
  evening   full sweep + evening brief         17:00
  all       every collector once
"""
from __future__ import annotations

import fcntl
import json
import os
import sys

from . import brief, collect, db, fields, reader

LOCK_DIR = os.environ.get("QS_LOCK_DIR", "/tmp")


def _locked(name: str):
    """One instance per job: cron overlap on a slow network never stacks runs."""
    f = open(os.path.join(LOCK_DIR, f"qs_intel_{name}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        return None


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    job = argv[0] if argv else "all"
    lock = _locked(job)
    if lock is None:
        print(f"{job}: already running"); return 0
    db.migrate()
    if job in collect.JOBS:
        out = collect.run([job])
    elif job == "all":
        out = collect.run()
    elif job == "read":
        out = reader.run(budget_s=int(os.environ.get("QS_READ_BUDGET", "1100")))
    elif job == "home":
        from . import home
        out = home.run()
    elif job == "fields":
        out = fields.run()
    elif job in ("morning", "evening"):
        groups = ["world", "filings", "meetings", "insider"] + (["flows"] if job == "evening" else [])
        out = {"collect": collect.run(groups), "brief": brief.run(job)}
        from . import home
        out["home"] = home.run()
    else:
        print(__doc__); return 2
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
