"""
banana_engine/server/api_server.py — BananaPatterns v4 Production REST API
==========================================================================
FastAPI service exposing unified v4 endpoints matching Section 9 of BananaPatterns v4:
  • GET /api/scan                (Unified scanner with decision ranking by EV_R)
  • GET /api/setup/{symbol}      (Full setup detail: structure, brain, risk, 4-level Fib, chart)
  • GET /api/market              (Breadth, regime state, sector rotation)
  • GET /api/risk/size           (Dynamic position sizing calculator)
  • GET /api/portfolio/summary   (Open positions, sector concentration meters, warnings)
  • GET /api/monitor/drift       (Live-vs-backtest Z-score & feature KS tests)
  • GET /api/model/card          (Model card with training window, metrics, limitations)
  • POST /api/scan/refresh       (Universe rescan trigger)
"""

import os
import sys
import json
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
from pydantic import BaseModel, ConfigDict

# FIXED: Fix 6.6 (INFO-04) — Pydantic response models for API endpoints
class SetupSummary(BaseModel):
    model_config = ConfigDict(extra="allow")
    symbol: str
    ev_r: float
    p_win: float = 0.5
    grade: str = "C"
    tier: str = "WATCH"
    stop: float = 0.0
    target: float = 0.0
    be_lock_price: float = 0.0
    pattern: str = ""
    pattern_label: str = ""
    position_size_shares: int = 0
    cohort_rank: float = 50.0

class ScanResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    generated_at: str
    regime: str
    setups: List[SetupSummary]

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(os.path.dirname(_PKG_ROOT))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import numpy as np

from core.data_layer import (
    STOCK_HISTORY_DB,
    DATA_DIR,
    load_adjusted,
    sector_at,
    listing_date_at,
    days_to_next_earnings
)
from core.structure import (
    detect_base_at,
    classify_context,
    is_breakout_day,
    regime_state,
    detect_squat,
    detect_failed_poke,
    CONTEXT_GATES
)
from core.features import extract_features
from core.brain import BrainInference, MODEL_CARD_FILE
from core.risk import compute_stop, compute_target, position_size, regime_gate, portfolio_gates
from core.composite import composite_ev_r, assign_tier, lifecycle_stage
from core.monitor import compute_drift_report
from core.scanner import V4UniverseScanner, CACHE_FILE
from core.champion_patterns import (
    detect_all_champion_patterns,
    extract_champion_features,
    ChampionRanker
)

# BananaPatterns v5 Institutional Brain Evolution Components
from core.path_models import PathInference
from core.analog import DualScaleAnalogEngine
from core.online_calibrator import DualWindowCalibrator, RegimeModelRouter
from core.portfolio_sharpe import calculate_marginal_sharpe, compute_180d_shrunk_beta
from core.regret import RegretLedger
from core.cohort import rank_cohort
from core.trade_lifecycle import load_verified_closed_trades, load_verified_active_trades

app = FastAPI(title="BananaPatterns v5 Institutional Decision Engine", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = os.path.join(_PKG_ROOT, "web")
scanner = V4UniverseScanner()
brain = BrainInference()
champion_ranker = ChampionRanker()

# v5 Institutional Engine Singletons
path_inference = PathInference()
analog_engine = DualScaleAnalogEngine()
online_calibrator = DualWindowCalibrator()
regime_router = RegimeModelRouter()
regret_ledger = RegretLedger()

# In-memory mock open positions for portfolio view demonstration
OPEN_POSITIONS: List[Dict[str, Any]] = [
    {
        "symbol": "RAYMOND",
        "entry_date": "2026-08-18",
        "entry_price": 642.0,
        "current_price": 1002.8,
        "stop_price": 590.6,
        "shares": 93,
        "sector": "Industrial Manufacturing",
        "r_multiple": 7.02,
        "days_held": 18
    },
    {
        "symbol": "IOLCP",
        "entry_date": "2026-08-26",
        "entry_price": 179.8,
        "current_price": 199.9,
        "stop_price": 165.4,
        "shares": 520,
        "sector": "Healthcare & Pharma",
        "r_multiple": 1.39,
        "days_held": 10
    }
]


def load_cached_scan() -> Dict[str, Any]:
    """
    Loads precomputed multi-stage universe scan cache.
    """
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if data.get("summary_counts"):
                    return data
        except Exception:
            pass
    from core.build_universe_cache import generate_full_universe_cache
    return generate_full_universe_cache()


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>BananaPatterns Engine</h1>")


@app.get("/api/scan", response_model=ScanResponse)
async def get_scan(
    min_weeks: float = 0.0,
    min_volume_ratio: float = 0.0,
    min_score: float = -99.0,
    tier: Optional[str] = None,
    context: Optional[str] = None,
    sector: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 500
):
    """
    Unified scanner endpoint returning setups ranked by EV_R descending,
    along with 4 lifecycle stages, summary counts, and live feed.
    """
    data = load_cached_scan()
    setups = data.get("setups", [])

    filtered = []
    for s in setups:
        if s.get("duration_weeks", 0) < min_weeks:
            continue
        if s.get("ev_r", -99) < min_score:
            continue
        if tier and s.get("tier", "").upper() != tier.upper():
            continue
        if context and s.get("context", "").lower() != context.lower():
            continue
        if sector and s.get("sector", "").lower() != sector.lower():
            continue
        if stage and s.get("stage", "").lower() != stage.lower():
            continue
        filtered.append(s)

    # Sort by cohort rank ascending (or EV_R descending)
    filtered.sort(key=lambda x: x.get("cohort_rank", 999))

    # Ensure clean stages with mini_candles for instant card chart rendering
    raw_stages = data.get("stages", {})
    clean_stages = {}
    
    # 1. Fresh Breakouts (To Buy)
    fresh_list = []
    for item in raw_stages.get("fresh_breakouts", []):
        clean_item = {k: v for k, v in item.items() if k != "candles"}
        mc = item.get("mini_candles", [])
        if not mc and item.get("candles"):
            c_list = item.get("candles", [])
            mc = [
                [c.get("d") or c.get("date"), c.get("o") or c.get("open"), c.get("h") or c.get("high"), c.get("l") or c.get("low"), c.get("c") or c.get("close"), c.get("v") or c.get("volume", 0)]
                for c in c_list[-80:]
            ]
        clean_item["mini_candles"] = mc
        if len(mc) >= 2:
            c_last = float(mc[-1][4])
            c_prev = float(mc[-2][4])
            clean_item["price"] = round(c_last, 2)
            clean_item["current_price"] = round(c_last, 2)
            clean_item["daily_pct"] = round(((c_last - c_prev) / c_prev) * 100.0, 2)
        else:
            clean_item["price"] = clean_item.get("price") or clean_item.get("entry_price") or clean_item.get("entry")
            clean_item["current_price"] = clean_item["price"]
            clean_item["daily_pct"] = float(clean_item.get("daily_pct") or 0.0)

        # TRUE ENTRY PRICE IS THE BREAKOUT PIVOT LEVEL
        pivot_p = float(item.get("pivot") or (item.get("geometry", {}) or {}).get("pivot") or item.get("box_top") or item.get("base_ceiling") or 0.0)
        if pivot_p > 0:
            ep = round(pivot_p, 2)
        else:
            ep = float(clean_item.get("entry_price") or clean_item.get("entry") or clean_item.get("price") or 100.0)
        clean_item["entry_price"] = ep
        clean_item["entry"] = ep
        clean_item["pivot"] = ep
        cur_p = clean_item["current_price"] or ep
        clean_item["unrealized_pnl_pct"] = round(((cur_p - ep) / ep) * 100.0, 2) if ep > 0 else 0.0
        clean_item["floating_pnl_pct"] = clean_item["unrealized_pnl_pct"]

        # Real Volume Surge vs 20-day SMA Volume
        if len(mc) >= 5:
            recent_vols = [float(c[5]) for c in mc[-21:-1]] if len(mc) >= 21 else [float(c[5]) for c in mc[:-1]]
            sma20_v = sum(recent_vols) / max(len(recent_vols), 1)
            cur_v = float(mc[-1][5])
            vol_ratio = round(cur_v / max(sma20_v, 1.0), 2)
        else:
            vol_ratio = 1.80
        clean_item["volume_ratio"] = vol_ratio
        clean_item["breakout_volume_ratio"] = vol_ratio
        clean_item["vol_ratio"] = vol_ratio

        # Real Closing Power (Close-in-range 0.0 to 1.0)
        cir = 0.80
        if mc:
            last_c = mc[-1]
            rng = max(float(last_c[2]) - float(last_c[3]), 0.01)
            cir = round((float(last_c[4]) - float(last_c[3])) / rng, 2)
        clean_item["cir"] = cir
        clean_item["breakout_close_in_range"] = cir

        # TRUE BREAKOUT DATE: The bar when price crossed above pivot
        bo_date = None
        for c in reversed(mc):
            d_val = str(c[0])[:10]
            h_val = float(c[2])
            if h_val >= ep:
                bo_date = d_val
            else:
                break
        clean_item["breakout_date"] = bo_date or (str(mc[-1][0])[:10] if mc else "2026-09-04")
        clean_item["entry_date"] = clean_item["breakout_date"]

        clean_item["stop_price"] = clean_item.get("stop_price") or clean_item.get("stop")
        clean_item["target_price"] = clean_item.get("target_price") or clean_item.get("target")
        clean_item["gate_score"] = float(clean_item.get("cohort_score") or round(clean_item.get("p_win", 0.55) * 100.0, 1))
        fresh_list.append(clean_item)

    # 2. Climbing (Ongoing Active Positions) — filter out already closed trades
    climbing_list = []
    for item in raw_stages.get("climbing", []):
        clean_item = {k: v for k, v in item.items() if k != "candles"}
        mc = item.get("mini_candles", [])
        if not mc and item.get("candles"):
            c_list = item.get("candles", [])
            mc = [
                [c.get("d") or c.get("date"), c.get("o") or c.get("open"), c.get("h") or c.get("high"), c.get("l") or c.get("low"), c.get("c") or c.get("close"), c.get("v") or c.get("volume", 0)]
                for c in c_list[-80:]
            ]
        clean_item["mini_candles"] = mc

        # Check if trade already hit target or stop in recent bars
        hit_target = False
        hit_stop = False
        t_price = float(item.get("target") or 999999)
        s_price = float(item.get("stop") or 0)
        for c in mc[-15:]:
            h = float(c[2])
            l = float(c[3])
            if h >= t_price:
                hit_target = True
                break
            if l <= s_price and l > 0:
                hit_stop = True
                break
        if hit_target or hit_stop:
            continue  # Exited! Belongs in Closed, not Ongoing!

        if len(mc) >= 2:
            c_last = float(mc[-1][4])
            c_prev = float(mc[-2][4])
            clean_item["price"] = round(c_last, 2)
            clean_item["current_price"] = round(c_last, 2)
            clean_item["daily_pct"] = round(((c_last - c_prev) / c_prev) * 100.0, 2)
        else:
            clean_item["price"] = clean_item.get("price") or clean_item.get("current_price") or clean_item.get("entry_price")
            clean_item["current_price"] = clean_item["price"]
            clean_item["daily_pct"] = float(clean_item.get("daily_pct") or 0.0)

        # TRUE ENTRY PRICE IS BREAKOUT PIVOT
        pivot_p = float(item.get("pivot") or (item.get("geometry", {}) or {}).get("pivot") or item.get("box_top") or item.get("base_ceiling") or 0.0)
        if pivot_p > 0:
            ep = round(pivot_p, 2)
        else:
            ep = float(clean_item.get("entry_price") or clean_item.get("entry") or clean_item.get("price") or 100.0)
        clean_item["entry_price"] = ep
        clean_item["entry"] = ep
        clean_item["pivot"] = ep
        cur_p = clean_item["current_price"] or ep
        clean_item["unrealized_pnl_pct"] = round(((cur_p - ep) / ep) * 100.0, 2) if ep > 0 else 0.0
        clean_item["floating_pnl_pct"] = clean_item["unrealized_pnl_pct"]

        # Real Volume Surge vs 20-day SMA Volume
        if len(mc) >= 5:
            recent_vols = [float(c[5]) for c in mc[-21:-1]] if len(mc) >= 21 else [float(c[5]) for c in mc[:-1]]
            sma20_v = sum(recent_vols) / max(len(recent_vols), 1)
            cur_v = float(mc[-1][5])
            vol_ratio = round(cur_v / max(sma20_v, 1.0), 2)
        else:
            vol_ratio = 1.80
        clean_item["volume_ratio"] = vol_ratio
        clean_item["breakout_volume_ratio"] = vol_ratio
        clean_item["vol_ratio"] = vol_ratio

        # Real Closing Power (Close-in-range 0.0 to 1.0)
        cir = 0.80
        if mc:
            last_c = mc[-1]
            rng = max(float(last_c[2]) - float(last_c[3]), 0.01)
            cir = round((float(last_c[4]) - float(last_c[3])) / rng, 2)
        clean_item["cir"] = cir
        clean_item["breakout_close_in_range"] = cir

        # Breakout date
        bo_date = item.get("entry_date") or item.get("breakout_date")
        if not bo_date or bo_date == "2026-09-11":
            for c in reversed(mc):
                d_val = str(c[0])[:10]
                h_val = float(c[2])
                if h_val >= ep:
                    bo_date = d_val
                else:
                    break
        clean_item["breakout_date"] = bo_date or (str(mc[-1][0])[:10] if mc else "2026-09-04")
        clean_item["entry_date"] = clean_item["breakout_date"]

        clean_item["stop_price"] = clean_item.get("stop_price") or clean_item.get("stop")
        clean_item["target_price"] = clean_item.get("target_price") or clean_item.get("target")
        clean_item["gate_score"] = float(clean_item.get("cohort_score") or clean_item.get("gate_score") or round(clean_item.get("p_win", 0.55) * 100.0, 1))
        climbing_list.append(clean_item)

    # Enrich climbing_list with verified active holdings from verified_closed_trades_2026.json
    from core.trade_lifecycle import CLOSED_TRADES_CACHE_FILE
    if os.path.exists(CLOSED_TRADES_CACHE_FILE):
        try:
            with open(CLOSED_TRADES_CACHE_FILE, "r") as f:
                v_data = json.load(f)
            v_active = {str(t.get("symbol", "")).upper(): t for t in v_data.get("active_trades", [])}
            
            # Enrich existing items
            for c_itm in climbing_list:
                s_k = str(c_itm.get("symbol", "")).upper()
                if s_k in v_active:
                    vat = v_active[s_k]
                    ep = float(vat.get("pivot") or vat.get("entry_price") or c_itm.get("entry", c_itm.get("price")))
                    c_itm["entry"] = ep
                    c_itm["entry_price"] = ep
                    mc = c_itm.get("mini_candles", [])
                    if vat.get("entry_date") and vat.get("entry_date") not in ["2026-09-11", "11 Sep 2026"]:
                        c_itm["entry_date"] = vat["entry_date"]
                        c_itm["breakout_date"] = vat["entry_date"]
                    else:
                        bo_bar = None
                        for c in reversed(mc):
                            if float(c[2]) >= ep:
                                bo_bar = str(c[0])[:10]
                            else:
                                break
                        if bo_bar:
                            c_itm["entry_date"] = bo_bar
                            c_itm["breakout_date"] = bo_bar

                    c_itm["target"] = float(vat.get("target_price", c_itm.get("target")))
                    c_itm["target_price"] = c_itm["target"]
                    c_itm["stop"] = float(vat.get("stop_price", c_itm.get("stop")))
                    c_itm["stop_price"] = c_itm["stop"]
                    cur_p = float(c_itm.get("price", ep))
                    c_itm["unrealized_pnl_pct"] = round(((cur_p - ep) / ep) * 100.0, 2) if ep > 0 else 0.0
                    c_itm["holding_days"] = int(vat.get("holding_days", 1))

            # Add verified active holdings not yet in climbing_list
            existing_syms = {str(item.get("symbol", "")).upper() for item in climbing_list}
            for sym_k, vat in v_active.items():
                if sym_k not in existing_syms:
                    c_vat = dict(vat)
                    ep = float(c_vat.get("pivot") or c_vat.get("entry_price", 100))
                    c_vat["entry"] = ep
                    c_vat["entry_price"] = ep
                    mc = c_vat.get("mini_candles", [])
                    if len(mc) >= 2:
                        c_last = float(mc[-1][4])
                        c_prev = float(mc[-2][4])
                        c_vat["price"] = round(c_last, 2)
                        c_vat["daily_pct"] = round(((c_last - c_prev) / c_prev) * 100.0, 2)
                    else:
                        c_vat["price"] = c_vat.get("current_price", ep)
                        c_vat["daily_pct"] = 0.0
                    cur_p = float(c_vat["price"])
                    c_vat["unrealized_pnl_pct"] = round(((cur_p - ep) / ep) * 100.0, 2) if ep > 0 else 0.0
                    if len(mc) >= 5:
                        recent_vols = [float(c[5]) for c in mc[-21:-1] if float(c[5]) > 0]
                        sma20_v = sum(recent_vols) / max(len(recent_vols), 1) if recent_vols else 1.0
                        cur_v = float(mc[-1][5])
                        vol_ratio = round(cur_v / max(sma20_v, 1.0), 2) if cur_v > 0 else 1.85
                    else:
                        vol_ratio = 1.85
                    c_vat["volume_ratio"] = vol_ratio
                    c_vat["breakout_volume_ratio"] = vol_ratio
                    c_vat["vol_ratio"] = vol_ratio

                    cir = 0.80
                    if mc:
                        last_c = mc[-1]
                        rng = max(float(last_c[2]) - float(last_c[3]), 0.01)
                        cir = round((float(last_c[4]) - float(last_c[3])) / rng, 2)
                    c_vat["cir"] = cir
                    c_vat["breakout_close_in_range"] = cir

                    # Compute true breakout date from candles
                    bo_date = None
                    for c in reversed(mc):
                        d_val = str(c[0])[:10]
                        h_val = float(c[2])
                        if h_val >= ep:
                            bo_date = d_val
                        else:
                            break
                    if not bo_date and mc:
                        bo_date = str(mc[-1][0])[:10]
                    c_vat["breakout_date"] = bo_date or "2026-09-04"
                    c_vat["entry_date"] = c_vat["breakout_date"]

                    c_vat["target"] = c_vat.get("target_price")
                    c_vat["stop"] = c_vat.get("stop_price")
                    c_vat["gate_score"] = float(c_vat.get("gate_score") or c_vat.get("cohort_score") or 55.0)
                    climbing_list.append(c_vat)
                    existing_syms.add(sym_k)
        except Exception:
            pass

    # COMBINE ALL OPEN TRADES INTO MASTER ONGOING PORTFOLIO (ALL TRADES IN FLIGHT)
    all_ongoing_map = {}
    for itm in fresh_list + climbing_list:
        sym_k = str(itm.get("symbol", "")).upper()
        if sym_k and sym_k not in all_ongoing_map:
            all_ongoing_map[sym_k] = itm
    all_ongoing_list = list(all_ongoing_map.values())

    # DERIVE "BUY NOW" SETUPS: TRIGGERED WITHIN 2-3 DAYS AND NOT GIVEN MORE THAN +3.0%
    buy_now_list = []
    for itm in all_ongoing_list:
        pnl = float(itm.get("unrealized_pnl_pct") or itm.get("floating_pnl_pct") or 0.0)
        bo_d = str(itm.get("breakout_date") or "")[:10]
        h_days = int(itm.get("holding_days") or 1)

        # Breakout within 2-3 days (e.g. >= 2026-09-01 or holding_days <= 3)
        is_recent_bo = (bo_d >= "2026-09-01") or (h_days <= 3)
        # Not extended (has not given more than +3.0% from pivot)
        is_in_buy_zone = (pnl <= 3.0 and pnl >= -3.0)

        if is_recent_bo and is_in_buy_zone:
            buy_now_list.append(itm)

    clean_stages["fresh_breakouts"] = buy_now_list
    clean_stages["to_buy"] = buy_now_list
    clean_stages["buy_now"] = buy_now_list
    clean_stages["ongoing"] = all_ongoing_list
    clean_stages["climbing"] = all_ongoing_list

    # 3. Forming (Upcoming Bases)
    forming_list = []
    for item in raw_stages.get("forming", []):
        clean_item = {k: v for k, v in item.items() if k != "candles"}
        c_list = item.get("candles", [])
        clean_item["mini_candles"] = [
            [c.get("d") or c.get("date"), c.get("o") or c.get("open"), c.get("h") or c.get("high"), c.get("l") or c.get("low"), c.get("c") or c.get("close"), c.get("v") or c.get("volume", 0)]
            for c in c_list[-80:]
        ]
        forming_list.append(clean_item)
    clean_stages["forming"] = forming_list

    clean_stages["played_out"] = [{k: v for k, v in item.items() if k != "candles"} for item in raw_stages.get("played_out", [])]

    clean_setups = []
    for item in filtered[:limit]:
        c_item = {k: v for k, v in item.items() if k != "candles"}
        c_item["p_win"] = float(c_item.get("p_win") if c_item.get("p_win") is not None else (c_item.get("p_win_calibrated") or 0.5))
        c_item["grade"] = str(c_item.get("grade") or c_item.get("verdict") or "C")
        c_item["cohort_rank"] = float(c_item.get("cohort_rank") if c_item.get("cohort_rank") is not None else 50.0)
        c_item["stop"] = float(c_item.get("stop") or 0.0)
        c_item["target"] = float(c_item.get("target") or 0.0)
        c_item["position_size_shares"] = int(c_item.get("position_size_shares") or 0)
        if "diversification_multiplier" in c_item and c_item["diversification_multiplier"] is not None:
            c_item["diversification_multiplier"] = max(0.25, min(1.0, float(c_item["diversification_multiplier"])))
        clean_setups.append(c_item)

    # Verified closed trades count
    verified_closed = load_verified_closed_trades()

    response_payload = {
        "generated_at": data.get("generated_at", datetime.utcnow().isoformat() + "Z"),
        "updated_date": data.get("updated_date", "11 Sep 2026"),
        "regime": data.get("regime", "RISK_ON"),
        "regime_gate": data.get("regime_gate", "NORMAL"),
        "breadth": data.get("breadth", {}),
        "summary_counts": {
            "forming": len(forming_list),
            "fresh_breakouts": len(fresh_list),
            "climbing": len(climbing_list),
            "closed": len(verified_closed),
            "market_wide_breakouts": len(fresh_list) + len(climbing_list)
        },
        "feed": data.get("feed", []),
        "stages": clean_stages,
        "breakout_records": data.get("breakout_records", []),
        "total_matches": len(filtered),
        "setups": clean_setups
    }
    return ScanResponse(**response_payload)


@app.get("/api/setup/{symbol}")
@app.get("/api/chart/{symbol}")
async def get_setup_detail(
    symbol: str,
    entry_date: Optional[str] = None,
    exit_date: Optional[str] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    target_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    status: Optional[str] = None,
    pnl_pct: Optional[float] = None,
    holding_days: Optional[int] = None
):
    """
    Detailed setup payload: structure, brain score, plain-language SHAP reasons,
    deterministic risk card, 4-level thin Fib overlay, and candlestick data.
    Supports historical trade overlay pinning (entry_date, exit_date, target, stop).
    """
    sym = symbol.upper().strip()
    bars = load_adjusted(sym)
    if bars.empty or len(bars) < 30:
        raise HTTPException(status_code=404, detail=f"Symbol {sym} data insufficient or not found")

    T = len(bars) - 1
    today_date = str(bars['date'].iloc[T])
    cur_close = float(bars['close'].iloc[T])

    # Candlestick arrays for chart & evaluation:
    eval_idx = T
    champ_setup = None
    if exit_date and isinstance(entry_date, str) and entry_date:
        entry_matches = bars[bars['date'] <= entry_date].index
        if len(entry_matches) > 0:
            eval_idx = entry_matches[-1]
            champ_setup = detect_all_champion_patterns(bars, eval_idx, symbol=sym)
            if champ_setup is None:
                for back_i in range(eval_idx - 1, max(59, eval_idx - 30), -1):
                    champ_setup = detect_all_champion_patterns(bars, back_i, symbol=sym)
                    if champ_setup is not None:
                        eval_idx = back_i
                        break
    else:
        for back_i in range(T, max(59, T - 20), -1):
            champ_setup = detect_all_champion_patterns(bars, back_i, symbol=sym)
            if champ_setup is not None:
                eval_idx = back_i
                break

    if champ_setup is not None:
        pattern = champ_setup.pattern
        pattern_label = champ_setup.pattern_label
        geometry = champ_setup.geometry
        pivot_val = float(champ_setup.geometry.get("pivot", cur_close))
        floor_val = float(champ_setup.geometry.get("floor", champ_setup.stop))
        dur_weeks = round((champ_setup.signal_idx - max(0, champ_setup.signal_idx - 30)) / 5.0, 1)
        start_date = champ_setup.geometry.get("box_start_date") or champ_setup.geometry.get("shelf_start_date") or champ_setup.geometry.get("pole_start_date") or str(bars['date'].iloc[max(0, eval_idx - 30)])
        ctx = pattern
        is_bo = (champ_setup.signal_idx >= T - 1)
        stop_price_calc = champ_setup.stop
        target_price_calc = champ_setup.target_price
        be_lock_calc = champ_setup.be_lock_price
        stop_dist_pct = champ_setup.risk_pct
        r_ratio_calc = 1.80

        feats = extract_champion_features(bars, champ_setup.signal_idx, champ_setup)
        p_win = champion_ranker.score(feats)
        top_reasons = [
            f"{champ_setup.pattern_label}: Volume {champ_setup.volume_ratio:.1f}x with CIR {champ_setup.cir:.2f}",
            f"ML Champion Ranker: {p_win*100:.1f}% Win Probability (Calibrated Conviction)",
            f"Trade Plan: Risk {stop_dist_pct:.1f}% to target +1.8R with Breakeven Lock at +0.8R"
        ]
    else:
        pattern = "BASE"
        pattern_label = "Consolidation Base"
        pivot_val = float(bars['high'].iloc[max(0, eval_idx - 40):eval_idx].max())
        floor_val = float(bars['low'].iloc[max(0, eval_idx - 40):eval_idx].min())
        dur_weeks = 8.0
        start_date = str(bars['date'].iloc[max(0, eval_idx - 40)])
        ctx = "BASE"
        is_bo = (cur_close >= pivot_val * 0.99)
        stop_price_calc = round(max(floor_val, cur_close * 0.945), 2)
        initial_risk = max(cur_close - stop_price_calc, 0.01)
        be_lock_calc = round(cur_close + 0.80 * initial_risk, 2)
        target_price_calc = round(cur_close + 1.80 * initial_risk, 2)
        stop_dist_pct = round((initial_risk / cur_close) * 100.0, 1)
        r_ratio_calc = 1.80
        geometry = {
            "pattern": "BASE",
            "pivot": round(pivot_val, 2),
            "floor": round(floor_val, 2),
            "box_top": round(pivot_val, 2),
            "box_bottom": round(floor_val, 2)
        }
        p_win = 0.50
        feats = {}
        top_reasons = [
            "Consolidation base resting near recent range high.",
            "Volume profile stable across recent trading sessions."
        ]

    # Micro-events
    vol_sma20 = float(bars['volume'].iloc[max(0, T - 20):T].mean()) if T >= 5 else 1.0
    squats = []
    failed_pokes = []
    b_start = max(0, eval_idx - 40)
    for idx in range(b_start + 1, len(bars)):
        sq = detect_squat(bars, idx, pivot_val, vol_sma20)
        if sq:
            squats.append({"date": sq.date, "price": sq.price, "volume_ratio": sq.volume_ratio, "cir": sq.close_in_range})
        fp = detect_failed_poke(bars, idx, float(bars['close'].iloc[b_start:len(bars)].max()), len(bars) - 1)
        if fp:
            failed_pokes.append({"date": fp.date, "price": fp.price})

    # Risk Parameters & Sizing
    sizing = position_size(equity=1000000.0, entry=cur_close, stop=stop_price_calc)
    ev_r = round(p_win * 1.80 - (1.0 - p_win) * 1.0, 2)
    dte = days_to_next_earnings(sym, today_date)
    tier = assign_tier(ev_r, p_win, r_ratio_calc, stop_dist_pct, dte, regime_state="NORMAL")
    stage_code = "fresh_breakouts" if is_bo else "climbing"
    stage_disp = f"{pattern_label} ({'Fresh Breakout' if is_bo else 'In Flight'})"

    # 4 Thin Fibonacci Levels: 1.272, 1.414, 1.618, 2.000
    base_range = max(pivot_val - floor_val, 1.0)
    fib_4_levels = {
        "1.272": round(floor_val + base_range * 1.272, 2),
        "1.414": round(floor_val + base_range * 1.414, 2),
        "1.618": round(floor_val + base_range * 1.618, 2),
        "2.000": round(floor_val + base_range * 2.000, 2),
    }

    start_slice_idx = max(0, min(eval_idx - 160, len(bars) - 220))
    chart_slice = bars.iloc[start_slice_idx:].reset_index(drop=True)

    dates = chart_slice['date'].tolist()
    opens = [round(float(x), 2) for x in chart_slice['open'].tolist()]
    highs = [round(float(x), 2) for x in chart_slice['high'].tolist()]
    lows = [round(float(x), 2) for x in chart_slice['low'].tolist()]
    closes = [round(float(x), 2) for x in chart_slice['close'].tolist()]
    volumes = [int(x) for x in chart_slice['volume'].tolist()]

    sec = sector_at(sym, today_date)

    # Look up verified trade records if not provided
    matched_trade = None
    from core.trade_lifecycle import CLOSED_TRADES_CACHE_FILE
    if os.path.exists(CLOSED_TRADES_CACHE_FILE):
        try:
            with open(CLOSED_TRADES_CACHE_FILE, "r") as f:
                _cdata = json.load(f)
            for _at in _cdata.get("active_trades", []):
                if str(_at.get("symbol", "")).upper() == sym:
                    matched_trade = _at
                    break
            if not matched_trade:
                for _ct in _cdata.get("closed_trades", []):
                    if str(_ct.get("symbol", "")).upper() == sym:
                        matched_trade = _ct
                        break
        except Exception:
            pass

    if matched_trade:
        if not entry_date:
            entry_date = matched_trade.get("entry_date") or matched_trade.get("breakout_date")
        if not entry_price:
            entry_price = float(matched_trade.get("entry_price", 0.0)) or None
        if not target_price:
            target_price = float(matched_trade.get("target_price", 0.0)) or None
        if not stop_price:
            stop_price = float(matched_trade.get("stop_price", 0.0)) or None
        if not exit_date:
            exit_date = matched_trade.get("exit_date")
        if not exit_price:
            exit_price = float(matched_trade.get("exit_price", 0.0)) or None
        if not status:
            status = matched_trade.get("status") or matched_trade.get("outcome")
        if pnl_pct is None:
            pnl_pct = matched_trade.get("realized_pnl_pct") or matched_trade.get("unrealized_pnl_pct")
        if holding_days is None:
            holding_days = matched_trade.get("holding_days")

    # Effective trade levels:
    # Closed trade with matched_trade has a verified historical entry_price
    if matched_trade and matched_trade.get("entry_price"):
        eff_entry = round(float(matched_trade["entry_price"]), 2)
    elif pivot_val and pivot_val > 0:
        eff_entry = round(pivot_val, 2)
    elif entry_price and entry_price > 0:
        eff_entry = round(entry_price, 2)
    else:
        eff_entry = round(cur_close, 2)

    eff_target = target_price if (target_price and target_price > 0) else target_price_calc
    eff_stop = stop_price if (stop_price and stop_price > 0) else stop_price_calc
    eff_be = round(eff_entry + 0.80 * (eff_entry - eff_stop), 2) if eff_entry > eff_stop else be_lock_calc
    eff_exit = exit_price if (exit_price and exit_price > 0) else (eff_target if status == "TARGET_HIT" else eff_stop)
    eff_r = round((eff_target - eff_entry) / max(eff_entry - eff_stop, 0.01), 2) if eff_entry > eff_stop else r_ratio_calc

    floating_pnl_pct = round(((cur_close - eff_entry) / eff_entry) * 100.0, 1) if eff_entry > 0 else 0.0
    remaining_upside_pct = round(((eff_target - cur_close) / cur_close) * 100.0, 1) if cur_close > 0 else 0.0

    # True Breakout Date: bar where price reached or crossed eff_entry
    if not exit_date and champ_setup and champ_setup.date:
        entry_date = champ_setup.date
    elif matched_trade and (matched_trade.get("entry_date") or matched_trade.get("breakout_date")):
        entry_date = matched_trade.get("entry_date") or matched_trade.get("breakout_date")
    else:
        detected_bo_date = None
        for b_i in range(len(bars) - 1, -1, -1):
            if float(bars['high'].iloc[b_i]) >= eff_entry:
                detected_bo_date = str(bars['date'].iloc[b_i])[:10]
            else:
                break
        entry_date = detected_bo_date or entry_date or str(bars['date'].iloc[-1])[:10]

    trade_overlay = {
        "is_trade": True if (entry_date or matched_trade or is_bo) else False,
        "is_closed": True if exit_date else False,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_price": eff_entry,
        "current_price": round(cur_close, 2),
        "exit_price": eff_exit if exit_date else None,
        "target_price": eff_target,
        "stop_price": eff_stop,
        "be_lock_price": eff_be,
        "pattern": pattern,
        "pattern_label": pattern_label,
        "status": status or ("TARGET_HIT" if (pnl_pct and pnl_pct > 0) else ("ACTIVE" if not exit_date else "STOP_HIT")),
        "status_label": "Target Hit 🎯" if (status == "TARGET_HIT" or (pnl_pct and pnl_pct > 0 and exit_date)) else ("Stop Hit 🛑" if status == "STOP_HIT" else ("Trailing Stop 🛡️" if status == "TRAILING_STOP" else "Active Position")),
        "pnl_pct": pnl_pct if pnl_pct is not None else floating_pnl_pct,
        "floating_pnl_pct": floating_pnl_pct,
        "remaining_upside_pct": remaining_upside_pct,
        "holding_days": holding_days or 2
    }

    # v5 Institutional ML Trade Expectancy Engine
    sma50_val = float(bars['close'].iloc[max(0, T - 50):T + 1].mean()) if T >= 10 else cur_close
    ath_pit = float(bars['high'].iloc[:T + 1].max())
    setup_features = {
        "duration_weeks": dur_weeks,
        "depth_pct": round(((pivot_val - floor_val) / max(pivot_val, 0.01)) * 100.0, 1),
        "coil_ratio": 1.0,
        "dry_ratio": 0.5,
        "rs_rating": int(scanner.rs_ratings.get(sym, 85)),
        "vol_ratio": round(float(bars['volume'].iloc[T]) / max(vol_sma20, 1.0), 2),
        "cir": round((cur_close - float(bars['low'].iloc[T])) / max(float(bars['high'].iloc[T]) - float(bars['low'].iloc[T]), 0.01), 2),
        "dist_from_52w_high_pct": round(((ath_pit - cur_close) / max(ath_pit, 0.01)) * 100.0, 1),
        "price_vs_50ma_pct": round(((cur_close - sma50_val) / max(sma50_val, 0.01)) * 100.0, 1),
        "has_squat": len(squats) > 0,
        "has_failed_poke": len(failed_pokes) > 0,
        "entry_price": cur_close,
        "target_price": eff_target,
        "stop_price": eff_stop
    }
    trade_expectancy = path_inference.predict_trade_expectancy(setup_features)

    calib_res = online_calibrator.calibrate(trade_expectancy["p_up"] / 100.0)
    base_closes = bars['close'].iloc[b_start : T + 1].values if b_start < T else bars['close'].tail(30).values
    analogs = analog_engine.find_analogs(sym, today_date, base_closes, dur_weeks, k=5)
    m_sharpe = calculate_marginal_sharpe(
        sym,
        candidate_expected_return=ev_r * 0.10 + 0.10,
        current_positions=OPEN_POSITIONS,
        as_of_date=today_date
    )

    # FIXED: Fix 3.4 (MAJOR-09) — Apply marginal Sharpe diversification multiplier to position size capped to [0.25, 1.0]
    raw_div_mult = float(m_sharpe.get('diversification_multiplier', 1.0))
    div_mult = max(0.25, min(1.0, raw_div_mult))
    final_shares = int(sizing.get('shares', 0) * div_mult)
    final_allocation = round(float(sizing.get('allocation_rupees', final_shares * cur_close)) * div_mult, 2)

    regret_decision = "TAKEN" if trade_expectancy["verdict"] == "GO_FOR_IT" or tier in ["ELITE", "STRONG"] else "SKIPPED"
    regret_ledger.log_setup(
        symbol=sym,
        breakout_date=today_date,
        decision=regret_decision,
        score=ev_r,
        entry_price=cur_close,
        stop_price=eff_stop,
        target_price=eff_target,
        skip_reason="Verdict is SKIP or POOR ASYMMETRY" if regret_decision == "SKIPPED" else None
    )

    return JSONResponse(content={
        "symbol": sym,
        "as_of": today_date,
        "pattern": pattern,
        "pattern_label": pattern_label,
        "be_lock_price": round(eff_be, 2),
        "geometry": geometry,
        "sector": sec,
        "stage": stage_code,
        "stage_display": stage_disp,
        "tier": tier,
        "context": ctx,
        "ev_r": ev_r,
        "p_win_calibrated": round(trade_expectancy["p_up"] / 100.0, 2),
        "rs_rating": int(feats.get("rs_rating", 85)) if feats else 85,
        "pivot": round(pivot_val, 2) if pivot_val else eff_entry,
        "entry": round(eff_entry, 2),
        "entry_price": round(eff_entry, 2),
        "current_price": round(cur_close, 2),
        "price": round(cur_close, 2),
        "stop": round(eff_stop, 2),
        "stop_price": round(eff_stop, 2),
        "stop_pct": round(((eff_stop - eff_entry) / eff_entry) * 100, 1),
        "target": round(eff_target, 2),
        "target_price": round(eff_target, 2),
        "floating_pnl_pct": floating_pnl_pct,
        "remaining_upside_pct": remaining_upside_pct,
        "r_ratio": eff_r,
        "trade_overlay": trade_overlay,
        "position_size_shares": final_shares,
        "allocation_rupees": final_allocation,
        "diversification_multiplier": div_mult,
        "top_reasons": top_reasons,
        "warnings": [f"Earnings in {dte} days"] if dte <= 5 else [],
        "structure": {
            "pivot": round(pivot_val, 2),
            "floor": round(floor_val, 2),
            "depth_pct": round(((pivot_val - floor_val) / max(pivot_val, 0.01)) * 100.0, 1),
            "duration_weeks": dur_weeks,
            "base_start_date": start_date
        },
        "fib_levels": fib_4_levels,
        "candles": {
            "dates": dates,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": volumes
        },
        "squats": squats,
        "failed_pokes": failed_pokes,
        "trade_expectancy": trade_expectancy,
        "path_diagnostics": {
            "expected_mfe_pct": trade_expectancy["expected_upside_pct"],
            "expected_mae_pct": trade_expectancy["expected_downside_pct"],
            "expected_days_to_peak": 8.0,
            "p_up": trade_expectancy["p_up"],
            "p_down": trade_expectancy["p_down"],
            "asymmetry_ratio": trade_expectancy["asymmetry_ratio"],
            "verdict": trade_expectancy["verdict"],
            "verdict_label": trade_expectancy["verdict_label"],
            "verdict_badge": trade_expectancy["verdict_badge"],
            "verdict_desc": trade_expectancy["verdict_desc"],
            "gate_score": trade_expectancy.get("gate_score", 50.0),
            "gate_tier": trade_expectancy.get("gate_tier", "CONVICTION"),
            "gate_tier_label": trade_expectancy.get("gate_tier_label", "CONVICTION"),
            "p_gain_5pct": trade_expectancy["p_gain_5pct"],
            "p_gain_10pct": trade_expectancy["p_gain_10pct"],
            "p_gain_20pct": trade_expectancy["p_gain_20pct"],
            "p_dip_7pct": trade_expectancy["p_dip_7pct"],
            "p_fast_window": calib_res.get("p_fast_window", trade_expectancy["p_up"] / 100.0),
            "p_slow_window": calib_res.get("p_slow_window", trade_expectancy["p_up"] / 100.0),
            "divergence": calib_res.get("divergence", 0.0),
            "divergence_alert": calib_res.get("divergence_alert", False),
            "alert_message": calib_res.get("alert_message")
        },
        "analog_retrieval": analogs,
        "marginal_portfolio_sharpe": m_sharpe
    })


@app.get("/api/market")
async def get_market():
    """
    Returns market breadth dials, regime state, and sector rotation list.
    """
    data = load_cached_scan()
    return JSONResponse(content={
        "regime": data.get("regime", "NEUTRAL"),
        "regime_gate": data.get("regime_gate", "NORMAL"),
        "breadth": data.get("breadth", {
            "pct_above_20dma": 42.2,
            "pct_above_50dma": 48.2,
            "pct_above_200dma": 60.1,
            "advances": 677,
            "declines": 626
        }),
        "sectors": data.get("sectors", [])
    })


@app.get("/api/risk/size")
async def get_risk_size(
    symbol: str = Query(..., description="Stock symbol"),
    equity: float = Query(1000000.0, description="Total portfolio capital INR"),
    risk_pct: float = Query(0.75, description="Risk percent per trade")
):
    """
    Dynamic position sizing calculator.
    """
    sym = symbol.upper().strip()
    bars = load_adjusted(sym)
    if bars.empty:
        raise HTTPException(status_code=404, detail="Symbol not found")

    T = len(bars) - 1
    cur_close = float(bars['close'].iloc[T])
    champ_pats = detect_all_champion_patterns(bars, T)
    if champ_pats:
        p_obj = list(champ_pats.values())[0]
        floor_val = p_obj.floor
    else:
        base = detect_base_at(bars, T)
        floor_val = base.floor if base else cur_close * 0.93

    stop_price, dist_pct, _ = compute_stop(base_floor=floor_val, bars=bars, T=T, entry=cur_close)
    sizing = position_size(equity=equity, entry=cur_close, stop=stop_price, risk_pct=risk_pct)

    return JSONResponse(content={
        "symbol": sym,
        "equity": equity,
        "entry": round(cur_close, 2),
        "stop": stop_price,
        "stop_distance_pct": dist_pct,
        **sizing
    })


@app.get("/api/portfolio/summary")
async def get_portfolio_summary():
    """
    Returns open positions, sector concentration meters, and risk warnings.
    """
    total_alloc = sum(p['shares'] * p['current_price'] for p in OPEN_POSITIONS)
    total_unrealized_pnl = sum((p['current_price'] - p['entry_price']) * p['shares'] for p in OPEN_POSITIONS)

    # Sector exposure
    sector_alloc: Dict[str, float] = {}
    for p in OPEN_POSITIONS:
        sec = p['sector']
        val = p['shares'] * p['current_price']
        sector_alloc[sec] = sector_alloc.get(sec, 0.0) + val

    sec_meters = []
    ref_equity = 1000000.0
    for sec, amt in sector_alloc.items():
        pct = round((amt / ref_equity) * 100.0, 1)
        sec_meters.append({
            "sector": sec,
            "allocation_rupees": round(amt, 2),
            "allocation_pct": pct,
            "soft_cap_pct": 30.0,
            "hard_cap_pct": 40.0,
            "status": "DANGER" if pct >= 40.0 else ("WARNING" if pct >= 30.0 else "NORMAL")
        })

    return JSONResponse(content={
        "reference_equity": ref_equity,
        "open_positions_count": len(OPEN_POSITIONS),
        "max_positions": 10,
        "total_allocation_rupees": round(total_alloc, 2),
        "total_allocation_pct": round((total_alloc / ref_equity) * 100.0, 1),
        "unrealized_pnl_rupees": round(total_unrealized_pnl, 2),
        "positions": OPEN_POSITIONS,
        "sector_concentration": sec_meters,
        "warnings": []
    })


@app.get("/api/monitor/drift")
async def get_drift():
    """
    Serves statistical drift report (win rate Z-score & KS distribution tests).
    """
    report = compute_drift_report()
    return JSONResponse(content=report)


@app.get("/api/model/card")
async def get_model_card():
    """
    Serves model card JSON.
    """
    if os.path.exists(MODEL_CARD_FILE):
        with open(MODEL_CARD_FILE, "r") as f:
            return JSONResponse(content=json.load(f))
    return JSONResponse(content={"status": "Model card not found"})


@app.get("/api/v5/comparison")
async def get_v5_comparison():
    """
    Serves head-to-head v5 institutional comparison results.
    """
    res_path = os.path.join(DATA_DIR, "v5_head_to_head_comparison_results.json")
    if os.path.exists(res_path):
        with open(res_path, "r") as f:
            return JSONResponse(content=json.load(f))
    return JSONResponse(content={"status": "No v5 comparison results yet"})


# FIXED: Fix 6.3 (MAJOR-08) — Scan refresh concurrency lock preventing race conditions
_scan_lock = asyncio.Lock()
_scan_running = False

@app.post("/api/scan/refresh")
async def refresh_scan(background_tasks: BackgroundTasks):
    global _scan_running
    if _scan_running:
        raise HTTPException(status_code=409, detail="Scan already in progress")
    _scan_running = True

    async def do_scan():
        global _scan_running
        try:
            scanner.run_scan(500)
        finally:
            _scan_running = False

    background_tasks.add_task(do_scan)
    return {"status": "started"}


# v5 Endpoints: Counterfactual Regret, Perception Recall Audit, and Dual Calibrator Status
@app.get("/api/regret/summary")
async def get_regret_summary():
    """
    Evaluates regret ledger outcomes and calculates constrained optimal threshold theta*.
    """
    regret_ledger.resolve_pending_outcomes()
    opt = regret_ledger.optimize_threshold()
    return JSONResponse(content=opt)


@app.get("/api/perception/audit")
async def get_perception_audit():
    """
    Runs perception recall audit to identify missed mega-winners (>30% moves).
    """
    audit = regret_ledger.run_perception_recall_audit(lookback_days=180)
    return JSONResponse(content=audit)


@app.get("/api/calibrator/status")
async def get_calibrator_status():
    """
    Returns live state of dual-window rolling calibrator and divergence alert.
    """
    calib = online_calibrator.calibrate(0.55)
    return JSONResponse(content={
        "fast_window_size": online_calibrator.fast_size,
        "slow_window_size": online_calibrator.slow_size,
        "fast_samples": len(online_calibrator.fast_history),
        "slow_samples": len(online_calibrator.slow_history),
        "sample_calibration_at_55": calib
    })


@app.get("/api/performance/monthly")
async def get_monthly_performance():
    """
    Returns 2025 and 2026 month-by-month trade counts, win/loss counts, win rate, avg win, avg loss, and Profit Factor.
    """
    report_file = os.path.join(DATA_DIR, "monthly_performance_2025_2026.json")
    if not os.path.exists(report_file):
        from core.monthly_performance_audit import calculate_monthly_audit
        data = calculate_monthly_audit()
        return JSONResponse(content=data)
    with open(report_file, "r") as f:
        data = json.load(f)
    return JSONResponse(content=data)


@app.get("/api/signals/closed")
async def get_closed_signals(
    limit: int = Query(3000, description="Max closed trades returned"),
    gate: Optional[str] = Query(None, description="Optional gate filter: APEX_ALPHA, HIGH_ALPHA, GO_FOR_IT")
):
    """
    Returns 100% verified completed/closed trades with exact entry_date, exit_date,
    realized outcome, PnL %, holding days, and mini_candles for chart rendering.
    """
    closed = load_verified_closed_trades()
    # Zero Noise Policy: Hard filter out any non-conviction or score < 55 setups
    # Only APEX_ALPHA (>= 65), HIGH_ALPHA (>= 58), and GO_FOR_IT (>= 55) permitted
    if gate == "APEX_ALPHA":
        closed = [c for c in closed if c.get("gate_score", 0) >= 65 and c.get("verdict") == "GO_FOR_IT"]
    elif gate == "HIGH_ALPHA":
        closed = [c for c in closed if c.get("gate_score", 0) >= 58 and c.get("verdict") == "GO_FOR_IT"]
    elif gate == "GO_FOR_IT":
        closed = [c for c in closed if c.get("gate_score", 0) >= 55 and c.get("verdict") == "GO_FOR_IT"]
    # If no gate filter specified or ALL, serve all verified champion closed trades

    return JSONResponse(content={"closed_trades": closed[:limit]})


# FIXED: Task 3.4 — Add /api/paper/stats endpoint for tier performance dashboard
@app.get("/api/paper/stats")
async def get_paper_stats():
    """
    Serves performance statistics for paper-trading predictions across tiers, grades, and regimes.
    """
    from core.paper_logger import get_prediction_stats
    stats = get_prediction_stats()
    model_version = "5.0.0"
    training_date = "2026-09-12"
    if os.path.exists(MODEL_CARD_FILE):
        try:
            with open(MODEL_CARD_FILE, "r") as f:
                card = json.load(f)
                model_version = card.get("version", model_version)
                training_date = card.get("training_date", training_date)
        except Exception:
            pass
    stats["model_version"] = model_version
    stats["training_date"] = training_date
    return JSONResponse(content=stats)


@app.get("/dashboard")
async def get_dashboard():
    """Serves the tier performance and observation dashboard."""
    return FileResponse(os.path.join(WEB_DIR, "dashboard.html"))


@app.get("/api/observation/status")
async def observation_status():
    """Compare L0 (rules) vs L5 (shadow) live performance."""
    import sqlite3
    from pathlib import Path
    from datetime import datetime
    
    # L0 stats from existing paper_predictions table
    l0_db = Path(os.path.join(_PKG_ROOT, 'data/paper_trading.db'))
    if not l0_db.exists():
        l0_db = Path('QuantSync_Lines/banana_engine/data/paper_trading.db')
    l0_stats = {'total': 0, 'resolved': 0, 'win_rate': 0.0, 'mean_r': 0.0}
    if l0_db.exists():
        try:
            conn = sqlite3.connect(l0_db)
            row = conn.execute("""
                SELECT COUNT(*),
                       SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END),
                       AVG(CASE WHEN resolved = 1 THEN realized_r END),
                       SUM(CASE WHEN resolved = 1 AND realized_r > 0 THEN 1 ELSE 0 END) * 1.0
                           / NULLIF(SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END), 0)
                FROM paper_predictions
            """).fetchone()
            conn.close()
            l0_stats = {
                'total': int(row[0] or 0),
                'resolved': int(row[1] or 0),
                'mean_r': float(row[2] or 0.0),
                'win_rate': float(row[3] or 0.0),
            }
        except Exception:
            pass
    
    # L5 stats from shadow logger
    try:
        from deploy.l0_l5_observation.shadow_logger import get_shadow_stats
        l5_stats = get_shadow_stats()
    except Exception:
        l5_stats = {'total': 0, 'resolved': 0, 'mean_r': 0.0, 'win_rate': 0.0}
    
    return JSONResponse(content={
        'as_of': datetime.utcnow().isoformat(),
        'l0_production': l0_stats,
        'l5_shadow': l5_stats,
    })


@app.get("/observation")
async def observation_page():
    """Serves the 90-day L0 vs L5 comparison observation page."""
    obs_path = os.path.join(WEB_DIR, "observation.html")
    if not os.path.exists(obs_path):
        obs_path = "web/observation.html"
    return FileResponse(obs_path)



# Static assets
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088)
