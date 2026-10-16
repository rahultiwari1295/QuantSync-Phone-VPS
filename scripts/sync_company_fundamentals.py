import sqlite3
import json
import re
import datetime

DB_HISTORY = "/root/quantsync/Data/stock_history.db"
DB_CACHE = "/root/quantsync/Data/fundamental_cache.db"

MONTH_MAP = {
    "JAN": ("01", "31"), "FEB": ("02", "28"), "MAR": ("03", "31"),
    "APR": ("04", "30"), "MAY": ("05", "31"), "JUN": ("06", "30"),
    "JUL": ("07", "31"), "AUG": ("08", "31"), "SEP": ("09", "30"),
    "OCT": ("10", "31"), "NOV": ("11", "30"), "DEC": ("12", "31")
}

def parse_period_end_date(quarter_str):
    # e.g. "Sep 2025" -> "2025-09-30"
    m = re.search(r'([A-Za-z]{3})\s+(\d{4})', quarter_str.strip())
    if not m:
        return "1970-01-01"
    mon = m.group(1).upper()
    yr = m.group(2)
    mm, dd = MONTH_MAP.get(mon, ("12", "31"))
    # leap year check for Feb
    if mm == "02" and int(yr) % 4 == 0 and (int(yr) % 100 != 0 or int(yr) % 400 == 0):
        dd = "29"
    return f"{yr}-{mm}-{dd}"

def to_float(val):
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").replace("%", "").strip()
        if s in ("", "-", "N/A", "nan", "None"):
            return None
        return float(s)
    except:
        return None

def run_sync():
    conn_cache = sqlite3.connect(DB_CACHE)
    c_cache = conn_cache.cursor()
    
    conn_hist = sqlite3.connect(DB_HISTORY)
    c_hist = conn_hist.cursor()

    rows = c_cache.execute("SELECT symbol, data_json FROM stock_fundamentals").fetchall()
    print(f"Total symbols in cache: {len(rows)}")

    inserted_count = 0
    updated_count = 0
    symbols_processed = set()

    for sym, data_json in rows:
        if not data_json:
            continue
        try:
            d = json.loads(data_json)
        except Exception:
            continue

        q_perf = d.get("quarterly_performance", [])
        if not q_perf:
            continue

        source_url = d.get("source_url")
        fetched_at = d.get("fetched_at")
        kr = d.get("key_ratios", {})
        sh = d.get("shareholding_pattern", {})
        bs = d.get("balance_sheet", {})

        roce_val = to_float(kr.get("roce_pct"))
        roe_val = to_float(kr.get("roe_pct"))
        de_val = to_float(kr.get("debt_to_equity"))

        # Shareholding details (if available)
        promoter_val = to_float(sh.get("Promoters", {}).get("holding_pct"))
        fii_val = to_float(sh.get("FIIs", {}).get("holding_pct"))
        dii_val = to_float(sh.get("DIIs", {}).get("holding_pct"))
        public_val = to_float(sh.get("Public", {}).get("holding_pct"))

        # Balance sheet details
        eq_cap = to_float(bs.get("Equity Capital") or bs.get("Share Capital"))
        reserves = to_float(bs.get("Reserves"))
        borrowings = to_float(bs.get("Borrowings"))
        tot_assets = to_float(bs.get("Total Assets") or bs.get("Total Liabilities"))

        # Sort quarters chronologically
        for q in q_perf:
            quarter = q.get("quarter")
            if not quarter:
                continue
            period_end = parse_period_end_date(quarter)
            rev = to_float(q.get("sales_cr") or q.get("revenue_cr"))
            op = to_float(q.get("op_profit_cr"))
            opm = to_float(q.get("opm_pct"))
            net_prof = to_float(q.get("net_profit_cr"))
            eps = to_float(q.get("eps"))

            # Determine latest quarter in q_perf to assign point-in-time balance sheet / ratios if appropriate
            # We insert with ON CONFLICT UPDATE
            c_hist.execute("""
                INSERT INTO CompanyFundamentalsHistory (
                    symbol, quarter, period_type, period_end_date,
                    source, source_url, source_fetched_at,
                    revenue_cr, op_profit_cr, opm_pct, net_profit_cr, eps,
                    total_assets_cr, equity_capital_cr, reserves_cr, borrowings_cr,
                    roce_pct, roe_pct, debt_to_equity,
                    promoter_holding_pct, fii_holding_pct, dii_holding_pct, public_holding_pct
                ) VALUES (?, ?, 'QUARTERLY', ?, 'SCREENER_QUARTERS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, period_end_date, period_type) DO UPDATE SET
                    revenue_cr = COALESCE(excluded.revenue_cr, CompanyFundamentalsHistory.revenue_cr),
                    op_profit_cr = COALESCE(excluded.op_profit_cr, CompanyFundamentalsHistory.op_profit_cr),
                    opm_pct = COALESCE(excluded.opm_pct, CompanyFundamentalsHistory.opm_pct),
                    net_profit_cr = COALESCE(excluded.net_profit_cr, CompanyFundamentalsHistory.net_profit_cr),
                    eps = COALESCE(excluded.eps, CompanyFundamentalsHistory.eps),
                    total_assets_cr = COALESCE(excluded.total_assets_cr, CompanyFundamentalsHistory.total_assets_cr),
                    equity_capital_cr = COALESCE(excluded.equity_capital_cr, CompanyFundamentalsHistory.equity_capital_cr),
                    reserves_cr = COALESCE(excluded.reserves_cr, CompanyFundamentalsHistory.reserves_cr),
                    borrowings_cr = COALESCE(excluded.borrowings_cr, CompanyFundamentalsHistory.borrowings_cr),
                    roce_pct = COALESCE(excluded.roce_pct, CompanyFundamentalsHistory.roce_pct),
                    roe_pct = COALESCE(excluded.roe_pct, CompanyFundamentalsHistory.roe_pct),
                    debt_to_equity = COALESCE(excluded.debt_to_equity, CompanyFundamentalsHistory.debt_to_equity),
                    promoter_holding_pct = COALESCE(excluded.promoter_holding_pct, CompanyFundamentalsHistory.promoter_holding_pct),
                    fii_holding_pct = COALESCE(excluded.fii_holding_pct, CompanyFundamentalsHistory.fii_holding_pct),
                    dii_holding_pct = COALESCE(excluded.dii_holding_pct, CompanyFundamentalsHistory.dii_holding_pct),
                    public_holding_pct = COALESCE(excluded.public_holding_pct, CompanyFundamentalsHistory.public_holding_pct),
                    source_url = COALESCE(excluded.source_url, CompanyFundamentalsHistory.source_url),
                    source_fetched_at = COALESCE(excluded.source_fetched_at, CompanyFundamentalsHistory.source_fetched_at)
            """, (
                sym, quarter, period_end,
                source_url, fetched_at,
                rev, op, opm, net_prof, eps,
                tot_assets, eq_cap, reserves, borrowings,
                roce_val, roe_val, de_val,
                promoter_val, fii_val, dii_val, public_val
            ))
            inserted_count += 1
        symbols_processed.add(sym)

    conn_hist.commit()
    conn_hist.close()
    conn_cache.close()

    print(f"Sync complete! Processed {len(symbols_processed)} symbols, total {inserted_count} quarter rows processed.")

if __name__ == '__main__':
    run_sync()
