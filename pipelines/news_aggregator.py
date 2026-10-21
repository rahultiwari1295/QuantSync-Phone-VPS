"""
news_aggregator.py — Institutional Multi-Source Financial News & Intelligence Engine
=====================================================================================
Aggregates real-time financial headlines, corporate filings, and market catalysts from:
1. Economic Times Markets RSS (Live)
2. LiveMint Markets RSS (Live)
3. Google News India Equities (Live)
4. Moneycontrol Markets (XML Sanitized)
5. Trendlyne BSE/NSE Filings & Corporate Insights

Provides automated stock ticker tagging, category classification, sentiment analysis,
and relative time calculation.
"""

import sys, os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
for _sub in ("common", "core", "api", "pipelines", "engines", "collectors", "intel"):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


import os
import re
import sys
import time
import json
import sqlite3
import hashlib
import logging
import datetime
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List, Any, Optional

logger = logging.getLogger("NewsAggregator")

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(SCRIPT_DIR, "Data", "market_intel.db")

RSS_FEEDS = [
    {
        "source": "Economic Times",
        "category": "Markets",
        "url": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"
    },
    {
        "source": "LiveMint",
        "category": "Markets",
        "url": "https://www.livemint.com/rss/markets"
    },
    {
        "source": "Google News",
        "category": "Equities",
        "url": "https://news.google.com/rss/search?q=NSE+stocks+India+when:2d&hl=en-IN&gl=IN&ceid=IN:en"
    },
    {
        "source": "Moneycontrol",
        "category": "Reports",
        "url": "https://www.moneycontrol.com/rss/marketreports.xml"
    }
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

# Common High-Frequency Ticker Aliases for Intelligent Matching
TICKER_MAP = {
    "RELIANCE": ["RELIANCE", "RIL", "JIO"],
    "TCS": ["TCS", "TATA CONSULTANCY"],
    "HDFCBANK": ["HDFC BANK", "HDFCBANK"],
    "ICICIBANK": ["ICICI BANK", "ICICIBANK"],
    "SBIN": ["STATE BANK OF INDIA", "SBI"],
    "BHARTIARTL": ["BHARTI AIRTEL", "AIRTEL"],
    "INFY": ["INFOSYS"],
    "ITC": ["ITC"],
    "LT": ["LARSEN & TOUBRO", "L&T"],
    "TATAMOTORS": ["TATA MOTORS", "TATAMOTORS", "JLR"],
    "TATASTEEL": ["TATA STEEL"],
    "MARUTI": ["MARUTI SUZUKI", "MARUTI"],
    "WIPRO": ["WIPRO"],
    "HCLTECH": ["HCL TECH", "HCL TECHNOLOGIES"],
    "SUNPHARMA": ["SUN PHARMA"],
    "BAJFINANCE": ["BAJAJ FINANCE"],
    "ADANIENT": ["ADANI ENTERPRISES"],
    "ADANIPORTS": ["ADANI PORTS"],
    "KOTAKBANK": ["KOTAK MAHINDRA", "KOTAK BANK"],
    "AXISBANK": ["AXIS BANK"],
    "NTPC": ["NTPC"],
    "ONGC": ["ONGC", "OIL AND NATURAL GAS"],
    "POWERGRID": ["POWER GRID"],
    "COALINDIA": ["COAL INDIA"],
    "VEDL": ["VEDANTA"],
    "JSWSTEEL": ["JSW STEEL"],
    "HINDALCO": ["HINDALCO"],
    "GRASIM": ["GRASIM"],
    "TECHM": ["TECH MAHINDRA"],
    "CIPLA": ["CIPLA"],
    "UPL": ["UPL"],
    "IDEA": ["VODAFONE IDEA", "VI "],
    "ZOMATO": ["ZOMATO"],
    "PAYTM": ["PAYTM", "ONE97"],
    "HUDCO": ["HUDCO"],
    "BEML": ["BEML"],
    "GRSE": ["GRSE", "GARDEN REACH"],
    "HAL": ["HAL", "HINDUSTAN AERONAUTICS"],
    "BEL": ["BEL", "BHARAT ELECTRONICS"],
    "IRCTC": ["IRCTC"],
    "RVNL": ["RVNL", "RAIL VIKAS"],
    "IREDA": ["IREDA"],
    "INDORAMA": ["INDORAMA"],
    "NEOGEN": ["NEOGEN CHEMICALS", "NEOGEN"]
}


def init_news_db(db_path: str = DEFAULT_DB_PATH):
    """Ensures market_news_feed table exists with comprehensive columns."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_news_feed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guid TEXT UNIQUE,
                title TEXT NOT NULL,
                summary TEXT,
                source TEXT NOT NULL,
                category TEXT DEFAULT 'Markets',
                tag TEXT DEFAULT 'market',
                impact TEXT DEFAULT 'neutral',
                url TEXT,
                stock_code TEXT,
                stock_name TEXT,
                published_at TEXT,
                published_ts INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Check and add any missing columns if table existed previously
        cur.execute("PRAGMA table_info(market_news_feed)")
        existing_cols = {c[1] for c in cur.fetchall()}
        cols_to_add = [
            ("published_ts", "INTEGER"),
            ("tag", "TEXT DEFAULT 'market'"),
            ("impact", "TEXT DEFAULT 'neutral'"),
            ("stock_name", "TEXT")
        ]
        for col, col_type in cols_to_add:
            if col not in existing_cols:
                try:
                    cur.execute(f"ALTER TABLE market_news_feed ADD COLUMN {col} {col_type}")
                except Exception:
                    pass

        cur.execute("CREATE INDEX IF NOT EXISTS idx_news_guid ON market_news_feed(guid)")
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_news_ts ON market_news_feed(published_ts)")
        except Exception:
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_news_stock ON market_news_feed(stock_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_news_category ON market_news_feed(category)")
        conn.commit()
    finally:
        conn.close()


def clean_html(raw_html: str) -> str:
    """Removes HTML tags and cleans entities from text."""
    if not raw_html:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw_html)
    clean = re.sub(r"&amp;", "&", clean)
    clean = re.sub(r"&quot;", '"', clean)
    clean = re.sub(r"&#39;", "'", clean)
    clean = re.sub(r"&apos;", "'", clean)
    clean = re.sub(r"&nbsp;", " ", clean)
    clean = re.sub(r"&lt;", "<", clean)
    clean = re.sub(r"&gt;", ">", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def parse_pub_timestamp(date_str: str) -> int:
    """Parses various date string formats into unix epoch timestamp."""
    if not date_str:
        return int(time.time())
    
    date_str_clean = date_str.strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%a, %d %b %Y %H:%M:%S +0530",
        "%Y-%m-%d %H:%M:%S",
        "%d %b, %Y %I:%M %p (IST)",
        "%d %b, %Y %I:%M %p IST",
        "%d %b, %Y %H:%M IST",
        "%Y-%m-%dT%H:%M:%SZ"
    ]
    
    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(date_str_clean, fmt)
            return int(dt.timestamp())
        except Exception:
            continue

    # Fallback to current time if unparseable
    return int(time.time())


def format_relative_time(ts: int) -> str:
    """Formats timestamp into friendly relative time: '5m ago', '1h ago', or 'Today 14:30'."""
    now = int(time.time())
    diff = max(0, now - ts)

    if diff < 60:
        return "Just now"
    elif diff < 3600:
        mins = diff // 60
        return f"{mins}m ago"
    elif diff < 86400:
        hrs = diff // 3600
        return f"{hrs}h ago"
    elif diff < 172800:
        return "Yesterday"
    else:
        dt = datetime.datetime.fromtimestamp(ts)
        return dt.strftime("%d %b")


def extract_stock_entity(text: str) -> tuple[Optional[str], Optional[str]]:
    """Identifies mentioned stock ticker and company name from headline."""
    upper_text = text.upper()
    
    for sym, aliases in TICKER_MAP.items():
        for alias in aliases:
            pattern = r"\b" + re.escape(alias) + r"\b"
            if re.search(pattern, upper_text):
                return sym, alias.title()
                
    # Regex fallback for standalone NSE ticker patterns (e.g., "VEDL:", "[TATAMOTORS]")
    m = re.search(r"\b([A-Z]{3,12})\b(?:\s*shares|\s*stock|\s*rallies|\s*surges|\s*plunges)", text)
    if m:
        cand = m.group(1).upper()
        if cand not in ["INDIA", "MARKET", "NIFTY", "SENSEX", "STOCK", "SHARE", "GLOBAL", "BANKS", "TODAY"]:
            return cand, cand

    return None, None


def classify_news(title: str, desc: str) -> tuple[str, str, str]:
    """
    Returns (category, tag, impact):
    category: Corporate, Earnings, Macro, Breaking, Markets
    tag: order, fund, results, jv, macro, ipo, alert
    impact: bullish, bearish, neutral
    """
    full_text = (title + " " + desc).lower()

    # Category & Tag
    if any(w in full_text for w in ["order", "bagged", "contract", "wins order", "awarded"]):
        category, tag = "Corporate", "order"
    elif any(w in full_text for w in ["qip", "fundraise", "debt", "loan", "consortium", "secures $", "credit line"]):
        category, tag = "Corporate", "fund"
    elif any(w in full_text for w in ["q1", "q2", "q3", "q4", "quarterly", "net profit", "ebitda", "pat up", "pat falls", "revenue up"]):
        category, tag = "Earnings", "results"
    elif any(w in full_text for w in ["jv", "joint venture", "partnership", "mou", "strategic pact"]):
        category, tag = "Corporate", "jv"
    elif any(w in full_text for w in ["rbi", "inflation", "cpi", "wpi", "fed", "repo rate", "gdp", "direct tax", "gst"]):
        category, tag = "Macro", "macro"
    elif any(w in full_text for w in ["ipo", "debut", "listing", "premium to issue"]):
        category, tag = "Markets", "ipo"
    else:
        category, tag = "Markets", "market"

    # Impact Sentiment
    bull_words = ["surges", "jump", "rallies", "higher", "gains", "order win", "secures", "record high", "soars", "approval", "wins", "nod"]
    bear_words = ["falls", "slumps", "plunges", "down", "drags", "penalty", "fraud", "probe", "cuts", "loss", "crash", "declines"]

    if any(w in full_text for w in bull_words):
        impact = "bullish"
    elif any(w in full_text for w in bear_words):
        impact = "bearish"
    else:
        impact = "neutral"

    return category, tag, impact


def sanitize_xml(content: bytes) -> bytes:
    """Removes invalid control characters that break ElementTree XML parsing."""
    try:
        text = content.decode("utf-8", errors="ignore")
        text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
        return text.encode("utf-8")
    except Exception:
        return content


def fetch_feed_items(feed_cfg: Dict[str, str]) -> List[Dict[str, Any]]:
    """Fetches, parses and structures items from an individual RSS source."""
    items = []
    url = feed_cfg["url"]
    source = feed_cfg["source"]

    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw_content = resp.read()
            clean_bytes = sanitize_xml(raw_content)
            root = ET.fromstring(clean_bytes)

            for item in root.findall(".//item"):
                t_el = item.find("title")
                l_el = item.find("link")
                d_el = item.find("description")
                p_el = item.find("pubDate")
                g_el = item.find("guid")

                raw_title = clean_html(t_el.text if t_el is not None else "")
                link = (l_el.text or "").strip() if l_el is not None else ""
                raw_desc = clean_html(d_el.text if d_el is not None else "")
                pub_date = (p_el.text or "").strip() if p_el is not None else ""
                guid = (g_el.text or "").strip() if g_el is not None else link

                if not raw_title:
                    continue

                if not guid:
                    guid = hashlib.md5((raw_title + source).encode("utf-8")).hexdigest()

                ts = parse_pub_timestamp(pub_date)
                stock_code, stock_name = extract_stock_entity(raw_title)
                category, tag, impact = classify_news(raw_title, raw_desc)

                items.append({
                    "guid": guid,
                    "title": raw_title,
                    "summary": raw_desc[:300] if raw_desc else raw_title,
                    "source": source,
                    "category": category,
                    "tag": tag,
                    "impact": impact,
                    "url": link,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "published_at": pub_date,
                    "published_ts": ts
                })
    except Exception as e:
        logger.warning(f"Error fetching RSS from {url}: {e}")

    return items


def import_trendlyne_insights(db_path: str = DEFAULT_DB_PATH) -> int:
    """Transfers Trendlyne corporate notifications into market_news_feed."""
    imported = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trendlyne_market_insights'")
        if not cur.fetchone():
            return 0

        cur.execute("SELECT * FROM trendlyne_market_insights ORDER BY id DESC LIMIT 100")
        rows = cur.fetchall()

        for r in rows:
            stk = r["stock_code"] or r["nse_code"] or ""
            co_name = r["stock_name"] or stk
            title = r["notification"] or ""
            if not title:
                continue

            guid = f"trendlyne_{r['id']}_{stk}"
            url = r["complete_url"] or r["screen_redirect_url"] or f"https://trendlyne.com/equity/{stk}/"
            t_str = r["time_stamp"] or r["fetched_at"] or ""
            ts = parse_pub_timestamp(t_str)

            category, tag, impact = classify_news(title, r["label"] or "")

            cur.execute("""
                INSERT OR IGNORE INTO market_news_feed (
                    guid, title, summary, source, category, tag, impact, url, stock_code, stock_name, published_at, published_ts
                ) VALUES (?, ?, ?, 'Trendlyne', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (guid, title, f"{co_name} · {r['label'] or 'Filing'}", category, tag, impact, url, stk, co_name, t_str, ts))
            if cur.rowcount > 0:
                imported += 1
        conn.commit()

    return imported


def sync_all_news(db_path: str = DEFAULT_DB_PATH) -> Dict[str, Any]:
    """Primary ingestion entry point with deduplication and auto-pruning."""
    init_news_db(db_path)
    total_added = 0
    breakdown = {}

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        for feed in RSS_FEEDS:
            items = fetch_feed_items(feed)
            feed_added = 0
            for it in items:
                cur.execute("""
                    INSERT OR IGNORE INTO market_news_feed (
                        guid, title, summary, source, category, tag, impact, url, stock_code, stock_name, published_at, published_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    it["guid"], it["title"], it["summary"], it["source"],
                    it["category"], it["tag"], it["impact"], it["url"],
                    it["stock_code"], it["stock_name"], it["published_at"], it["published_ts"]
                ))
                if cur.rowcount > 0:
                    feed_added += 1
            conn.commit()
            total_added += feed_added
            breakdown[feed["source"]] = breakdown.get(feed["source"], 0) + feed_added

    # Sync Trendlyne corporate filings
    tl_added = import_trendlyne_insights(db_path)
    total_added += tl_added
    breakdown["Trendlyne"] = tl_added

    # Purge news older than 5 days
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        five_days_ago = int(time.time()) - (5 * 86400)
        cur.execute("DELETE FROM market_news_feed WHERE published_ts < ?", (five_days_ago,))
        purged = cur.rowcount
        conn.commit()

    return {
        "status": "SUCCESS",
        "total_new_articles": total_added,
        "breakdown": breakdown,
        "purged_old_articles": purged,
        "timestamp": datetime.datetime.now().isoformat()
    }


def get_latest_news(
    db_path: str = DEFAULT_DB_PATH,
    limit: int = 50,
    stock: Optional[str] = None,
    category: Optional[str] = None,
    source: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieves formatted real-time news articles with relative time and stock metadata."""
    init_news_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        query = "SELECT * FROM market_news_feed WHERE 1=1"
        params: List[Any] = []

        if stock:
            query += " AND (stock_code = ? OR title LIKE ? OR summary LIKE ?)"
            params.extend([stock.strip().upper(), f"%{stock.strip()}%", f"%{stock.strip()}%"])

        if category and category.lower() != "all":
            query += " AND LOWER(category) = ?"
            params.append(category.strip().lower())

        if source and source.lower() != "all":
            query += " AND source = ?"
            params.append(source.strip())

        query += " ORDER BY published_ts DESC, id DESC LIMIT ?"
        params.append(limit)

        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    formatted = []
    for r in rows:
        ts = r.get("published_ts") or parse_pub_timestamp(r.get("published_at"))
        rel_time = format_relative_time(ts)
        
        # Day header for grouping
        dt = datetime.datetime.fromtimestamp(ts)
        now_dt = datetime.datetime.now()
        if dt.date() == now_dt.date():
            day_label = f"Today · {dt.strftime('%d %B')}"
        elif (now_dt.date() - dt.date()).days == 1:
            day_label = f"Yesterday · {dt.strftime('%d %B')}"
        else:
            day_label = dt.strftime("%d %B")

        formatted.append({
            "id": r["id"],
            "guid": r["guid"],
            "title": r["title"],
            "label": r["title"],
            "summary": r["summary"],
            "body": r["summary"],
            "notification": r["summary"],
            "source": r["source"],
            "src": (r.get("stock_code") or r["source"][:2]).upper(),
            "category": r["category"],
            "tag": r.get("tag") or "market",
            "impact": r.get("impact") or "neutral",
            "url": r["url"],
            "sym": r.get("stock_code") or "",
            "stock_code": r.get("stock_code") or "",
            "co": r.get("stock_name") or r.get("stock_code") or r["source"],
            "stock_name": r.get("stock_name") or r.get("stock_code") or r["source"],
            "time": rel_time,
            "day": day_label,
            "published_at": r["published_at"],
            "published_ts": ts
        })

    return formatted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    stats = sync_all_news()
    print("Sync Result:", json.dumps(stats, indent=2))
    sample = get_latest_news(limit=6)
    print("\nSample Live Articles:")
    for a in sample:
        print(f"[{a['source']}] [{a['time']}] {a['sym']}: {a['title']}")
