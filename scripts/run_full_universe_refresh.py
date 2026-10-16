#!/usr/bin/env python3
"""
scripts/run_full_universe_refresh.py
====================================
End-to-end full refresh pipeline:
1. Dual-broker parallel fetch of the last 2,000 candles for the full universe (zero cache, direct replace).
2. Corporate actions split/bonus adjustment and bundle rebuild.
3. Daily technicals computation (Wilder RSI, volume ratios, delivery %).
4. DNA engine evaluation for all ~2,500 stocks (zero cache).
5. V3 Pattern candidate recalculation and Screener / Radar snapshot generation.
6. Server cache bust & hot reload.
"""

import os
import sys
import time
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import hmac
import hashlib
import struct
import base64
import threading
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT = os.path.dirname(SCRIPT_DIR)
if PROD_ROOT not in sys.path:
    sys.path.insert(0, PROD_ROOT)

DB_PATH = os.path.join(PROD_ROOT, "Data", "stock_history.db")
CACHE_DB_PATH = os.path.join(PROD_ROOT, "Data", "stock_dna_cache.db")
MASTER_DB_PATH = os.path.join(PROD_ROOT, "Data", "quantsync_master.db")
CONFIG_PATH = os.path.join(PROD_ROOT, "config.json")
MASTER_JSON = os.path.join(PROD_ROOT, "Data", "OpenAPIScripMaster.json")
LOG_PATH = os.path.join(PROD_ROOT, "logs", "full_refresh.log")

CANDLE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1/getCandleData"
LOGIN_URL = "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword"

db_lock = threading.Lock()

BENCHMARK_INDICES = [
    ("NIFTY", "99926000"),
    ("BANKNIFTY", "99926009"),
    ("NIFTY100", "99926012"),
    ("NIFTYMIDCAP100", "99926011"),
    ("NIFTYNXT50", "99926013"),
    ("NIFTYMIDCAP50", "99926014"),
    ("NIFTY500", "99926004"),
    ("NIFTYIT", "99926008"),
    ("INDIAVIX", "99926017"),
]


def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def compute_totp(secret: str) -> str:
    sec = secret.strip().replace(" ", "").upper()
    missing = len(sec) % 8
    if missing:
        sec += "=" * (8 - missing)
    key = base64.b32decode(sec)
    counter = int(time.time() // 30)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


class AngelBrokerSession:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.client_id = cfg.get("client_id", "")
        self.password = cfg.get("password", "")
        self.api_key = cfg.get("api_key", "")
        self.totp_secret = cfg.get("totp_secret", "")
        self.jwt_token = None
        self.expires_at = 0
        self.lock = threading.Lock()

    def get_jwt(self, force: bool = False) -> str:
        with self.lock:
            now = time.time()
            if not force and self.jwt_token and self.expires_at > now + 300:
                return self.jwt_token

            payload = {
                "clientcode": self.client_id,
                "password": self.password,
                "totp": compute_totp(self.totp_secret),
            }
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-UserType": "USER",
                "X-SourceID": "WEB",
                "X-ClientLocalIP": "127.0.0.1",
                "X-ClientPublicIP": "127.0.0.1",
                "X-MACAddress": "00:00:00:00:00:00",
                "X-PrivateKey": self.api_key,
            }
            req = urllib.request.Request(LOGIN_URL, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("status") and data.get("data", {}).get("jwtToken"):
                        self.jwt_token = data["data"]["jwtToken"]
                        self.expires_at = time.time() + 14 * 3600
                        log(f"[{self.name}] Login success (Client: {self.client_id})")
                        return self.jwt_token
                    else:
                        log(f"[{self.name}] Login failed: {data.get('message')}")
                        return None
            except Exception as e:
                log(f"[{self.name}] Login error: {e}")
                return None

    def fetch_window(self, symbol_token: str, from_date: str, to_date: str) -> list:
        token = self.get_jwt()
        if not token:
            return []

        payload = {
            "exchange": "NSE",
            "symboltoken": str(symbol_token),
            "interval": "ONE_DAY",
            "fromdate": from_date,
            "todate": to_date,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-PrivateKey": self.api_key,
            "Authorization": f"Bearer {token}",
        }

        for attempt in range(3):
            req = urllib.request.Request(CANDLE_URL, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    if res_data.get("status") and res_data.get("data"):
                        return res_data["data"]
                    msg = str(res_data.get("message", "")).lower()
                    if "jwt" in msg or "session" in msg or "unauthorized" in msg or res_data.get("errorcode") in ("AG8001", "AG8002"):
                        token = self.get_jwt(force=True)
                        headers["Authorization"] = f"Bearer {token}"
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore").lower()
                if "exceeding access rate" in err_body or e.code == 429:
                    time.sleep(1.0 * (attempt + 1))
                elif e.code in (401, 403):
                    token = self.get_jwt(force=True)
                    headers["Authorization"] = f"Bearer {token}"
                    time.sleep(0.5)
            except Exception:
                time.sleep(0.5)
            time.sleep(0.35)

        return []

    def fetch_2000_candles(self, symbol_token: str, to_date: str) -> list:
        # Window 1: 2018-01-01 to 2022-06-01
        # Window 2: 2022-06-02 to to_date
        c1 = self.fetch_window(symbol_token, "2018-01-01 09:15", "2022-06-01 15:30")
        time.sleep(0.35)
        c2 = self.fetch_window(symbol_token, "2022-06-02 09:15", to_date)
        time.sleep(0.35)

        all_raw = (c1 or []) + (c2 or [])
        if not all_raw:
            return []

        formatted = {}
        for c in all_raw:
            try:
                t_str = str(c[0])[:10]
                formatted[t_str] = {
                    "t": t_str,
                    "o": float(c[1]),
                    "h": float(c[2]),
                    "l": float(c[3]),
                    "c": float(c[4]),
                    "v": int(c[5]),
                }
            except Exception:
                continue

        final_bars = sorted(formatted.values(), key=lambda x: x["t"])
        if len(final_bars) > 2000:
            final_bars = final_bars[-2000:]
        return final_bars


def worker_ingest(broker: AngelBrokerSession, items: list, to_date: str, tier_name: str) -> dict:
    total = len(items)
    success = 0
    empty = 0
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    cur = conn.cursor()

    log(f"[{broker.name}] Started {tier_name} ({total} symbols assigned)")

    for idx, (sym, tok) in enumerate(items, 1):
        bars = broker.fetch_2000_candles(tok, to_date)
        if bars:
            with db_lock:
                cur.execute(
                    """
                    INSERT INTO StockHistory (token, symbol, rawCandles, candleCount, isAlgoEligible, isV3Eligible)
                    VALUES (?, ?, ?, ?, 0, 0)
                    ON CONFLICT(symbol) DO UPDATE SET
                        token = excluded.token,
                        rawCandles = excluded.rawCandles,
                        candleCount = excluded.candleCount
                """,
                    (tok, sym, json.dumps(bars), len(bars)),
                )
                conn.commit()
            success += 1
        else:
            empty += 1

        if idx % 25 == 0 or idx == total:
            log(f"[{broker.name}] Progress: {idx}/{total} (Success: {success}, Empty: {empty})")

    conn.close()
    return {"total": total, "success": success, "empty": empty}


def step_1_candle_ingest():
    log("=" * 70)
    log("STEP 1: DUAL-BROKER CANDLE INGESTION (FRESH LAST 2000 BARS)")
    log("=" * 70)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    broker1 = AngelBrokerSession("Broker-1 (S817363)", cfg.get("broker", {}))
    broker2 = AngelBrokerSession("Broker-2 (R281124)", cfg.get("broker2", {}))

    b1_jwt = broker1.get_jwt()
    time.sleep(1.0)
    b2_jwt = broker2.get_jwt()

    if not b1_jwt and not b2_jwt:
        raise RuntimeError("Both brokers failed authentication!")

    to_date = datetime.datetime.now().strftime("%Y-%m-%d 15:30")

    # Load scrip master for missing token lookup
    token_map = {}
    if os.path.exists(MASTER_JSON):
        with open(MASTER_JSON) as f:
            for item in json.load(f):
                if item.get("exch_seg") == "NSE" and str(item.get("symbol", "")).endswith("-EQ"):
                    name = item.get("name", "").strip()
                    tok = str(item.get("token", "")).strip()
                    if name and tok:
                        token_map[name] = tok

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT symbol, token, isV3Eligible FROM StockHistory ORDER BY isV3Eligible DESC, symbol ASC")
    rows = cur.fetchall()
    conn.close()

    indices_and_v3 = []
    others = []
    seen = set()

    # Add indices first
    for idx_sym, idx_tok in BENCHMARK_INDICES:
        indices_and_v3.append((idx_sym, idx_tok))
        seen.add(idx_sym)

    for sym, tok, is_v3 in rows:
        if sym in seen:
            continue
        tok = tok or token_map.get(sym)
        if tok:
            if is_v3:
                indices_and_v3.append((sym, tok))
            else:
                others.append((sym, tok))
            seen.add(sym)

    universe = indices_and_v3 + others
    log(f"Total Universe: {len(universe)} symbols ({len(indices_and_v3)} priority V3/Indices, {len(others)} others)")

    # Partition alternately to evenly distribute workload
    b1_items = universe[0::2]
    b2_items = universe[1::2]

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(worker_ingest, broker1, b1_items, to_date, "Half-1")
        f2 = ex.submit(worker_ingest, broker2, b2_items, to_date, "Half-2")
        r1 = f1.result()
        r2 = f2.result()

    log(f"Ingestion complete: {r1['success'] + r2['success']}/{len(universe)} symbols successfully updated.")


def step_2_corporate_actions_and_bundles():
    log("=" * 70)
    log("STEP 2: CORPORATE ACTIONS ADJUSTMENT & ENGINE BUNDLE REBUILD")
    log("=" * 70)
    try:
        from common.corporate_actions import sync_and_apply
        ca_res = sync_and_apply(days_back=90)
        log(f"Corporate actions applied: {ca_res.get('applied_count', 0)} adjustments")
    except Exception as e:
        log(f"Corporate actions warning: {e}")

    try:
        from common.candle_sync import rebuild_engine_bundles
        rebuilt = rebuild_engine_bundles(verbose=True)
        log(f"Engine bundles rebuilt: {rebuilt} symbols")
    except Exception as e:
        log(f"Bundle rebuild error: {e}")


def step_3_technicals():
    log("=" * 70)
    log("STEP 3: COMPUTE DAILY TECHNICALS (WILDER RSI, VOLUME, DELIVERY %)")
    log("=" * 70)
    import subprocess
    script = os.path.join(SCRIPT_DIR, "compute_daily_technicals.py")
    res = subprocess.run([sys.executable, script], capture_output=True, text=True)
    if res.returncode != 0:
        log(f"compute_daily_technicals error: {res.stderr}")
    else:
        log("Daily technicals computed successfully.")


def step_4_evaluate_dna():
    log("=" * 70)
    log("STEP 4: MULTI-PROCESS UNIVERSE DNA EVALUATION (ZERO CACHE)")
    log("=" * 70)
    # Clear old cache to force 100% fresh DNA generation
    try:
        c_conn = sqlite3.connect(CACHE_DB_PATH)
        c_conn.execute("DELETE FROM stock_dna_reports")
        c_conn.commit()
        c_conn.close()
        log("Cleared old stock_dna_reports cache.")
    except Exception as e:
        log(f"Cache clear note: {e}")

    import evaluate_universe_dna
    evaluate_universe_dna.run_multiprocess_universe_evaluation(workers=3, skip_cached=False)
    log("DNA Universe evaluation complete.")


def step_5_patterns_and_radar():
    log("=" * 70)
    log("STEP 5: RECALCULATE PATTERNS & RADAR SCREENER SNAPSHOT")
    log("=" * 70)
    from common.market_calendar import last_completed_session
    scan_date = last_completed_session().isoformat()

    import build_screener_snapshot
    build_screener_snapshot.build_all(scan_date=scan_date)
    log(f"Screener snapshot rows built for {scan_date}.")

    import snapshot_manager
    count = snapshot_manager.sync_v3_pattern_candidates()
    log(f"Synced {count} V3 pattern candidates.")

    snapshot_manager.generate_screener_snapshot()
    log("Materialized fresh screener_breakouts_v2 snapshot for Radar.")

    try:
        from common.base_rates import build as build_base_rates
        build_base_rates(step=3)
        log("Refreshed chart base rates.")
    except Exception as e:
        log(f"Base rates note: {e}")


def step_6_hot_reload():
    log("=" * 70)
    log("STEP 6: HOT-RELOAD SERVER MEMORY CACHES")
    log("=" * 70)
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:8443/api/screener/breakouts?limit=50")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    log("ALL STEPS COMPLETED SUCCESSFULLY!")


def main():
    t_start = time.time()
    log("🚀 LAUNCHING COMPLETE QUANT-SYNC UNIVERSE REFRESH PIPELINE")
    try:
        step_1_candle_ingest()
        step_2_corporate_actions_and_bundles()
        step_3_technicals()
        step_4_evaluate_dna()
        step_5_patterns_and_radar()
        step_6_hot_reload()
        total_time = (time.time() - t_start) / 60.0
        log(f"🎉 FULL REFRESH FINISHED IN {total_time:.2f} MINUTES.")
    except Exception as e:
        log(f"❌ PIPELINE ERROR: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
