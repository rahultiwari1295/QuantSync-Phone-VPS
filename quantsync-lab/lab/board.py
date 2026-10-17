"""The Screens board: every ready-made screen worked out ahead of time and stored, so the app asks ONCE and gets all cards.

  python -m lab.cli board [--force]   (cron every 30 min; it does nothing unless the data changed)

screens_def.json is generated from the website (scripts/gen-screens-def.mjs), so the cards and the server always agree on
the rule behind each screen. Each screen is a scan of today's bar (milliseconds, from the snapshot) over ALL stocks."""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

from . import pipeline, store
from .config import Config
from .db import jdump, tx

DEF_PATH = Path(__file__).with_name("screens_def.json")
TOP = 8          # rows per card on the board
FULL = 300       # rows kept for a screen's own page


def definitions() -> list[dict]:
    return json.loads(DEF_PATH.read_text())


def _clean(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _signature(cfg: Config) -> str:
    h = hashlib.sha256(DEF_PATH.read_bytes())
    for tf in ("1D", "1W"):
        h.update(str((store.meta(cfg, tf) or {}).get("as_of")).encode())
    return h.hexdigest()[:16]


def build(cfg: Config, con, force: bool = False) -> dict:
    sig = _signature(cfg)
    cur = con.execute("SELECT sig, built_at FROM board_cache WHERE id=1").fetchone()
    if cur and cur["sig"] == sig and not force:
        return {"skipped": True, "reason": "data unchanged", "built_at": cur["built_at"]}
    t0, ok, failed, screens = time.time(), 0, [], {}
    now = time.time()
    as_of = {tf: (store.meta(cfg, tf) or {}).get("as_of") for tf in ("1D", "1W")}
    for d in definitions():
        try:
            r = pipeline.scan(cfg, d["rule"], d["tf"], limit=FULL, extra=[d["col"]])
            rows = [{"symbol": x["symbol"], "close": _clean(x.get("close")), "ret_1": _clean(x.get("ret_1")), "v": _clean(x.get(d["col"]))} for x in r["rows"]]
            rows.sort(key=lambda x: -(abs(x["v"]) if isinstance(x["v"], (int, float)) else -1))
            n, err = int(r["count"]), None
        except Exception as e:  # noqa: BLE001 - a screen that needs data we do not have yet must not break the others
            rows, n, err = [], 0, f"{type(e).__name__}: {str(e)[:160]}"
            failed.append(d["id"])
        ok += err is None
        with tx(con):
            con.execute("INSERT OR REPLACE INTO screen_results(screen_id,tf,as_of,n,rows_json,error,built_at) VALUES(?,?,?,?,?,?,?)",
                        (d["id"], d["tf"], as_of.get(d["tf"]), n, jdump(rows), err, now))
        screens[d["id"]] = {"count": n, "rows": rows[:TOP]} | ({"pending": True} if err else {})
    body = {"as_of": as_of["1D"], "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "screens": screens}
    raw = jdump(body)
    etag = hashlib.md5(raw.encode()).hexdigest()
    with tx(con):
        con.execute("INSERT OR REPLACE INTO board_cache(id,sig,etag,built_at,body_json) VALUES(1,?,?,?,?)", (sig, etag, now, raw))
    return {"screens": len(screens), "ok": ok, "failed": failed, "seconds": round(time.time() - t0, 1), "bytes": len(raw)}


def load(con) -> dict | None:
    r = con.execute("SELECT etag, built_at, body_json FROM board_cache WHERE id=1").fetchone()
    return None if not r else {"etag": r["etag"], "built_at": r["built_at"], "body": r["body_json"]}


def one(con, screen_id: str) -> dict | None:
    r = con.execute("SELECT * FROM screen_results WHERE screen_id=?", (screen_id,)).fetchone()
    if not r:
        return None
    return {"id": r["screen_id"], "as_of": r["as_of"], "count": r["n"], "rows": json.loads(r["rows_json"]), "pending": bool(r["error"]), "built_at": r["built_at"]}
