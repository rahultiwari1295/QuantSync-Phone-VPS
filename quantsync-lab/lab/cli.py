"""python -m lab.cli cook|live|snapshot|universe|board|library|build-flagships|backtest-all|seed-flagship ..."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import db, service, store
from .config import load_config


def main(argv=None, cfg=None):
    ap = argparse.ArgumentParser(prog="qslab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cook"); c.add_argument("--tf", choices=["1D", "1W", "all"], default="all")
    sub.add_parser("live")
    sn = sub.add_parser("snapshot", help="rebuild the one-file latest-bars snapshot that makes scans instant"); sn.add_argument("--tf", choices=["1D", "1W", "all"], default="all")
    sub.add_parser("universe", help="re-pick the ~500 liquid stocks that get past tests and strategy tracking (monthly)")
    bd = sub.add_parser("board", help="work out every ready-made screen for the app (cheap: does nothing if the data has not changed)"); bd.add_argument("--force", action="store_true")
    sub.add_parser("library", help="create/refresh the public strategy library and run every past test")
    sub.add_parser("backtest-all", help="backtest every flagship + public strategy (cached for the UI)")
    b = sub.add_parser("build-flagships", help="create AI Alpha + AI Pattern, run past tests and the AI score gate"); b.add_argument("--no-ai", action="store_true")
    s = sub.add_parser("seed-flagship"); s.add_argument("name"); s.add_argument("--tf", default="1D"); s.add_argument("file", help="JSON with entry/exit")
    aic = sub.add_parser("ai-check", help="run or refresh the 3-stage AI check for library/flagship strategies")
    aic.add_argument("--all", action="store_true", help="check all strategies regardless of staleness")
    aic.add_argument("--stale", action="store_true", default=False, help="check only strategies with missing or stale checks")
    aic.add_argument("--rescore", action="store_true", help="rescore blind year only with stored model (no retraining)")
    aic.add_argument("--name", type=str, default=None, help="filter by strategy name")
    a = ap.parse_args(argv)
    cfg = cfg or load_config(); db.migrate(cfg)
    run = cfg.run_dir; run.mkdir(parents=True, exist_ok=True)
    if a.cmd == "cook":
        lock = run / "cook.lock"; lock.write_text(str(time.time()))
        try:
            for tf in (["1D", "1W"] if a.tf == "all" else [a.tf]):
                print(json.dumps(store.cook(cfg, tf)))
        finally:
            lock.unlink(missing_ok=True)
        con = db.connect(cfg)
        from . import board, universe
        if not con.execute("SELECT 1 FROM universe LIMIT 1").fetchone():
            print("universe:", json.dumps(universe.build(cfg, con)))
        print("live rows:", service.update_live(cfg, con))
        print("board:", json.dumps(board.build(cfg, con, force=True)))
    elif a.cmd == "snapshot":
        for tf in (["1D", "1W"] if a.tf == "all" else [a.tf]):
            print(json.dumps(store.build_snapshot(cfg, tf)))
            if tf == "1D":
                print(json.dumps(store.build_xsec(cfg, tf)))
    elif a.cmd == "universe":
        from . import universe
        print(json.dumps(universe.build(cfg, db.connect(cfg))))
    elif a.cmd == "board":
        from . import board
        print(json.dumps(board.build(cfg, db.connect(cfg), force=a.force)))
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
    elif a.cmd == "ai-check":
        con = db.connect(cfg)
        from . import jobs
        q_rows = con.execute("""
            SELECT s.id as strategy_id, s.name, s.tf, s.kind, v.id as version_id
            FROM strategies s
            JOIN strategy_versions v ON v.id = s.current_version
            WHERE s.archived = 0 AND (s.kind IN ('flagship', 'system', 'library') OR s.visibility = 'public' OR s.owner_uid IS NULL)
        """).fetchall()

        if a.name:
            q_rows = [r for r in q_rows if a.name.lower() in r["name"].lower()]

        queued_count = 0
        for r in q_rows:
            tf = r["tf"]
            cur_as_of = (store.meta(cfg, tf) or {}).get("as_of", "")[:7]

            if a.rescore:
                jid = jobs.enqueue(con, "ai_check", None, {
                    "strategy_id": r["strategy_id"], "version_id": r["version_id"], "rescore_only": True
                }, queue_cap=10_000)
                queued_count += 1
                print(f"Queued rescore for {r['name']} ({jid})")
                continue

            if a.all:
                jid = jobs.enqueue(con, "ai_check", None, {
                    "strategy_id": r["strategy_id"], "version_id": r["version_id"]
                }, queue_cap=10_000)
                queued_count += 1
                print(f"Queued ai_check for {r['name']} ({jid})")
                continue

            # Default: stale check
            enh = con.execute("""SELECT id, status, protocol, as_of, verdict FROM ai_enhancements 
                                 WHERE version_id=? AND kind='system' ORDER BY created_at DESC LIMIT 1""", 
                              (r["version_id"],)).fetchone()
            is_stale = (
                enh is None or
                enh["status"] != "done" or
                enh["protocol"] != "v2" or
                (enh["as_of"] or "")[:7] != cur_as_of
            )
            if is_stale:
                existing_job = con.execute("""SELECT id FROM jobs WHERE kind='ai_check' 
                                              AND status IN ('queued', 'running') 
                                              AND json_extract(payload_json, '$.version_id')=?""",
                                           (r["version_id"],)).fetchone()
                if not existing_job:
                    jid = jobs.enqueue(con, "ai_check", None, {
                        "strategy_id": r["strategy_id"], "version_id": r["version_id"]
                    }, queue_cap=10_000)
                    queued_count += 1
                    print(f"Queued ai_check for {r['name']} ({jid})")
        print(f"ai-check: {queued_count} jobs queued.")
        return 0
    else:
        d = json.load(open(a.file)); con = db.connect(cfg)
        print(service.create_strategy(con, cfg, None, a.name, a.tf, d["entry"], d.get("exit"), kind="flagship", visibility="public"))


if __name__ == "__main__":
    sys.exit(main())
