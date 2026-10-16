import sqlite3, urllib.request, re, time

db = '/root/quantsync/Data/stock_history.db'
conn = sqlite3.connect(db)
cur = conn.cursor()

cur.execute('SELECT symbol FROM CompanyMaster WHERE market_cap_cr IS NULL OR market_cap_cr = 0')
missing = [r[0] for r in cur.fetchall()]
print(f'Total missing market_cap: {len(missing)}')

headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

def fetch_screener_quick(sym):
    url = f'https://www.screener.in/company/{sym}/'
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            html = r.read().decode('utf-8', errors='ignore')
            mcap = re.search(r'Market Cap.*?class="number">([\d,]+)', html, re.DOTALL)
            pe = re.search(r'Stock P/E.*?class="number">([\d,.]+)', html, re.DOTALL)
            roce = re.search(r'ROCE.*?class="number">([\d,.]+)', html, re.DOTALL)
            roe = re.search(r'ROE.*?class="number">([\d,.]+)', html, re.DOTALL)
            bv = re.search(r'Book Value.*?class="number">([\d,.]+)', html, re.DOTALL)
            div = re.search(r'Dividend Yield.*?class="number">([\d,.]+)', html, re.DOTALL)
            fv = re.search(r'Face Value.*?class="number">([\d,.]+)', html, re.DOTALL)
            
            # About
            about_m = re.search(r'<div class="about">\s*<p>(.*?)</p>', html, re.DOTALL)
            about = re.sub(r'<[^>]+>', '', about_m.group(1)).strip() if about_m else None
            
            # Name
            name_m = re.search(r'<h1[^>]*class="margin-0 [^"]*">\s*([^<]+?)\s*</h1>', html, re.DOTALL)
            name = name_m.group(1).strip() if name_m else None

            return {
                'name': name,
                'about': about,
                'mcap': float(mcap.group(1).replace(',', '')) if mcap else None,
                'pe': float(pe.group(1).replace(',', '')) if pe else None,
                'roce': float(roce.group(1).replace(',', '')) if roce else None,
                'roe': float(roe.group(1).replace(',', '')) if roe else None,
                'bv': float(bv.group(1).replace(',', '')) if bv else None,
                'div': float(div.group(1).replace(',', '')) if div else None,
                'fv': float(fv.group(1).replace(',', '')) if fv else None
            }
    except Exception:
        return None

found = 0
for s in missing[:15]:
    res = fetch_screener_quick(s)
    if res and res.get('mcap') is not None:
        found += 1
        print(f"  [FOUND] {s:12}: Mcap={res['mcap']} Cr | PE={res['pe']} | ROCE={res['roce']}% | Name={res['name']}")
    else:
        print(f"  [MISS]  {s:12}")
    time.sleep(0.3)

print(f'Sample recovery rate: {found}/15 ({found/15*100:.1f}%)')
