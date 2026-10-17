"""Collectors. Each pulls one public source, stores raw facts in their own table and one row per item in `events`.
Every collector is idempotent (content-hash ids), so running twice never duplicates anything.

NSE JSON endpoints change without notice; every parser reads fields defensively (several possible key names) and a
source that fails is recorded in source_state and skipped, never allowed to stop the rest of the run."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from . import classify as C
from . import db
from .net import Blocked, Net
from .score import impact

NSE = "https://www.nseindia.com/api"
ARCH = "https://nsearchives.nseindia.com"
BSE = "https://api.bseindia.com/BseIndiaAPI/api"


# ---------------------------------------------------------------- universe: names, ISINs, market caps
class Universe:
    def __init__(self):
        self.by_sym: dict[str, dict] = {}
        self.by_isin: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        for path, sql in ((db.COLLECTOR_DB, "SELECT symbol, isin, name FROM listing"),
                          (db.HIST_DB, "SELECT symbol, NULL, company_name FROM stock_screener_rows GROUP BY symbol")):
            try:
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=20)
                for s, i, n in con.execute(sql):
                    if not s:
                        continue
                    d = self.by_sym.setdefault(s, {"symbol": s})
                    if i and not d.get("isin"):
                        d["isin"] = i; self.by_isin[i] = s
                    if n and not d.get("name"):
                        d["name"] = n; self.by_name[self._norm(n)] = s
                con.close()
            except sqlite3.Error:
                pass
        self.mcap: dict[str, float] = {}
        try:
            con = sqlite3.connect(f"file:{db.COLLECTOR_DB}?mode=ro", uri=True, timeout=20)
            self.mcap = {s: m for s, m in con.execute("SELECT symbol, market_cap_cr FROM ratios_daily") if m}
            con.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _norm(n: str) -> str:
        n = re.sub(r"[^a-z0-9 ]", " ", (n or "").lower())
        n = re.sub(r"\b(limited|ltd|the|india|co|company|corporation|corp|inc|pvt|private)\b", " ", n)
        return " ".join(n.split())

    def resolve(self, symbol: str | None = None, isin: str | None = None, name: str | None = None) -> str | None:
        if symbol and (not self.by_sym or symbol in self.by_sym):
            return symbol
        if isin and isin in self.by_isin:
            return self.by_isin[isin]
        if name:
            return self.by_name.get(self._norm(name))
        return symbol

    def name(self, sym: str | None) -> str:
        return (self.by_sym.get(sym or "", {}).get("name")) or (sym or "")


# ---------------------------------------------------------------- helpers
def _g(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, "", "-", "NA", "nil"):
            return d[k]
    return default


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        return float(str(x).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


_DT = ["%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
       "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%B-%Y"]


def parse_dt(s) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for cand in dict.fromkeys((s, s[:26], s[:20], s[:19], s[:11], s[:10])):
        for f in _DT:
            try:
                return datetime.strptime(cand.strip(), f)
            except ValueError:
                continue
    try:
        return parsedate_to_datetime(s).astimezone(db.IST).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def iso(d: datetime | None, fallback: datetime | None = None) -> str:
    d = d or fallback or db.now_ist().replace(tzinfo=None)
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def known(d: datetime | None) -> str:
    """Filed after 15:30 IST -> tradable (known) the next day."""
    d = d or db.now_ist().replace(tzinfo=None)
    if d.hour * 60 + d.minute > 15 * 60 + 30:
        d = d + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def qty_fmt(q: float | None) -> str:
    if not q:
        return ""
    return f"{q / 1e7:.2f} cr" if q >= 1e7 else f"{q / 1e5:.1f} lakh" if q >= 1e5 else f"{int(q):,}"


def _first_in_year(con, sym: str, typ: str, ts: str) -> bool:
    r = con.execute("SELECT 1 FROM events WHERE symbol=? AND type=? AND ts<? AND ts>=date(?, '-365 day') LIMIT 1", (sym, typ, ts, ts)).fetchone()
    return r is None


def _event(con, U: Universe, *, sym, isin=None, company=None, ts, typ=None, base=None, sent=None, category, source, url=None,
           headline, detail="", numbers=None, value_cr=None, promoter=False, ai=False) -> bool:
    if typ is None:
        typ, base, sent = C.classify(headline, detail, category)
    company = company or U.name(sym)
    t = parse_dt(ts) if isinstance(ts, str) else ts
    ts_iso = iso(t)
    score, parts = impact(base or 15, value_cr if value_cr is not None else C.money_cr(f"{headline} {detail}"),
                          U.mcap.get(sym or ""), sym is not None and _first_in_year(con, sym, typ, ts_iso), promoter)
    nums = dict(numbers or {}); nums["_score"] = parts
    ev = {"id": db.h(sym or company, typ, headline, ts_iso[:10], url or ""), "symbol": sym, "isin": isin, "company": company,
          "ts": ts_iso, "known_at": known(t), "type": typ, "category": category, "source": source, "url": url,
          "headline": (headline or "").strip()[:400], "detail": (detail or "").strip()[:4000], "sentiment": sent, "impact": score,
          "numbers_json": nums, "summary": C.summary(typ, company, headline, detail, numbers),
          "ai_status": "pending" if ai and typ in C.READ_TYPES and url and url.lower().endswith(".pdf") else "none"}
    return db.upsert_event(con, ev)


def _dates(days: int):
    end = db.now_ist().replace(tzinfo=None)
    return (end - timedelta(days=days)), end


# ---------------------------------------------------------------- filings
def nse_announcements(con, net: Net, U: Universe, days: int = 1) -> int:
    a, b = _dates(days)
    data = net.json(f"{NSE}/corporate-announcements?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", "nse")
    rows = data if isinstance(data, list) else data.get("data", [])
    n = 0
    for r in rows:
        sym = U.resolve(_g(r, "symbol"), _g(r, "isin", "sm_isin"), _g(r, "sm_name"))
        head = _g(r, "desc", "subject", default="Filing")
        det = _g(r, "attchmntText", "details", default="")
        n += _event(con, U, sym=sym, isin=_g(r, "sm_isin", "isin"), company=_g(r, "sm_name"), ts=_g(r, "sort_date", "an_dt", "exchdisstime"),
                    category="filing", source="NSE", url=_g(r, "attchmntFile"), headline=head, detail=det, ai=True)
    return n


def bse_announcements(con, net: Net, U: Universe, days: int = 1) -> int:
    a, b = _dates(days)
    n = 0
    for page in range(1, 6):
        url = (f"{BSE}/AnnSubCategoryGetData/w?pageno={page}&strCat=-1&strPrevDate={a:%Y%m%d}&strScrip=&strSearch=P"
               f"&strToDate={b:%Y%m%d}&strType=C&subcategory=-1")
        data = net.json(url, "bse")
        rows = data.get("Table", []) if isinstance(data, dict) else []
        if not rows:
            break
        for r in rows:
            name = _g(r, "SLONGNAME", "SHORT_NAME")
            sym = U.resolve(None, None, name)
            att = _g(r, "ATTACHMENTNAME")
            url_pdf = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{att}" if att else _g(r, "NSURL")
            n += _event(con, U, sym=sym, company=name, ts=_g(r, "DT_TM", "NEWS_DT"), category="filing", source="BSE", url=url_pdf,
                        headline=_g(r, "NEWSSUB", "HEADLINE", default="Filing"), detail=_g(r, "HEADLINE", default="") or "",
                        ai=True)
    return n


def board_meetings(con, net: Net, U: Universe, days_back: int = 2, days_ahead: int = 30) -> int:
    a = db.now_ist().replace(tzinfo=None) - timedelta(days=days_back)
    b = db.now_ist().replace(tzinfo=None) + timedelta(days=days_ahead)
    data = net.json(f"{NSE}/corporate-board-meetings?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", "nse")
    rows = data if isinstance(data, list) else data.get("data", [])
    n = 0
    for r in rows:
        sym = U.resolve(_g(r, "bm_symbol", "symbol"), _g(r, "sm_isin"), _g(r, "sm_name"))
        md = parse_dt(_g(r, "bm_date", "meetingDate"))
        if not sym or not md:
            continue
        purpose = _g(r, "bm_purpose", "purpose", default="")
        is_res = int(bool(re.search(r"result", f"{purpose} {_g(r, 'bm_desc', default='')}", re.I)))
        filed = parse_dt(_g(r, "bm_timestamp", "attachment_date")) or db.now_ist().replace(tzinfo=None)
        con.execute("INSERT OR IGNORE INTO meetings(symbol,meeting_date,purpose,is_results,known_at,source) VALUES(?,?,?,?,?,?)",
                    (sym, md.strftime("%Y-%m-%d"), purpose[:200], is_res, known(filed), "NSE"))
        n += _event(con, U, sym=sym, ts=filed, typ="results_date" if is_res else "meeting", base=20 if is_res else 15, sent="neutral",
                    category="meeting", source="NSE", headline=f"Board meeting on {md:%d %b %Y}: {purpose}"[:300],
                    detail=_g(r, "bm_desc", default="") or "", numbers={"meeting_date": md.strftime("%d %b %Y"), "purpose": purpose})
    return n


# ---------------------------------------------------------------- insider, SAST, pledges
def insider(con, net: Net, U: Universe, days: int = 7) -> int:
    a, b = _dates(days)
    data = net.json(f"{NSE}/corporates-pit?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", "nse")
    rows = data.get("data", []) if isinstance(data, dict) else data
    n = 0
    for r in rows:
        sym = U.resolve(_g(r, "symbol"), None, _g(r, "company"))
        side_raw = str(_g(r, "tdpTransactionType", "acqMode", default="")).lower()
        if "buy" in side_raw or "acqui" in side_raw:
            side = "Buy"
        elif "sell" in side_raw or "dispos" in side_raw:
            side = "Sell"
        else:
            continue
        qty, val = _num(_g(r, "secAcq", "noOfShares")), _num(_g(r, "secVal", "value"))
        if not sym or not qty:
            continue
        value_cr = (val or 0) / 1e7
        filed = parse_dt(_g(r, "date", "intimDt", "acqtoDt"))
        person, cat, mode = _g(r, "acqName", default="Insider"), _g(r, "personCategory", default=""), _g(r, "acqMode", default="")
        pb, pa = _num(_g(r, "befAcqSharesPer")), _num(_g(r, "afterAcqSharesPer"))
        tid = db.h(sym, person, side, qty, _g(r, "acqfromDt"), _g(r, "acqtoDt"))
        con.execute("INSERT OR IGNORE INTO insider_trades(id,symbol,person,category,side,qty,value_cr,mode,pct_before,pct_after,trade_from,trade_to,known_at,source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, sym, person, cat, side, qty, value_cr, mode, pb, pa, _g(r, "acqfromDt"), _g(r, "acqtoDt"), known(filed), "NSE PIT"))
        promoter = "promoter" in (cat or "").lower()
        typ = "insider_buy" if side == "Buy" else "insider_sell"
        base = (45 if promoter else 35) if side == "Buy" else (40 if promoter else 25)
        if "market" not in (mode or "").lower():
            base -= 15  # gifts, transfers, ESOPs say little
        nums = {"person": person, "category": cat, "qty": qty, "qty_fmt": qty_fmt(qty), "value_cr": round(value_cr, 2), "mode": mode,
                "pct_before": pb, "pct_after": pa}
        n += _event(con, U, sym=sym, ts=filed, typ=typ, base=base, sent="positive" if side == "Buy" else "negative", category="insider",
                    source="NSE PIT", headline=f"{person} ({cat or 'insider'}) {'bought' if side == 'Buy' else 'sold'} {qty_fmt(qty)} shares",
                    numbers=nums, value_cr=value_cr, promoter=promoter)
    return n


def sast(con, net: Net, U: Universe, days: int = 7) -> int:
    a, b = _dates(days)
    data = net.json(f"{NSE}/corporate-sast-reg29?index=equities&from_date={a:%d-%m-%Y}&to_date={b:%d-%m-%Y}", "nse")
    rows = data.get("data", []) if isinstance(data, dict) else data
    n = 0
    for r in rows:
        sym = U.resolve(_g(r, "symbol"), None, _g(r, "company"))
        who = _g(r, "acquirerName", "acqName", default="An investor")
        after = _num(_g(r, "totAcqShare", "afterAcqSharesPer", "totAftShare"))
        sale = "sale" in str(_g(r, "acqSaleType", "transactionType", default="")).lower()
        head = f"{who} {'sold' if sale else 'acquired'} shares (takeover rules disclosure)" + (f"; now holds {after:.2f}%" if after else "")
        n += _event(con, U, sym=sym, ts=_g(r, "timestamp", "date", "intimDt"), typ="insider_sell" if sale else "insider_buy",
                    base=40, sent="negative" if sale else "positive", category="insider", source="NSE SAST", headline=head,
                    numbers={"person": who, "pct_after": after, "category": "Substantial holder"})
    return n


def pledges(con, net: Net, U: Universe) -> int:
    data = net.json(f"{NSE}/corporate-pledgedata?index=equities", "nse")
    rows = data.get("data", []) if isinstance(data, dict) else data
    n = 0
    for r in rows:
        sym = U.resolve(_g(r, "symbol"), None, _g(r, "comName", "company"))
        if not sym:
            continue
        pct = None
        for k, v in r.items():
            if "pledg" in k.lower() and ("per" in k.lower() or "%" in k):
                pct = _num(v)
                if pct is not None:
                    break
        prom = _num(_g(r, "percPromoterHolding", "promoterHolding", "totPromoterHoldingPer"))
        filed = parse_dt(_g(r, "broadcastDt", "date", "dissDate")) or db.now_ist().replace(tzinfo=None)
        prev = con.execute("SELECT pledged_pct FROM pledges WHERE symbol=? ORDER BY known_at DESC LIMIT 1", (sym,)).fetchone()
        pid = db.h(sym, pct, filed.date())
        cur = con.execute("INSERT OR IGNORE INTO pledges(id,symbol,pledged_pct,promoter_pct,known_at,source) VALUES(?,?,?,?,?,?)",
                          (pid, sym, pct, prom, known(filed), "NSE"))
        if cur.rowcount and prev and pct is not None and prev[0] is not None and abs(pct - prev[0]) >= 0.5:
            up = pct > prev[0]
            n += _event(con, U, sym=sym, ts=filed, typ="pledge_create" if up else "pledge_release", base=45 if up else 40,
                        sent="negative" if up else "positive", category="insider", source="NSE pledge",
                        headline=f"Promoter pledge {'rose' if up else 'fell'} from {prev[0]:.2f}% to {pct:.2f}% of their shares",
                        numbers={"pledged_pct": pct, "prev": prev[0]})
    return n


# ---------------------------------------------------------------- deals and flows
def deals(con, net: Net, U: Universe) -> int:
    n = 0
    for kind, path in (("Bulk", "/content/equities/bulk.csv"), ("Block", "/content/equities/block.csv")):
        rows = net.csv(ARCH + path, "nsearch")
        for r in rows:
            sym = U.resolve(_g(r, "Symbol"), None, _g(r, "Security Name"))
            side = "Buy" if str(_g(r, "Buy/Sell", default="")).upper().startswith("B") else "Sell"
            qty = _num(_g(r, "Quantity Traded", "Quantity"))
            px = _num(_g(r, "Trade Price / Wght. Avg. Price", "Trade Price", "Price"))
            d = parse_dt(_g(r, "Date")) or db.now_ist().replace(tzinfo=None)
            if not sym or not qty or not px:
                continue
            party = _g(r, "Client Name", default="Unknown")
            val = qty * px / 1e7
            did = db.h(kind, sym, party, side, qty, px, d.date())
            con.execute("INSERT OR IGNORE INTO deals(id,symbol,party,side,qty,price,value_cr,kind,deal_date,known_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (did, sym, party, side, qty, px, val, kind, d.strftime("%Y-%m-%d"), (d + timedelta(days=1)).strftime("%Y-%m-%d"), "NSE"))
            n += _event(con, U, sym=sym, ts=d.replace(hour=17), typ="bulk_buy" if side == "Buy" else "bulk_sell", base=35 if side == "Buy" else 30,
                        sent="positive" if side == "Buy" else "negative", category="deal", source=f"NSE {kind.lower()} deals",
                        headline=f"{party} {'bought' if side == 'Buy' else 'sold'} {qty_fmt(qty)} shares at ₹{px:,.2f} ({kind.lower()} deal)",
                        numbers={"party": party, "qty": qty, "qty_fmt": qty_fmt(qty), "price": px, "value_cr": round(val, 2), "kind": kind},
                        value_cr=val)
    return n


def fii_dii(con, net: Net, U: Universe) -> int:
    data = net.json(f"{NSE}/fiidiiTradeReact", "nse")
    n = 0
    for r in data if isinstance(data, list) else []:
        who = "FII" if "FII" in str(_g(r, "category", default="")).upper() or "FPI" in str(_g(r, "category", default="")).upper() else "DII"
        d = parse_dt(_g(r, "date"))
        if not d:
            continue
        con.execute("INSERT OR REPLACE INTO flows(day,who,buy_cr,sell_cr,net_cr) VALUES(?,?,?,?,?)",
                    (d.strftime("%Y-%m-%d"), who, _num(_g(r, "buyValue")), _num(_g(r, "sellValue")), _num(_g(r, "netValue"))))
        n += 1
    return n


# ---------------------------------------------------------------- the world
MARKETS = [("^GSPC", "S&P 500", "US", "World"), ("^IXIC", "Nasdaq", "US", "World"), ("^DJI", "Dow Jones", "US", "World"), ("^FTSE", "FTSE 100", "Europe", "World"),
           ("^GDAXI", "DAX", "Europe", "World"), ("^N225", "Nikkei 225", "Asia", "World"), ("^HSI", "Hang Seng", "Asia", "World"), ("000001.SS", "Shanghai", "Asia", "World"),
           ("^NSEI", "Nifty 50", "India", "India"), ("^NSEBANK", "Bank Nifty", "India", "India"), ("^BSESN", "Sensex", "India", "India"),
           ("^NSEMDCP50", "Nifty Midcap", "India", "India"), ("^CNXSC", "Nifty Smallcap", "India", "India"), ("^INDIAVIX", "India VIX", "India", "India"),
           ("BZ=F", "Brent crude", "Commodities", "Commodities & FX"), ("GC=F", "Gold", "Commodities", "Commodities & FX"), ("SI=F", "Silver", "Commodities", "Commodities & FX"),
           ("HG=F", "Copper", "Commodities", "Commodities & FX"), ("DX-Y.NYB", "Dollar index", "FX & rates", "Commodities & FX"),
           ("INR=X", "USD / INR", "FX & rates", "Commodities & FX"), ("^TNX", "US 10-year yield", "FX & rates", "Commodities & FX"), ("BTC-USD", "Bitcoin", "Other", "Other")]


def global_markets(con, net: Net, U: Universe) -> int:
    """One call per ticker: today's 15-minute line (sparkline), previous close, day high and low. A ticker Yahoo
    does not know is skipped and shows in source_state; the rest are unaffected."""
    import json as _json
    n = 0
    for key, name, region, grp in MARKETS:
        try:
            d = net.json(f"https://query1.finance.yahoo.com/v8/finance/chart/{key}?range=1d&interval=15m", "yahoo")
            res = d["chart"]["result"][0]
            meta = res["meta"]
            q = res["indicators"]["quote"][0]
            closes = [x for x in (q.get("close") or []) if x is not None]
            last = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if last is None:
                continue
            if not prev and len(closes) > 1:
                prev = closes[0]
            chg = (last / prev - 1) * 100 if prev else None
            hi = meta.get("regularMarketDayHigh") or (max(closes) if closes else None)
            lo = meta.get("regularMarketDayLow") or (min(closes) if closes else None)
            step = max(1, len(closes) // 40)                      # at most ~40 points: a sparkline needs no more
            spark = [round(x, 4) for x in closes[::step]]
            if closes and spark[-1] != round(closes[-1], 4):
                spark.append(round(closes[-1], 4))
            con.execute("""INSERT OR REPLACE INTO markets(key,name,region,last,chg_pct,as_of,prev,hi,lo,spark_json,grp) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (key, name, region, last, chg, iso(None), prev, hi, lo, _json.dumps(spark), grp))
            n += 1
        except (Blocked, KeyError, IndexError, TypeError, ValueError):
            continue
    return n


FEEDS = [
    ("ET Markets", "india", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("Mint Markets", "india", "https://www.livemint.com/rss/markets"),
    ("Moneycontrol", "india", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Business Standard", "india", "https://www.business-standard.com/rss/markets-106.rss"),
    ("BusinessLine", "india", "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
    ("NSE stocks (Google News)", "india", "https://news.google.com/rss/search?q=NSE+stock+order+OR+results+OR+acquisition+when:1d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("SEBI", "macro", "https://www.sebi.gov.in/sebirss.xml"),
    ("RBI", "macro", "https://www.rbi.org.in/pressreleases_rss.xml"),
    ("PIB Economy", "macro", "https://news.google.com/rss/search?q=site:pib.gov.in+(economy+OR+ministry+OR+cabinet)+when:1d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Reuters business", "global", "https://news.google.com/rss/search?q=site:reuters.com+markets+when:1d&hl=en&gl=US&ceid=US:en"),
    ("Global markets", "global", "https://news.google.com/rss/search?q=(stocks+OR+fed+OR+oil+OR+treasury)+markets+when:1d&hl=en&gl=US&ceid=US:en"),
]
_WORD = re.compile(r"\b[A-Z][A-Z0-9&]{2,14}\b")


def _tag(U: Universe, text: str) -> str | None:
    for w in _WORD.findall(text or ""):
        if w in U.by_sym and len(w) > 3:
            return w
    low = U._norm(text)
    for nm, s in U.by_name.items():
        if len(nm) > 6 and f" {nm} " in f" {low} ":
            return s
    return None


def news(con, net: Net, U: Universe) -> int:
    n = 0
    for src, cat, url in FEEDS:
        try:
            items = net.rss(url)
            db.mark_source(con, f"rss:{src}", True, f"{len(items)} items")
        except Exception as e:  # noqa: BLE001
            db.mark_source(con, f"rss:{src}", False, str(e))
            continue
        for it in items[:60]:
            title = re.sub(r"\s+", " ", it["title"]).strip()
            if not title:
                continue
            desc = re.sub(r"<[^>]+>", " ", it["summary"] or "")
            desc = re.sub(r"\s+", " ", desc).strip()[:600]
            sym = _tag(U, f"{title} {desc}") if cat == "india" else None
            typ, base, sent = C.classify(title, desc, "news")
            if typ == "filing":
                typ, base = ("news" if cat == "india" else cat), 10 if cat == "india" else 12
            n += _event(con, U, sym=sym, ts=parse_dt(it["published"]) or db.now_ist().replace(tzinfo=None), typ=typ, base=base, sent=sent,
                        category="news" if cat == "india" else cat, source=src, url=it["link"], headline=title, detail=desc)
    return n


JOBS = {
    "filings": [("nse_announcements", nse_announcements), ("bse_announcements", bse_announcements)],
    "meetings": [("board_meetings", board_meetings)],
    "insider": [("insider", insider), ("sast", sast), ("pledges", pledges)],
    "flows": [("deals", deals), ("fii_dii", fii_dii)],
    "markets": [("global_markets", global_markets)],
    "world": [("global_markets", global_markets), ("news", news)],
}


def run(groups: list[str] | None = None, log=print) -> dict:
    """Run collector groups; every source is isolated (one failure never stops the others)."""
    net, U, out = Net(), Universe(), {}
    with db.session() as con:
        for g in groups or list(JOBS):
            for name, fn in JOBS[g]:
                t0 = time.time()
                try:
                    added = fn(con, net, U)
                    db.mark_source(con, name, True, f"+{added}")
                    db.record_run(con, name, t0, True, added)
                    out[name] = added
                except Exception as e:  # noqa: BLE001
                    db.mark_source(con, name, False, f"{type(e).__name__}: {e}")
                    db.record_run(con, name, t0, False, 0, str(e))
                    out[name] = f"failed: {type(e).__name__}: {str(e)[:120]}"
                con.commit()
                log(f"{name}: {out[name]}")
    return out
