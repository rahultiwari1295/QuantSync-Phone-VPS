"""
generate_2026_monthly_report.py
===============================
Extracts all QuantSync-V4 trades executed across 2026 (Jan 1, 2026 -> Sep 22, 2026)
and groups them month-by-month showing:
- Month Name
- Total Trades
- Win Rate %
- Gross Profit / Loss
- Profit Factor
- Net PnL %
- Notable Winning & Losing tickers
"""

import os
import sys
import json
import sqlite3
import pandas as pd
import numpy as np
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HIST_DB = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")

from engines.v4.dna_bridge import extract_dna_metrics
from engines.v4.gates import evaluate_v4_gates
from test_v4_oos import simulate_v4_scaleout

def run_2026_report():
    conn = sqlite3.connect(HIST_DB)
    cur = conn.cursor()
    # Institutional trading basket (1,269 liquid equities)
    cur.execute("SELECT symbol, rawCandles FROM StockHistory WHERE isV3Eligible = 1 AND candleCount >= 100;")
    rows = cur.fetchall()
    conn.close()

    print(f"Loaded {len(rows)} institutional algorithm-eligible stocks (isV3Eligible = 1).")

    all_2026_trades = []

    for sym, raw_c in rows:
        try:
            candles = json.loads(raw_c)
            df = pd.DataFrame(candles).rename(columns={'t': 'Date', 'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
            df['Date'] = df['Date'].astype(str).str[:10]
            df = df.sort_values('Date').reset_index(drop=True)

            indices_2026 = df[(df['Date'] >= '2026-01-01') & (df['Date'] <= '2026-09-22')].index

            for idx in indices_2026:
                if idx < 50 or idx >= len(df) - 5:
                    continue

                sub_df = df.iloc[:idx+1].copy()
                curr_bar = sub_df.iloc[-1]
                e_date = curr_bar['Date']
                ep = curr_bar['Close']
                high_val = curr_bar['High']
                low_val = curr_bar['Low']
                vol = curr_bar['Volume']

                vol_sma20 = sub_df['Volume'].iloc[-21:-1].mean()
                vol_ratio = vol / (vol_sma20 + 1e-9)
                spread = max(1e-4, high_val - low_val)
                cir = (ep - low_val) / spread
                upper_wick = (high_val - max(curr_bar['Open'], ep)) / spread
                ath_dist = (ep - sub_df['High'].max()) / sub_df['High'].max()

                # V4 Base Criteria
                is_apex_compression = (
                    cir >= 0.90 and 
                    upper_wick <= 0.05 and 
                    vol_ratio >= 2.50 and
                    ath_dist >= -0.35 and
                    ep > sub_df['Close'].ewm(span=50).mean().iloc[-1]
                )

                if is_apex_compression:
                    dna = extract_dna_metrics(sub_df)
                    if dna["stage"] in ["STAGE_2_MARKUP", "STAGE_1_ACCUMULATION"] and dna["rsi_divergence"] != "Bearish Regular Divergence":
                        eval_res = evaluate_v4_gates(
                            candidate={
                                "symbol": sym,
                                "entry_price": ep,
                                "stop_price": max(curr_bar['Low'], ep * 0.955),
                                "cir": cir,
                                "upper_wick": upper_wick,
                                "fib_1_272": ep * 1.12,
                                "fib_1_618": ep * 1.25,
                            },
                            dna_metrics=dna,
                            portfolio_state={"current_drawdown_pct": 0.0},
                            sector_counts={}
                        )

                        if eval_res.passed:
                            forward_bars = df.iloc[idx+1:min(len(df), idx+25)]
                            if len(forward_bars) >= 3:
                                trade_res = simulate_v4_scaleout(
                                    ep,
                                    max(curr_bar['Low'], ep * 0.955),
                                    ep * 1.12,
                                    ep * 1.25,
                                    forward_bars,
                                    sizing_mult=eval_res.sizing_multiplier
                                )
                                all_2026_trades.append({
                                    "symbol": sym,
                                    "entry_date": e_date,
                                    "pnl": trade_res["pnl"],
                                    "mult": trade_res["mult"],
                                    "month": e_date[:7] # YYYY-MM
                                })
        except Exception:
            pass

    # Monthly aggregation
    df_trades = pd.DataFrame(all_2026_trades)
    if len(df_trades) == 0:
        print("No trades found in 2026.")
        return

    print("=" * 86)
    print("📅 QUANTSYNC-V4 2026 MONTH-BY-MONTH REALIZED PERFORMANCE")
    print("=" * 86)
    print(f"{'Month':<10} | {'Trades':<7} | {'Wins':<5} | {'Loss':<5} | {'Win Rate':<9} | {'Profit Factor':<14} | {'Net Return %':<12} | {'Top Winners / Losers'}")
    print("-" * 86)

    months = sorted(df_trades['month'].unique())
    cum_return = 0.0

    for m in months:
        m_df = df_trades[df_trades['month'] == m]
        tot = len(m_df)
        wins = m_df[m_df['pnl'] > 0]
        losses = m_df[m_df['pnl'] <= 0]
        wr = len(wins) / tot * 100 if tot > 0 else 0.0

        gp = sum(wins['pnl'] * wins['mult']) if len(wins) > 0 else 0.0
        gl = abs(sum(losses['pnl'] * losses['mult'])) if len(losses) > 0 else 0.0
        pf = (gp / gl) if gl > 0 else (99.0 if gp > 0 else 0.0)
        net_ret = sum(m_df['pnl'] * m_df['mult']) / tot
        cum_return += net_ret

        # Sample tickers
        top_w = wins.sort_values(by='pnl', ascending=False)['symbol'].head(2).tolist()
        top_l = losses.sort_values(by='pnl', ascending=True)['symbol'].head(1).tolist()
        notable = f"+{', '.join(top_w)}" if top_w else ""
        if top_l:
            notable += f" | -{top_l[0]}"

        print(f"{m:<10} | {tot:<7} | {len(wins):<5} | {len(losses):<5} | {wr:6.1f}%   | {pf:12.2f}   | {net_ret:+10.2f}%   | {notable}")

    print("=" * 86)
    total_trades = len(df_trades)
    tot_wins = len(df_trades[df_trades['pnl'] > 0])
    overall_wr = tot_wins / total_trades * 100
    overall_gp = sum(df_trades[df_trades['pnl'] > 0]['pnl'] * df_trades[df_trades['pnl'] > 0]['mult'])
    overall_gl = abs(sum(df_trades[df_trades['pnl'] <= 0]['pnl'] * df_trades[df_trades['pnl'] <= 0]['mult']))
    overall_pf = overall_gp / overall_gl if overall_gl > 0 else 99.0
    overall_avg = sum(df_trades['pnl'] * df_trades['mult']) / total_trades

    print(f"{'2026 YTD':<10} | {total_trades:<7} | {tot_wins:<5} | {total_trades - tot_wins:<5} | {overall_wr:6.1f}%   | {overall_pf:12.2f}   | {overall_avg:+10.2f}%   | Cumulative: {cum_return:+.1f}%")
    print("=" * 86)

if __name__ == "__main__":
    run_2026_report()
