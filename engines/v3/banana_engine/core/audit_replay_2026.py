"""
banana_engine/core/audit_replay_2026.py — Deep Month-by-Month 2026 Out-of-Sample Replay
=======================================================================================
Performs a rigorous historical audit across all 9 months of 2026 (Jan–Sep 2026)
on strictly held-out out-of-sample data (trained only on pre-2026 data).

Audits:
  1. What the ML model predicted vs what happened in real life.
  2. Month-by-month win rates, realized MFE gains, and drawdown risk.
  3. Verification of known multi-baggers (RAYMOND, BLUSPRING, POLYCAB, TRENT, etc.)
  4. Accuracy and separation between GO FOR IT, SELECTIVE, and SKIP.
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


def run_month_by_month_replay() -> Dict[str, Any]:
    dataset_file = os.path.join(DATA_DIR, "historical_dataset_oos2026.parquet")
    if not os.path.exists(dataset_file):
        raise FileNotFoundError(f"Dataset file {dataset_file} not found. Run train_path_predictors.py first.")

    df_all = pd.read_parquet(dataset_file)
    df_2026 = df_all[df_all['date'] >= '2026-01-01'].copy().sort_values('date').reset_index(drop=True)

    print(f"\n{'='*80}")
    print(f"2026 STRICT OUT-OF-SAMPLE (OOS) MONTH-BY-MONTH AUDIT REPLAY")
    print(f"Total 2026 Formations Evaluated: {len(df_2026)}")
    print(f"{'='*80}\n")

    pi = PathInference()

    # Score every 2026 trade using model
    preds = []
    for _, row in df_2026.iterrows():
        feats = {
            "duration_weeks": row['duration_weeks'],
            "depth_pct": row['depth_pct'],
            "coil_ratio": row['coil_ratio'],
            "dry_ratio": row['dry_ratio'],
            "rs_rating": row['rs_rating'],
            "vol_ratio": row['vol_ratio'],
            "cir": row['cir'],
            "dist_from_52w_high_pct": row['dist_from_52w_high_pct'],
            "price_vs_50ma_pct": row['price_vs_50ma_pct'],
            "entry_price": row['entry_price']
        }
        res = pi.predict_trade_expectancy(feats)
        preds.append({
            "symbol": row['symbol'],
            "date": row['date'],
            "month": str(row['date'])[:7],
            "real_mfe": row['mfe_pct'],
            "real_mae": row['mae_pct'],
            "is_real_winner": 1 if (row['mfe_pct'] >= 10.0 and row['mfe_pct'] >= 1.25 * abs(row['mae_pct'])) or row['mfe_pct'] >= 15.0 else 0,
            "is_real_10pct": 1 if row['mfe_pct'] >= 10.0 else 0,
            "is_real_20pct": 1 if row['mfe_pct'] >= 20.0 else 0,
            "pred_upside": res['expected_upside_pct'],
            "pred_downside": res['expected_downside_pct'],
            "pred_p_up": res['p_up'],
            "pred_asym": res['asymmetry_ratio'],
            "verdict": res['verdict']
        })

    pdf = pd.DataFrame(preds)

    # 1. Overall Cohort Comparison
    print(f"--- 1. OVERALL 2026 OUT-OF-SAMPLE PERFORMANCE BY PREDICTED VERDICT ---")
    overall_summary = {}
    for verdict in ["GO_FOR_IT", "SELECTIVE", "SKIP"]:
        sub = pdf[pdf['verdict'] == verdict]
        count = len(sub)
        if count == 0:
            continue
        avg_mfe = sub['real_mfe'].mean()
        avg_mae = sub['real_mae'].mean()
        win_rate = sub['is_real_winner'].mean() * 100.0
        pct_10 = sub['is_real_10pct'].mean() * 100.0
        pct_20 = sub['is_real_20pct'].mean() * 100.0

        overall_summary[verdict] = {
            "count": count,
            "share_pct": round(count / len(pdf) * 100, 1),
            "avg_realized_mfe": round(avg_mfe, 1),
            "avg_realized_mae": round(avg_mae, 1),
            "win_rate_pct": round(win_rate, 1),
            "hit_10pct_gain_rate": round(pct_10, 1),
            "hit_20pct_super_gain_rate": round(pct_20, 1)
        }
        print(f"  • {verdict:12s} ({count:4d} setups, {count/len(pdf)*100:4.1f}%): "
              f"Win Rate = {win_rate:4.1f}% | Avg Real MFE = +{avg_mfe:4.1f}% | "
              f"Avg Real MAE = {avg_mae:5.1f}% | Hit >=10% = {pct_10:4.1f}% | Hit >=20% = {pct_20:4.1f}%")

    print("\n" + "="*80)
    print("--- 2. MONTH-BY-MONTH REPLAY AUDIT (JANUARY 2026 – SEPTEMBER 2026) ---")
    print("="*80)

    monthly_summary = {}
    for m in sorted(pdf['month'].unique()):
        m_df = pdf[pdf['month'] == m]
        go_df = m_df[m_df['verdict'] == 'GO_FOR_IT']
        sel_df = m_df[m_df['verdict'] == 'SELECTIVE']
        skip_df = m_df[m_df['verdict'] == 'SKIP']

        go_cnt = len(go_df)
        go_mfe = go_df['real_mfe'].mean() if go_cnt > 0 else 0.0
        go_win = go_df['is_real_winner'].mean() * 100.0 if go_cnt > 0 else 0.0

        skip_cnt = len(skip_df)
        skip_mfe = skip_df['real_mfe'].mean() if skip_cnt > 0 else 0.0
        skip_win = skip_df['is_real_winner'].mean() * 100.0 if skip_cnt > 0 else 0.0

        monthly_summary[m] = {
            "total_setups": len(m_df),
            "go_for_it": {"count": go_cnt, "avg_mfe": round(go_mfe, 1), "win_rate": round(go_win, 1)},
            "selective": {"count": len(sel_df), "avg_mfe": round(sel_df['real_mfe'].mean(), 1) if len(sel_df) > 0 else 0},
            "skip": {"count": skip_cnt, "avg_mfe": round(skip_mfe, 1), "win_rate": round(skip_win, 1)}
        }

        print(f"\n📅 Month {m} (Total Setups: {len(m_df)}):")
        print(f"   🟢 GO FOR IT: {go_cnt:3d} trades | Win Rate: {go_win:4.1f}% | Avg Upside: +{go_mfe:4.1f}%")
        print(f"   🟡 SELECTIVE: {len(sel_df):3d} trades | Avg Upside: +{sel_df['real_mfe'].mean() if len(sel_df)>0 else 0:4.1f}%")
        print(f"   🔴 SKIP     : {skip_cnt:3d} trades | Win Rate: {skip_win:4.1f}% | Avg Upside: +{skip_mfe:4.1f}%")

    print("\n" + "="*80)
    print("--- 3. VERIFICATION ON TOP 2026 PROFITABLE TRADES (USER HIGHLIGHTS) ---")
    print("="*80)
    key_symbols = ['RAYMOND', 'BLUSPRING', 'POLYCAB', 'TRENT', 'JINDWORLD', 'SHIVALIK', 'VISHNU', 'CYIENTDLM', 'SALSTEEL']
    key_df = pdf[pdf['symbol'].isin(key_symbols) & (pdf['real_mfe'] >= 15.0)].copy()

    for _, krow in key_df.iterrows():
        status_icon = "✅" if krow['verdict'] in ['GO_FOR_IT', 'SELECTIVE'] else "❌"
        print(f"{status_icon} {krow['symbol']:10s} | Date: {krow['date']} | Real MFE: +{krow['real_mfe']:4.1f}% | Real MAE: {krow['real_mae']:5.1f}% | Predicted: {krow['verdict']}")

    report = {
        "total_2026_samples": len(pdf),
        "overall_summary": overall_summary,
        "monthly_summary": monthly_summary
    }

    report_path = os.path.join(DATA_DIR, "audit_2026_oos_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nAudit Report exported to {report_path}")
    return report


if __name__ == "__main__":
    run_month_by_month_replay()
