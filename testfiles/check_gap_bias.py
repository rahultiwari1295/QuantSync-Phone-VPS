import os, json, sqlite3, pandas as pd
from engines.v5.dna_core import compute_dna_profile
from engines.v5.patterns import detect_v5_setups

HIST_DB = 'Data/stock_history.db'
conn = sqlite3.connect(HIST_DB)
rows = conn.cursor().execute("SELECT symbol, rawCandles FROM StockHistory WHERE isV3Eligible = 1 AND candleCount >= 100;").fetchall()
conn.close()

all_signals = []
for sym, raw_c in rows:
    try:
        df = pd.DataFrame(json.loads(raw_c)).rename(columns={'t': 'Date', 'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
        df['Date'] = df['Date'].astype(str).str[:10]
        df = df.sort_values('Date').reset_index(drop=True)
        idx_2026 = df[(df['Date'] >= '2026-01-01') & (df['Date'] <= '2026-09-22')].index
        for idx in idx_2026:
            if idx < 50 or idx >= len(df) - 5: continue
            sub = df.iloc[:idx+1]
            dna = compute_dna_profile(sub)
            s = detect_v5_setups(sym, sub, dna)
            if s and dna.get('health_score', 0) >= 65 and 'VOLUME_CHURN' not in dna.get('vsa_flags', []):
                all_signals.append({'symbol': sym, 'idx': idx, 'df': df, 'setup': s})
    except: pass

print(f'Total signals: {len(all_signals)}')

# Test 1: Just Next-Day Open Entry (keeping other naive assumptions)
open_pnl = []
close_pnl = []
for s in all_signals:
    df = s['df']
    idx = s['idx']
    fwd = df.iloc[idx+1:min(len(df), idx+25)]
    if len(fwd) < 3: continue
    
    # Close entry
    c_ep = float(df.iloc[idx]['Close'])
    # Open entry
    o_ep = float(fwd.iloc[0]['Open'])
    
    gap_pct = (o_ep - c_ep) / c_ep * 100
    close_pnl.append(gap_pct)

s_gap = pd.Series(close_pnl)
print(f'Average Next-Day Gap: {s_gap.mean():.2f}% (Median: {s_gap.median():.2f}%, Max gap: {s_gap.max():.2f}%)')
print(f'Gapping up > +2%: {(s_gap > 2.0).mean()*100:.1f}% of trades')
print(f'Gapping up > +5%: {(s_gap > 5.0).mean()*100:.1f}% of trades')
