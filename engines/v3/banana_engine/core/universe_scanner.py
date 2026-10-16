"""
banana_engine/core/universe_scanner.py — High-Performance Universe Scanner
==========================================================================
Scans the entire stock universe in stock_history.db, executes the 4 pattern analyzers,
evaluates lifecycle stages, and generates the live data feeds for BananaPatterns.
"""

import os
import sys
import json
import sqlite3

# Ensure package root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from typing import Dict, List, Any

from core.pattern_engine import BananaPatternEngine, PatternResult
from core.market_breadth import MarketBreadthEngine


# Standard Sector / Industry mapping for prominent NSE symbols
SECTOR_MAP = {
    "SYRMA": ("Industrial Manufacturing", "Industrial Products"),
    "RAYMOND": ("Industrial Manufacturing", "Textiles & Apparel"),
    "ANTELOPUS": ("Energy & Petrochemicals", "Oil & Gas - E&P"),
    "RELIANCE": ("Energy & Petrochemicals", "Refineries & Retail"),
    "TCS": ("Information Technology", "IT Services"),
    "INFY": ("Information Technology", "IT Services"),
    "HDFCBANK": ("Financial Services", "Private Banks"),
    "ICICIBANK": ("Financial Services", "Private Banks"),
    "SBIN": ("Financial Services", "Public Sector Banks"),
    "BHARTIARTL": ("Telecommunications", "Telecom Services"),
    "TATAMOTORS": ("Automotive", "Automobiles"),
    "M&M": ("Automotive", "Commercial Vehicles"),
    "MARUTI": ("Automotive", "Passenger Cars"),
    "SUNPHARMA": ("Healthcare & Pharma", "Pharmaceuticals"),
    "CIPLA": ("Healthcare & Pharma", "Pharmaceuticals"),
    "TRENT": ("Consumer Goods", "Retail & Apparel"),
    "ZOMATO": ("Consumer Tech", "Food Delivery & Quick Commerce"),
    "BEL": ("Defence & Capital Goods", "Aerospace & Defence"),
    "HAL": ("Defence & Capital Goods", "Aerospace & Defence"),
    "LT": ("Infrastructure", "Civil Engineering & EPC"),
    "ADANIENT": ("Conglomerate", "Trading & Mining"),
    "ADANIPORTS": ("Infrastructure", "Ports & Logistics"),
    "ITC": ("Consumer Goods", "FMCG & Cigarettes"),
    "HINDUNILVR": ("Consumer Goods", "FMCG & Personal Care"),
    "TITAN": ("Consumer Goods", "Jewellery & Watches"),
    "BAJFINANCE": ("Financial Services", "NBFC"),
    "WOCKPHARMA": ("Healthcare & Pharma", "Pharmaceuticals"),
    "CHENNPETRO": ("Energy & Petrochemicals", "Refineries"),
    "POLYCAB": ("Industrial Manufacturing", "Cables & Wires"),
}


def get_stock_sector(sym: str) -> tuple:
    if sym in SECTOR_MAP:
        return SECTOR_MAP[sym]
    # Inferred categories
    s_upper = sym.upper()
    if any(k in s_upper for k in ["BANK", "FIN", "CAP", "HOLD", "SEC"]):
        return ("Financial Services", "Banking & NBFC")
    if any(k in s_upper for k in ["TECH", "SOFT", "INFO", "DIGI"]):
        return ("Information Technology", "IT & Digital")
    if any(k in s_upper for k in ["PHARM", "LAB", "BIO", "HEALTH"]):
        return ("Healthcare & Pharma", "Pharma & Diagnostics")
    if any(k in s_upper for k in ["ENG", "IND", "STEEL", "POWER", "ELECT"]):
        return ("Industrial Manufacturing", "Industrial Products")
    if any(k in s_upper for k in ["AUTO", "MOT", "TYRE", "GEAR"]):
        return ("Automotive", "Automotive Components")
    return ("General Industrials", "Diversified")


class UniverseScanner:
    def __init__(self, db_path: str = "QuantSync_Lines/data/stock_history.db", data_dir: str = "QuantSync_Lines/banana_engine/data"):
        self.db_path = db_path
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.engine = BananaPatternEngine()
        self.breadth_engine = MarketBreadthEngine()

    def run_scan(self, max_stocks: int = 1500) -> Dict[str, Any]:
        """
        Executes scan over all stocks and writes json artifacts.
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT symbol, rawCandles FROM StockHistory")
        rows = cur.fetchall()
        conn.close()

        print(f"[Scanner] Loaded {len(rows)} instruments from {self.db_path}")

        # Compute RS Ratings across the universe first
        rs_scores = []
        valid_stocks = []

        for sym, raw in rows:
            # Skip broad indices
            if sym.startswith("NIFTY") or sym.startswith("BANKNIFTY") or sym.startswith("SENSEX"):
                continue
            try:
                candles = json.loads(raw)
                if len(candles) >= 30:
                    c_now = float(candles[-1]['c'])
                    c_1y = float(candles[-min(len(candles), 250)]['c'])
                    c_6m = float(candles[-min(len(candles), 125)]['c'])
                    c_3m = float(candles[-min(len(candles), 63)]['c'])
                    ret = 2.0 * (c_now / c_3m - 1.0) + (c_now / c_6m - 1.0) + (c_now / c_1y - 1.0)
                    rs_scores.append((sym, ret, candles))
            except Exception:
                pass

        rs_scores.sort(key=lambda x: x[1])
        total_valid = len(rs_scores)
        rs_dict = {}
        for idx, (sym, score, candles) in enumerate(rs_scores):
            rs_val = int(np.clip((idx + 1) / total_valid * 99, 1, 99))
            rs_dict[sym] = rs_val
            valid_stocks.append((sym, candles, rs_val))

        print(f"[Scanner] Evaluated Relative Strength for {total_valid} liquid equities")

        # Scan each stock
        patterns_found: List[Dict[str, Any]] = []
        breakout_records: List[Dict[str, Any]] = []
        stock_summaries: List[Dict[str, Any]] = []

        for sym, candles, rs_rating in valid_stocks[:max_stocks]:
            try:
                df = pd.DataFrame(candles).rename(columns={'t':'date','o':'open','h':'high','l':'low','c':'close','v':'volume'})
                sec, ind = get_stock_sector(sym)
                c_now = float(df['close'].iloc[-1])
                c_prev = float(df['close'].iloc[-2]) if len(df) > 1 else c_now
                d_pct = round(((c_now - c_prev) / c_prev) * 100.0, 2)
                sma20 = float(df['close'].rolling(20).mean().iloc[-1]) if len(df) >= 20 else c_now
                sma50 = float(df['close'].rolling(50).mean().iloc[-1]) if len(df) >= 50 else c_now
                sma200 = float(df['close'].rolling(200).mean().iloc[-1]) if len(df) >= 200 else c_now
                ath = float(df['high'].max())
                atl = float(df['low'].min())
                ath_dist = round(((ath - c_now) / ath) * 100.0, 1)
                turnover = round((c_now * float(df['volume'].iloc[-1])) / 10000000.0, 2)

                stock_summaries.append({
                    "symbol": sym,
                    "close": c_now,
                    "sma20": sma20,
                    "sma50": sma50,
                    "sma200": sma200,
                    "daily_pct": d_pct,
                    "turnover_cr": turnover,
                    "sector": sec,
                    "industry": ind,
                    "ath_dist": ath_dist,
                    "atl_dist": round(((c_now - atl) / atl) * 100.0, 1),
                    "rs_rating": rs_rating
                })

                # Pattern Engine Check
                res: Optional[PatternResult] = self.engine.analyze_stock(
                    df, symbol=sym, company_name=sym, sector=sec, industry=ind, rs_rating=rs_rating
                )

                if res is not None:
                    res_dict = {
                        "symbol": res.symbol,
                        "company_name": res.company_name,
                        "sector": res.sector,
                        "industry": res.industry,
                        "pattern_type": res.pattern_type,
                        "lifecycle_stage": res.lifecycle_stage,
                        "verdict_display": res.verdict_display,
                        "pivot_price": res.pivot_price,
                        "base_floor_price": res.base_floor_price,
                        "base_start_date": res.base_start_date,
                        "base_end_date": res.base_end_date,
                        "duration_weeks": res.duration_weeks,
                        "depth_pct": res.depth_pct,
                        "level": res.level,
                        "is_macro": res.is_macro,
                        "coil_ratio": res.coil_ratio,
                        "dry_ratio": res.dry_ratio,
                        "ud_ratio": res.ud_ratio,
                        "has_breakout": res.has_breakout,
                        "breakout_date": res.breakout_date,
                        "breakout_price": res.breakout_price,
                        "breakout_volume_ratio": res.breakout_volume_ratio,
                        "breakout_close_in_range": res.breakout_close_in_range,
                        "current_price": res.current_price,
                        "current_date": res.current_date,
                        "now_vs_pivot_pct": res.now_vs_pivot_pct,
                        "rs_rating": res.rs_rating,
                        "daily_pct_change": res.daily_pct_change,
                        "turnover_cr": res.turnover_cr,
                        "squats_count": len(res.squats),
                        "failed_pokes_count": len(res.failed_pokes),
                    }
                    patterns_found.append(res_dict)

                    if res.has_breakout and res.breakout_date:
                        bo_cir = res.breakout_close_in_range or 0.85
                        bo_vol = res.breakout_volume_ratio or 2.5
                        # Institutional Quality Filter: CIR >= 0.50, Volume >= 1.40x, Chase <= 5.0%
                        if bo_cir >= 0.50 and bo_vol >= 1.40 and res.now_vs_pivot_pct <= 5.0:
                            breakout_records.append({
                                "sym": res.symbol,
                                "name": res.company_name,
                                "sector": res.sector,
                                "d": res.breakout_date,
                                "px": res.current_price,
                                "pivot": res.pivot_price,
                                "chg": res.now_vs_pivot_pct,
                                "volX": bo_vol,
                                "cir": bo_cir,
                                "level": res.level,
                                "type": "macro_breakout" if res.is_macro else "breakout",
                                "rs": res.rs_rating,
                                "liq": res.turnover_cr,
                                "duration_weeks": res.duration_weeks
                            })
            except Exception as e:
                pass

        # Compute breadth and sector rotation
        breadth = self.breadth_engine.compute_breadth(stock_summaries)
        sectors = self.breadth_engine.compute_sector_rotation(stock_summaries)

        # Sort breakouts by most recent and volume
        breakout_records.sort(key=lambda x: (x['d'] or '', x['volX'] or 0), reverse=True)

        # Write output files
        with open(os.path.join(self.data_dir, "market_breadth.json"), "w") as f:
            json.dump(breadth, f, indent=2)

        with open(os.path.join(self.data_dir, "sector_rotation.json"), "w") as f:
            json.dump(sectors, f, indent=2)

        with open(os.path.join(self.data_dir, "breakouts.json"), "w") as f:
            json.dump(breakout_records, f, indent=2)

        with open(os.path.join(self.data_dir, "live_scans.json"), "w") as f:
            json.dump(patterns_found, f, indent=2)

        summary = {
            "total_stocks_scanned": len(stock_summaries),
            "patterns_detected": len(patterns_found),
            "breakouts_active": len(breakout_records),
            "market_breadth": breadth
        }

        with open(os.path.join(self.data_dir, "universe_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

        print(f"[Scanner] Complete! Patterns detected: {len(patterns_found)}, Breakouts: {len(breakout_records)}")
        return summary


if __name__ == "__main__":
    scanner = UniverseScanner()
    scanner.run_scan()
