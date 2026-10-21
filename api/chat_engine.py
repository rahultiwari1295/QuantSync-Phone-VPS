#!/usr/bin/env python3
"""
chat_engine.py — QuantSync Institutional Community & AI Chat Engine
===================================================================
Provides high-performance, real-time WhatsApp-style channels and chat:
- 2 Distinct Sections:
    • Groups: Traders Lounge, Recommendation, Nifty & BankNifty F&O
    • Personal: Stock Analysis (AI Co-Pilot), AI Buy/Sell Alert (read-only), Breakout Alert (read-only)
- Interactive Stock Analysis Bot with live auto-complete, chart snapshots & action buttons:
    [ 📊 Technical ] [ 💼 Financial ] [ 🧬 DNA Report ] [ 🔍 New Stock ]
- Slash Commands (/chart, /dna, /finance, /technical, /poll)
- Trading Polls with preset scenarios (Buy/Sell, Nifty UP/Down, Buy/Ignore) & timer options
- SQLite persistence (quantsync_chat.db) with WebSocket live broadcast
- Firebase authentication synchronization
"""

import os
import sys
import json
import uuid
import base64
import sqlite3
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Set, Optional, Any, Tuple
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("QuantSyncChat")

# ── Paths ─────────────────────────────────────────────────────────────────────
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(ENGINE_DIR, "Data")
os.makedirs(DATA_DIR, exist_ok=True)
CHAT_DB    = os.path.join(DATA_DIR, "quantsync_chat.db")

UPLOADS_DIR = os.path.join(ENGINE_DIR, "public", "uploads", "chat")
os.makedirs(UPLOADS_DIR, exist_ok=True)


# ── Database Initialization ───────────────────────────────────────────────────
def init_chat_db():
    conn = sqlite3.connect(CHAT_DB)
    cur = conn.cursor()

    # Users Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
            email TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            avatar_url TEXT,
            role TEXT DEFAULT 'trader',
            bio TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        )
    """)

    # Channels Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_channels (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT UNIQUE,
            description TEXT,
            icon TEXT,
            section TEXT DEFAULT 'group',
            is_read_only INTEGER DEFAULT 0,
            channel_type TEXT DEFAULT 'chat',
            is_announcement INTEGER DEFAULT 0,
            order_idx INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # Migration: Ensure new columns exist
    for col, col_type in [
        ("section", "TEXT DEFAULT 'group'"),
        ("is_read_only", "INTEGER DEFAULT 0"),
        ("channel_type", "TEXT DEFAULT 'chat'")
    ]:
        try:
            cur.execute(f"ALTER TABLE chat_channels ADD COLUMN {col} {col_type}")
        except Exception:
            pass

    # Messages Table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            user_avatar TEXT,
            user_role TEXT DEFAULT 'trader',
            text TEXT NOT NULL,
            attachment_url TEXT,
            attachment_type TEXT,
            reply_to_id INTEGER,
            reply_preview TEXT,
            reactions_json TEXT DEFAULT '{}',
            is_pinned INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (channel_id) REFERENCES chat_channels(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_channel ON chat_messages(channel_id, id DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_user ON chat_messages(user_email)")

    conn.commit()

    # Remap deprecated channel IDs if existing
    try:
        cur.execute("UPDATE chat_messages SET channel_id = 'traders-lounge' WHERE channel_id = 'quant-lounge'")
        cur.execute("UPDATE chat_messages SET channel_id = 'stock-analysis' WHERE channel_id = 'dna-analysis'")
        cur.execute("UPDATE chat_messages SET channel_id = 'ai-alerts' WHERE channel_id = 'announcements'")
        cur.execute("UPDATE chat_messages SET channel_id = 'breakout-alert' WHERE channel_id = 'breakout-radar'")
        cur.execute("DELETE FROM chat_channels WHERE id IN ('quant-lounge', 'dna-analysis', 'announcements', 'breakout-radar')")
        conn.commit()
    except Exception as ex:
        log.debug(f"Channel remap exception: {ex}")

    now_str = datetime.now(timezone.utc).isoformat()

    # 6 Core Channels defined by user
    channels_def = [
        # Section 1: Groups
        ("traders-lounge", "Traders Lounge", "traders-lounge", "General swing trading community, trade ideas & chart discussion", "💬", "group", 0, "chat", 0, 1, now_str),
        ("recommendation", "Recommendation", "recommendation", "High-conviction algorithmic trade calls & verified recommendations", "🎯", "group", 0, "chat", 0, 2, now_str),
        ("nifty-banknifty", "Nifty & BankNifty F&O", "nifty-banknifty", "Index levels, options chain, PCR, expiry strategy & open interest", "📊", "group", 0, "chat", 0, 3, now_str),

        # Section 2: Personal
        ("stock-analysis", "Stock Analysis", "stock-analysis", "Interactive Quant AI Assistant: Instant chart, technicals, financials & DNA", "🤖", "personal", 0, "analysis_bot", 0, 4, now_str),
        ("ai-alerts", "AI Buy/Sell Alert", "ai-alerts", "Official real-time V2 AI buy/sell trade execution notifications", "⚡", "personal", 1, "alert_feed", 1, 5, now_str),
        ("breakout-alert", "Breakout Alert", "breakout-alert", "Daily automated alerts for stocks entering breakout radar", "🚀", "personal", 1, "alert_feed", 1, 6, now_str),
    ]

    for c in channels_def:
        cur.execute("""
            INSERT INTO chat_channels (id, name, slug, description, icon, section, is_read_only, channel_type, is_announcement, order_idx, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                icon=excluded.icon,
                section=excluded.section,
                is_read_only=excluded.is_read_only,
                channel_type=excluded.channel_type,
                order_idx=excluded.order_idx
        """, c)
    conn.commit()

    # Seed Bot and Admin Users
    bot_email = "bot@quantsync.ai"
    admin_email = "admin@quantsync.ai"
    cur.execute("""
        INSERT OR IGNORE INTO chat_users (email, name, avatar_url, role, bio, created_at, last_active)
        VALUES 
        (?, 'QuantSync AI Bot', 'https://api.dicebear.com/7.x/bottts/svg?seed=QuantSync', 'quant_bot', 'Automated institutional breakout intelligence bot', ?, ?),
        (?, 'QuantSync Research', 'https://api.dicebear.com/7.x/shapes/svg?seed=QuantSyncResearch', 'admin', 'Lead quantitative research & platform updates', ?, ?)
    """, (bot_email, now_str, now_str, admin_email, now_str, now_str))
    conn.commit()

    # Seed Initial Channel Messages if empty
    def seed_if_empty(ch_id: str, sender_email: str, sender_name: str, role: str, text: str, att_type: Optional[str] = None, att_url: Optional[str] = None):
        cur.execute("SELECT COUNT(*) FROM chat_messages WHERE channel_id = ?", (ch_id,))
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO chat_messages (channel_id, user_email, user_name, user_avatar, user_role, text, attachment_type, attachment_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ch_id, sender_email, sender_name,
                'https://api.dicebear.com/7.x/bottts/svg?seed=QuantSync' if role == 'quant_bot' else 'https://api.dicebear.com/7.x/shapes/svg?seed=QuantSyncResearch',
                role, text, att_type, att_url, now_str
            ))
            conn.commit()

    # 1. Stock Analysis Welcome
    init_stock_card = {
        "type": "stock_analysis_card",
        "symbol": "TATAMOTORS",
        "name": "Tata Motors Limited",
        "sector": "Consumer Cyclical",
        "price": 295.0,
        "pivot": 305.0,
        "target_1": 335.0,
        "target_2": 360.0,
        "stop_loss": 278.0,
        "dna_score": 88,
        "stage": "Stage 2 Markup",
        "stance": "BULLISH",
        "query_type": "overview",
        "buttons": [
            {"id": "technical", "label": "📊 Technical", "symbol": "TATAMOTORS"},
            {"id": "financial", "label": "💼 Financial", "symbol": "TATAMOTORS"},
            {"id": "dna", "label": "🧬 DNA Report", "symbol": "TATAMOTORS"},
            {"id": "new_stock", "label": "🔍 New Stock", "symbol": "TATAMOTORS"}
        ]
    }
    seed_if_empty(
        "stock-analysis", bot_email, "QuantSync AI Bot", "quant_bot",
        "👋 **Welcome to QuantSync AI Stock Analysis!**\n\nType any stock name or symbol (e.g. `TATAMOTORS`, `RELIANCE`, `INFY`) or use `/chart [symbol]`. I'll immediately generate the chart analysis and give you interactive Technical, Financial, and DNA breakdowns.",
        "stock_card", json.dumps(init_stock_card)
    )

    # 2. AI Buy/Sell Alert
    seed_if_empty(
        "ai-alerts", bot_email, "QuantSync AI Bot", "quant_bot",
        "⚡ **QUANT ENGINE UPDATE · V2 PORTFOLIO STATUS**\n\n🎯 **TARGET 1 HIT**: $KCPSUGIND sold at ₹29.68 (+14.99%) · 2.23R profit locked.\n🎯 **TARGET 1 HIT**: $SIGACHI sold at ₹30.41 (+15.02%) · 2.09R profit locked.\n🟢 **ACTIVE HOLD**: $VIJAYA entered at ₹1,510.60 | Target: ₹1,737.19 | SL: ₹1,412.00 | Status: In Profit."
    )

    # 3. Breakout Alert
    seed_if_empty(
        "breakout-alert", bot_email, "QuantSync AI Bot", "quant_bot",
        "🚀 **DAILY BREAKOUT RADAR ALERT**\n\n⚡ **CONFIRMED BREAKOUT**: $NOCIL cleared breakout pivot ₹204.50 with 1.8x volume surge. DNA Score: 90/100 · Target 1: ₹225.00 · SL: ₹195.00.\n🚨 **ENTERING BREAKOUT ZONE**: $POLYCAB trading at ₹6,420 (1.4% from pivot ₹6,510). 3-Contraction VCP compression active."
    )

    # 4. Recommendation
    seed_if_empty(
        "recommendation", admin_email, "QuantSync Research", "admin",
        "🎯 **INSTITUTIONAL RECOMMENDATION: $TMCV (Tata Motors)**\n\n• **Setup:** Stage 2 Ascending Base Breakout\n• **Entry Zone:** ₹295.00 - ₹302.00\n• **Target 1:** ₹335.00 (+13.5%)\n• **Target 2:** ₹360.00 (+22.0%)\n• **Stop Loss:** ₹278.00 (Risk: 5.7%)\n• **Risk / Reward:** 1 : 2.4R · DNA Score: 88/100 · Strong volume accumulation."
    )

    # 5. Traders Lounge
    seed_if_empty(
        "traders-lounge", admin_email, "QuantSync Research", "admin",
        "💬 **Welcome to the QuantSync Traders Lounge!**\n\nFeel free to share chart setups, discuss sector rotation, ask questions with `/chart [symbol]`, or launch a community poll using the 📊 **Poll** button!"
    )

    # 6. Nifty & BankNifty F&O
    seed_if_empty(
        "nifty-banknifty", admin_email, "QuantSync Research", "admin",
        "📊 **NIFTY & BANKNIFTY DERIVATIVES BRIEFING**\n\n• **Nifty Spot:** 26,420 (+0.32%) · PCR: 1.14 (Mildly Bullish)\n• **Key Resistance:** 26,500 Call Max Pain · Support: 26,300 Put Wall\n• **BankNifty Spot:** 54,180 · Straddle IV Contraction · Recommended strategy: Bull Put Spread."
    )

    conn.close()


# Initialize on import
init_chat_db()


# ── Avatar Generator ──────────────────────────────────────────────────────────
def get_default_avatar(name: str, email: str) -> str:
    """Generates a consistent, clean DiceBear avatar URL."""
    clean_seed = (name or email or "trader").strip().lower()
    return f"https://api.dicebear.com/7.x/initials/svg?seed={clean_seed}&backgroundColor=00a884,005c4b,128c7e,075e54,25d366&textColor=ffffff"


# ── User Helper ───────────────────────────────────────────────────────────────
def get_or_create_user(email: str, name: Optional[str] = None, avatar_url: Optional[str] = None) -> dict:
    email = (email or "").strip().lower()
    if not email or "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required.")

    if not name or not name.strip():
        local_part = email.split("@")[0]
        name = " ".join([part.capitalize() for part in local_part.replace(".", " ").replace("_", " ").split()])

    name = name.strip()
    if not avatar_url or not avatar_url.strip():
        avatar_url = get_default_avatar(name, email)

    now_str = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM chat_users WHERE email = ?", (email,))
    existing = cur.fetchone()

    if existing:
        cur.execute("""
            UPDATE chat_users 
            SET last_active = ?, name = COALESCE(NULLIF(?, ''), name), avatar_url = COALESCE(NULLIF(?, ''), avatar_url)
            WHERE email = ?
        """, (now_str, name, avatar_url, email))
        conn.commit()
        cur.execute("SELECT * FROM chat_users WHERE email = ?", (email,))
        user_row = cur.fetchone()
    else:
        role = 'admin' if email.startswith("admin@") else 'trader'
        cur.execute("""
            INSERT INTO chat_users (email, name, avatar_url, role, bio, created_at, last_active)
            VALUES (?, ?, ?, ?, '', ?, ?)
        """, (email, name, avatar_url, role, now_str, now_str))
        conn.commit()
        cur.execute("SELECT * FROM chat_users WHERE email = ?", (email,))
        user_row = cur.fetchone()

    conn.close()
    return dict(user_row)


# ── Stock Analysis Bot Engine ─────────────────────────────────────────────────
def generate_bot_stock_reply(symbol: str, query_type: str = "overview") -> Tuple[str, dict]:
    cache_db = os.path.join(DATA_DIR, "stock_dna_cache.db")
    if not os.path.exists(cache_db):
        return (f"⚠️ QuantSync stock database is initializing. Please try again shortly.", {})

    conn = sqlite3.connect(cache_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    clean_sym = symbol.strip().upper().replace("$", "").replace("NSE:", "").replace("BSE:", "")
    aliases = {"TATAMOTORS": "TMCV", "BAJAJ-AUTO": "BAJAJ_AUTO", "M&M": "M_M"}
    lookup_sym = aliases.get(clean_sym, clean_sym)

    row = cur.execute("SELECT * FROM stock_screener_rows WHERE symbol = ?", (lookup_sym,)).fetchone()
    if not row:
        row = cur.execute(
            "SELECT * FROM stock_screener_rows WHERE symbol LIKE ? OR company_name LIKE ? ORDER BY CASE WHEN symbol = ? THEN 0 ELSE 1 END, dna_score DESC LIMIT 1",
            (f"{clean_sym}%", f"%{clean_sym}%", clean_sym)
        ).fetchone()
    conn.close()

    if not row:
        return (f"⚠️ Symbol **{clean_sym}** not found in universe database. Try symbols like `TATAMOTORS`, `RELIANCE`, `INFY`, `TCS`, `POLYCAB`.", {})

    r = dict(row)
    sym = r.get("symbol") or clean_sym
    name = r.get("company_name") or sym
    sector = r.get("sector") or "Equity"
    cur_price = float(r.get("curr_price") or 0.0)
    pivot = float(r.get("breakout_level") or 0.0)
    tgt1 = float(r.get("target_1") or 0.0)
    tgt2 = float(r.get("target_2") or 0.0)
    sl = float(r.get("stop_loss") or 0.0)
    dna_score = int(r.get("dna_score") or 0)
    stage = r.get("minervini_stage") or "Stage 2 Markup"
    stance = r.get("stance") or "BULLISH"
    pattern = r.get("pattern_type") or "Contraction Base"
    rsi = float(r.get("rsi") or 50.0)
    ema20 = float(r.get("ema20") or 0.0)
    ema50 = float(r.get("ema50") or 0.0)
    ema200 = float(r.get("ema200") or 0.0)
    vol_ratio = float(r.get("volume_ratio_20d") or 1.0)
    mcap = float(r.get("market_cap_cr") or 0.0)
    pe = float(r.get("pe_ratio") or 0.0)
    ind_pe = float(r.get("industry_pe") or 0.0)
    roce = float(r.get("roce_pct") or 0.0)
    roe = float(r.get("roe_pct") or 0.0)
    piotroski = r.get("piotroski_score") or 0
    solvency = r.get("solvency_grade") or "A"
    fii = float(r.get("fii_holding_pct") or 0.0)
    promoter = float(r.get("promoter_holding_pct") or 0.0)
    sales_3y = float(r.get("sales_cagr_3y") or 0.0)
    profit_3y = float(r.get("profit_cagr_3y") or 0.0)
    s1 = float(r.get("support_1") or 0.0)
    r1 = float(r.get("resistance_1") or 0.0)

    if query_type in ("overview", "chart"):
        text = (
            f"📈 **{sym} · {name}** ({sector})\n\n"
            f"**LTP:** ₹{cur_price:,.2f} · **Breakout Pivot:** ₹{pivot:,.2f}\n"
            f"**Target 1:** ₹{tgt1:,.2f} | **Target 2:** ₹{tgt2:,.2f} | **Stop Loss:** ₹{sl:,.2f}\n"
            f"**DNA Conviction:** {dna_score}/100 · **Pattern:** {pattern}\n"
            f"**Institutional Stance:** {stance} ({stage})"
        )
        buttons = [
            {"id": "technical", "label": "📊 Technical", "symbol": sym},
            {"id": "financial", "label": "💼 Financial", "symbol": sym},
            {"id": "dna", "label": "🧬 DNA Report", "symbol": sym},
            {"id": "new_stock", "label": "🔍 New Stock", "symbol": sym}
        ]
    elif query_type == "technical":
        text = (
            f"📊 **Technical Deep-Dive: {sym}**\n\n"
            f"• **Trend & Stage:** {stage} · {r.get('ema_alignment') or 'BULL_STACK'}\n"
            f"• **Moving Averages:** EMA20: ₹{ema20:,.2f} | EMA50: ₹{ema50:,.2f} | EMA200: ₹{ema200:,.2f}\n"
            f"• **RSI (14):** {rsi:.1f} ({r.get('rsi_zone') or 'Bullish'}) · **ATR (14):** ₹{float(r.get('atr') or 0):,.2f}\n"
            f"• **Volume Surge:** {vol_ratio:.1f}× 20-Day Average\n"
            f"• **Support / Resistance:** S1: ₹{s1:,.2f} | R1: ₹{r1:,.2f}\n"
            f"• **Distance from 52W High:** {float(r.get('dist_from_52w_high_pct') or 0):+.1f}%"
        )
        buttons = [
            {"id": "financial", "label": "💼 Financial", "symbol": sym},
            {"id": "dna", "label": "🧬 DNA Report", "symbol": sym},
            {"id": "overview", "label": "📈 Chart & Pivot", "symbol": sym},
            {"id": "new_stock", "label": "🔍 New Stock", "symbol": sym}
        ]
    elif query_type == "financial":
        text = (
            f"💼 **Financial & Valuation Profile: {sym}**\n\n"
            f"• **Market Cap:** ₹{mcap:,.0f} Cr ({r.get('mcap_tier') or 'Mid Cap'})\n"
            f"• **Valuation:** P/E: {pe:.1f}x (Industry: {ind_pe:.1f}x) · P/B: {float(r.get('pb_ratio') or 0):.1f}x\n"
            f"• **Profitability:** ROCE: {roce:.1f}% | ROE: {roe:.1f}%\n"
            f"• **Quality & Solvency:** Piotroski: {piotroski}/9 · Solvency: Grade {solvency}\n"
            f"• **Ownership:** Promoters: {promoter:.1f}% | FIIs: {fii:.1f}%\n"
            f"• **3Y Growth:** Sales CAGR: {sales_3y:+.1f}% | Profit: {profit_3y:+.1f}%"
        )
        buttons = [
            {"id": "technical", "label": "📊 Technical", "symbol": sym},
            {"id": "dna", "label": "🧬 DNA Report", "symbol": sym},
            {"id": "overview", "label": "📈 Chart & Pivot", "symbol": sym},
            {"id": "new_stock", "label": "🔍 New Stock", "symbol": sym}
        ]
    elif query_type == "dna":
        text = (
            f"🧬 **Stock DNA 10-D Radar Report: {sym}**\n\n"
            f"• **Overall DNA Score:** {dna_score} / 100 ({'🔥 High Conviction' if dna_score >= 70 else '⚠️ Watchlist'})\n"
            f"• **Pattern Structure:** {pattern} ({r.get('pattern_stage') or 'Stage 2'})\n"
            f"• **Breakout Status:** {r.get('breakout_status') or 'IN_BASE'} · Volume Spike: {float(r.get('breakout_volume_spike') or 1.0):.1f}×\n"
            f"• **Risk / Reward:** {float(r.get('risk_reward_ratio') or 2.5):.1f}R (Target: ₹{tgt1:,.2f} | SL: ₹{sl:,.2f})\n"
            f"• **Action Stance:** {r.get('action_title') or r.get('action') or 'ACCUMULATE ON PIVOT'}"
        )
        buttons = [
            {"id": "technical", "label": "📊 Technical", "symbol": sym},
            {"id": "financial", "label": "💼 Financial", "symbol": sym},
            {"id": "overview", "label": "📈 Chart & Pivot", "symbol": sym},
            {"id": "new_stock", "label": "🔍 New Stock", "symbol": sym}
        ]
    else:
        text = f"**{sym}**: Analysis ready."
        buttons = [{"id": "overview", "label": "📈 Chart & Pivot", "symbol": sym}]

    card_data = {
        "type": "stock_analysis_card",
        "symbol": sym,
        "name": name,
        "sector": sector,
        "price": cur_price,
        "pivot": pivot,
        "target_1": tgt1,
        "target_2": tgt2,
        "stop_loss": sl,
        "dna_score": dna_score,
        "stage": stage,
        "stance": stance,
        "query_type": query_type,
        "buttons": buttons
    }
    return (text, card_data)


# ── WebSocket Connection Manager ──────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.socket_meta: Dict[WebSocket, dict] = {}
        self.lock = threading.Lock()

    async def connect(self, websocket: WebSocket, channel_id: str, email: str, name: str):
        await websocket.accept()
        with self.lock:
            if channel_id not in self.active_connections:
                self.active_connections[channel_id] = set()
            self.active_connections[channel_id].add(websocket)
            self.socket_meta[websocket] = {
                "channel_id": channel_id,
                "email": email,
                "name": name
            }
        await self.broadcast_online_count(channel_id)

    def disconnect(self, websocket: WebSocket):
        channel_id = None
        with self.lock:
            meta = self.socket_meta.pop(websocket, None)
            if meta:
                channel_id = meta.get("channel_id")
                if channel_id and channel_id in self.active_connections:
                    self.active_connections[channel_id].discard(websocket)
                    if not self.active_connections[channel_id]:
                        del self.active_connections[channel_id]
        if channel_id:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self.broadcast_online_count(channel_id))
            except Exception:
                pass

    def get_online_count(self, channel_id: str) -> int:
        with self.lock:
            sockets = len(self.active_connections.get(channel_id, set()))
            return max(sockets, 1) + 14  # active community presence baseline

    async def broadcast_online_count(self, channel_id: str):
        payload = {
            "type": "online_count",
            "channel_id": channel_id,
            "count": self.get_online_count(channel_id)
        }
        await self.broadcast(channel_id, payload)

    async def broadcast(self, channel_id: str, message: dict):
        with self.lock:
            sockets = list(self.active_connections.get(channel_id, set()))

        dead_sockets = []
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead_sockets.append(ws)

        if dead_sockets:
            with self.lock:
                for ws in dead_sockets:
                    self.disconnect(ws)


manager = ConnectionManager()


# ── Router Setup ──────────────────────────────────────────────────────────────
chat_router = APIRouter(prefix="/api/chat", tags=["Chat"])


# ── Models ────────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: str
    name: Optional[str] = None
    avatar_url: Optional[str] = None


class SendMessageRequest(BaseModel):
    email: str
    text: str
    attachment_url: Optional[str] = None
    attachment_type: Optional[str] = None
    reply_to_id: Optional[int] = None


class ReactRequest(BaseModel):
    email: str
    emoji: str


class CreatePollRequest(BaseModel):
    channel_id: str
    email: str
    question: str
    options: List[str]
    duration_minutes: int = 60
    symbol: Optional[str] = None


class VotePollRequest(BaseModel):
    email: str
    option_id: str


class Base64UploadRequest(BaseModel):
    image_base64: str
    file_name: Optional[str] = "screenshot.png"


# ── REST Endpoints ────────────────────────────────────────────────────────────

@chat_router.post("/register")
def api_register_or_login(req: RegisterRequest):
    user = get_or_create_user(req.email, req.name, req.avatar_url)
    return {"status": "ok", "user": user}


@chat_router.get("/channels")
def api_get_channels(email: Optional[str] = None):
    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT c.*,
               (SELECT COUNT(*) FROM chat_messages m WHERE m.channel_id = c.id) as total_messages
        FROM chat_channels c
        ORDER BY c.order_idx ASC
    """)
    channels = [dict(r) for r in cur.fetchall()]

    for ch in channels:
        cur.execute("""
            SELECT id, user_name, text, attachment_url, attachment_type, created_at
            FROM chat_messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (ch["id"],))
        last_m = cur.fetchone()
        if last_m:
            ch["last_message"] = {
                "id": last_m["id"],
                "user_name": last_m["user_name"],
                "text": last_m["text"],
                "has_attachment": bool(last_m["attachment_url"]),
                "attachment_type": last_m["attachment_type"],
                "created_at": last_m["created_at"]
            }
        else:
            ch["last_message"] = None

        ch["online_count"] = manager.get_online_count(ch["id"])

    conn.close()
    return {"channels": channels}


@chat_router.get("/autocomplete")
def api_chat_autocomplete(q: str = ""):
    clean_q = (q or "").strip().upper().replace("$", "")
    if not clean_q:
        return []
    cache_db = os.path.join(DATA_DIR, "stock_dna_cache.db")
    if not os.path.exists(cache_db):
        return []

    conn = sqlite3.connect(cache_db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Support space-agnostic matching so 'TATAMOTORS' finds 'Tata Motors Limited'
    clean_no_space = clean_q.replace(" ", "")
    query = """
        SELECT symbol, company_name, sector, curr_price, MAX(dna_score) as dna_score 
        FROM stock_screener_rows 
        WHERE symbol LIKE ? OR REPLACE(UPPER(company_name), ' ', '') LIKE ? 
        GROUP BY symbol
        ORDER BY CASE 
            WHEN symbol = ? THEN 0 
            WHEN symbol LIKE ? THEN 1 
            ELSE 2 
        END, dna_score DESC 
        LIMIT 8
    """
    rows = [dict(r) for r in cur.execute(query, (f"{clean_q}%", f"%{clean_no_space}%", clean_q, f"{clean_q}%")).fetchall()]
    conn.close()
    return rows


@chat_router.get("/messages/{channel_id}")
def api_get_messages(
    channel_id: str,
    limit: int = Query(50, ge=1, le=100),
    before_id: Optional[int] = None,
    current_user_email: Optional[str] = None
):
    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if before_id:
        cur.execute("""
            SELECT * FROM chat_messages
            WHERE channel_id = ? AND id < ?
            ORDER BY id DESC
            LIMIT ?
        """, (channel_id, before_id, limit))
    else:
        cur.execute("""
            SELECT * FROM chat_messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (channel_id, limit))

    raw_messages = [dict(r) for r in cur.fetchall()]
    conn.close()

    raw_messages.reverse()
    curr_email = (current_user_email or "").strip().lower()

    for msg in raw_messages:
        try:
            reactions_map = json.loads(msg.get("reactions_json") or "{}")
        except Exception:
            reactions_map = {}

        formatted_reactions = {}
        my_reactions = []
        for emoji, users in reactions_map.items():
            if isinstance(users, list) and users:
                formatted_reactions[emoji] = len(users)
                if curr_email and curr_email in users:
                    my_reactions.append(emoji)

        msg["reactions"] = formatted_reactions
        msg["my_reactions"] = my_reactions
        try:
            msg["reply_preview"] = json.loads(msg.get("reply_preview") or "null")
        except Exception:
            msg["reply_preview"] = None

    return {
        "channel_id": channel_id,
        "count": len(raw_messages),
        "messages": raw_messages
    }


@chat_router.post("/messages/{channel_id}")
async def api_send_message(channel_id: str, req: SendMessageRequest):
    text = (req.text or "").strip()
    if not text and not req.attachment_url:
        raise HTTPException(status_code=400, detail="Message text or attachment is required.")

    user = get_or_create_user(req.email)
    now_str = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM chat_channels WHERE id = ?", (channel_id,))
    ch = cur.fetchone()
    if not ch:
        conn.close()
        raise HTTPException(status_code=404, detail="Channel not found.")

    # Guard read-only channels
    if (ch["is_announcement"] or ch["is_read_only"]) and user["role"] not in ("admin", "quant_bot"):
        conn.close()
        raise HTTPException(status_code=403, detail="This is a verified broadcast channel. Only algorithmic bots and research admins can post.")

    reply_preview_json = None
    if req.reply_to_id:
        cur.execute("SELECT id, user_name, text FROM chat_messages WHERE id = ?", (req.reply_to_id,))
        rep_row = cur.fetchone()
        if rep_row:
            reply_preview_json = json.dumps({
                "id": rep_row["id"],
                "user_name": rep_row["user_name"],
                "text": rep_row["text"][:100] + ("..." if len(rep_row["text"]) > 100 else "")
            })

    cur.execute("""
        INSERT INTO chat_messages (
            channel_id, user_email, user_name, user_avatar, user_role,
            text, attachment_url, attachment_type, reply_to_id, reply_preview,
            reactions_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
    """, (
        channel_id, user["email"], user["name"], user["avatar_url"], user["role"],
        text, req.attachment_url, req.attachment_type, req.reply_to_id, reply_preview_json,
        now_str
    ))
    new_id = cur.lastrowid
    conn.commit()

    cur.execute("SELECT * FROM chat_messages WHERE id = ?", (new_id,))
    msg_dict = dict(cur.fetchone())
    conn.close()

    try:
        msg_dict["reply_preview"] = json.loads(msg_dict.get("reply_preview") or "null")
    except Exception:
        msg_dict["reply_preview"] = None
    msg_dict["reactions"] = {}
    msg_dict["my_reactions"] = []

    # Broadcast user's message
    payload = {
        "type": "new_message",
        "channel_id": channel_id,
        "message": msg_dict
    }
    await manager.broadcast(channel_id, payload)

    # ── AI Bot Processing (Stock Analysis or Slash Commands) ──────────────────
    async def process_bot_reply():
        target_sym = None
        q_type = "overview"

        if text.startswith("[ACTION:"):
            # Format: [ACTION:query_type:SYMBOL]
            parts = text.replace("[ACTION:", "").replace("]", "").split(":")
            if len(parts) >= 2:
                q_type = parts[0].strip().lower()
                target_sym = parts[1].strip().upper()
                if q_type == "new_stock":
                    bot_text = "🔍 **Type any stock name or symbol in the input box below:**\n\nOr tap any trending ticker below to analyze:"
                    bot_card = {
                        "type": "stock_analysis_card",
                        "symbol": "",
                        "query_type": "new_stock",
                        "buttons": [
                            {"id": "select_sym", "label": "📈 TATAMOTORS", "symbol": "TATAMOTORS"},
                            {"id": "select_sym", "label": "📈 RELIANCE", "symbol": "RELIANCE"},
                            {"id": "select_sym", "label": "📈 HDFCBANK", "symbol": "HDFCBANK"},
                            {"id": "select_sym", "label": "📈 INFY", "symbol": "INFY"},
                            {"id": "select_sym", "label": "📈 POLYCAB", "symbol": "POLYCAB"}
                        ]
                    }
                    await save_and_broadcast_bot(channel_id, bot_text, bot_card)
                    return
        elif text.startswith("/chart "):
            target_sym = text.replace("/chart ", "").strip().upper()
            q_type = "overview"
        elif text.startswith("/dna "):
            target_sym = text.replace("/dna ", "").strip().upper()
            q_type = "dna"
        elif text.startswith("/finance "):
            target_sym = text.replace("/finance ", "").strip().upper()
            q_type = "financial"
        elif text.startswith("/technical "):
            target_sym = text.replace("/technical ", "").strip().upper()
            q_type = "technical"
        elif channel_id == "stock-analysis":
            # Direct stock typed in Stock Analysis channel
            target_sym = text.strip().upper().replace("$", "")
            q_type = "overview"

        if target_sym:
            bot_text, bot_card = generate_bot_stock_reply(target_sym, q_type)
            await save_and_broadcast_bot(channel_id, bot_text, bot_card)

    async def save_and_broadcast_bot(ch_id: str, b_text: str, b_card: dict):
        bot_now = datetime.now(timezone.utc).isoformat()
        conn2 = sqlite3.connect(CHAT_DB)
        conn2.row_factory = sqlite3.Row
        cur2 = conn2.cursor()
        cur2.execute("""
            INSERT INTO chat_messages (
                channel_id, user_email, user_name, user_avatar, user_role,
                text, attachment_url, attachment_type, reactions_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
        """, (
            ch_id, "bot@quantsync.ai", "QuantSync AI Bot",
            "https://api.dicebear.com/7.x/bottts/svg?seed=QuantSync", "quant_bot",
            b_text, json.dumps(b_card), "stock_card", bot_now
        ))
        bot_id = cur2.lastrowid
        conn2.commit()
        cur2.execute("SELECT * FROM chat_messages WHERE id = ?", (bot_id,))
        bot_dict = dict(cur2.fetchone())
        conn2.close()

        bot_dict["reactions"] = {}
        bot_dict["my_reactions"] = []
        bot_dict["reply_preview"] = None

        await manager.broadcast(ch_id, {
            "type": "new_message",
            "channel_id": ch_id,
            "message": bot_dict
        })

    # Trigger bot response asynchronously if matched
    import asyncio
    asyncio.create_task(process_bot_reply())

    return {"status": "ok", "message": msg_dict}


# ── Trading Polls Endpoints ───────────────────────────────────────────────────

@chat_router.post("/poll/create")
async def api_create_poll(req: CreatePollRequest):
    user = get_or_create_user(req.email)
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.isoformat()
    duration_mins = max(min(req.duration_minutes, 240), 10)
    expires_dt = now_dt + timedelta(minutes=duration_mins)
    expires_str = expires_dt.isoformat()

    poll_options = []
    for idx, opt_text in enumerate(req.options[:5]):
        t = (opt_text or "").strip()
        if t:
            poll_options.append({
                "id": f"opt_{idx}",
                "text": t,
                "votes": []
            })

    if len(poll_options) < 2:
        raise HTTPException(status_code=400, detail="A poll must have at least 2 options.")

    poll_data = {
        "poll_id": str(uuid.uuid4())[:8],
        "question": req.question.strip(),
        "symbol": (req.symbol or "").strip().upper(),
        "options": poll_options,
        "created_at": now_str,
        "expires_at": expires_str,
        "duration_minutes": duration_mins,
        "total_votes": 0,
        "status": "active"
    }

    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO chat_messages (
            channel_id, user_email, user_name, user_avatar, user_role,
            text, attachment_url, attachment_type, reactions_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'poll', '{}', ?)
    """, (
        req.channel_id, user["email"], user["name"], user["avatar_url"], user["role"],
        f"📊 **Poll:** {poll_data['question']}", json.dumps(poll_data), now_str
    ))
    new_id = cur.lastrowid
    conn.commit()

    cur.execute("SELECT * FROM chat_messages WHERE id = ?", (new_id,))
    msg_dict = dict(cur.fetchone())
    conn.close()

    msg_dict["reactions"] = {}
    msg_dict["my_reactions"] = []
    msg_dict["reply_preview"] = None

    payload = {
        "type": "new_message",
        "channel_id": req.channel_id,
        "message": msg_dict
    }
    await manager.broadcast(req.channel_id, payload)
    return {"status": "ok", "message": msg_dict, "poll": poll_data}


@chat_router.post("/poll/{message_id}/vote")
async def api_vote_poll(message_id: int, req: VotePollRequest):
    email = (req.email or "").strip().lower()
    option_id = (req.option_id or "").strip()
    if not email or not option_id:
        raise HTTPException(status_code=400, detail="Email and option_id required.")

    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,))
    row = cur.fetchone()
    if not row or row["attachment_type"] != "poll":
        conn.close()
        raise HTTPException(status_code=404, detail="Poll message not found.")

    channel_id = row["channel_id"]
    try:
        poll = json.loads(row["attachment_url"])
    except Exception:
        conn.close()
        raise HTTPException(status_code=500, detail="Corrupted poll data.")

    now_iso = datetime.now(timezone.utc).isoformat()
    # Check expiry
    if poll.get("expires_at") and now_iso > poll["expires_at"]:
        poll["status"] = "ended"
        conn.close()
        return {"status": "ended", "poll": poll, "detail": "Poll has already ended."}

    # Register Vote (Single vote per user: remove prior vote if any)
    for opt in poll["options"]:
        if email in opt.get("votes", []):
            opt["votes"].remove(email)
        if opt["id"] == option_id:
            opt.setdefault("votes", []).append(email)

    total_votes = sum(len(opt.get("votes", [])) for opt in poll["options"])
    poll["total_votes"] = total_votes

    new_poll_json = json.dumps(poll)
    cur.execute("UPDATE chat_messages SET attachment_url = ? WHERE id = ?", (new_poll_json, message_id))
    conn.commit()
    conn.close()

    # Broadcast poll update
    await manager.broadcast(channel_id, {
        "type": "poll_update",
        "channel_id": channel_id,
        "message_id": message_id,
        "poll": poll
    })

    return {"status": "ok", "poll": poll}


# ── Reactions ─────────────────────────────────────────────────────────────────

@chat_router.post("/messages/{message_id}/react")
async def api_react_message(message_id: int, req: ReactRequest):
    emoji = (req.emoji or "").strip()
    email = (req.email or "").strip().lower()
    if not emoji or not email:
        raise HTTPException(status_code=400, detail="Email and emoji are required.")

    conn = sqlite3.connect(CHAT_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT id, channel_id, reactions_json FROM chat_messages WHERE id = ?", (message_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Message not found.")

    channel_id = row["channel_id"]
    try:
        reactions_map = json.loads(row["reactions_json"] or "{}")
    except Exception:
        reactions_map = {}

    user_list = reactions_map.get(emoji, [])
    if email in user_list:
        user_list.remove(email)
        if not user_list:
            reactions_map.pop(emoji, None)
        else:
            reactions_map[emoji] = user_list
    else:
        user_list.append(email)
        reactions_map[emoji] = user_list

    new_reactions_json = json.dumps(reactions_map)
    cur.execute("UPDATE chat_messages SET reactions_json = ? WHERE id = ?", (new_reactions_json, message_id))
    conn.commit()
    conn.close()

    formatted_reactions = {k: len(v) for k, v in reactions_map.items() if v}

    payload = {
        "type": "reaction_update",
        "channel_id": channel_id,
        "message_id": message_id,
        "reactions": formatted_reactions
    }
    await manager.broadcast(channel_id, payload)

    return {
        "status": "ok",
        "message_id": message_id,
        "reactions": formatted_reactions
    }


# ── Upload Endpoint ───────────────────────────────────────────────────────────

@chat_router.post("/upload")
async def api_upload_file(payload: Base64UploadRequest):
    if not payload or not payload.image_base64:
        raise HTTPException(status_code=400, detail="No base64 image provided.")

    filename = f"chat_{uuid.uuid4().hex[:12]}.png"
    filepath = os.path.join(UPLOADS_DIR, filename)

    raw_b64 = payload.image_base64
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]

    try:
        img_bytes = base64.b64decode(raw_b64)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid base64 image data: {e}")

    public_url = f"/uploads/chat/{filename}"
    return {"status": "ok", "url": public_url, "filename": filename}


# ── WebSocket Endpoint ────────────────────────────────────────────────────────
async def chat_websocket_endpoint(websocket: WebSocket, channel_id: str):
    email = websocket.query_params.get("email") or "anonymous@quantsync.ai"
    name = websocket.query_params.get("name") or "Trader"

    await manager.connect(websocket, channel_id, email, name)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "typing":
                is_typing = bool(data.get("is_typing", True))
                await manager.broadcast(channel_id, {
                    "type": "typing",
                    "channel_id": channel_id,
                    "email": email,
                    "name": name,
                    "is_typing": is_typing
                })
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        log.debug(f"WebSocket error: {e}")
        manager.disconnect(websocket)
