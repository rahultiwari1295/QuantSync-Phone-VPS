import sqlite3
import json

conn = sqlite3.connect('/root/quantsync/Data/stock_history.db')
cur = conn.cursor()

cur.execute('''
    SELECT s.symbol, c.company_name, t.cmp, t.vol_20d_avg, s.candleCount
    FROM StockHistory s
    LEFT JOIN CompanyMaster c ON s.symbol = c.symbol
    LEFT JOIN StockTechnicalsDaily t ON s.symbol = t.symbol
    ORDER BY s.symbol ASC
''')
rows = cur.fetchall()
conn.close()

EQUITY_EXCLUSIONS = {
    'JETFREIGHT', 'GOLDIAM', 'SILVERTUC', 'SILVEROAK', 'GOLDKRISH', 'GOLDKART', 
    'GOLDENTOBC', 'BETAFASPH', 'BETAPHARMA'
}

etfs_by_cat = {}

for sym, comp_name, cmp, vol, candles in rows:
    sym_u = sym.upper()
    comp_u = (comp_name or '').upper()
    
    if sym_u in EQUITY_EXCLUSIONS:
        continue
        
    is_etf = False
    category = 'Broad Market & Equity Index ETFs'
    
    # 1. BeES family (Nippon India ETFs)
    if sym_u.endswith('BEES') or sym_u.startswith('BEES'):
        is_etf = True
        if 'GOLD' in sym_u: category = 'Gold & Precious Metals ETFs'
        elif 'SILVER' in sym_u: category = 'Silver ETFs'
        elif any(x in sym_u for x in ['LIQUID', 'LIQ', 'LTGILT']): category = 'Liquid, G-Sec & Debt ETFs'
        elif any(x in sym_u for x in ['BANK', 'IT', 'PHARMA', 'AUTO', 'INFRA', 'PSU', 'CPSE', 'METAL', 'CONSUM', 'MANUFG', 'DIVOPP', 'SHARIA']): category = 'Sectoral & Thematic ETFs'
        elif any(x in sym_u for x in ['NIFTY', 'JUNIOR', 'MID', 'NIF100', 'NV20', 'SNXT30', 'HNGSNGBEES']): category = 'Broad Market & Global Index ETFs'
        else: category = 'Broad Market & Global Index ETFs'
        
    # 2. Ends with ETF / IETF / BETF / GETF
    elif sym_u.endswith(('ETF', 'IETF', 'BETF', 'GETF')):
        is_etf = True
        if 'GOLD' in sym_u: category = 'Gold & Precious Metals ETFs'
        elif 'SILVER' in sym_u: category = 'Silver ETFs'
        elif any(x in sym_u for x in ['LIQ', 'GSEC', 'GILT', 'CASH']): category = 'Liquid, G-Sec & Debt ETFs'
        elif any(x in sym_u for x in ['BANK', 'IT', 'PHARMA', 'AUTO', 'INFRA', 'PSU', 'PVT', 'HEALTH', 'INSURE', 'OIL', 'METAL', 'EV', 'COMMO', 'TECH']): category = 'Sectoral & Thematic ETFs'
        elif any(x in sym_u for x in ['NIFTY', 'SENSEX', 'MID', 'SMALL', 'NEXT50', 'TOP', 'QUAL', 'LOWVOL', 'MOM', 'VAL', 'ALPHA', 'ALPL', 'BSE500']): category = 'Broad Market & Global Index ETFs'
        else: category = 'Broad Market & Global Index ETFs'
        
    # 3. SBI ETFs
    elif sym_u.startswith(('SETF', 'SBIETF', 'SBILIQ', 'SBINEQ', 'SBIVAL', 'SBISML')):
        is_etf = True
        if 'GOLD' in sym_u: category = 'Gold & Precious Metals ETFs'
        elif any(x in sym_u for x in ['10GILT', 'LIQ']): category = 'Liquid, G-Sec & Debt ETFs'
        elif any(x in sym_u for x in ['NIF', 'SENSEX', 'NN50', 'MID', 'SML', 'VAL', 'QLTY']): category = 'Broad Market & Global Index ETFs'
        else: category = 'Sectoral & Thematic ETFs'
        
    # 4. Global & Special ETFs
    elif sym_u in {'MON100', 'MAFANG', 'MOM100', 'MASPTOP50', 'HDFCNIFETF', 'HDFCBANKETF', 'HDFCGOLD', 'AXISGOLD', 'KOTAKGOLD', 'BSLGOLD', 'TATAGOLD', 'QGOLDHALF', 'CPSEETF', 'BHARAT22', 'ICICIMCAP'}:
        is_etf = True
        if 'FANG' in sym_u or '100' in sym_u or 'TOP50' in sym_u: category = 'Broad Market & Global Index ETFs'
        elif 'GOLD' in sym_u: category = 'Gold & Precious Metals ETFs'
        else: category = 'Broad Market & Global Index ETFs'
        
    # 5. Bullion & G-Sec tracking
    elif (sym_u.startswith(('GOLD', 'SILVER', 'GSEC')) or sym_u.endswith(('BND', 'CASE', 'BETA', 'ADD', 'AXIS', '360', '001', '1', 'AG'))) and ('FUND' in comp_u or 'ETF' in comp_u or not comp_name):
        if 'GOLD' in sym_u:
            is_etf = True
            category = 'Gold & Precious Metals ETFs'
        elif 'SILVER' in sym_u:
            is_etf = True
            category = 'Silver ETFs'
        elif 'GSEC' in sym_u:
            is_etf = True
            category = 'Liquid, G-Sec & Debt ETFs'
            
    if is_etf:
        etfs_by_cat.setdefault(category, []).append({
            'symbol': sym,
            'name': comp_name or sym,
            'cmp': cmp or 0.0,
            'candles': candles or 0
        })

total = sum(len(v) for v in etfs_by_cat.values())
print(f"Total Classified ETFs: {total}\n")
for cat in sorted(etfs_by_cat.keys()):
    items = etfs_by_cat[cat]
    print(f"### {cat} ({len(items)})")
    for it in sorted(items, key=lambda x: x['symbol']):
        print(f"- **`{it['symbol']}`** | CMP: ₹{it['cmp']:.2f} | ({it['candles']} bars)")
    print()
