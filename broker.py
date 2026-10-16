"""
common/broker.py — Shared Angel One SmartAPI Integration for Prod_VPS
=====================================================================
Unified broker service supporting batched quotes, holdings, orders, and market data ingestion.
All credentials read from Prod_VPS/config.json["broker"].
"""

import os
import sys
import time
import hmac
import hashlib
import struct
import base64
import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger("ProdVPS.Broker")

_COMMON_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_ROOT = os.path.dirname(_COMMON_DIR)


def _load_broker_cfg() -> Dict[str, Any]:
    cfg_path = os.path.join(_PROD_ROOT, "config.json")
    defaults = {
        "api_key": "", "client_id": "", "password": "", "totp_secret": "",
        "client_ip": "106.193.147.98", "mac_address": "fe80::216:3eff:fe10:1",
        "session_cache": os.path.join(_PROD_ROOT, "Data", "angel_session.json")
    }
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r") as f:
                b = json.load(f).get("broker", {})
                defaults.update(b)
        except Exception as e:
            logger.warning(f"Broker config load error: {e}")
    # Fall back to env vars
    for env_key, cfg_key in [
        ("ANGEL_API_KEY", "api_key"), ("ANGEL_USER_ID", "client_id"),
        ("ANGEL_PASSWORD", "password"), ("ANGEL_TOTP_SECRET", "totp_secret"),
        ("ANGEL_CLIENT_IP", "client_ip"), ("ANGEL_MAC_ADDRESS", "mac_address")
    ]:
        val = os.environ.get(env_key, "")
        if val:
            defaults[cfg_key] = val
    return defaults


_BCFG = _load_broker_cfg()

_LOGIN_URL   = "https://apiconnect.angelbroking.com/rest/auth/angelbroking/user/v1/loginByPassword"
_HOLDING_URL = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/portfolio/v1/getHolding"
_CANDLE_URL  = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/historical/v1/getCandleData"
_MARKET_URL  = "https://apiconnect.angelbroking.com/rest/secure/angelbroking/market/v1/quote/"

_SESSION = {"jwt": None, "expires_at": 0}


def _totp(secret: str = "") -> str:
    sec = (secret or _BCFG.get("totp_secret", "")).strip().replace(" ", "").upper()
    missing = len(sec) % 8
    if missing:
        sec += '=' * (8 - missing)
    key = base64.b32decode(sec)
    counter = int(time.time() // 30)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _post(url: str, payload: dict, token: str = "") -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": _BCFG.get("client_ip", "127.0.0.1"),
        "X-ClientPublicIP": _BCFG.get("client_ip", "127.0.0.1"),
        "X-MACAddress": _BCFG.get("mac_address", "00:00:00:00:00:00"),
        "X-PrivateKey": _BCFG.get("api_key", ""),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code} on {url}: {err_body}")
        return {"status": False, "message": f"HTTP {e.code}: {err_body}"}
    except Exception as ex:
        logger.error(f"Request error on {url}: {ex}")
        return {"status": False, "message": str(ex)}


def _get(url: str, token: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": _BCFG.get("client_ip", "127.0.0.1"),
        "X-ClientPublicIP": _BCFG.get("client_ip", "127.0.0.1"),
        "X-MACAddress": _BCFG.get("mac_address", "00:00:00:00:00:00"),
        "X-PrivateKey": _BCFG.get("api_key", ""),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"HTTP {e.code} on {url}: {err_body}")
        return {"status": False, "message": f"HTTP {e.code}: {err_body}"}
    except Exception as ex:
        logger.error(f"Request error on {url}: {ex}")
        return {"status": False, "message": str(ex)}


def get_session(force: bool = False) -> Optional[str]:
    now = time.time()
    if not force and _SESSION["jwt"] and _SESSION["expires_at"] > now + 300:
        return _SESSION["jwt"]

    cache_file = _BCFG.get("session_cache", "")
    if not force and cache_file and os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                c = json.load(f)
            if c.get("expires_at", 0) > now + 300:
                _SESSION["jwt"] = c["jwt"]
                _SESSION["expires_at"] = c["expires_at"]
                return c["jwt"]
        except Exception:
            pass

    client_id = _BCFG.get("client_id", "")
    password  = _BCFG.get("password", "")
    if not client_id or not password:
        logger.error("Missing client_id or password in config.json")
        return None

    totp_code = _totp()
    payload = {"clientcode": client_id, "password": password, "totp": totp_code}
    resp = _post(_LOGIN_URL, payload)

    if not resp.get("status") or not resp.get("data"):
        logger.error(f"Login failed: {resp.get('message', 'No data')}")
        return None

    jwt = resp["data"]["jwtToken"]
    expires_at = now + 12 * 3600
    _SESSION["jwt"] = jwt
    _SESSION["expires_at"] = expires_at

    if cache_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_file)), exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump({"jwt": jwt, "expires_at": expires_at}, f)
        except Exception as e:
            logger.warning(f"Could not write session cache: {e}")

    logger.info("Angel One authentication successful.")
    return jwt


def fetch_batch_quotes(tokens: List[str], mode: str = "OHLC") -> List[Dict[str, Any]]:
    if not tokens:
        return []
    token = get_session()
    if not token:
        logger.error("fetch_batch_quotes: No session available")
        return []

    results = []
    chunk_size = 50
    for i in range(0, len(tokens), chunk_size):
        chunk = [str(t) for t in tokens[i:i+chunk_size]]
        payload = {"mode": mode, "exchangeTokens": {"NSE": chunk}}
        resp = _post(_MARKET_URL, payload, token)

        if resp.get("status") and resp.get("data"):
            fetched = resp["data"].get("fetched", [])
            for item in fetched:
                results.append({
                    "symbolToken": str(item.get("symbolToken", "")),
                    "open": float(item.get("open", 0.0)),
                    "high": float(item.get("high", 0.0)),
                    "low": float(item.get("low", 0.0)),
                    "ltp": float(item.get("ltp", 0.0)),
                    "tradeVolume": int(item.get("tradeVolume", 0)),
                })
        else:
            logger.warning(f"Batch quote chunk {i//chunk_size + 1} failed: {resp.get('message')}")
        if i + chunk_size < len(tokens):
            time.sleep(0.35)

    return results


def fetch_holdings() -> List[Dict[str, Any]]:
    token = get_session()
    if not token:
        return []
    resp = _get(_HOLDING_URL, token)
    if resp.get("status") and resp.get("data"):
        return resp["data"]
    return []


# Backward compatibility alias
fetch_angel_holdings = fetch_holdings


def upsert_today_candle(
    conn: sqlite3.Connection,
    token_to_sym: Dict[str, str],
    quotes: List[Dict[str, Any]],
    today_str: str,
) -> int:
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_candles (
            symbol      TEXT    NOT NULL,
            date        TEXT    NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      INTEGER,
            PRIMARY KEY (symbol, date)
        )
    """)

    updated = 0
    for q in quotes:
        tok = q["symbolToken"]
        sym = token_to_sym.get(tok)
        if not sym or q["ltp"] <= 0.0:
            continue
        cur.execute("""
            INSERT INTO stock_candles (symbol, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open   = excluded.open,
                high   = max(stock_candles.high, excluded.high),
                low    = min(stock_candles.low, excluded.low),
                close  = excluded.close,
                volume = excluded.volume
        """, (
            sym, today_str,
            q["open"] or q["ltp"],
            q["high"] or q["ltp"],
            q["low"]  or q["ltp"],
            q["ltp"],
            q["tradeVolume"],
        ))
        updated += 1

    conn.commit()
    return updated
