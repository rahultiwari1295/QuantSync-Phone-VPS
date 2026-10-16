import sqlite3
import json

conn = sqlite3.connect('file:/root/quantsync/Data/quantsync_master.db?mode=ro', uri=True)
cur = conn.cursor()

cur.execute('SELECT date, cash, invested_capital, total_nav, active_positions_count FROM daily_nav WHERE engine_version="v2" ORDER BY date DESC LIMIT 20')
nav_rows = sorted(cur.fetchall(), key=lambda x: x[0])
dates = [r[0] for r in nav_rows]

cur.execute('''
    SELECT trade_id, symbol, strategy_id, entry_date, exit_date, entry_price, exit_price, shares, invested_capital, gross_pnl, net_pnl, exit_reason, holding_days, 'CLOSED' as status
    FROM closed_trades WHERE engine_version = 'v2'
    UNION ALL
    SELECT trade_id, symbol, strategy_id, entry_date, '' as exit_date, entry_price, current_ltp as exit_price, shares, invested_capital, unrealized_pnl as gross_pnl, unrealized_pnl as net_pnl, '' as exit_reason, 0 as holding_days, status
    FROM active_trades WHERE engine_version = 'v2'
''')
all_trades = cur.fetchall()

cur.execute('SELECT symbol, current_stop, target_price FROM active_trades WHERE engine_version="v2"')
active_meta = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

conn.close()

result = []
prev_nav = None

for idx, d in enumerate(dates, 1):
    entries = [t for t in all_trades if t[3] == d]
    exits = [t for t in all_trades if t[4] == d]
    open_pos = [t for t in all_trades if t[3] <= d and (t[4] > d or not t[4])]
    
    nav_row = [r for r in nav_rows if r[0] == d][0]
    cash, inv, nav, pos_cnt = nav_row[1], nav_row[2], nav_row[3], nav_row[4]
    
    chg = (nav - prev_nav) if prev_nav else 0.0
    pct = (chg / prev_nav * 100.0) if prev_nav else 0.0
    prev_nav = nav
    
    ent_list = []
    for e in entries:
        meta_val = active_meta.get(e[1], (round(e[5]*0.935, 2), round(e[5]*1.15, 2)))
        ent_list.append({
            "symbol": e[1],
            "strategy": e[2],
            "entry_price": round(e[5], 2),
            "shares": e[7],
            "invested": round(e[8], 2),
            "stop": round(meta_val[0], 2),
            "target": round(meta_val[1], 2)
        })
        
    ext_list = []
    for x in exits:
        pnl_pct = (x[6] - x[5]) / x[5] * 100.0 if x[5] else 0.0
        ext_list.append({
            "symbol": x[1],
            "strategy": x[2],
            "entry_date": x[3],
            "entry_price": round(x[5], 2),
            "exit_price": round(x[6], 2),
            "pnl_pct": round(pnl_pct, 2),
            "net_pnl": round(x[10], 2),
            "reason": x[11],
            "hold_days": x[12]
        })
        
    result.append({
        "day_num": idx,
        "date": d,
        "nav": round(nav, 2),
        "daily_pct": round(pct, 2),
        "cash": round(cash, 2),
        "invested": round(inv, 2),
        "open_count": pos_cnt,
        "open_symbols": [p[1] for p in open_pos],
        "entries": ent_list,
        "exits": ext_list
    })

with open('/root/quantsync/logs/replay_v2_20days.json', 'w') as f:
    json.dump(result, f, indent=2)

print(f"Generated 20-day replay for {len(result)} trading days.")
EOF
