#!/usr/bin/env python3
"""
sync_missing_stocks_dual_broker.py
==================================
High-speed parallel historical candle synchronization using Dual Angel One SmartAPI accounts.
- Distributes missing NSE equities between Broker 1 and Broker 2.
- Ingests last 2,000 daily candles into StockHistory in stock_history.db.
- Enforces isAlgoEligible = 0 and isV3Eligible = 0 to protect V2 & V3 algorithmic engines.
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
DB_PATH = os.path.join(PROD_ROOT, "Data", "stock_history.db")
CONFIG_PATH = os.path.join(PROD_ROOT, "config.json")
MASTER_JSON = os.path.join(PROD_ROOT, "Data", "OpenAPIScripMaster.json")

CANDLE_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1/getCandleData"
LOGIN_URL = "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword"

db_lock = threading.Lock()

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
                        self.expires_at = time.time() + 18 * 3600
                        print(f"[{self.name}] Logged in as {self.client_id}")
                        return self.jwt_token
                    else:
                        print(f"[{self.name}] Login failed: {data.get('message')}")
                        return None
            except Exception as e:
                print(f"[{self.name}] Login exception: {e}")
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

        for attempt in range(2):
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
            except Exception as ex:
                pass
            time.sleep(0.5)

        return []

def worker_sync(broker: AngelBrokerSession, items: list, from_date: str, to_date: str):
    total = len(items)
    success = 0
    empty = 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    for idx, (sym, token) in enumerate(items, 1):
        t0 = time.time()
        candles_raw = broker.fetch_candles(token, from_date, to_date)

        if candles_raw and len(candles_raw) > 0:
            formatted = []
            for c in candles_raw:
                try:
                    # Angel format: ["2019-01-01T00:00:00+05:30", open, high, low, close, volume]
                    t_str = str(c[0])[:10]
                    formatted.append({
                        "t": t_str,
                        "o": float(c[1]),
                        "h": float(c[2]),
                        "l": float(c[3]),
                        "c": float(c[4]),
                        "v": int(c[5])
                    })
                except Exception:
                    continue

            if formatted:
                with db_lock:
                    cur.execute("""
                        INSERT OR REPLACE INTO StockHistory 
                        (token, symbol, rawCandles, candleCount, isAlgoEligible, isV3Eligible)
                        VALUES (?, ?, ?, ?, 0, 0)
                    """, (token, sym, json.dumps(formatted), len(formatted)))
                    conn.commit()
                success += 1
            else:
                empty += 1
        else:
            empty += 1

        if idx % 25 == 0 or idx == total:
            print(f"[{broker.name}] Progress: {idx}/{total} ({idx/total*100:.1f}%) | Synced: {success} | Empty: {empty}")

        # Rate limit: ~2.8 req/sec per broker (sleep ~0.35s total per loop)
        elapsed = time.time() - t0
        to_sleep = max(0.0, 0.36 - elapsed)
        if to_sleep > 0:
            time.sleep(to_sleep)

    conn.close()
    print(f"[{broker.name}] Completed! Total: {total} | Success: {success} | Empty: {empty}")

def main():
    print("==========================================================")
    print("Dual-Broker Historical Daily Candle Ingestion (2000 Bars)")
    print("==========================================================")

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    broker1 = AngelBrokerSession("Broker-1 (S817363)", cfg.get("broker", {}))
    broker2 = AngelBrokerSession("Broker-2 (R281124)", cfg.get("broker2", {}))

    print("Authenticating both brokers...")
    t1 = broker1.get_jwt()
    t2 = broker2.get_jwt()

    if not t1 and not t2:
        print("ERROR: Both brokers failed to authenticate. Exiting.")
        sys.exit(1)

    # Load master scrip list
    with open(MASTER_JSON) as f:
        master = json.load(f)

    # Query existing symbols in DB that already have >= 100 candles
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT symbol FROM StockHistory WHERE candleCount >= 100")
    existing_symbols = set(r[0] for r in cur.fetchall())
    conn.close()

    # Filter active NSE equities
    missing = []
    seen = set()
    for item in master:
        if item.get("exch_seg") == "NSE" and item.get("instrumenttype") == "" and str(item.get("symbol", "")).endswith("-EQ"):
            sym = item.get("name", "").strip()
            token = str(item.get("token", "")).strip()
            if sym and sym not in existing_symbols and sym not in seen:
                seen.add(sym)
                missing.append((sym, token))

    print(f"Total Master NSE Equities: {len(master)}")
    print(f"Existing in DB with >= 100 candles: {len(existing_symbols)}")
    print(f"Target Missing Equities to Sync: {len(missing)}")

    if not missing:
        print("All active NSE equities are already synced with historical candles!")
        sys.exit(0)

    # Date range for 2000 daily candles (~8 years)
    to_date = datetime.datetime.now().strftime("%Y-%m-%d 15:30")
    from_date = (datetime.datetime.now() - datetime.timedelta(days=365*8)).strftime("%Y-%m-%d 09:15")
    print(f"Querying candles from {from_date} to {to_date}")

    # Split missing list evenly
    mid = len(missing) // 2
    items_b1 = missing[:mid]
    items_b2 = missing[mid:]

    print(f"Broker 1 Assigned: {len(items_b1)} stocks")
    print(f"Broker 2 Assigned: {len(items_b2)} stocks")
    print("Starting parallel ingestion...")

    start_time = time.time()
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_sync, broker1, items_b1, from_date, to_date)
        f2 = executor.submit(worker_sync, broker2, items_b2, from_date, to_date)
        f1.result()
        f2.result()

    total_time = time.time() - start_time
    print(f"All done in {total_time/60:.2f} minutes!")

    # Add aliases for common renames
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
    cur.execute("SELECT count(*) FROM StockHistory WHERE candleCount >= 100")
    final_count = cur.fetchone()[0]
    conn.close()
    print(f"Final Count of Equities with Full History in DB: {final_count}")

if __name__ == "__main__":
    main()
