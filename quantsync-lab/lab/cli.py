"""python -m lab.cli cook|live|build-flagships|backtest-all|seed-flagship ..."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import db, service, store
from .config import load_config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="qslab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cook"); c.add_argument("--tf", choices=["1D", "1W", "all"], default="all")
    sub.add_parser("live")
    sub.add_parser("library", help="create/refresh the public strategy library and run every past test")
    sub.add_parser("backtest-all", help="backtest every flagship + public strategy (cached for the UI)")
    b = sub.add_parser("build-flagships", help="create AI Alpha + AI Pattern, run past tests and the AI score gate"); b.add_argument("--no-ai", action="store_true")
    s = sub.add_parser("seed-flagship"); s.add_argument("name"); s.add_argument("--tf", default="1D"); s.add_argument("file", help="JSON with entry/exit")
    a = ap.parse_args(argv)
    cfg = load_config(); db.migrate(cfg)
    run = cfg.run_dir; run.mkdir(parents=True, exist_ok=True)
    if a.cmd == "cook":
        lock = run / "cook.lock"; lock.write_text(str(time.time()))
        try:
            for tf in (["1D", "1W"] if a.tf == "all" else [a.tf]):
                print(json.dumps(store.cook(cfg, tf)))
        finally:
            lock.unlink(missing_ok=True)
        con = db.connect(cfg); print("live rows:", service.update_live(cfg, con))
    elif a.cmd == "live":
        print("live rows:", service.update_live(cfg, db.connect(cfg)))
    elif a.cmd == "library":
        from . import library
        res = library.build(cfg, db.connect(cfg))
        bad = [r for r in res if not r["ok"]]
        print(f"library: {len(res) - len(bad)} ok, {len(bad)} failed")
        return 1 if bad else 0
    elif a.cmd == "build-flagships":
        from . import flagship
        con = db.connect(cfg)
        for name in flagship.FLAGSHIPS:
            print(json.dumps(flagship.build(cfg, con, name, ai=not a.no_ai), default=str))
    elif a.cmd == "backtest-all":
        con = db.connect(cfg)
        for r in con.execute("SELECT s.name, s.tf, v.id vid, v.entry_json, v.exit_json FROM strategies s JOIN strategy_versions v ON v.id=s.current_version "
                             "WHERE s.archived=0 AND (s.kind='flagship' OR s.visibility='public')").fetchall():
            res = service.run_backtest(cfg, con, json.loads(r["entry_json"]), json.loads(r["exit_json"]), r["tf"], r["vid"])
            print(r["name"], json.dumps(res["summary"].get("All")))
    else:
        d = json.load(open(a.file)); con = db.connect(cfg)
        print(service.create_strategy(con, cfg, None, a.name, a.tf, d["entry"], d.get("exit"), kind="flagship", visibility="public"))


if __name__ == "__main__":
    sys.exit(main())
