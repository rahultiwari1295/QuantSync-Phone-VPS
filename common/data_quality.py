"""
common/data_quality.py — the quality gate between raw candles and every engine.

Nothing downstream (technicals, DNA, screener, Radar, V2/V3 settlement) should
compute on a session whose candles are not official. This module answers:
  * assess_session(): how much of the universe has an official, coherent,
    non-zero-volume bar for a date  → OK / DEGRADED / FAILED
  * symbol_flags(): per-symbol issues (unexplained price gaps, stale data,
    zero volume) so Radar/AI can quarantine a symbol instead of trusting it
  * write_status()/read_status(): Data/data_status.json, served by the API so
    the app can show "Data as of …" and an honest "under review" banner.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT, "Data")
HIST_DB = os.path.join(DATA_DIR, "stock_history.db")
STATUS_PATH = os.path.join(DATA_DIR, "data_status.json")

OK_COVERAGE = 0.90        # ≥90% of universe official → OK
FAILED_COVERAGE = 0.50    # <50% official → FAILED (do not trade / publish signals)
GAP_LIMIT = 0.40          # overnight open/close move beyond ±40% needs a corporate action


def bar_is_official(bar: Dict[str, Any]) -> bool:
    return bool(bar) and not bar.get("p") and (bar.get("v") or 0) > 0


def bar_is_coherent(bar: Dict[str, Any]) -> bool:
    try:
        o, h, l, c = float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"])
    except Exception:
        return False
    return min(o, h, l, c) > 0 and h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9 and h >= l


def repair_bar(bar: Dict[str, Any]) -> Dict[str, Any]:
    """Make high/low envelope the body (feed glitches leave high < close etc.)."""
    b = dict(bar)
    o, c = float(b["o"]), float(b["c"])
    b["h"] = max(float(b["h"]), o, c)
    b["l"] = min(float(b["l"]), o, c)
    return b


def unexplained_gaps(candles: List[Dict[str, Any]], explained_dates: Iterable[str] = (),
                     lookback: Optional[int] = None) -> List[Dict[str, Any]]:
    """Overnight moves beyond ±GAP_LIMIT without a known corporate action on that date."""
    explained = {str(d)[:10] for d in explained_dates}
    seq = candles[-(lookback + 1):] if lookback else candles
    out = []
    for a, b in zip(seq, seq[1:]):
        prev_c, op = float(a.get("c") or 0), float(b.get("o") or 0)
        if prev_c <= 0 or op <= 0:
            continue
        r = op / prev_c
        d = str(b["t"])[:10]
        if (r < 1 - GAP_LIMIT or r > 1 + GAP_LIMIT * 1.5) and d not in explained:
            out.append({"date": d, "prev_close": prev_c, "open": op, "ratio": round(r, 4)})
    return out


def symbol_flags(candles: List[Dict[str, Any]], session: str,
                 explained_dates: Iterable[str] = ()) -> Dict[str, Any]:
    """Per-symbol trust flags used to quarantine signals."""
    last = candles[-1] if candles else {}
    recent = candles[-20:]
    gaps = unexplained_gaps(candles, explained_dates, lookback=260)
    zero_vol = sum(1 for b in recent if not (b.get("v") or 0))
    flags = {
        "stale": (str(last.get("t", ""))[:10] < session) if last else True,
        "provisional_last_bar": bool(last.get("p")),
        "zero_volume_20d": zero_vol,
        "unexplained_gaps_1y": gaps,
        "incoherent_bars_20d": sum(1 for b in recent if not bar_is_coherent(b)),
    }
    flags["quarantine"] = bool(flags["stale"] or gaps or zero_vol >= 5 or flags["provisional_last_bar"])
    return flags


def assess_session(session: str, db_path: str = HIST_DB) -> Dict[str, Any]:
    """Coverage of official bars for `session` across the equity universe."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT symbol, rawCandles FROM StockHistory WHERE rawCandles IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    total = present = official = coherent = 0
    missing: List[str] = []
    for sym, raw in rows:
        try:
            cs = json.loads(raw)
        except Exception:
            continue
        if not cs:
            continue
        total += 1
        bar = next((b for b in reversed(cs[-40:]) if str(b.get("t", ""))[:10] == session), None)
        if bar is None:
            missing.append(sym)
            continue
        present += 1
        if bar_is_official(bar):
            official += 1
        if bar_is_coherent(bar):
            coherent += 1
    cov = official / total if total else 0.0
    status = "OK" if cov >= OK_COVERAGE else ("DEGRADED" if cov >= FAILED_COVERAGE else "FAILED")
    return {
        "session": session,
        "universe": total,
        "bars_present": present,
        "official_bars": official,
        "coherent_bars": coherent,
        "official_coverage": round(cov, 4),
        "status": status,
        "missing_sample": missing[:25],
    }


def write_status(payload: Dict[str, Any], path: str = STATUS_PATH) -> None:
    doc = dict(payload)
    doc["written_at"] = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30))).isoformat()
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, default=str)
    os.replace(tmp, path)


def read_status(path: str = STATUS_PATH) -> Dict[str, Any]:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {"status": "UNKNOWN"}
