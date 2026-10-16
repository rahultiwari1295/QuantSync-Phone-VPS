"""
banana_engine/core/market_breadth.py — Market Breadth & Sector Rotation Engine
==============================================================================
Calculates institutional market breadth metrics (moving average health, 52W highs/lows)
and generates the Sector Rotation Heatmap / Treemap matching BananaPatterns /market.
"""

from typing import Dict, List, Any
import pandas as pd
import numpy as np


class MarketBreadthEngine:
    """
    Computes market breadth and sector rotation analytics across the universe.
    """

    def __init__(self):
        pass

    def compute_breadth(self, stock_summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        stock_summaries is a list of dicts with:
        symbol, close, sma20, sma50, sma200, daily_pct, turnover_cr, sector, ath_dist
        """
        if not stock_summaries:
            return {
                "pct_above_20dma": 50.0,
                "pct_above_50dma": 50.0,
                "pct_above_200dma": 50.0,
                "advances": 0,
                "declines": 0,
                "new_52w_highs": 0,
                "new_52w_lows": 0,
                "total_tracked": 0,
                "liquid_universe": 0
            }

        n = len(stock_summaries)
        above_20 = sum(1 for s in stock_summaries if s.get('close', 0) >= s.get('sma20', 0))
        above_50 = sum(1 for s in stock_summaries if s.get('close', 0) >= s.get('sma50', 0))
        above_200 = sum(1 for s in stock_summaries if s.get('close', 0) >= s.get('sma200', 0))
        advances = sum(1 for s in stock_summaries if s.get('daily_pct', 0) > 0)
        declines = sum(1 for s in stock_summaries if s.get('daily_pct', 0) < 0)
        new_highs = sum(1 for s in stock_summaries if s.get('ath_dist', 100) <= 2.0)
        new_lows = sum(1 for s in stock_summaries if s.get('atl_dist', 100) <= 2.0)
        liquid_count = sum(1 for s in stock_summaries if s.get('turnover_cr', 0) >= 2.0)

        return {
            "pct_above_20dma": round((above_20 / n) * 100.0, 1),
            "pct_above_50dma": round((above_50 / n) * 100.0, 1),
            "pct_above_200dma": round((above_200 / n) * 100.0, 1),
            "advances": advances,
            "declines": declines,
            "new_52w_highs": new_highs,
            "new_52w_lows": new_lows,
            "total_tracked": n,
            "liquid_universe": liquid_count
        }

    def compute_sector_rotation(self, stock_summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Aggregates performance by sector for the Rotation Treemap.
        """
        if not stock_summaries:
            return []

        df = pd.DataFrame(stock_summaries)
        if 'sector' not in df.columns:
            return []

        grouped = df.groupby('sector')
        sectors_data = []

        for sector, grp in grouped:
            count = len(grp)
            avg_ret = round(float(grp['daily_pct'].mean()), 2)
            avg_rs = round(float(grp['rs_rating'].mean()), 1) if 'rs_rating' in grp.columns else 50.0
            total_turnover = round(float(grp['turnover_cr'].sum()), 1) if 'turnover_cr' in grp.columns else 0.0
            pct_above_50 = round((grp['close'] >= grp['sma50']).mean() * 100.0, 1)

            # Leaders inside this sector
            leaders = grp.sort_values('daily_pct', ascending=False).head(3)['symbol'].tolist()

            sectors_data.append({
                "sector": sector,
                "stock_count": count,
                "avg_daily_pct": avg_ret,
                "avg_rs_rating": avg_rs,
                "turnover_cr": total_turnover,
                "pct_above_50dma": pct_above_50,
                "leaders": leaders
            })

        sectors_data.sort(key=lambda x: x['turnover_cr'], reverse=True)
        return sectors_data
