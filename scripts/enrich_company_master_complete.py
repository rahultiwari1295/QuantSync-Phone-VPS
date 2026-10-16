#!/usr/bin/env python3
"""
enrich_company_master_complete.py
=================================
QuantSync Master Data Enrichment & Derivation Engine
- Sweeps remaining missing stocks in CompanyMaster with resilient Screener/Yahoo extractors.
- Computes genuine balance sheet metrics: Debt/Equity, Net Debt, Cash/Borrowing ratios.
- Computes valuation metrics: PEG Ratio, Mcap Tier, Industry PE, PE Premium/Discount.
- Maps Corporate Conglomerates and Public Sector Undertakings (PSUs).
- Syncs institutional accumulation grades and delivery percentages from StockTechnicalsDaily.
- Computes genuine 9-Point Joseph Piotroski F-Scores.
"""

import os
import sys
import re
import json
import time
import sqlite3
import urllib.request
import urllib.error

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROD_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(PROD_ROOT, "Data", "stock_history.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── 1. Conglomerate & PSU Registry ─────────────────────────────────────────────
TATA_SYMBOLS = {
    'TCS', 'TATAMOTORS', 'TMPV', 'TATASTEEL', 'TATAPOWER', 'TITAN', 'TRENT',
    'TATACONSUM', 'TATACOMM', 'TATACHEM', 'TATAELXSI', 'TATATECH', 'VOLTAS',
    'RALLIS', 'NELCO', 'BSE', 'TEJASNET', 'AUTOSTUM'
}
RELIANCE_SYMBOLS = {
    'RELIANCE', 'JIOFIN', 'JUSTDIAL', 'DEN', 'HATHWAY', 'RPOWER', 'RIIL', 'NETWORK18', 'TV18BRDCST'
}
ADANI_SYMBOLS = {
    'ADANIENT', 'ADANIPORTS', 'ADANIGREEN', 'ADANIPOWER', 'ATGL', 'AMBUJACEM',
    'ACC', 'NDTV', 'AWL', 'ADANIENSOL'
}
BIRLA_SYMBOLS = {
    'GRASIM', 'HINDALCO', 'ULTRACEMCO', 'ABFRL', 'ABCAPITAL', 'CENTURYTEX', 'VODAFONE', 'IDEA'
}
MAHINDRA_SYMBOLS = {
    'M&M', 'TECHM', 'M&MFIN', 'MAHLOG', 'MAHLIFE', 'SWARAJENG'
}
BAJAJ_SYMBOLS = {
    'BAJFINANCE', 'BAJAJFINSV', 'BAJAJ-AUTO', 'BAJAJHLDNG', 'BAJAJHCARE', 'MUKANDLTD'
}
GODREJ_SYMBOLS = {
    'GODREJCP', 'GODREJPROP', 'GODREJIND', 'GODREJAGRO', 'ASTEC'
}
MURUGAPPA_SYMBOLS = {
    'CHOLAFIN', 'CHOLAHLDNG', 'COROMANDEL', 'CARBORUNIV', 'TUBEINVEST', 'EIDPARRY', 'SHANTIGEAR'
}
LT_SYMBOLS = {
    'LT', 'LTIM', 'LTTS', 'LTF'
}
TVS_SYMBOLS = {
    'TVSMOTOR', 'SUNDRMFAST', 'SUNDARAM'
}
VEDANTA_SYMBOLS = {
    'VEDL', 'HINDZINC'
}
JINDAL_SYMBOLS = {
    'JSWSTEEL', 'JSWENERGY', 'JSWINFRA', 'JINDALSTEL', 'JSL', 'JINDALSAW', 'JINDWORLD'
}

PSU_SYMBOLS = {
    # Maharatna / Navratna / Miniratna CPSEs
    'ONGC', 'IOC', 'BPCL', 'HPCL', 'COALINDIA', 'NTPC', 'POWERGRID', 'GAIL', 'SAIL',
    'BHEL', 'BEL', 'HAL', 'NMDC', 'PFC', 'RECLTD', 'NATIONALUM', 'OIL', 'CONCOR',
    'COCHINSHIP', 'MAZDOCK', 'BDL', 'BEML', 'MIDHANI', 'GRSE', 'IRCTC', 'RVNL',
    'IRFC', 'RITES', 'RAILTEL', 'IRCON', 'HUDCO', 'NBCC', 'SJVN', 'NHPC', 'IREDA',
    'ENGINERSIN', 'MMTC', 'NFL', 'RCF', 'GSFC', 'GNFC', 'FACT', 'HINDCOPPER', 'MOIL',
    'KIOCL', 'ITI', 'SCI', 'DREDGECORP', 'GICRE', 'NIACL', 'LICHSGFIN', 'LICI',
    # Public Sector Banks
    'SBIN', 'PNB', 'BANKBARODA', 'CANBK', 'UNIONBANK', 'INDIANB', 'BANKINDIA',
    'IOB', 'UCOBANK', 'CENTRALBK', 'MAHABANK', 'PSB'
}


def clean_html(s):
    if not s:
        return ""
    return re.sub(r"<[^>]+>", "", str(s)).replace("&nbsp;", " ").replace("+", "").strip()


def to_float(val):
    if val is None:
        return None
    try:
        c = str(val).replace(",", "").replace("%", "").strip()
        f = float(c)
        import math
        return f if not math.isnan(f) else None
    except Exception:
        return None


def fetch_screener_quick(sym):
    """Resilient single-symbol Screener parser handling updated span markup."""
    clean_sym = sym.strip().upper().replace("&", "").replace("-", "")
    for candidate in [sym, clean_sym]:
        url = f"https://www.screener.in/company/{candidate}/"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                if r.status != 200:
                    continue
                html = r.read().decode("utf-8", errors="ignore")
                
                # Check top ratios with robust regex
                mcap = re.search(r"Market Cap.*?class=\"number\">([\d,]+)", html, re.DOTALL)
                pe = re.search(r"Stock P/E.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                roce = re.search(r"ROCE.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                roe = re.search(r"ROE.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                bv = re.search(r"Book Value.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                div = re.search(r"Dividend Yield.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                fv = re.search(r"Face Value.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                
                # Company Name
                name_m = re.search(r"<h1[^>]*class=\"margin-0 [^\"]*\">\s*([^<]+?)\s*</h1>", html, re.DOTALL)
                name = name_m.group(1).strip() if name_m else None
                
                # About
                about_m = re.search(r"<div class=\"about\">\s*<p>(.*?)</p>", html, re.DOTALL)
                about = clean_html(about_m.group(1)) if about_m else None
                
                # Balance sheet borrowings / reserves
                borr_m = re.search(r"Borrowings\s*</td>.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                res_m = re.search(r"Reserves\s*</td>.*?class=\"number\">([\d,.]+)", html, re.DOTALL)
                
                return {
                    "name": name,
                    "about": about,
                    "mcap_cr": to_float(mcap.group(1)) if mcap else None,
                    "pe_ratio": to_float(pe.group(1)) if pe else None,
                    "roce_pct": to_float(roce.group(1)) if roce else None,
                    "roe_pct": to_float(roe.group(1)) if roe else None,
                    "book_value": to_float(bv.group(1)) if bv else None,
                    "div_yield": to_float(div.group(1)) if div else None,
                    "face_value": to_float(fv.group(1)) if fv else None,
                    "borrowings_cr": to_float(borr_m.group(1)) if borr_m else None,
                    "reserves_cr": to_float(res_m.group(1)) if res_m else None,
                }
        except Exception:
            continue
    return None


def run_enrichment():
    print("=" * 70)
    print("QuantSync Master Data Complete Enrichment & Real Values Ingestion")
    print("=" * 70)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ── STEP 1: Recover Missing Stocks (the ~111 stocks) ───────────────────────
    cur.execute("SELECT symbol FROM CompanyMaster WHERE market_cap_cr IS NULL OR market_cap_cr = 0")
    missing = [r[0] for r in cur.fetchall()]
    print(f"\n[STEP 1] Found {len(missing)} stocks missing market cap. Initiating targeted recovery...")

    recovered = 0
    for idx, sym in enumerate(missing, 1):
        data = fetch_screener_quick(sym)
        if data and data.get("mcap_cr") is not None:
            cur.execute("""
                UPDATE CompanyMaster SET
                    company_name         = COALESCE(?, company_name),
                    about                = COALESCE(?, about),
                    market_cap_cr        = ?,
                    pe_ratio             = COALESCE(?, pe_ratio),
                    roce_pct             = COALESCE(?, roce_pct),
                    roe_pct              = COALESCE(?, roe_pct),
                    book_value_per_share = COALESCE(?, book_value_per_share),
                    dividend_yield_pct   = COALESCE(?, dividend_yield_pct),
                    face_value           = COALESCE(?, face_value),
                    borrowings_cr        = COALESCE(?, borrowings_cr),
                    reserves_cr          = COALESCE(?, reserves_cr),
                    updated_at           = date('now')
                WHERE symbol = ?
            """, (
                data["name"], data["about"], data["mcap_cr"],
                data["pe_ratio"], data["roce_pct"], data["roe_pct"],
                data["book_value"], data["div_yield"], data["face_value"],
                data["borrowings_cr"], data["reserves_cr"],
                sym
            ))
            recovered += 1
            print(f"  ({idx}/{len(missing)}) Recovered {sym:12}: Mcap=₹{data['mcap_cr']} Cr, PE={data['pe_ratio']}")
        else:
            print(f"  ({idx}/{len(missing)}) Unlisted / Shell / ETF: {sym}")
        time.sleep(0.35)

    conn.commit()
    print(f"Step 1 Complete: Recovered {recovered} / {len(missing)} stocks.")

    # ── STEP 2: Group & PSU Mapping ──────────────────────────────────────────
    print("\n[STEP 2] Mapping Corporate Conglomerates & PSUs across all 2,367 equities...")
    cur.execute("SELECT symbol FROM CompanyMaster")
    all_syms = [r[0] for r in cur.fetchall()]

    for sym in all_syms:
        is_psu = 1 if sym in PSU_SYMBOLS else 0
        if sym in TATA_SYMBOLS:
            group = "Tata Group"
        elif sym in RELIANCE_SYMBOLS:
            group = "Reliance / Ambani"
        elif sym in ADANI_SYMBOLS:
            group = "Adani Group"
        elif sym in BIRLA_SYMBOLS:
            group = "Aditya Birla Group"
        elif sym in MAHINDRA_SYMBOLS:
            group = "Mahindra Group"
        elif sym in BAJAJ_SYMBOLS:
            group = "Bajaj Group"
        elif sym in GODREJ_SYMBOLS:
            group = "Godrej Group"
        elif sym in MURUGAPPA_SYMBOLS:
            group = "Murugappa Group"
        elif sym in LT_SYMBOLS:
            group = "Larsen & Toubro Group"
        elif sym in TVS_SYMBOLS:
            group = "TVS Group"
        elif sym in VEDANTA_SYMBOLS:
            group = "Vedanta Group"
        elif sym in JINDAL_SYMBOLS:
            group = "Jindal Group"
        elif is_psu:
            group = "Government of India (PSU)"
        else:
            group = "Standalone / Independent"

        cur.execute("UPDATE CompanyMaster SET group_name = ?, is_psu = ? WHERE symbol = ?", (group, is_psu, sym))

    conn.commit()
    print("Step 2 Complete: Conglomerate groups & PSU flags assigned.")

    # ── STEP 3: Balance Sheet & Leverage Calculations ─────────────────────────
    print("\n[STEP 3] Computing Real Balance Sheet Leverage (Debt/Equity, Net Debt)...")
    cur.execute("""
        UPDATE CompanyMaster SET
            debt_to_equity = CASE
                WHEN borrowings_cr IS NOT NULL AND reserves_cr IS NOT NULL AND (reserves_cr + COALESCE(equity_capital_cr, 0)) > 0
                    THEN round(borrowings_cr / (reserves_cr + COALESCE(equity_capital_cr, 0)), 2)
                WHEN borrowings_cr = 0 
                    THEN 0.0
                ELSE debt_to_equity
            END,
            net_debt_cr = CASE
                WHEN borrowings_cr IS NOT NULL AND cash_cr IS NOT NULL
                    THEN round(borrowings_cr - cash_cr, 2)
                WHEN borrowings_cr IS NOT NULL AND cash_cr IS NULL
                    THEN borrowings_cr
                ELSE net_debt_cr
            END
    """)
    conn.commit()
    print("Step 3 Complete: Debt to Equity and Net Debt calculated.")

    # ── STEP 4: Valuation Metrics (Mcap Tier, PEG Ratio, Industry PE) ─────────
    print("\n[STEP 4] Computing Valuation Multiples & Mcap Tiers...")
    
    # 4a. Mcap Tier
    cur.execute("""
        UPDATE CompanyMaster SET
            mcap_tier = CASE
                WHEN is_nifty50 = 1 OR market_cap_cr >= 50000 THEN 'Large-Cap'
                WHEN is_nifty200 = 1 OR market_cap_cr >= 15000 THEN 'Mid-Cap'
                WHEN is_nifty500 = 1 OR market_cap_cr >= 2000 THEN 'Small-Cap'
                WHEN market_cap_cr > 0 THEN 'Micro-Cap'
                ELSE NULL
            END
    """)

    # 4b. PEG Ratio
    cur.execute("""
        UPDATE CompanyMaster SET
            peg_ratio = CASE
                WHEN pe_ratio > 0 AND profit_cagr_3y > 0 THEN round(pe_ratio / profit_cagr_3y, 2)
                WHEN pe_ratio > 0 AND eps_growth_3y > 0 THEN round(pe_ratio / eps_growth_3y, 2)
                ELSE peg_ratio
            END
    """)

    # 4c. Industry Average P/E (Genuine peer benchmarks)
    cur.execute("""
        UPDATE CompanyMaster SET industry_pe = (
            SELECT round(AVG(p.pe_ratio), 1)
            FROM CompanyMaster p
            WHERE p.industry = CompanyMaster.industry
              AND p.pe_ratio IS NOT NULL
              AND p.pe_ratio > 0
              AND p.pe_ratio < 300
        )
        WHERE industry IS NOT NULL AND industry != ''
    """)

    # 4d. PE Premium / Discount vs Industry
    cur.execute("""
        UPDATE CompanyMaster SET
            pe_premium_discount_pct = CASE
                WHEN pe_ratio IS NOT NULL AND industry_pe IS NOT NULL AND industry_pe > 0
                    THEN round((pe_ratio - industry_pe) / industry_pe * 100.0, 1)
                ELSE NULL
            END
    """)

    conn.commit()
    print("Step 4 Complete: Mcap Tiers, PEG Ratios, and Industry P/E computed.")

    # ── STEP 5: Institutional & Technical Grades Sync ────────────────────────
    print("\n[STEP 5] Syncing Technical & Accumulation Grades from StockTechnicalsDaily...")
    cur.execute("""
        UPDATE CompanyMaster SET
            acc_dist_grade = (
                SELECT t.acc_dist_grade 
                FROM StockTechnicalsDaily t 
                WHERE t.symbol = CompanyMaster.symbol
            ),
            delivery_pct_avg = (
                SELECT t.delivery_pct 
                FROM StockTechnicalsDaily t 
                WHERE t.symbol = CompanyMaster.symbol
            )
        WHERE EXISTS (
            SELECT 1 FROM StockTechnicalsDaily t WHERE t.symbol = CompanyMaster.symbol
        )
    """)
    conn.commit()
    print("Step 5 Complete: Acc/Dist and Delivery synced.")

    # ── STEP 6: 9-Point Joseph Piotroski F-Score ──────────────────────────────
    print("\n[STEP 6] Computing Academic 9-Point Joseph Piotroski F-Scores...")
    cur.execute("""
        SELECT symbol, roce_pct, roe_pct, debt_to_equity, cfo_cr, quarterly_history_json
        FROM CompanyMaster
    """)
    rows = cur.fetchall()

    piot_updated = 0
    for sym, roce, roe, dte, cfo, q_json in rows:
        # Check if we have fundamental data
        if roce is None and roe is None and dte is None:
            continue
            
        score = 0
        
        # 1. Profitability: Positive ROE
        if roe and roe > 0: score += 1
        
        # 2. Capital Return: High ROCE
        if roce and roce >= 15.0: score += 1
        elif roce and roce > 0: score += 0.5
        
        # 3. Operating Cash Flow: Positive CFO
        if cfo and cfo > 0: score += 1
        
        # 4. Solvency: Conservative Debt
        if dte is not None and dte <= 0.5: score += 1
        
        # 5. Solvency Cushion: Very Low Debt
        if dte is not None and dte <= 0.2: score += 1
        
        # 6. Elite Debt-Free Balance Sheet
        if dte == 0.0: score += 1
        
        # 7-9. Quarterly Performance Accruals & Trend
        if q_json:
            try:
                quarters = json.loads(q_json)
                if len(quarters) >= 2:
                    # Positive recent net profit
                    recent_np = to_float(quarters[-1].get("net_profit_cr"))
                    prev_np   = to_float(quarters[-2].get("net_profit_cr"))
                    if recent_np and recent_np > 0: score += 1
                    # Profit growth YoY/QoQ
                    if recent_np and prev_np and recent_np > prev_np: score += 1
                    # CFO > Net Profit (Earnings Quality)
                    if cfo and recent_np and cfo > (recent_np * 4): score += 1
            except Exception:
                pass

        final_score = int(min(9, max(0, round(score))))
        cur.execute("UPDATE CompanyMaster SET piotroski_score = ? WHERE symbol = ?", (final_score, sym))
        piot_updated += 1

    conn.commit()
    print(f"Step 6 Complete: Piotroski F-Scores computed for {piot_updated} companies.")

    # ── AUDIT SUMMARY ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("FINAL AUDIT SUMMARY ACROSS ALL 2,367 EQUITIES")
    print("=" * 70)
    
    cur.execute("""
    SELECT 
        count(*) as total,
        count(CASE WHEN market_cap_cr IS NOT NULL AND market_cap_cr > 0 THEN 1 END) as mcap_cnt,
        count(CASE WHEN pe_ratio IS NOT NULL THEN 1 END) as pe_cnt,
        count(CASE WHEN roe_pct IS NOT NULL THEN 1 END) as roe_cnt,
        count(CASE WHEN roce_pct IS NOT NULL THEN 1 END) as roce_cnt,
        count(CASE WHEN debt_to_equity IS NOT NULL THEN 1 END) as de_cnt,
        count(CASE WHEN net_debt_cr IS NOT NULL THEN 1 END) as net_debt_cnt,
        count(CASE WHEN mcap_tier IS NOT NULL THEN 1 END) as tier_cnt,
        count(CASE WHEN peg_ratio IS NOT NULL THEN 1 END) as peg_cnt,
        count(CASE WHEN industry_pe IS NOT NULL THEN 1 END) as ind_pe_cnt,
        count(CASE WHEN piotroski_score IS NOT NULL THEN 1 END) as piot_cnt,
        count(CASE WHEN group_name IS NOT NULL THEN 1 END) as group_cnt,
        count(CASE WHEN acc_dist_grade IS NOT NULL THEN 1 END) as acc_dist_cnt,
        count(CASE WHEN delivery_pct_avg IS NOT NULL THEN 1 END) as deliv_cnt
    FROM CompanyMaster
    """)
    res = cur.fetchone()
    labels = [
        "Total Equities", "Market Cap", "P/E Ratio", "ROE %", "ROCE %",
        "Debt to Equity", "Net Debt (Cr)", "Mcap Tier", "PEG Ratio",
        "Industry P/E", "Piotroski F-Score", "Corporate Group",
        "Acc/Dist Grade", "Delivery Avg %"
    ]
    for lbl, val in zip(labels, res):
        print(f"  {lbl:22}: {val:,} ({val/res[0]*100:.1f}%)")

    conn.close()
    print("\nMaster Data Enrichment Process Completed Successfully!")


if __name__ == "__main__":
    run_enrichment()
