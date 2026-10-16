"""
core/monthly_performance_audit.py — Rigorous Month-by-Month Performance Audit (2025 & 2026)
============================================================================================
Calculates for every month in 2025 and 2026:
  - Total Trades
  - Win Count & Loss Count
  - Win Rate %
  - Average Win % & Average Loss %
  - Profit Factor (PF = Gross Profits / Gross Losses)
  - Net Expectancy & Total Realized Return %
Evaluates both:
  1. HIGH CONVICTION (GO FOR IT) Setups
  2. ALL ACTIONABLE (GO FOR IT + SELECTIVE) Setups
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from typing import Dict, List, Any

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR
from core.path_models import PathInference


def calculate_monthly_audit():
    """
    Computes month-by-month and yearly performance matrices exclusively from
    bar-by-bar verified closed trades of the 4 Champion Strategies
    (data/verified_closed_trades_2026.json).
    """
    closed_trades_file = os.path.join(DATA_DIR, "verified_closed_trades_2026.json")

    records = []

    # Ground-Truth Closed Trades exclusively from the 4 Champion Strategies
    if os.path.exists(closed_trades_file):
        with open(closed_trades_file, "r") as f:
            cdata = json.load(f)
        for t in cdata.get("closed_trades", []):
            exit_d = t.get("exit_date") or t.get("entry_date") or ""
            year = exit_d[:4]
            if year not in ["2025", "2026"]:
                continue
            month = exit_d[:7]
            pnl = float(t.get("realized_pnl_pct", 0.0))
            is_win = 1 if (t.get("is_win") or pnl > 0) else 0
            records.append({
                "symbol": t.get("symbol", ""),
                "date": exit_d,
                "year": year,
                "month": month,
                "verdict": t.get("verdict", "GO_FOR_IT"),
                "gate_score": float(t.get("gate_score", 60.0)),
                "gate_tier": t.get("gate_tier", "APEX_ALPHA"),
                "strategy": t.get("pattern", "HTF"),
                "is_win": is_win,
                "realized_pnl": round(pnl, 2)
            })

    rdf = pd.DataFrame(records)
    if rdf.empty:
        return {}

    results = {}

    cohorts = [
        ("ALL", "⭐ All Champion Strategies"),
        ("HTF", "🚀 High-Tight Flag (HTF)"),
        ("BASE", "🏛️ Ultra Base"),
        ("52W_HIGH", "📈 52-Week High Breakout"),
        ("DARVAS", "📦 Darvas Box"),
        ("APEX_ALPHA", "🔥 Apex Alpha Tier (Legacy)"),
        ("HIGH_ALPHA", "⚡ High Alpha Tier (Legacy)"),
        ("GO_FOR_IT", "🟢 High Conviction Setups (Legacy)")
    ]

    for cohort_key, cohort_title in cohorts:
        if cohort_key == "ALL":
            c_df = rdf.copy()
        elif cohort_key in ["HTF", "BASE", "52W_HIGH", "DARVAS"]:
            c_df = rdf[rdf["strategy"] == cohort_key].copy()
        elif cohort_key == "APEX_ALPHA":
            c_df = rdf[(rdf["gate_score"] >= 65.0)].copy()
        elif cohort_key == "HIGH_ALPHA":
            c_df = rdf[(rdf["gate_score"] >= 58.0)].copy()
        elif cohort_key == "GO_FOR_IT":
            c_df = rdf[(rdf["gate_score"] >= 55.0)].copy()
        else:
            c_df = rdf.copy()

        monthly_rows = []
        months = sorted(c_df["month"].unique())

        for m in months:
            m_data = c_df[c_df["month"] == m]
            trades = len(m_data)
            if trades == 0:
                continue

            wins = int((m_data["is_win"] == 1).sum())
            losses = int((m_data["is_win"] == 0).sum())
            win_rate = round((wins / trades) * 100.0, 1)

            win_pnls = m_data[m_data["is_win"] == 1]["realized_pnl"]
            loss_pnls = m_data[m_data["is_win"] == 0]["realized_pnl"]

            avg_win = round(float(win_pnls.mean()), 1) if len(win_pnls) > 0 else 0.0
            avg_loss = round(float(loss_pnls.mean()), 1) if len(loss_pnls) > 0 else 0.0

            gross_gain = float(win_pnls.sum()) if len(win_pnls) > 0 else 0.0
            gross_loss = abs(float(loss_pnls.sum())) if len(loss_pnls) > 0 else 0.001
            pf = round(gross_gain / gross_loss, 2) if gross_loss > 0 else 99.0

            net_pnl = round(float(m_data["realized_pnl"].sum()), 1)
            avg_trade = round(float(m_data["realized_pnl"].mean()), 2)

            monthly_rows.append({
                "month": m,
                "year": m[:4],
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": win_rate,
                "avg_win_pct": avg_win,
                "avg_loss_pct": avg_loss,
                "profit_factor": pf,
                "net_pnl_pct": net_pnl,
                "avg_trade_pnl": avg_trade
            })

        # Year aggregates
        m_df = pd.DataFrame(monthly_rows)
        year_summaries = {}
        for y in ["2025", "2026"]:
            y_sub = m_df[m_df["year"] == y]
            if len(y_sub) > 0:
                y_trades = int(y_sub["trades"].sum())
                y_wins = int(y_sub["wins"].sum())
                y_losses = int(y_sub["losses"].sum())
                y_wr = round(y_wins / max(y_trades, 1) * 100.0, 1)

                all_y_trades = c_df[c_df["year"] == y]
                all_wins = all_y_trades[all_y_trades["is_win"] == 1]["realized_pnl"]
                all_losses = all_y_trades[all_y_trades["is_win"] == 0]["realized_pnl"]

                y_avg_win = round(float(all_wins.mean()), 1) if len(all_wins) > 0 else 0.0
                y_avg_loss = round(float(all_losses.mean()), 1) if len(all_losses) > 0 else 0.0

                y_gross_win = float(all_wins.sum()) if len(all_wins) > 0 else 0.0
                y_gross_loss = abs(float(all_losses.sum())) if len(all_losses) > 0 else 0.001
                y_pf = round(y_gross_win / y_gross_loss, 2)

                year_summaries[y] = {
                    "trades": y_trades,
                    "wins": y_wins,
                    "losses": y_losses,
                    "win_rate_pct": y_wr,
                    "avg_win_pct": y_avg_win,
                    "avg_loss_pct": y_avg_loss,
                    "profit_factor": y_pf
                }

        results[cohort_key] = {
            "title": cohort_title,
            "monthly": monthly_rows,
            "yearly": year_summaries
        }

    output_path = os.path.join(DATA_DIR, "monthly_performance_2025_2026.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    return results


if __name__ == "__main__":
    res = calculate_monthly_audit()
    
    for key, data in res.items():
        print(f"\n{'='*95}")
        print(f"📊 {data['title'].upper()}")
        print(f"{'='*95}")
        header = f"{'Month':<8} | {'Trades':<7} | {'Wins':<6} | {'Losses':<6} | {'Win Rate':<9} | {'Avg Win %':<10} | {'Avg Loss %':<11} | {'Profit Factor (PF)':<18}"
        print(header)
        print("-" * 95)
        for row in data["monthly"]:
            print(f"{row['month']:<8} | {row['trades']:<7} | {row['wins']:<6} | {row['losses']:<6} | {row['win_rate_pct']:>7.1f}% | {row['avg_win_pct']:>8.1f}% | {row['avg_loss_pct']:>9.1f}% | {row['profit_factor']:>14.2f}")
        
        print("-" * 95)
        for y, ysum in data["yearly"].items():
            print(f"⭐ FULL YEAR {y}: {ysum['trades']} Trades | {ysum['wins']} Wins | {ysum['losses']} Losses | Win Rate: {ysum['win_rate_pct']}% | Avg Win: +{ysum['avg_win_pct']}% | Avg Loss: {ysum['avg_loss_pct']}% | PF: {ysum['profit_factor']}")
