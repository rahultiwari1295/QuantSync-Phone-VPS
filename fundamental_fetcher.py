#!/usr/bin/env python3
"""
QuantSync — Fundamental Intelligence Fetcher (Comprehensive Audited Edition)
Fetches audited fundamental data from Screener.in with 100% data integrity:
- Full 10-Year Annual Statements (Income Statement, Balance Sheet, Cash Flow).
- Multi-Year Operating Efficiency Ratios & Compounded Growth Bento Cards.
- Comprehensive Multi-Quarter Earnings & Historical Dividend Track Record.
- Missing data returns None / NULL (NEVER defaults to 0.0 or fake numbers).
- Automatic cache expiry (refreshes if older than 7 days).
"""

import sys
import os
import re
import json
import sqlite3
import datetime
import requests
import numpy as np

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DB_PATH = os.path.join(SCRIPT_DIR, "Data", "fundamental_cache.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9"
}

def init_db():
    os.makedirs(os.path.dirname(CACHE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_fundamentals (
            symbol TEXT PRIMARY KEY,
            data_json TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def clean_row_name(s):
    return re.sub(r"<[^>]+>", "", str(s)).replace("&nbsp;", " ").replace("+", "").strip()

def to_float(val_str, default=None):
    """Parses numeric string to float. Returns default (None) if missing or invalid."""
    if val_str is None or str(val_str).strip() in ("", "N/A", "-", "None"):
        return default
    try:
        clean = str(val_str).replace(",", "").replace("%", "").strip()
        f = float(clean)
        return f if not np.isnan(f) else default
    except Exception:
        return default

def fetch_fundamentals(symbol: str, force_refresh: bool = False, max_cache_age_days: int = 7):
    init_db()
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "Empty symbol provided"}

    # 1. Check cache freshness
    if not force_refresh:
        try:
            conn = sqlite3.connect(CACHE_DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT data_json, updated_at FROM stock_fundamentals WHERE symbol = ?", (sym,))
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                updated_at = row[1]
                is_fresh = True
                if updated_at:
                    try:
                        cache_time = datetime.datetime.fromisoformat(str(updated_at))
                        age = (datetime.datetime.now() - cache_time).days
                        if age > max_cache_age_days:
                            is_fresh = False
                    except Exception:
                        pass
                if is_fresh:
                    data = json.loads(row[0])
                    if data and isinstance(data, dict):
                        return data
        except Exception:
            pass

    # 2. Query Screener (Consolidated -> Standalone fallback)
    TICKER_ALIASES = {
        "ZOMATO": ["ETERNAL"],
        "TATAMOTORS": ["TMCV", "TMPV"],
        "MSUMI": ["MOTHERSON"],
    }
    clean_sym = sym.replace("&", "").replace("-", "")
    raw_candidates = [sym] + TICKER_ALIASES.get(sym, []) + ([clean_sym] if clean_sym != sym else [])
    candidates = []
    for c in raw_candidates:
        if c not in candidates:
            candidates.append(c)
    html = None
    source_url = None

    def _extract_latest_quarter_date(html_text: str):
        sec_m = re.search(r'<section id="quarters".*?</section>', html_text, re.DOTALL)
        if not sec_m:
            return None
        keys = re.findall(r'data-date-key="([\d\-]+)"', sec_m.group(0))
        return max(keys) if keys else None

    for c in candidates:
        consol_url = f"https://www.screener.in/company/{c}/consolidated/"
        stand_url = f"https://www.screener.in/company/{c}/"
        c_html, c_date = None, None
        s_html, s_date = None, None

        try:
            r_c = requests.get(consol_url, headers=HEADERS, timeout=5.0)
            if r_c.status_code == 200:
                c_html = r_c.text
                c_date = _extract_latest_quarter_date(c_html)
        except Exception:
            pass

        if not c_html:
            try:
                from collectors.core.fetcher import Fetcher, FetchSpec, Attempt
                f = Fetcher()
                r_c = f.fetch(FetchSpec("fundamentals", "screener", c, [Attempt("chromium", "screener", consol_url)], expect="text"))
                if r_c.ok and isinstance(r_c.data, str) and "data-date-key" in r_c.data:
                    c_html = r_c.data
                    c_date = _extract_latest_quarter_date(c_html)
            except Exception:
                pass

        cutoff_date = (datetime.date.today() - datetime.timedelta(days=120)).isoformat()
        if c_date and c_date >= cutoff_date:
            html = c_html
            source_url = consol_url
            break

        try:
            r_s = requests.get(stand_url, headers=HEADERS, timeout=5.0)
            if r_s.status_code == 200:
                s_html = r_s.text
                s_date = _extract_latest_quarter_date(s_html)
        except Exception:
            pass

        if not s_html:
            try:
                from collectors.core.fetcher import Fetcher, FetchSpec, Attempt
                f = Fetcher()
                r_s = f.fetch(FetchSpec("fundamentals", "screener", c, [Attempt("chromium", "screener", stand_url)], expect="text"))
                if r_s.ok and isinstance(r_s.data, str) and "data-date-key" in r_s.data:
                    s_html = r_s.data
                    s_date = _extract_latest_quarter_date(s_html)
            except Exception:
                pass

        if s_date and (not c_date or s_date > c_date):
            html = s_html
            source_url = stand_url
            break
        elif c_html:
            html = c_html
            source_url = consol_url
            break
        elif s_html:
            html = s_html
            source_url = stand_url
            break

    if not html:
        # Network/source failure: serve the last good copy, clearly marked stale,
        # instead of dropping fundamentals from the DNA report altogether.
        try:
            conn = sqlite3.connect(CACHE_DB_PATH)
            row = conn.execute("SELECT data_json, updated_at FROM stock_fundamentals WHERE symbol = ?", (sym,)).fetchone()
            conn.close()
            if row and row[0]:
                data = json.loads(row[0])
                data["stale"] = True
                data["cache_updated_at"] = row[1]
                return data
        except Exception:
            pass
        return {"error": f"Failed to fetch fundamental data for {sym} from Screener"}

    result = parse_screener_html(html, symbol=sym, source_url=source_url)

    # 12. Cache into SQLite
    try:
        conn = sqlite3.connect(CACHE_DB_PATH)
        conn.execute("""
            INSERT INTO stock_fundamentals (symbol, data_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(symbol) DO UPDATE SET
                data_json = excluded.data_json,
                updated_at = CURRENT_TIMESTAMP
        """, (sym, json.dumps(result)))
        conn.commit()
        conn.close()
    except Exception as e:
        sys.stderr.write(f"Cache write error: {e}\n")

    return result

def parse_screener_html(html: str, symbol: str = "", source_url: str = "") -> dict:
    sym = (symbol or "").strip().upper()
    # Setup BeautifulSoup
    soup = BeautifulSoup(html, "html.parser") if HAS_BS4 else None

    # 3. Parse Top Key Ratios (Strict None if absent)
    ratios = {}
    for m in re.finditer(r"<span class=\"name\">\s*([^<]+?)\s*</span>\s*<span class=\"nowrap value\">(.*?)</span>\s*</li>", html, re.DOTALL):
        name = m.group(1).strip()
        val_part = m.group(2)
        nums = re.findall(r"-?[\d,\.]+", val_part)
        val = nums[0] if nums else val_part.strip()
        ratios[name] = val

    market_cap_cr = to_float(ratios.get("Market Cap"))
    stock_pe = to_float(ratios.get("Stock P/E"))
    book_value = to_float(ratios.get("Book Value"))
    div_yield = to_float(ratios.get("Dividend Yield"))
    roce_pct = to_float(ratios.get("ROCE"))
    roe_pct = to_float(ratios.get("ROE"))
    face_value = to_float(ratios.get("Face Value"))
    debt_to_equity = to_float(ratios.get("Debt to equity"))

    # Helper function to parse standard financial tables
    def parse_table_section(sec_id):
        if soup:
            sec = soup.find("section", id=sec_id)
            if not sec: return None
            tbl = sec.find("table")
            if not tbl: return None
            thead = tbl.find("thead")
            headers = [clean_row_name(th.get_text()) for th in thead.find_all("th") if th.get_text().strip()] if thead else []
            tbody = tbl.find("tbody")
            rows = []
            if tbody:
                for tr in tbody.find_all("tr"):
                    tds = [clean_row_name(td.get_text()) for td in tr.find_all("td")]
                    if tds:
                        rows.append({"metric": tds[0], "values": tds[1:]})
            return {"years": headers, "rows": rows}
        else:
            # Fallback regex parser
            m_sec = re.search(rf"<section id=\"{sec_id}\".*?</section>", html, re.DOTALL)
            if not m_sec: return None
            sec_text = m_sec.group(0)
            headers = [clean_row_name(x) for x in re.findall(r"<th[^>]*>\s*([A-Za-z0-9\s]+?)\s*</th>", sec_text) if x.strip()]
            tr_matches = re.findall(r"<tr[^>]*>\s*<td[^>]*class=\"text\"[^>]*>(.*?)</td>(.*?)</tr>", sec_text, re.DOTALL)
            rows = []
            for r_title, r_vals in tr_matches:
                metric = clean_row_name(r_title)
                vals = [clean_row_name(v) for v in re.findall(r"<td[^>]*>\s*([\d,\.\-%]+?)\s*</td>", r_vals)]
                rows.append({"metric": metric, "values": vals})
            return {"years": headers, "rows": rows}

    # 4. Parse Full Annual Statements
    annual_income_statement = parse_table_section("profit-loss")
    annual_balance_sheet = parse_table_section("balance-sheet")
    annual_cash_flow = parse_table_section("cash-flow")
    efficiency_ratios = parse_table_section("ratios")

    # 5. Parse Multi-Quarter Performance (Extended up to 12 quarters)
    quarterly_data = []
    q_sec = parse_table_section("quarters")
    if q_sec and q_sec.get("years"):
        q_headers = q_sec["years"]
        recent_quarters = q_headers[-12:] if len(q_headers) >= 12 else q_headers
        q_metrics = {r["metric"]: r["values"][-len(recent_quarters):] for r in q_sec.get("rows", [])}
        is_bank = "Financing Profit" in q_metrics or ("Interest" in q_metrics and "Sales" not in q_metrics)

        for i, q_name in enumerate(recent_quarters):
            def get_q_metric(*names):
                for name in names:
                    arr = q_metrics.get(name, [])
                    if i < len(arr) and arr[i] not in ("N/A", "-", ""):
                        return arr[i]
                return None

            quarterly_data.append({
                "quarter": q_name,
                "sales_cr": get_q_metric("Revenue", "Sales", "Interest"),
                "expenses_cr": None if is_bank else get_q_metric("Expenses"),
                "op_profit_cr": None if is_bank else get_q_metric("Operating Profit"),
                "opm_pct": None if is_bank else get_q_metric("OPM %"),
                "other_income_cr": get_q_metric("Other Income"),
                "interest_cr": get_q_metric("Interest"),
                "depreciation_cr": get_q_metric("Depreciation"),
                "pbt_cr": get_q_metric("Profit before tax"),
                "tax_pct": get_q_metric("Tax %"),
                "net_profit_cr": get_q_metric("Net Profit"),
                "eps": get_q_metric("EPS in Rs")
            })

    # 6. Parse Latest Balance Sheet (Map for backwards compatibility)
    balance_sheet = {}
    if annual_balance_sheet and annual_balance_sheet.get("rows"):
        for r in annual_balance_sheet["rows"]:
            if r.get("values"):
                balance_sheet[r["metric"]] = r["values"][-1]

    # 7. Compounded Growth Bento Cards
    compounded_growth = {
        "sales_growth": [],
        "profit_growth": [],
        "price_cagr": [],
        "roe": []
    }
    if soup:
        for rt in soup.find_all("table", class_="ranges-table"):
            th = rt.find("th")
            title = th.get_text().strip() if th else ""
            items = []
            for tr in rt.find_all("tr"):
                tds = [clean_row_name(td.get_text()) for td in tr.find_all("td")]
                if len(tds) >= 2 and tds[0] and tds[1]:
                    items.append({"period": tds[0].rstrip(":").strip(), "value": tds[1].strip()})
            if "Sales" in title: compounded_growth["sales_growth"] = items
            elif "Profit" in title: compounded_growth["profit_growth"] = items
            elif "Price" in title: compounded_growth["price_cagr"] = items
            elif "Equity" in title or "ROE" in title: compounded_growth["roe"] = items
    else:
        # Regex fallback for ranges-table
        for m_table in re.finditer(r"<table class=\"ranges-table\">(.*?)</table>", html, re.DOTALL):
            tbl_txt = m_table.group(1)
            m_th = re.search(r"<th>(.*?)</th>", tbl_txt)
            title = clean_row_name(m_th.group(1)) if m_th else ""
            items = []
            for m_tr in re.finditer(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", tbl_txt):
                p = clean_row_name(m_tr.group(1)).rstrip(":")
                v = clean_row_name(m_tr.group(2))
                if p and v:
                    items.append({"period": p, "value": v})
            if "Sales" in title: compounded_growth["sales_growth"] = items
            elif "Profit" in title: compounded_growth["profit_growth"] = items
            elif "Price" in title: compounded_growth["price_cagr"] = items
            elif "Equity" in title or "ROE" in title: compounded_growth["roe"] = items

    # 8. Historical Dividend Track Record (Extracted from Income Statement)
    dividend_history = []
    if annual_income_statement and annual_income_statement.get("rows"):
        years = annual_income_statement.get("years", [])
        eps_row = next((r["values"] for r in annual_income_statement["rows"] if "EPS" in r["metric"]), None)
        payout_row = next((r["values"] for r in annual_income_statement["rows"] if "Dividend Payout" in r["metric"]), None)
        net_profit_row = next((r["values"] for r in annual_income_statement["rows"] if "Net Profit" in r["metric"]), None)

        if eps_row and payout_row:
            for y_idx, yr in enumerate(years):
                if str(yr).strip().upper() == "TTM":
                    continue
                eps_val = eps_row[y_idx] if y_idx < len(eps_row) else ""
                payout_val = payout_row[y_idx] if y_idx < len(payout_row) else ""
                np_val = net_profit_row[y_idx] if net_profit_row and y_idx < len(net_profit_row) else ""

                eps_f = to_float(eps_val)
                payout_f = to_float(payout_val)
                dps = round(eps_f * payout_f / 100.0, 2) if (eps_f is not None and payout_f is not None) else None

                dividend_history.append({
                    "year": yr,
                    "eps": eps_val,
                    "payout_pct": payout_val,
                    "dps": dps,
                    "net_profit_cr": np_val
                })

    dividends_data = {
        "dividend_yield_pct": div_yield,
        "latest_payout_pct": dividend_history[-1]["payout_pct"] if dividend_history else None,
        "latest_dps": dividend_history[-1]["dps"] if dividend_history else None,
        "history": dividend_history
    }

    # 9. Parse Shareholding Pattern (None if no prior quarter, never fake 0.0)
    shareholding = {}
    sh_sec = re.search(r"<section id=\"shareholding\".*?</section>", html, re.DOTALL)
    if sh_sec:
        sh_text = sh_sec.group(0)
        sh_headers = re.findall(r"<th[^>]*>\s*([A-Za-z]{3}\s+\d{4})\s*</th>", sh_text)
        latest_sh_q = sh_headers[-1] if sh_headers else None
        sh_rows = re.findall(r"<tr[^>]*>\s*<td[^>]*class=\"text\"[^>]*>(.*?)</td>(.*?)</tr>", sh_text, re.DOTALL)
        for r_title, r_vals in sh_rows:
            c_title = clean_row_name(r_title)
            vals = re.findall(r"<td[^>]*>\s*([\d,\.\-]+%?)\s*</td>", r_vals)
            if vals and len(vals) >= 1:
                curr_h = to_float(vals[-1])
                if len(vals) >= 2:
                    prev_h = to_float(vals[-2])
                    diff = round(curr_h - prev_h, 2) if (curr_h is not None and prev_h is not None) else None
                else:
                    diff = None

                shareholding[c_title] = {
                    "holding_pct": curr_h,
                    "qoq_change": diff,
                    "quarter": latest_sh_q
                }

    # 10. Parse Pros & Cons (Source-derived)
    pros_list = []
    m_pros = re.search(r"<div class=\"pros\"[^>]*>.*?<ul>(.*?)</ul>", html, re.DOTALL)
    if m_pros:
        pros_list = [clean_row_name(x) for x in re.findall(r"<li>(.*?)</li>", m_pros.group(1))]

    cons_list = []
    m_cons = re.search(r"<div class=\"cons\"[^>]*>.*?<ul>(.*?)</ul>", html, re.DOTALL)
    if m_cons:
        cons_list = [clean_row_name(x) for x in re.findall(r"<li>(.*?)</li>", m_cons.group(1))]

    # 11. Model Data: Internal Quality Heuristic (Strictly labeled as internal_model_score)
    fund_score = 50
    if roce_pct is not None:
        if roce_pct >= 20.0: fund_score += 15
        elif roce_pct >= 12.0: fund_score += 8
        elif roce_pct < 6.0: fund_score -= 10

    if roe_pct is not None:
        if roe_pct >= 18.0: fund_score += 15
        elif roe_pct >= 10.0: fund_score += 8
        elif roe_pct < 5.0: fund_score -= 10

    if debt_to_equity is not None:
        if debt_to_equity <= 0.3: fund_score += 15
        elif debt_to_equity <= 0.8: fund_score += 8
        elif debt_to_equity > 1.4: fund_score -= 15

    fii_data = shareholding.get("FIIs", {})
    fii_chg = fii_data.get("qoq_change")
    if fii_chg is not None:
        if fii_chg > 0.3: fund_score += 8
        elif fii_chg < -0.3: fund_score -= 6

    fund_score = max(15, min(95, int(fund_score)))

    if fund_score >= 75:
        quality_label = "High-Quality Compounder (High Moat)"
    elif fund_score >= 50:
        quality_label = "Moderate Quality (Cyclical / Capital Intensive)"
    else:
        quality_label = "Elevated Leverage / Capital Destruction Risk"

    result = {
        "symbol": sym,
        "source_url": source_url,
        "fetched_at": datetime.datetime.now().isoformat(),
        "market_cap_cr": market_cap_cr,
        "key_ratios": {
            "stock_pe": stock_pe,
            "book_value": book_value,
            "dividend_yield_pct": div_yield,
            "roce_pct": roce_pct,
            "roe_pct": roe_pct,
            "debt_to_equity": debt_to_equity,
            "face_value": face_value
        },
        "internal_model_score": {
            "score": fund_score,
            "label": quality_label
        },
        "quarterly_performance": quarterly_data,
        "balance_sheet": balance_sheet,
        "annual_income_statement": annual_income_statement,
        "annual_balance_sheet": annual_balance_sheet,
        "annual_cash_flow": annual_cash_flow,
        "efficiency_ratios": efficiency_ratios,
        "compounded_growth": compounded_growth,
        "dividends": dividends_data,
        "shareholding_pattern": shareholding,
        "pros": pros_list[:5],
        "cons": cons_list[:5]
    }

    return result

if __name__ == "__main__":
    test_sym = sys.argv[1] if len(sys.argv) > 1 else "IRCON"
    res = fetch_fundamentals(test_sym, force_refresh=True)
    print(json.dumps({k: bool(v) for k, v in res.items()}, indent=2))
