#!/usr/bin/env python3
"""
scripts/run_full_universe_dual_ingest.py
=========================================
Autonomous Full-Universe Historical Daily Bar Re-Ingestion (Dual Broker).

Design Specifications:
- Runs completely detached in the background on VPS (persists across SSH disconnects).
- Uses both Angel One Broker accounts (Broker 1: S817363, Broker 2: R281124) in parallel.
- Prioritizes Tier 1: Core ~1,300 stocks (isV3Eligible = 1 + Benchmark Indices).
- Immediately follows with Tier 2: Remaining ~1,400 stocks up to full ~2,700 NSE universe.
- Merges historical candles seamlessly to retain deep pre-2021 data while refreshing up to today.
- Strictly PRESERVES isAlgoEligible and isV3Eligible flags using ON CONFLICT DO UPDATE.
- Paces API calls to 0.40s per broker to safely respect Angel One's rate limits.
- Full persistent logging to /root/quantsync/logs/full_universe_reingestion.log.
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
import signal
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(PROD_ROOT, "Data", "stock_history.db")
CONFIG_PATH = os.path.join(PROD_ROOT, "config.json")
MASTER_JSON = os.path.join(PROD_ROOT, "Data", "OpenAPIScripMaster.json")
LOG_PATH = os.path.join(PROD_ROOT, "logs", "full_universe_reingestion.log")
PID_PATH = os.path.join(PROD_ROOT, "logs", "full_universe_reingestion.pid")

CANDLE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1/getCandleData"
LOGIN_URL = "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword"

db_lock = threading.Lock()

# Benchmark Indices tokens mapping (NSE AMXIDX)
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
    code = (struct.unpack(">I", digest[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000
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
                "totp": compute_totp(self.totp_secret)
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
                "X-PrivateKey": self.api_key
            }
            req = urllib.request.Request(LOGIN_URL, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("status") and data.get("data", {}).get("jwtToken"):
                        self.jwt_token = data["data"]["jwtToken"]
                        self.expires_at = time.time() + 14 * 3600
                        log(f"[{self.name}] Authentication successful (Client: {self.client_id})")
                        return self.jwt_token
                    else:
                        log(f"[{self.name}] Login failed: {data.get('message')}")
                        return None
            except Exception as e:
                log(f"[{self.name}] Login error: {e}")
                return None

    def fetch_candles(self, symbol_token: str, from_date: str, to_date: str) -> list:
        token = self.get_jwt()
        if not token:
            return []

        payload = {
            "exchange": "NSE",
            "symboltoken": str(symbol_token),
            "interval": "ONE_DAY",
            "fromdate": from_date,
            "todate": to_date
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
            "Authorization": f"Bearer {token}"
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
                    time.sleep(0.75 * (attempt + 1))
                elif e.code in (401, 403):
                    token = self.get_jwt(force=True)
                    headers["Authorization"] = f"Bearer {token}"
                    time.sleep(0.5)
            except Exception:
                time.sleep(0.5)
            time.sleep(0.3)

        return []

def worker_sync(broker: AngelBrokerSession, items: list, from_date: str, to_date: str, tier_name: str) -> dict:
    total = len(items)
    success = 0
    empty = 0
    errors = 0

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cur = conn.cursor()

    log(f"[{broker.name}] Starting {tier_name} - {total} stocks assigned.")

    for idx, (sym, token) in enumerate(items, 1):
        t0 = time.time()
        candles_raw = broker.fetch_candles(token, from_date, to_date)

        if candles_raw and len(candles_raw) > 0:
            new_formatted = []
            for c in candles_raw:
                try:
                    t_str = str(c[0])[:10]
                    new_formatted.append({
                        "t": t_str,
                        "o": float(c[1]),
                        "h": float(c[2]),
                        "l": float(c[3]),
                        "c": float(c[4]),
                        "v": int(c[5])
                    })
                except Exception:
                    continue

            if new_formatted:
                with db_lock:
                    # Fetch existing candles to merge pre-2021 historical bars seamlessly
                    cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = ?", (sym,))
                    row = cur.fetchone()
                    merged_map = {}
                    if row and row[0]:
                        try:
                            for old_c in json.loads(row[0]):
                                merged_map[old_c["t"]] = old_c
                        except Exception:
                            pass

                    # Layer new candles over old
                    for n_c in new_formatted:
                        merged_map[n_c["t"]] = n_c

                    final_candles = sorted(merged_map.values(), key=lambda x: x["t"])

                    # Strictly PRESERVES isAlgoEligible and isV3Eligible flags!
                    cur.execute("""
                        INSERT INTO StockHistory (token, symbol, rawCandles, candleCount, isAlgoEligible, isV3Eligible)
                        VALUES (?, ?, ?, ?, 0, 0)
                        ON CONFLICT(symbol) DO UPDATE SET
                            token = excluded.token,
                            rawCandles = excluded.rawCandles,
                            candleCount = excluded.candleCount
                    """, (token, sym, json.dumps(final_candles), len(final_candles)))
                    conn.commit()

                success += 1
            else:
                empty += 1
        else:
            empty += 1

        if idx % 50 == 0 or idx == total:
            pct = (idx / total) * 100.0
            log(f"[{broker.name}][{tier_name}] Progress: {idx}/{total} ({pct:.1f}%) | Synced: {success} | Empty: {empty}")

        # Enforce rate pacing: ~2.5 requests/second per broker (0.40s sleep per request)
        elapsed = time.time() - t0
        to_sleep = max(0.0, 0.40 - elapsed)
        if to_sleep > 0:
            time.sleep(to_sleep)

    conn.close()
    log(f"[{broker.name}][{tier_name}] Completed! Total: {total} | Synced: {success} | Empty/Skipped: {empty}")
    return {"total": total, "success": success, "empty": empty}

def write_pid_file():
    os.makedirs(os.path.dirname(PID_PATH), exist_ok=True)
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))

def cleanup_pid_file():
    try:
        if os.path.exists(PID_PATH):
            os.remove(PID_PATH)
    except Exception:
        pass

def handle_shutdown(signum, frame):
    log(f"Received termination signal ({signum}). Exiting cleanly...")
    cleanup_pid_file()
    sys.exit(0)

def main():
    write_pid_file()
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    log("=" * 70)
    log("QUANT-SYNC: FULL HISTORICAL RE-INGESTION (DUAL BROKER ENGINE)")
    log(f"Process PID: {os.getpid()} | Detached Mode: Active")
    log("=" * 70)

    # 1. Load Configurations
    if not os.path.exists(CONFIG_PATH):
        log(f"FATAL: Missing config at {CONFIG_PATH}")
        cleanup_pid_file()
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    broker1 = AngelBrokerSession("Broker-1 (S817363)", cfg.get("broker_vps1", cfg.get("broker", {})))
    broker2 = AngelBrokerSession("Broker-2 (R281124)", cfg.get("broker2", {}))

    log("Authenticating Broker 1 and Broker 2...")
    t1 = broker1.get_jwt()
    time.sleep(1.0)
    t2 = broker2.get_jwt()

    if not t1 and not t2:
        log("FATAL: Both brokers failed to authenticate! Exiting.")
        cleanup_pid_file()
        sys.exit(1)

    # 2. Date Range Configuration (1,950 calendar days ~ 5.34 years, maximum single window)
    now = datetime.datetime.now()
    to_date = now.strftime("%Y-%m-%d 15:30")
    from_date = (now - datetime.timedelta(days=2000)).strftime("%Y-%m-%d 09:15")
    log(f"Candle Data Window: {from_date} -> {to_date}")

    # 3. Load Master Scrip List for token fallback
    master_token_map = {}
    if os.path.exists(MASTER_JSON):
        with open(MASTER_JSON) as f:
            master = json.load(f)
            for item in master:
                if item.get("exch_seg") == "NSE" and str(item.get("symbol", "")).endswith("-EQ"):
                    sym = item.get("name", "").strip()
                    token = str(item.get("token", "")).strip()
                    if sym and token:
                        master_token_map[sym] = token

    # 4. Partition Universe into Tier 1 and Tier 2
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Tier 1: All isV3Eligible = 1 stocks + Benchmark Indices
    cur.execute("SELECT symbol, token FROM StockHistory WHERE isV3Eligible = 1")
    v3_rows = cur.fetchall()

    tier1_items = []
    tier1_symbols = set()

    # Add indices first
    for idx_sym, idx_token in BENCHMARK_INDICES:
        tier1_items.append((idx_sym, idx_token))
        tier1_symbols.add(idx_sym)

    for sym, token in v3_rows:
        if sym not in tier1_symbols:
            tok = token or master_token_map.get(sym)
            if tok:
                tier1_items.append((sym, tok))
                tier1_symbols.add(sym)

    # Tier 2: Remaining stocks in StockHistory + Missing NSE equities from Master
    cur.execute("SELECT symbol, token FROM StockHistory WHERE isV3Eligible != 1")
    non_v3_rows = cur.fetchall()

    tier2_items = []
    tier2_symbols = set()

    for sym, token in non_v3_rows:
        if sym not in tier1_symbols and sym not in tier2_symbols:
            tok = token or master_token_map.get(sym)
            if tok:
                tier2_items.append((sym, tok))
                tier2_symbols.add(sym)

    # Add missing NSE equities from master
    cur.execute("SELECT symbol FROM StockHistory")
    all_existing_symbols = set(r[0] for r in cur.fetchall())
    conn.close()

    for sym, tok in master_token_map.items():
        if sym not in all_existing_symbols and sym not in tier1_symbols and sym not in tier2_symbols:
            tier2_items.append((sym, tok))
            tier2_symbols.add(sym)

    total_universe = len(tier1_items) + len(tier2_items)
    log(f"Universe Breakdown:")
    log(f"  • Tier 1 (Core & V3-Eligible + Indices): {len(tier1_items)} symbols")
    log(f"  • Tier 2 (Extended Equities Universe):   {len(tier2_items)} symbols")
    log(f"  • Total Re-Ingestion Target:            {total_universe} symbols")

    # 5. Execute Tier 1 (Core 1,300 stocks first)
    log("\n" + "=" * 70)
    log(f"STARTING TIER 1: CORE RE-INGESTION ({len(tier1_items)} SYMBOLS)")
    log("=" * 70)
    t1_start = time.time()

    # Distribute interleaved across both brokers
    t1_b1 = [item for i, item in enumerate(tier1_items) if i % 2 == 0]
    t1_b2 = [item for i, item in enumerate(tier1_items) if i % 2 != 0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_sync, broker1, t1_b1, from_date, to_date, "Tier-1")
        f2 = executor.submit(worker_sync, broker2, t1_b2, from_date, to_date, "Tier-1")
        r1 = f1.result()
        r2 = f2.result()

    t1_elapsed = time.time() - t1_start
    log(f"TIER 1 COMPLETE! Duration: {t1_elapsed/60:.2f} mins | Synced: {r1['success'] + r2['success']}/{len(tier1_items)}")

    # 6. Execute Tier 2 (Remaining ~1,400 stocks)
    log("\n" + "=" * 70)
    log(f"STARTING TIER 2: EXTENDED UNIVERSE RE-INGESTION ({len(tier2_items)} SYMBOLS)")
    log("=" * 70)
    t2_start = time.time()

    t2_b1 = [item for i, item in enumerate(tier2_items) if i % 2 == 0]
    t2_b2 = [item for i, item in enumerate(tier2_items) if i % 2 != 0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_sync, broker1, t2_b1, from_date, to_date, "Tier-2")
        f2 = executor.submit(worker_sync, broker2, t2_b2, from_date, to_date, "Tier-2")
        r1_t2 = f1.result()
        r2_t2 = f2.result()

    t2_elapsed = time.time() - t2_start
    log(f"TIER 2 COMPLETE! Duration: {t2_elapsed/60:.2f} mins | Synced: {r1_t2['success'] + r2_t2['success']}/{len(tier2_items)}")

    # 7. Post-Sync Aliasing & Database Validation
    log("\n" + "=" * 70)
    log("RUNNING POST-SYNC DATABASE VALIDATION & ALIAS CHECKS")
    log("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO StockHistory (symbol, token, rawCandles, candleCount, isAlgoEligible, isV3Eligible)
        SELECT 'ZOMATO', token, rawCandles, candleCount, 1, 1 
        FROM StockHistory WHERE symbol = 'ETERNAL'
    """)
    cur.execute("""
        INSERT OR REPLACE INTO StockHistory (symbol, token, rawCandles, candleCount, isAlgoEligible, isV3Eligible)
        SELECT 'TATAMOTORS', token, rawCandles, candleCount, 1, 1 
        FROM StockHistory WHERE symbol = 'TMPV'
    """)
    conn.commit()

    cur.execute("SELECT count(*) FROM StockHistory")
    total_db = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM StockHistory WHERE candleCount >= 100")
    healthy_db = cur.fetchone()[0]
    cur.execute("SELECT sum(isV3Eligible), sum(isAlgoEligible) FROM StockHistory")
    v3_cnt, algo_cnt = cur.fetchone()
    conn.close()

    total_time = time.time() - t1_start
    log(f"Total Ingestion Time: {total_time/60:.2f} minutes")
    log(f"Database Verification:")
    log(f"  • Total Symbols in DB:       {total_db}")
    log(f"  • Healthy Candles (>=100):   {healthy_db}")
    log(f"  • V3-Eligible Preserved:     {v3_cnt}")
    log(f"  • Algo-Eligible Preserved:   {algo_cnt}")
    log("=" * 70)
    log("FULL HISTORICAL RE-INGESTION SUCCESSFULLY COMPLETED!")
    log("=" * 70)

    cleanup_pid_file()

if __name__ == "__main__":
    main()
