"""
collectors/core/parsers.py — turn raw exchange/screener payloads into clean typed rows.
Pure functions (no I/O). Never raise on missing keys.
Small helpers (to_num, parse_dividend_amount, classify_action) were drafted by local model
ornith:9b and corrected in review; the structured parsers were written by Claude after the
model's versions failed review (wrong field names, use-before-define).
"""
from __future__ import annotations

import calendar
import csv
import hashlib
import io
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y %H:%M:%S",
                 "%d-%B-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d %b %Y")


def parse_date(s: Any) -> Optional[date]:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    t = str(s).strip()
    if not t or t in ("-", "NA", "N/A"):
        return None
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    return None


def to_num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None
    s = str(v).strip()
    if not s or s in ("-", "NA", "N/A", "--"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[₹,%\s]|Rs\.?|Cr\.?", "", s.strip("()"), flags=re.I)
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def pick(d: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


_RS = re.compile(r"R[se]\.?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def parse_dividend_amount(subject: Optional[str]) -> Optional[float]:
    if not subject or "dividend" not in subject.lower():
        return None
    m = _RS.findall(subject)
    return round(sum(float(x) for x in m), 4) if m else None


def classify_action(subject: Optional[str]) -> str:
    t = (subject or "").lower()
    for label, kws in (("SPLIT", ("split", "sub-division", "sub division")), ("BONUS", ("bonus",)),
                       ("DIVIDEND", ("dividend",)), ("RIGHTS", ("rights",)),
                       ("BUYBACK", ("buyback", "buy back", "buy-back")), ("DEMERGER", ("demerger",)),
                       ("AGM", ("agm", "annual general"))):
        if any(k in t for k in kws):
            return label
    return "OTHER"


def parse_equity_list_csv(text: str) -> List[Dict[str, Any]]:
    rows = list(csv.reader(io.StringIO(text or "")))
    if not rows:
        return []
    idx = {h.strip().upper(): i for i, h in enumerate(rows[0])}

    def get(r, name):
        i = idx.get(name)
        return r[i].strip() if i is not None and i < len(r) else ""

    out = []
    for r in rows[1:]:
        sym = get(r, "SYMBOL")
        if not sym:
            continue
        out.append({"symbol": sym, "name": get(r, "NAME OF COMPANY"), "series": get(r, "SERIES"),
                    "listing_date": parse_date(get(r, "DATE OF LISTING")), "isin": get(r, "ISIN NUMBER"),
                    "face_value": to_num(get(r, "FACE VALUE"))})
    return out


def parse_nse_corporate_actions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        sym = (r.get("symbol") or "").strip()
        ex = parse_date(r.get("exDate"))
        if not sym or ex is None:
            continue
        subj = r.get("subject") or ""
        kind = classify_action(subj)
        out.append({"symbol": sym, "series": r.get("series"), "isin": r.get("isin"), "subject": subj, "kind": kind,
                    "ex_date": ex, "record_date": parse_date(r.get("recDate")),
                    "amount": parse_dividend_amount(subj) if kind == "DIVIDEND" else None,
                    "face_value": to_num(r.get("faceVal"))})
    return out


def parse_nse_board_meetings(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        sym = (r.get("bm_symbol") or r.get("symbol") or "").strip()
        d = parse_date(r.get("bm_date"))
        if not sym or d is None:
            continue
        purpose = (r.get("bm_purpose") or "").strip()
        desc = (r.get("bm_desc") or "").strip()
        low = (purpose + " " + desc).lower()
        out.append({"symbol": sym, "meeting_date": d, "purpose": purpose, "detail": desc,
                    "isin": (r.get("sm_isin") or "").strip(), "is_results": ("result" in low or "financial" in low)})
    return out


_CATS = (
    ("RESULTS", ("financial result", "audited result", "unaudited result")),
    ("DIVIDEND", ("dividend", "record date")),
    ("LEGAL", ("sebi order", "court order")),
    ("ORDER_WIN", ("order", "contract", "letter of award")),
    ("MANAGEMENT", ("resignation", "appointment", "cessation", "chief executive", "ceo", "cfo",
                    "managing director", "key managerial")),
    ("PLEDGE", ("pledge", "encumbrance")),
    ("M_AND_A", ("acquisition", "merger", "amalgamation", "scheme of arrangement", "demerger")),
    ("FUND_RAISE", ("qip", "qualified institutions placement", "preferential issue", "rights issue", "ncd",
                    "non-convertible")),
    ("BOARD_MEETING", ("board meeting", "outcome of board")),
    ("LEGAL", ("litigation", "penalty", "show cause")),
    ("CREDIT_RATING", ("credit rating",)),
)


def categorize_announcement(headline: Optional[str], detail: Optional[str]) -> str:
    text = f"{headline or ''} {detail or ''}".lower()
    for label, kws in _CATS:
        if any(k in text for k in kws):
            return label
    return "OTHER"


def parse_nse_announcements(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        sym = (r.get("symbol") or "").strip()
        head = (r.get("desc") or "").strip()
        if not sym or not head:
            continue
        raw = (r.get("an_dt") or "").strip()
        ts = None
        try:
            ts = datetime.strptime(raw, "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            d = parse_date(raw)
            ts = d.strftime("%Y-%m-%dT00:00:00") if d else None
        if ts is None:
            continue
        detail = (r.get("attchmntText") or "").strip()[:2000]
        out.append({"symbol": sym, "isin": (r.get("sm_isin") or "").strip(), "ts": ts, "headline": head,
                    "detail": detail, "url": r.get("attchmntFile") or "", "source_id": str(r.get("seq_id") or ""),
                    "category": categorize_announcement(head, detail),
                    "hash": hashlib.sha1(f"{sym}|{head.lower()}|{ts[:10]}".encode()).hexdigest()})
    return out


def _cr(v: Any) -> Optional[float]:
    n = to_num(v)
    return round(n / 100.0, 2) if n is not None else None   # NSE reports in Rs lakhs


def parse_nse_results(payload: Any) -> List[Dict[str, Any]]:
    """NSE results-comparison JSON (values in lakhs) -> quarterly rows in crore."""
    data = payload.get("resCmpData") if isinstance(payload, dict) else payload
    out = []
    for r in data or []:
        end = parse_date(pick(r, "re_to_dt", "toDate"))
        if end is None:
            continue
        rev = _cr(pick(r, "re_net_sale", "netSales", "re_int_earned", "re_tot_inc_frm_opr", "re_total_inc"))   # banks/NBFCs report interest earned, not sales
        exp = _cr(pick(r, "re_tot_exp", "re_total_exp"))
        intr = _cr(pick(r, "re_int_new", "re_int"))
        dep = _cr(pick(r, "re_depr_and_amort", "re_depr"))
        op = round(rev - exp + intr + dep, 2) if None not in (rev, exp, intr, dep) else None
        out.append({"period_end": end, "period_start": parse_date(pick(r, "re_from_dt", "fromDate")),
                    "consolidated": "consol" in str(pick(r, "re_con_type", "consolidated") or "").lower(),
                    "audited": str(pick(r, "re_res_type", "audited") or "").upper().startswith("A"),
                    "revenue_cr": rev, "other_income_cr": _cr(pick(r, "re_oth_inc_new", "re_oth_inc")),
                    "expenses_cr": exp, "interest_cr": intr, "depreciation_cr": dep,
                    "pbt_cr": _cr(pick(r, "re_pro_loss_bef_tax", "re_pbt")),
                    "net_profit_cr": _cr(pick(r, "re_net_profit", "re_con_pro_loss")),
                    "eps": to_num(pick(r, "re_basic_eps_for_cont_dic_opr", "re_basic_eps")),
                    "op_profit_cr": op,
                    "opm_pct": round(op / rev * 100, 2) if op is not None and rev else None})
    return out


def quarter_label(d: date) -> str:
    return d.strftime("%b %Y")


def month_end(label: str) -> Optional[date]:
    try:
        dt = datetime.strptime(label.strip(), "%b %Y")
    except (ValueError, AttributeError):
        return None
    return date(dt.year, dt.month, calendar.monthrange(dt.year, dt.month)[1])


try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


def clean_row_name(s: Any) -> str:
    return re.sub(r"<[^>]+>", "", str(s)).replace("&nbsp;", " ").replace("+", "").strip()


def parse_screener_html(html: str, symbol: str = "", source_url: str = "") -> dict:
    sym = (symbol or "").strip().upper()
    soup = BeautifulSoup(html, "html.parser") if HAS_BS4 else None

    # Key ratios
    ratios = {}
    for m in re.finditer(r"<span class=\"name\">\s*([^<]+?)\s*</span>\s*<span class=\"nowrap value\">(.*?)</span>\s*</li>", html, re.DOTALL):
        name = m.group(1).strip()
        val_part = m.group(2)
        nums = re.findall(r"-?[\d,\.]+", val_part)
        val = nums[0] if nums else val_part.strip()
        ratios[name] = val

    market_cap_cr = to_num(ratios.get("Market Cap"))
    stock_pe = to_num(ratios.get("Stock P/E"))
    book_value = to_num(ratios.get("Book Value"))
    div_yield = to_num(ratios.get("Dividend Yield"))
    roce_pct = to_num(ratios.get("ROCE"))
    roe_pct = to_num(ratios.get("ROE"))
    face_value = to_num(ratios.get("Face Value"))
    debt_to_equity = to_num(ratios.get("Debt to equity"))

    def parse_table_section(sec_id):
        if soup:
            sec = soup.find("section", id=sec_id)
            if not sec:
                return None
            tbl = sec.find("table")
            if not tbl:
                return None
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
            m_sec = re.search(rf"<section id=\"{sec_id}\".*?</section>", html, re.DOTALL)
            if not m_sec:
                return None
            sec_text = m_sec.group(0)
            headers = [clean_row_name(x) for x in re.findall(r"<th[^>]*>\s*([A-Za-z0-9\s]+?)\s*</th>", sec_text) if x.strip()]
            tr_matches = re.findall(r"<tr[^>]*>\s*<td[^>]*class=\"text\"[^>]*>(.*?)</td>(.*?)</tr>", sec_text, re.DOTALL)
            rows = []
            for r_title, r_vals in tr_matches:
                metric = clean_row_name(r_title)
                vals = [clean_row_name(v) for v in re.findall(r"<td[^>]*>\s*([\d,\.\-%]+?)\s*</td>", r_vals)]
                rows.append({"metric": metric, "values": vals})
            return {"years": headers, "rows": rows}

    annual_income_statement = parse_table_section("profit-loss")
    annual_balance_sheet = parse_table_section("balance-sheet")
    annual_cash_flow = parse_table_section("cash-flow")
    efficiency_ratios = parse_table_section("ratios")

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

    balance_sheet = {}
    if annual_balance_sheet and annual_balance_sheet.get("rows"):
        for r in annual_balance_sheet["rows"]:
            if r.get("values"):
                balance_sheet[r["metric"]] = r["values"][-1]

    compounded_growth: Dict[str, Any] = {
        "sales_growth": [], "profit_growth": [], "price_cagr": [], "roe": []
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
                eps_f = to_num(eps_val)
                payout_f = to_num(payout_val)
                dps = round(eps_f * payout_f / 100.0, 2) if (eps_f is not None and payout_f is not None) else None
                dividend_history.append({
                    "year": yr, "eps": eps_val, "payout_pct": payout_val, "dps": dps, "net_profit_cr": np_val
                })

    dividends_data = {
        "dividend_yield_pct": div_yield,
        "latest_payout_pct": dividend_history[-1]["payout_pct"] if dividend_history else None,
        "latest_dps": dividend_history[-1]["dps"] if dividend_history else None,
        "history": dividend_history
    }

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
                curr_h = to_num(vals[-1])
                prev_h = to_num(vals[-2]) if len(vals) >= 2 else None
                diff = round(curr_h - prev_h, 2) if (curr_h is not None and prev_h is not None) else None
                shareholding[c_title] = {
                    "holding_pct": curr_h, "qoq_change": diff, "quarter": latest_sh_q
                }

    pros_list = []
    m_pros = re.search(r"<div class=\"pros\"[^>]*>.*?<ul>(.*?)</ul>", html, re.DOTALL)
    if m_pros:
        pros_list = [clean_row_name(x) for x in re.findall(r"<li>(.*?)</li>", m_pros.group(1))]

    cons_list = []
    m_cons = re.search(r"<div class=\"cons\"[^>]*>.*?<ul>(.*?)</ul>", html, re.DOTALL)
    if m_cons:
        cons_list = [clean_row_name(x) for x in re.findall(r"<li>(.*?)</li>", m_cons.group(1))]

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
    if fund_score >= 75: quality_label = "High-Quality Compounder (High Moat)"
    elif fund_score >= 50: quality_label = "Moderate Quality (Cyclical / Capital Intensive)"
    else: quality_label = "Elevated Leverage / Capital Destruction Risk"

    return {
        "symbol": sym,
        "source_url": source_url,
        "fetched_at": datetime.now().isoformat(),
        "market_cap_cr": market_cap_cr,
        "key_ratios": {
            "stock_pe": stock_pe, "book_value": book_value, "dividend_yield_pct": div_yield,
            "roce_pct": roce_pct, "roe_pct": roe_pct, "debt_to_equity": debt_to_equity, "face_value": face_value
        },
        "internal_model_score": {"score": fund_score, "label": quality_label},
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


def parse_screener_quarters(blob: Dict[str, Any]) -> List[Dict[str, Any]]:
    consolidated = "/consolidated/" in str(blob.get("source_url") or "")
    out = []
    for r in blob.get("quarterly_performance") or []:
        end = month_end(str(r.get("quarter") or ""))
        if end is None:
            continue
        out.append({"period_end": end, "period_start": None, "consolidated": consolidated, "audited": False,
                    "revenue_cr": to_num(r.get("sales_cr")), "other_income_cr": to_num(r.get("other_income_cr")),
                    "expenses_cr": to_num(r.get("expenses_cr")), "interest_cr": to_num(r.get("interest_cr")),
                    "depreciation_cr": to_num(r.get("depreciation_cr")), "pbt_cr": to_num(r.get("pbt_cr")),
                    "net_profit_cr": to_num(r.get("net_profit_cr")), "eps": to_num(r.get("eps")),
                    "op_profit_cr": to_num(r.get("op_profit_cr")), "opm_pct": to_num(r.get("opm_pct"))})
    return out


def parse_screener_shareholding(blob: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sh = blob.get("shareholding_pattern") or {}
    if not isinstance(sh, dict) or not sh:
        return None

    def g(k):
        v = sh.get(k)
        return to_num(v.get("holding_pct")) if isinstance(v, dict) else None

    qs = [v.get("quarter") for v in sh.values() if isinstance(v, dict) and v.get("quarter")]
    qend = month_end(qs[0]) if qs else None
    prom = g("Promoters") if g("Promoters") is not None else g("Promoter")
    return {"quarter_end": qend, "promoter_pct": prom, "fii_pct": g("FIIs"), "dii_pct": g("DIIs"),
            "public_pct": g("Public"), "pledged_pct": to_num((blob.get("key_ratios") or {}).get("promoter_pledged_pct"))}


def parse_screener_dividends(blob: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for r in (blob.get("dividends") or {}).get("history") or []:
        end = month_end(str(r.get("year") or ""))
        dps = to_num(r.get("dps"))
        if end and dps is not None:
            out.append({"fy_end": end, "dps": dps, "eps": to_num(r.get("eps")), "payout_pct": to_num(r.get("payout_pct"))})
    return out
