"""
get_v5_active_trades.py
=======================
Simulates the continuous portfolio state of QuantSync-V5 up to the latest market session
(2026-09-22) and outputs all currently open/active positions with:
- Symbol
- Strategy
- Entry Date
- Entry Price
- Current LTP (Latest close)
- Current Trailing Stop
- Target 1 & Target 2
- Unrealized ROI %
- Unrealized PnL (₹)
"""

import os
import sys
import json
import sqlite3
import pandas as pd
from typing import Dict, List, Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_DB = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")

from engines.v5.dna_core import compute_dna_profile
from engines.v5.patterns import detect_v5_setups
from engines.v5.arbiter import arbiter_evaluate

def get_v5_active_portfolio():
    conn = sqlite3.connect(HIST_DB)
    cur = conn.cursor()
    cur.execute("SELECT symbol, rawCandles FROM StockHistory WHERE isV3Eligible = 1 AND candleCount >= 100;")
    rows = cur.fetchall()
    conn.close()

    # We evaluate trades entered in the last 40 trading days that haven't hit stop/target yet
    active_v5_positions = []

    for sym, raw_c in rows:
        try:
            candles = json.loads(raw_c)
            df = pd.DataFrame(candles).rename(columns={'t': 'Date', 'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
            df['Date'] = df['Date'].astype(str).str[:10]
            df = df.sort_values('Date').reset_index(drop=True)

            if len(df) < 50:
                continue

            latest_idx = len(df) - 1
            latest_date = df.iloc[-1]['Date']
            latest_close = float(df.iloc[-1]['Close'])

            # Look back over recent trading days (last 30 sessions)
            lookback_start = max(50, len(df) - 30)

            for idx in range(lookback_start, latest_idx + 1):
                sub_df = df.iloc[:idx+1].copy()
                e_date = sub_df.iloc[-1]['Date']

                dna = compute_dna_profile(sub_df)
                setup = detect_v5_setups(sym, sub_df, dna)

                if setup is not None:
                    decision = arbiter_evaluate(
                        setup=setup,
                        dna=dna,
                        portfolio_nav=300000.0,
                        current_dd_pct=0.0,
                        sector_counts={}
                    )

                    if decision.take:
                        ep = setup.entry_price
                        stop_p = setup.stop_price
                        t1 = setup.target_1
                        t2 = setup.target_2

                        # Forward bars from entry up to today
                        forward_bars = df.iloc[idx+1:latest_idx+1]
                        
                        is_open = True
                        curr_stop = stop_p
                        t1_hit = False

                        for _, b in forward_bars.iterrows():
                            h = float(b['High'])
                            l = float(b['Low'])

                            # Breakeven lock check
                            if h >= ep * 1.045 and curr_stop < ep:
                                curr_stop = ep

                            if not t1_hit and h >= t1:
                                t1_hit = True
                                curr_stop = max(curr_stop, ep)

                            # Final Target hit
                            if t1_hit and h >= t2:
                                is_open = False
                                break

                            # Stop hit
                            if l <= curr_stop:
                                is_open = False
                                break

                        if is_open:
                            allocated = decision.allocated_capital
                            shares = int(allocated / ep) if ep > 0 else 0
                            if shares > 0:
                                current_val = shares * latest_close
                                pnl = current_val - (shares * ep)
                                roi_pct = (latest_close - ep) / ep * 100.0

                                active_v5_positions.append({
                                    "symbol": sym,
                                    "strategy": setup.strategy_name,
                                    "entry_date": e_date,
                                    "entry_price": ep,
                                    "latest_ltp": latest_close,
                                    "current_stop": curr_stop,
                                    "target_1": t1,
                                    "target_2": t2,
                                    "shares": shares,
                                    "invested": round(shares * ep, 2),
                                    "unrealized_pnl": round(pnl, 2),
                                    "roi_pct": round(roi_pct, 2),
                                    "t1_hit": t1_hit
                                })
                            break # Only one open trade per symbol
        except Exception:
            pass

    df_active = pd.DataFrame(active_v5_positions)
    if len(df_active) == 0:
        print("No active positions currently open in V5.")
        return

    # Deduplicate and sort by return % descending
    df_active = df_active.sort_values(by="roi_pct", ascending=False).reset_index(drop=True)

    print("=" * 110)
    print(f"🔥 QUANTSYNC-V5 CURRENT ACTIVE LIVE POSITIONS (AS OF {latest_date})")
    print("=" * 110)
    print(f"{'Symbol':<12} | {'Strategy':<20} | {'Entry Date':<10} | {'Entry':<8} | {'LTP':<8} | {'Stop':<8} | {'Tgt 1':<8} | {'Tgt 2':<8} | {'ROI %':<8} | {'Unrealized ₹':<12}")
    print("-" * 110)

    tot_invested = 0.0
    tot_pnl = 0.0

    for _, r in df_active.iterrows():
        tot_invested += r['invested']
        tot_pnl += r['unrealized_pnl']
        roi_str = f"{r['roi_pct']:+6.2f}%"
        status_icon = "🟢" if r['roi_pct'] >= 0 else "🔴"
        be_tag = " (BE)" if r['current_stop'] >= r['entry_price'] else ""

        print(f"{r['symbol']:<12} | {r['strategy']:<20} | {r['entry_date']:<10} | ₹{r['entry_price']:<7.2f} | ₹{r['latest_ltp']:<7.2f} | ₹{r['current_stop']:<6.2f}{be_tag} | ₹{r['target_1']:<7.2f} | ₹{r['target_2']:<7.2f} | {roi_str} {status_icon} | ₹{r['unrealized_pnl']:+10.2f}")

    print("=" * 110)
    print(f"Total Active Positions: {len(df_active)} | Total Invested: ₹{tot_invested:,.2f} | Total Unrealized PnL: ₹{tot_pnl:+,.2f} ({(tot_pnl/tot_invested*100):+.2f}%)")
    print("=" * 110)

if __name__ == "__main__":
    get_v5_active_portfolio()
