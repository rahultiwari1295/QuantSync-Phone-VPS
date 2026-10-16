"""
snapshot_manager.py — Ultra-Low-Latency RAM & DB Snapshot Manager for QuantSync
================================================================================
Architecture:
1. Zero-Compute HTTP Reads:
   Endpoints do ZERO calculations and ZERO database joins on user request.
   They read pre-rendered, minified JSON directly from RAM (<0.5ms).

2. Multi-Tier Cache Hierarchy:
   - Tier 0: Python In-Memory String Cache (_MEMORY_CACHE)  -> 0.001ms
   - Tier 1: Linux Shared Memory RAM Disk (/dev/shm/quantsync/) -> 0.1ms (Nginx bypass)
   - Tier 2: Persistent SQLite DB (quantsync_master.db -> ui_snapshots) -> 1ms
   - Tier 3: Asynchronous Background Generation Engine

3. Thread-Safe Atomic Writes:
   Uses temporary files and atomic `os.replace` so concurrent readers (Nginx / Python)
   never encounter partial or corrupt JSON payloads.
"""

import os
import sys
import json
import time
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("QuantSync_SnapshotManager")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "Data")
MASTER_DB_PATH = os.path.join(DATA_DIR, "quantsync_master.db")

# Detect Linux shared memory RAM disk (/dev/shm) or fall back to local disk
if os.path.exists("/dev/shm") and os.access("/dev/shm", os.W_OK):
    RAM_DIR = "/dev/shm/quantsync"
else:
    RAM_DIR = os.path.join(DATA_DIR, "ram_cache")

os.makedirs(RAM_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# In-memory fast string cache: key -> {"json_str": str, "updated_at": float, "dict_obj": Optional[dict]}
_MEMORY_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOCK = threading.RLock()

# Background worker handle
_REFRESHER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()


def init_snapshots_table():
    """Ensures ui_snapshots table exists in master SQLite DB."""
    try:
        conn = sqlite3.connect(MASTER_DB_PATH, timeout=10.0)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ui_snapshots (
                snapshot_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_hash TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error initializing ui_snapshots table: {e}")


def get_ram_filepath(key: str) -> str:
    """Returns absolute path to the RAM-cached JSON file."""
    return os.path.join(RAM_DIR, f"{key}.json")


def save_snapshot(key: str, data: Any, persist_db: bool = True) -> str:
    """
    Serializes data to minified JSON, caches it in Python memory,
    writes atomically to /dev/shm for Nginx, and persists to SQLite.
    Returns the JSON string.
    """
    if isinstance(data, str):
        json_str = data
        try:
            dict_obj = json.loads(data)
        except Exception:
            dict_obj = None
    else:
        dict_obj = data
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)

    now = time.time()

    # 1. Update Python in-memory cache
    with _CACHE_LOCK:
        _MEMORY_CACHE[key] = {
            "json_str": json_str,
            "dict_obj": dict_obj,
            "updated_at": now
        }

    # 2. Write atomically to RAM disk (/dev/shm) for instant Nginx sendfile serving
    try:
        target_path = get_ram_filepath(key)
        temp_path = f"{target_path}.tmp_{os.getpid()}_{int(now*1000)}"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(json_str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
    except Exception as e:
        logger.warning(f"Failed to write snapshot to RAM disk {target_path}: {e}")

    # 3. Persist to SQLite ui_snapshots table
    if persist_db:
        try:
            conn = sqlite3.connect(MASTER_DB_PATH, timeout=10.0)
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO ui_snapshots (snapshot_key, payload, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (key, json_str))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to save snapshot '{key}' to SQLite ui_snapshots: {e}")

    return json_str


def get_snapshot_json(key: str) -> Optional[str]:
    """
    Ultra-fast retrieval of pre-rendered raw JSON string:
    Tier 0: Python RAM cache (< 0.001ms)
    Tier 1: Linux /dev/shm RAM file (< 0.1ms)
    Tier 2: SQLite ui_snapshots table (< 1ms)
    """
    # Tier 0: Python Memory
    with _CACHE_LOCK:
        cached = _MEMORY_CACHE.get(key)
        if cached and cached.get("json_str"):
            return cached["json_str"]

    # Tier 1: RAM Disk file
    ram_path = get_ram_filepath(key)
    if os.path.exists(ram_path):
        try:
            with open(ram_path, "r", encoding="utf-8") as f:
                content = f.read()
            if content:
                with _CACHE_LOCK:
                    _MEMORY_CACHE[key] = {
                        "json_str": content,
                        "dict_obj": None,
                        "updated_at": time.time()
                    }
                return content
        except Exception as e:
            logger.debug(f"Error reading RAM file {ram_path}: {e}")

    # Tier 2: SQLite ui_snapshots
    try:
        conn = sqlite3.connect(MASTER_DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT payload FROM ui_snapshots WHERE snapshot_key = ? LIMIT 1", (key,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            content = row[0]
            with _CACHE_LOCK:
                _MEMORY_CACHE[key] = {
                    "json_str": content,
                    "dict_obj": None,
                    "updated_at": time.time()
                }
            # Also restore RAM file
            try:
                with open(ram_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception:
                pass
            return content
    except Exception as e:
        logger.debug(f"Error reading ui_snapshots for {key}: {e}")

    return None


def get_snapshot_dict(key: str) -> Optional[dict]:
    """Returns parsed dictionary of the snapshot for endpoints that need filtering."""
    with _CACHE_LOCK:
        cached = _MEMORY_CACHE.get(key)
        if cached and cached.get("dict_obj") is not None:
            return cached["dict_obj"]

    raw = get_snapshot_json(key)
    if not raw:
        return None

    try:
        data = json.loads(raw)
        with _CACHE_LOCK:
            if key in _MEMORY_CACHE:
                _MEMORY_CACHE[key]["dict_obj"] = data
            else:
                _MEMORY_CACHE[key] = {
                    "json_str": raw,
                    "dict_obj": data,
                    "updated_at": time.time()
                }
        return data
    except Exception as e:
        logger.warning(f"Error parsing JSON snapshot for {key}: {e}")
        return None


def _universe_count() -> Optional[int]:
    try:
        import sqlite3 as _sq
        c = _sq.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "Data", "stock_history.db"))
        n = c.execute("SELECT COUNT(*) FROM StockHistory WHERE rawCandles IS NOT NULL").fetchone()[0]
        c.close()
        return n
    except Exception:
        return None


def _track_record(storage) -> dict:
    try:
        from common.ledger_truth import track_record
        return track_record(storage.get_closed_trades())
    except Exception as e:
        logger.warning(f"track record unavailable: {e}")
        return {}


def _data_status() -> dict:
    try:
        from common.data_quality import read_status
        return read_status()
    except Exception:
        return {"status": "UNKNOWN"}


def generate_terminal_v2_snapshot(storage) -> str:
    """
    Computes the complete, canonical V2 + V3 Unified AI Terminal snapshot once and caches it.
    Seamlessly merges V2 Institutional Ensemble and V3 Banana Sniper signals.
    """
    from common.wix_sync import format_active_v2, format_closed_v2, format_active_v3, format_closed_v3

    t0 = time.time()
    # 1. Fetch active trades from both V2 and V3
    active_v2_raw = storage.get_active_trades("v2")
    active_v3_raw = storage.get_active_trades("v3")
    
    active_v2 = [format_active_v2(t) for t in active_v2_raw]
    active_v3 = [format_active_v3(t) for t in active_v3_raw]
    for t in active_v2:
        t["engine"] = "v2"
        t["engineBadge"] = "AI PRO (V2)"
    for t in active_v3:
        t["engine"] = "v3"
        t["engineBadge"] = "AI SNIPER (V3)"

    # Merge active de-duplicated by symbol (V3 priority for precision)
    seen_active_syms = set()
    active = []
    for t in (active_v3 + active_v2):
        sym = (t.get("sym") or t.get("symbol") or "").upper().strip()
        if sym and sym not in seen_active_syms:
            seen_active_syms.add(sym)
            active.append(t)
    active.sort(key=lambda x: str(x.get("entryDate") or ""), reverse=True)

    # 2. Fetch closed trades from both V2 and V3
    closed_v2_raw = storage.get_closed_trades("v2")
    closed_v3_raw = storage.get_closed_trades("v3")
    closed_v2 = [format_closed_v2(t) for t in closed_v2_raw]
    closed_v3 = [format_closed_v3(t) for t in closed_v3_raw]
    from common.ledger_truth import origin_of as _origin_of
    for t, raw in zip(closed_v2, closed_v2_raw):
        t["engine"] = "v2"
        t["engineBadge"] = "AI PRO (V2)"
        t["origin"] = raw.get("origin") or _origin_of(raw)
        t["fillCheck"] = raw.get("fill_check")
    for t, raw in zip(closed_v3, closed_v3_raw):
        t["engine"] = "v3"
        t["engineBadge"] = "AI SNIPER (V3)"
        t["origin"] = raw.get("origin") or _origin_of(raw)
        t["fillCheck"] = raw.get("fill_check")
    
    closed = sorted(closed_v3 + closed_v2, key=lambda x: str(x.get("exitDate") or x.get("entryDate") or ""), reverse=True)

    # 3. Combine NAV and Cash
    v2_nav = storage.get_latest_nav("v2") or {}
    v3_nav = storage.get_latest_nav("v3") or {}
    cash = round(float(v2_nav.get("cash", 300000.0)) + float(v3_nav.get("cash", 300000.0)), 2)
    nav = round(float(v2_nav.get("total_nav", 300000.0)) + float(v3_nav.get("total_nav", 300000.0)), 2)
    base_capital = 600000.0

    to_buy = [t for t in active if t.get("action") in ("BUY_NOW", "BUY", "WAIT")]

    closed_count = len(closed)
    wins = [t for t in closed if t.get("realizedPnl", t.get("roiPct", 0)) > 0]
    losses = [t for t in closed if t.get("realizedPnl", t.get("roiPct", 0)) <= 0]
    wr = round(len(wins) / closed_count * 100.0, 1) if closed_count else None

    gross_win = sum(t.get("realizedPnl", 0.0) for t in wins)
    gross_loss = abs(sum(t.get("realizedPnl", 0.0) for t in losses))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None  # never a placeholder

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

    nav_history_v2 = storage.get_nav_history("v2", days=40)
    nav_history_v3 = storage.get_nav_history("v3", days=40)
    v2_series = nav_history_v2.get("series", [])
    v3_series = nav_history_v3.get("series", [])
    spark = []
    min_len = min(len(v2_series), len(v3_series))
    if min_len > 0:
        for idx in range(-min_len, 0):
            spark.append(round(v2_series[idx]["nav"] + v3_series[idx]["nav"], 2))
    elif v2_series:
        spark = [round(p["nav"] * 2, 2) for p in v2_series]
    if not spark:
        spark = [nav] * 40

    today_pnl = sum(t.get("pnl", 0.0) for t in active if t.get("days", 0) <= 1)
    if not today_pnl and active:
        today_pnl = active[0].get("pnl", 0.0)
    today_pnl = round(today_pnl, 2)
    today_pnl_pct = round((today_pnl / nav * 100.0) if nav else 0.0, 2)

    hero = {
        "nav": round(nav, 2),
        "todayPnl": today_pnl,
        "todayPnlPct": today_pnl_pct,
        "winRate": wr,
        "wins": len(wins),
        "losses": len(losses),
        "navSparkline": spark[-40:]
    }

    streak = storage.get_streaks("v2")
    scanner_report = storage.get_scanner_report("v2")  # None when no scan ran — never demo data

    closed_pnl_total = round(sum(t.get("pnl", t.get("realizedPnl", 0.0)) for t in closed), 2)
    avg_r = round(sum(t.get("r", 0.0) for t in closed) / max(1, len(closed)), 2)

    closed_summary = {
        "totalPnl": closed_pnl_total,
        "winRate": wr,
        "wins": len(wins),
        "losses": len(losses),
        "avgR": avg_r,
        "count": closed_count
    }

    total_summary = {
        "totalTrades": closed_count,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": wr,
        "profitFactor": pf,
        "totalReturnPct": round((nav - base_capital) / base_capital * 100.0, 2),
        "allTimeTradesClosed": closed_count,
    }

    kpis = {
        "totalStocks": _universe_count(),
        "activeCount": len(active),
        "totalTradesClosed": closed_count,
        "totalTrades": closed_count,
        "wins": len(wins),
        "losses": len(losses),
        "winRate": wr,
        "profitFactor": pf,
        "totalReturnPct": round((nav - base_capital) / base_capital * 100.0, 2),
        "allTimeTradesClosed": closed_count,
    }

    result = {
        "system": "QuantSync-AI (V2+V3 Ensemble)",
        "hero": hero,
        "streak": streak,
        "scanner": scanner_report,
        "closedSummary": closed_summary,
        "active": active,
        "closed": closed,
        "inTradeTrades": active,
        "closedTrades": closed,
        "toBuySignals": to_buy,
        "portfolio": active,
        "totalSummary": total_summary,
        "kpis": kpis,
        "strategySummary": [],
        "trackRecord": _track_record(storage),
        "dataStatus": _data_status(),
        "activeCount": len(active),
        "closedCount": closed_count,
        "winRate": wr,
        "profitFactor": pf,
        "totalReturnPct": round((nav - base_capital) / base_capital * 100.0, 2),
        "slotsUsed": len(active),
        "slotsMax": 25,
        "availableCash": round(cash, 2),
        "portfolioValue": round(nav, 2),
        "updatedAt": now_str,
        "status": "LIVE_TRADING_ACTIVE",
        "message": "QuantSync Zero-Compute RAM Snapshot (V2)",
        "source": "quantsync-ram-snapshot"
    }

    json_str = save_snapshot("terminal_v2", result, persist_db=True)
    logger.info(f"Terminal V2 snapshot generated & cached in {round((time.time() - t0)*1000, 1)}ms (size: {len(json_str)} bytes)")
    return json_str


def get_v3_screener_candidates(target_timeframe: Optional[str] = None, target_stage: Optional[str] = None) -> list:
    """
    Returns weekly/monthly multi-year pattern breakout setups from v3_pattern_candidates,
    enriched with fundamental and technical metrics from stock_screener_rows.
    """
    master_db = os.path.join(DATA_DIR, "quantsync_master.db")
    cache_db = os.path.join(DATA_DIR, "stock_dna_cache.db")
    if not os.path.exists(master_db) or not os.path.exists(cache_db):
        return []

    try:
        m_conn = sqlite3.connect(master_db)
        m_conn.row_factory = sqlite3.Row
        c_conn = sqlite3.connect(cache_db)
        c_conn.row_factory = sqlite3.Row

        m_cur = m_conn.cursor()
        c_cur = c_conn.cursor()

        latest_date_row = m_cur.execute("SELECT MAX(date) FROM v3_pattern_candidates").fetchone()
        latest_date = latest_date_row[0] if latest_date_row and latest_date_row[0] else None

        if latest_date:
            if target_timeframe and target_timeframe.upper() in ('DAILY', 'WEEKLY', 'MONTHLY'):
                q = "SELECT * FROM v3_pattern_candidates WHERE date = ? AND timeframe = ? ORDER BY score DESC"
                params = [latest_date, target_timeframe.upper()]
            else:
                q = "SELECT * FROM v3_pattern_candidates WHERE date = ? ORDER BY score DESC"
                params = [latest_date]
        else:
            if target_timeframe and target_timeframe.upper() in ('DAILY', 'WEEKLY', 'MONTHLY'):
                q = "SELECT * FROM v3_pattern_candidates WHERE timeframe = ? ORDER BY score DESC"
                params = [target_timeframe.upper()]
            else:
                q = "SELECT * FROM v3_pattern_candidates ORDER BY score DESC"
                params = []

        m_cur.execute(q, params)
        v3_rows = m_cur.fetchall()

        enriched = []
        seen_keys = set()
        for r in v3_rows:
            sym = r["symbol"]
            tf = (r["timeframe"] or "DAILY").upper()
            key = (sym, tf)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            c_cur.execute("SELECT * FROM stock_screener_rows WHERE symbol = ? ORDER BY scan_date DESC LIMIT 1", (sym,))  # latest, not oldest
            meta = c_cur.fetchone()
            meta = dict(meta) if meta else {}

            cmp = float(r["current_price"] or meta.get("curr_price") or 0)
            pvt = float(r["pivot_price"] or meta.get("breakout_level") or 0)
            tf = (r["timeframe"] or "DAILY").upper()
            base_yrs = float(r["base_duration_years"] or (0.3 if tf == "DAILY" else (1.5 if tf == "WEEKLY" else 3.0)))

            if pvt <= 0 or cmp <= 0:
                continue

            # State comes from common.trade_rules (stored by sync_v3_pattern_candidates). The old price-only buckets
            # (BUY_ZONE / RETEST / "SUBMERGED but counted as a confirmed breakout") are gone: a stock 6% below its
            # pivot is not a breakout, and a decisive close is judged on the close, not on how close price is to the pivot.
            keys = r.keys()
            t_state = r["trade_state"] if "trade_state" in keys else None
            t_action = r["trade_action"] if "trade_action" in keys else None
            if not t_state:
                continue                                    # rows from before the rules existed are stale by definition
            if target_timeframe is None and "is_primary" in keys and r["is_primary"] == 0:
                continue                                    # one verdict per stock; other timeframes are context
            try:
                t_call = json.loads(r["trade_json"]) if ("trade_json" in keys and r["trade_json"]) else {}
            except ValueError:
                t_call = {}
            diff_from_pivot_pct = ((cmp - pvt) / pvt) * 100.0
            grade_txt = t_call.get("grade") or ""
            if t_action == "BUY":
                execution_state = "RETEST_BUY" if t_state == "RETEST_HOLD" else "BUY_ZONE"
                action_badge_text = f"🟢 {'Retest' if t_state == 'RETEST_HOLD' else 'Breakout'} · grade {grade_txt}"
                is_success, is_near = True, False
            elif t_state == "RETEST_WATCH":
                execution_state = "RETEST"
                action_badge_text = "🔄 Back at the broken level — wait for a green close above"
                is_success, is_near = False, False
            elif t_state in ("NEAR", "ATTEMPT"):
                execution_state = "NEAR"
                action_badge_text = f"⚡ {abs(diff_from_pivot_pct):.1f}% from the level" if t_state == "NEAR" else "⚡ Tried the level, closed below"
                is_success, is_near = False, True
            elif t_state in ("BREAKOUT", "RETEST_HOLD"):
                execution_state = "UNPROVEN"
                action_badge_text = "⚪ Breakout with no proven edge — watch only"
                is_success, is_near = False, False
            else:
                execution_state = "FORMING"
                action_badge_text = "Base forming"
                is_success, is_near = False, False

            # Stage filtering
            if target_stage == "SUCCESS" and not is_success:
                continue
            if target_stage == "NEAR_BREAKOUT" and not is_near:
                continue
            if target_stage == "RETEST" and execution_state != "RETEST":
                continue

            tgt = float(r["target_1"]) if ("target_1" in r.keys() and r["target_1"]) else round(pvt * (1.15 if tf == "DAILY" else (1.25 if tf == "WEEKLY" else 1.45)), 2)
            sl = float(r["stop_loss"]) if ("stop_loss" in r.keys() and r["stop_loss"]) else round(pvt * (0.95 if tf == "DAILY" else (0.92 if tf == "WEEKLY" else 0.88)), 2)

            row = {
                "id": meta.get("id", 900000 + len(enriched)),
                "symbol": sym,
                "company_name": meta.get("company_name") or sym,
                "sector": meta.get("sector"),
                "industry": meta.get("industry"),
                "mcap_tier": meta.get("mcap_tier"),
                "curr_price": cmp,
                "breakout_level": pvt,
                "pattern_type": r["pattern_name"],
                "pattern_stage": "SUCCESS" if is_success else ("NEAR_BREAKOUT" if is_near else "FORMING"),
                "breakout_status": "SUCCESS" if is_success else ("PENDING" if is_near else "FORMING"),
                "trade_state": t_state, "trade_action": t_action, "trade_grade": grade_txt or None, "trade_call": t_call,
                "execution_state": execution_state,
                "action_badge_text": action_badge_text,
                "diff_from_pivot_pct": round(diff_from_pivot_pct, 2),
                "days_since_breakout": t_call.get("event_age") if is_success else 0,
                "timeframe": tf,
                "base_duration_years": base_yrs,
                "dna_score": meta.get("dna_score"),             # the DNA engine's score, not the pattern score
                "pattern_score": r["score"],
                # stance/action come from the DNA engine (one verdict per stock); the pattern state is kept separately
                "stance": meta.get("stance"),
                "action": meta.get("action"),
                "pattern_action": {"BUY_ZONE": "BUY_BREAKOUT", "RETEST_BUY": "BUY_RETEST", "RETEST": "WATCH_RETEST", "NEAR": "AWAIT_BREAKOUT"}.get(execution_state, "NO_SIGNAL"),
                "data_quarantine": meta.get("data_quarantine"),
                "data_note": meta.get("data_note"),
                "action_title": f"{tf.capitalize()} {('Buy ' + ('Retest' if t_state == 'RETEST_HOLD' else 'Breakout')) if t_action == 'BUY' else 'Watch'}",
                "target_1": tgt,
                "target_2": None,  # no invented second target
                "stop_loss": sl,
                "risk_reward_ratio": round((tgt - cmp) / (cmp - sl), 2) if (cmp > sl and tgt > cmp) else None,
                "rsi": meta.get("rsi"),
                "volume_ratio_20d": meta.get("volume_ratio_20d"),
                "breakout_volume_multiple": r["volume_multiple"],
                "pe_ratio": meta.get("pe_ratio"),
                "roce_pct": meta.get("roce_pct"),
                "market_cap_cr": meta.get("market_cap_cr"),
                "is_psu": meta.get("is_psu", 0),
                "is_fno": meta.get("is_fno", 0),
                "mini_candles_json": r["mini_candles_json"],
            }
            enriched.append(row)

        m_conn.close()
        c_conn.close()
        return enriched
    except Exception as e:
        logger.warning(f"Error fetching v3 screener candidates: {e}")
        return []


def generate_screener_snapshot() -> Optional[str]:
    """Pre-renders the Radar lists with the shared gates (common.radar_feed) and caches them."""
    try:
        sync_v3_pattern_candidates()
        from common.radar_feed import build_breakouts
        payload = build_breakouts(limit=50, v3_candidates=get_v3_screener_candidates)
        payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
        return save_snapshot("screener_breakouts_v2", payload, persist_db=True)
    except Exception as e:
        logger.warning(f"Error generating screener snapshot: {e}")
        return None


def sync_v3_pattern_candidates(storage=None) -> int:
    """
    Scans each stock across ALL timeframes (DAILY, WEEKLY, MONTHLY) and ALL classical pattern archetypes
    (Cup & Handle, Falling Wedge, Ascending Triangle, Symmetrical Triangle, Bullish Rectangle / Horizontal Base,
    Multi-Year Base, Double Bottom, Inverse Head & Shoulders).
    Populates v3_pattern_candidates in quantsync_master.db with exact timeframe, pattern name, breakout trigger, targets, and mini candles.
    """
    try:
        dna_db = os.path.join(APP_DIR, "Data", "stock_dna_cache.db")
        hist_db = os.path.join(APP_DIR, "Data", "stock_history.db")
        master_db = os.path.join(APP_DIR, "Data", "quantsync_master.db")
        if not (os.path.exists(dna_db) and os.path.exists(hist_db) and os.path.exists(master_db)):
            return 0

        import pandas as pd
        from classical_pattern_dna import ClassicalPatternAnalystDNA, resample_to_weekly, resample_to_monthly
        c_analyst = ClassicalPatternAnalystDNA()

        dna_conn = sqlite3.connect(f"file:{dna_db}?mode=ro", uri=True)
        d_cur = dna_conn.cursor()
        d_cur.execute("SELECT MAX(scan_date) FROM stock_screener_rows")
        row = d_cur.fetchone()
        latest_date = row[0] if row and row[0] else datetime.utcnow().strftime("%Y-%m-%d")
        dna_conn.close()

        hist_conn = sqlite3.connect(f"file:{hist_db}?mode=ro", uri=True)
        h_cur = hist_conn.cursor()
        h_cur.execute("SELECT symbol, rawCandles FROM StockHistory WHERE length(rawCandles) >= 3000 LIMIT 600")
        hist_rows = h_cur.fetchall()
        hist_conn.close()

        if not hist_rows:
            return 0

        candidates_to_insert = []
        portal_to_insert = []
        rank = 1

        for s_sym, raw_data in hist_rows:
            if not raw_data:
                continue
            try:
                raw_list = json.loads(raw_data)
                if len(raw_list) < 150:
                    continue

                df = pd.DataFrame(raw_list)
                df['Date'] = pd.to_datetime(df['t'])
                df = df.rename(columns={'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
                curr_p = float(df['Close'].iloc[-1])

                # Evaluate across each timeframe, then pick ONE primary call per stock (common.trade_rules.resolve_timeframes).
                from common import trade_rules as _tr
                per_tf = {}
                for tf in ['DAILY', 'WEEKLY', 'MONTHLY']:
                    if tf == 'MONTHLY' and len(raw_list) < 500:
                        continue
                    if tf == 'WEEKLY' and len(raw_list) < 200:
                        continue

                    res = c_analyst.analyze(df, timeframe=tf)
                    if not res:
                        continue
                    if res.stance == 'BEARISH':
                        continue
                    # states that are candidates for the Radar: validated buys, watch states, and honest "unproven" ones
                    if res.lifecycle_state not in ('CONFIRMED', 'NEAR_CONFIRMATION', 'FORMING', 'RETEST_WATCH', 'UNVALIDATED_BREAKOUT'):
                        continue
                    if not res.breakout_level or res.breakout_level <= 0:
                        continue
                    if (res.trade_call or {}).get('state') in (None, 'NA'):
                        continue
                    per_tf[tf] = res

                if not per_tf:
                    continue
                calls = {tf[0]: (r_.trade_call or {}) for tf, r_ in per_tf.items()}
                resolved = _tr.resolve_timeframes(calls)
                primary_letter = resolved.get("primary_tf")

                for tf, res in per_tf.items():
                    # Prepare mini candles for THAT exact timeframe
                    if tf == 'MONTHLY':
                        res_candles = resample_to_monthly(df).tail(20)
                    elif tf == 'WEEKLY':
                        res_candles = resample_to_weekly(df).tail(20)
                    else:
                        res_candles = df.tail(20)

                    mini_candles = []
                    for _, c in res_candles.iterrows():
                        mini_candles.append({
                            'o': round(float(c['Open']), 2),
                            'h': round(float(c['High']), 2),
                            'l': round(float(c['Low']), 2),
                            'c': round(float(c['Close']), 2),
                            'v': float(c.get('Volume', 0))
                        })

                    tcall = res.trade_call or {}
                    is_primary = 1 if tf[0] == primary_letter else 0
                    buy = tcall.get('action') == 'BUY'
                    score = int(res.confidence * 100)          # pattern GEOMETRY quality; the honest odds are in trade_json
                    tier = 'gold' if (buy and tcall.get('grade') == 'A') else 'silver'
                    base_dur_y = round(res.raw_geometry.get('duration_years', 0.3 if tf == 'DAILY' else (1.5 if tf == 'WEEKLY' else 3.2)), 1)
                    pvt = round(float(res.breakout_level), 2)
                    plan = tcall.get('plan') or {}
                    tgt = round(float(plan.get('target') or res.target_price or pvt * 1.15), 2)
                    sl = round(float(plan.get('stop') or res.invalidation_level or pvt * 0.95), 2)
                    tjson = dict(tcall)
                    tjson['resolution'] = dict(primary_tf=primary_letter, action=resolved.get('action'),
                                               conflict=resolved.get('conflict'), context=resolved.get('context'))
                    pat_name = f"{res.pattern_name}"

                    candidates_to_insert.append((
                        latest_date, s_sym, res.pattern_type, pat_name, score, tier,
                        int(base_dur_y * 365), 2.0, pvt, round(curr_p, 2),
                        json.dumps(mini_candles), tf, base_dur_y, tgt, sl,
                        tcall.get('state'), tcall.get('action'), tcall.get('grade'), is_primary,
                        json.dumps(tjson, default=str)[:8000]
                    ))

                    if rank <= 25 and buy and is_primary:
                        portal_to_insert.append((
                            latest_date, rank, s_sym, res.pattern_type, score, 2.0,
                            round(curr_p, 2), f"{tf} {pat_name}", "QUALIFIED_CAPITAL"
                        ))
                        rank += 1
            except Exception:
                pass

        master_conn = sqlite3.connect(master_db, timeout=30.0)
        master_conn.execute("PRAGMA journal_mode=WAL")
        m_cur = master_conn.cursor()

        # Schema ensure target_1 and stop_loss
        m_cur.execute("PRAGMA table_info(v3_pattern_candidates)")
        cols = [col[1] for col in m_cur.fetchall()]
        if 'target_1' not in cols:
            try:
                m_cur.execute("ALTER TABLE v3_pattern_candidates ADD COLUMN target_1 REAL")
                m_cur.execute("ALTER TABLE v3_pattern_candidates ADD COLUMN stop_loss REAL")
                master_conn.commit()
            except Exception:
                pass

        for _col, _typ in (("trade_state", "TEXT"), ("trade_action", "TEXT"), ("trade_grade", "TEXT"),
                           ("is_primary", "INTEGER DEFAULT 1"), ("trade_json", "TEXT")):
            if _col not in cols:
                try:
                    m_cur.execute(f"ALTER TABLE v3_pattern_candidates ADD COLUMN {_col} {_typ}")
                    master_conn.commit()
                except Exception:
                    pass

        m_cur.execute("DELETE FROM v3_pattern_candidates WHERE date = ?", (latest_date,))
        m_cur.execute("DELETE FROM v3_breakout_portal WHERE date = ?", (latest_date,))
        m_cur.execute("DELETE FROM v3_pattern_candidates WHERE symbol LIKE 'STOCK_%'")
        m_cur.execute("DELETE FROM v3_breakout_portal WHERE symbol LIKE 'STOCK_%'")

        m_cur.executemany("""
            INSERT OR REPLACE INTO v3_pattern_candidates (
                date, symbol, pattern_type, pattern_name, score, tier,
                base_duration_days, volume_multiple, pivot_price, current_price, mini_candles_json,
                timeframe, base_duration_years, target_1, stop_loss,
                trade_state, trade_action, trade_grade, is_primary, trade_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, candidates_to_insert)

        if portal_to_insert:
            m_cur.executemany("""
                INSERT OR REPLACE INTO v3_breakout_portal (
                    date, rank, symbol, pattern, score, volume_multiple,
                    price, notes, qualification_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, portal_to_insert)

        master_conn.commit()
        master_conn.close()
        logger.info(f"✅ Synced {len(candidates_to_insert)} multi-timeframe pattern candidates for {latest_date}")
        return len(candidates_to_insert)
    except Exception as e:
        logger.warning(f"Error syncing v3 pattern candidates: {e}")
        return 0


def generate_market_pulse_snapshot() -> Optional[str]:
    """Generates and caches market pulse snapshot in RAM."""
    try:
        import index_engine
        data = index_engine.get_live_market_pulse_payload()
        if data:
            return save_snapshot("market_pulse", data, persist_db=False)
    except Exception as e:
        logger.warning(f"Error generating market pulse snapshot: {e}")
    return None


def warmup_all_snapshots(storage):
    """Called on server startup to ensure all snapshots are hot in RAM."""
    init_snapshots_table()
    logger.info("Warming up QuantSync RAM snapshots...")
    try:
        generate_terminal_v2_snapshot(storage)
        generate_screener_snapshot()
        sync_v3_pattern_candidates(storage)
        generate_market_pulse_snapshot()
        logger.info("QuantSync RAM snapshots successfully warmed up and ready for 0ms reads.")
    except Exception as e:
        logger.error(f"Warmup snapshot error: {e}", exc_info=True)


def start_background_refresher(storage, interval_seconds: int = 45):
    """Starts background daemon thread that periodically refreshes the snapshots."""
    global _REFRESHER_THREAD
    if _REFRESHER_THREAD and _REFRESHER_THREAD.is_alive():
        return

    def _refresher_loop():
        logger.info(f"Snapshot background refresher started (interval: {interval_seconds}s).")
        while not _STOP_EVENT.is_set():
            _STOP_EVENT.wait(interval_seconds)
            if _STOP_EVENT.is_set():
                break
            try:
                generate_terminal_v2_snapshot(storage)
            except Exception as e:
                logger.debug(f"Background snapshot refresh cycle error: {e}")

    _STOP_EVENT.clear()
    _REFRESHER_THREAD = threading.Thread(target=_refresher_loop, daemon=True, name="QuantSyncSnapshotRefresher")
    _REFRESHER_THREAD.start()


def stop_background_refresher():
    """Stops the background refresher cleanly."""
    _STOP_EVENT.set()
