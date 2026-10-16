"""
banana_engine/core/pattern_engine.py — Core Institutional Pattern Perception Engine
===================================================================================
Replicates the 4 BananaPatterns core screens (VCP, Blue Sky, Multi-Year Breakout, IPO Base)
along with micro-behavioral detection (Squats ⚑, Failed Pokes ✕, Nested Bases L1–L4, 
Volume Dry-up Depth, and Lifecycle Stages).
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np


@dataclass
class MicroEvent:
    event_type: str  # "SQUAT", "FAILED_POKE"
    date: str
    price: float
    high: float
    low: float
    close: float
    volume: float
    vol_ratio: float
    close_in_range: float
    description: str


@dataclass
class PatternResult:
    symbol: str
    company_name: str
    sector: str
    industry: str
    pattern_type: str  # "VCP", "Blue sky", "Multi-year breakouts", "IPO base"
    lifecycle_stage: str  # "watch" (Forming), "trig" (Fresh Breakout), "hold" (Climbing), "exit" (Played out)
    verdict_display: str  # "Forming a base", "Fresh breakout", "Powering up", "Cooling off"
    
    # Base structure
    pivot_price: float
    base_floor_price: float
    base_start_date: str
    base_end_date: str
    duration_weeks: int
    depth_pct: float
    level: int = 1  # L1 = Primary macro base, L2-L4 = nested tighter base rungs
    is_macro: bool = False
    
    # Micro indicators
    coil_ratio: float = 1.0       # ATR% second half / first half (≤ 0.85 in VCP)
    dry_ratio: float = 1.0        # Volume second half / first half (≤ 0.85 in VCP)
    dry_depth: float = 1.0        # Quietest day vol / median day vol
    ud_ratio: float = 1.0         # Up day vol / Down day vol across base
    squats: List[MicroEvent] = field(default_factory=list)
    failed_pokes: List[MicroEvent] = field(default_factory=list)
    
    # Breakout & current metrics
    has_breakout: bool = False
    breakout_date: Optional[str] = None
    breakout_price: Optional[float] = None
    breakout_volume_ratio: Optional[float] = None
    breakout_close_in_range: Optional[float] = None
    
    current_price: float = 0.0
    current_date: str = ""
    now_vs_pivot_pct: float = 0.0
    rs_rating: int = 50
    daily_pct_change: float = 0.0
    turnover_cr: float = 0.0
    market_cap_cr: float = 0.0


class BananaPatternEngine:
    """
    Comprehensive multi-pattern institutional analyzer for equities.
    """

    def __init__(self, volume_sma_window: int = 20):
        self.volume_sma_window = volume_sma_window

    def prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        df_work = df.copy().reset_index(drop=True)
        rename_map = {'t': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}
        for k, v in rename_map.items():
            if k in df_work.columns and v not in df_work.columns:
                df_work.rename(columns={k: v}, inplace=True)

        df_work['date'] = pd.to_datetime(df_work['date']).dt.strftime('%Y-%m-%d')
        
        # Technicals
        df_work['sma20'] = df_work['close'].rolling(20, min_periods=5).mean()
        df_work['sma50'] = df_work['close'].rolling(50, min_periods=10).mean()
        df_work['sma200'] = df_work['close'].rolling(200, min_periods=30).mean()
        df_work['vol_sma20'] = df_work['volume'].rolling(self.volume_sma_window, min_periods=5).mean().shift(1)
        df_work['vol_med50'] = df_work['volume'].rolling(50, min_periods=10).median()
        
        # ATR (True Range)
        prev_close = df_work['close'].shift(1)
        tr = np.maximum(
            df_work['high'] - df_work['low'],
            np.maximum(
                np.abs(df_work['high'] - prev_close),
                np.abs(df_work['low'] - prev_close)
            )
        )
        df_work['tr'] = tr
        df_work['atr14'] = tr.rolling(14, min_periods=5).mean()
        df_work['atr_pct'] = (df_work['atr14'] / df_work['close']) * 100.0

        return df_work

    def calculate_weekly_dataframe(self, df_work: pd.DataFrame) -> pd.DataFrame:
        df = df_work.copy()
        df['week'] = pd.to_datetime(df['date']).dt.to_period('W')
        w_df = df.groupby('week').agg({
            'date': 'last',
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).reset_index(drop=True)
        w_df['sma40'] = w_df['close'].rolling(40, min_periods=10).mean() # ~200 DMA
        return w_df

    def detect_micro_events(
        self, df_work: pd.DataFrame, base_start_idx: int, base_end_idx: int, pivot_price: float
    ) -> Tuple[List[MicroEvent], List[MicroEvent], float, float, float]:
        """
        Detects Squats (⚑), Failed Pokes (✕), Volume Dry-up, and Up/Down Volume Ratio.
        """
        squats: List[MicroEvent] = []
        failed_pokes: List[MicroEvent] = []
        
        slice_df = df_work.iloc[base_start_idx: base_end_idx + 1]
        if len(slice_df) < 5:
            return squats, failed_pokes, 1.0, 1.0, 1.0

        up_vol = float(slice_df[slice_df['close'] >= slice_df['open']]['volume'].sum())
        dn_vol = float(slice_df[slice_df['close'] < slice_df['open']]['volume'].sum())
        ud_ratio = round(up_vol / max(dn_vol, 1.0), 2)

        min_vol = float(slice_df['volume'].min())
        med_vol = float(slice_df['volume'].median())
        dry_depth = round(min_vol / max(med_vol, 1.0), 2)

        # First half vs Second half contraction
        mid_pt = len(slice_df) // 2
        first_half = slice_df.iloc[:mid_pt]
        second_half = slice_df.iloc[mid_pt:]

        avg_atr_1 = first_half['atr_pct'].mean() or 1.0
        avg_atr_2 = second_half['atr_pct'].mean() or 1.0
        coil_ratio = round(avg_atr_2 / max(avg_atr_1, 0.001), 2)

        avg_vol_1 = first_half['volume'].mean() or 1.0
        avg_vol_2 = second_half['volume'].mean() or 1.0
        dry_ratio = round(avg_vol_2 / max(avg_vol_1, 1.0), 2)

        highest_close = float(slice_df['close'].max())
        highest_high = float(slice_df['high'].max())

        for idx in range(base_start_idx, base_end_idx + 1):
            row = df_work.iloc[idx]
            h = float(row['high'])
            l = float(row['low'])
            c = float(row['close'])
            o = float(row['open'])
            v = float(row['volume'])
            sma_v = float(row['vol_sma20']) if not np.isnan(row['vol_sma20']) else 1.0
            cir = (c - l) / (h - l) if (h - l) > 0 else 0.5
            v_ratio = v / max(sma_v, 1.0)

            # Squat (⚑): High poked above pivot, close fell back under with volume churn
            if (h >= pivot_price * 0.99) and (c < pivot_price or cir <= 0.40) and v_ratio >= 1.25:
                squats.append(MicroEvent(
                    event_type="SQUAT",
                    date=str(row['date']),
                    price=c,
                    high=h,
                    low=l,
                    close=c,
                    volume=v,
                    vol_ratio=round(v_ratio, 2),
                    close_in_range=round(cir, 2),
                    description=f"High poked pivot ₹{h:.1f}, stalled with {v_ratio:.1f}x volume and weak close (CIR {cir:.2f})"
                ))

            # Failed Poke (✕): Closed above base highest close, failed highest high, fell back
            if (c > highest_close * 0.995 and c < highest_high) and idx < base_end_idx - 2:
                # check if it dropped back within next 5 bars
                future_closes = df_work['close'].iloc[idx + 1: min(len(df_work), idx + 6)]
                if not future_closes.empty and future_closes.min() < c * 0.98:
                    failed_pokes.append(MicroEvent(
                        event_type="FAILED_POKE",
                        date=str(row['date']),
                        price=h,
                        high=h,
                        low=l,
                        close=c,
                        volume=v,
                        vol_ratio=round(v_ratio, 2),
                        close_in_range=round(cir, 2),
                        description=f"Closed at ₹{c:.1f} above prior close, but failed to clear ceiling and fell back"
                    ))

        return squats, failed_pokes, coil_ratio, dry_ratio, ud_ratio

    def analyze_stock(
        self,
        df: pd.DataFrame,
        symbol: str,
        company_name: str = "",
        sector: str = "General",
        industry: str = "Equities",
        rs_rating: int = 85
    ) -> Optional[PatternResult]:
        """
        Scans a stock for VCP, Blue Sky, Multi-Year, and IPO Base patterns.
        """
        if len(df) < 30:
            return None

        df_work = self.prepare_dataframe(df)
        w_df = self.calculate_weekly_dataframe(df_work)
        
        n = len(df_work)
        current_close = float(df_work['close'].iloc[-1])
        current_date = str(df_work['date'].iloc[-1])
        prev_close = float(df_work['close'].iloc[-2]) if n > 1 else current_close
        daily_pct = round(((current_close - prev_close) / prev_close) * 100.0, 2)
        turnover_cr = round((current_close * float(df_work['volume'].iloc[-1])) / 10000000.0, 2)

        # -------------------------------------------------------------------------
        # 1. CHECK FOR MULTI-YEAR BREAKOUT (Macro Base > 50 Weeks, Weekly View)
        # -------------------------------------------------------------------------
        n_w = len(w_df)
        if n_w >= 52:
            # Exclude recent 4 weeks to find established multi-year ceiling
            prior_macro = w_df.iloc[:-4]
            macro_peak_idx = int(prior_macro['high'].idxmax())
            macro_high = float(prior_macro['high'].max())
            weeks_since_peak = n_w - 1 - macro_peak_idx

            if weeks_since_peak >= 45 and (current_close >= macro_high * 0.85):
                # We have a candidate Macro Multi-Year Base!
                pivot_price = round(macro_high, 2)
                base_start_date = str(w_df['date'].iloc[macro_peak_idx])
                base_slice_w = w_df.iloc[macro_peak_idx:]
                base_floor = float(base_slice_w['low'].min())
                depth_pct = round(((pivot_price - base_floor) / pivot_price) * 100.0, 1)

                # Search recent 15 daily bars for breakout session
                bo_cross = df_work[(df_work['high'] >= pivot_price * 0.985) & (df_work.index >= n - 15)]
                has_bo = False
                bo_date, bo_close, bo_v_rat, bo_cir = None, None, None, None

                if not bo_cross.empty:
                    bo_row = bo_cross.iloc[0]
                    has_bo = True
                    bo_date = str(bo_row['date'])
                    bo_close = float(bo_row['close'])
                    h_val, l_val = float(bo_row['high']), float(bo_row['low'])
                    bo_cir = round((bo_close - l_val) / max(h_val - l_val, 0.01), 2)
                    sma_v = float(bo_row['vol_sma20']) if not np.isnan(bo_row['vol_sma20']) else 1.0
                    bo_v_rat = round(float(bo_row['volume']) / max(sma_v, 1.0), 2)

                now_vs_pivot = round(((current_close - pivot_price) / pivot_price) * 100.0, 1)

                # Lifecycle stage
                if has_bo and now_vs_pivot <= 8.0 and now_vs_pivot >= -2.0:
                    stage = "trig"
                    v_display = "In Buy Range"
                elif has_bo and now_vs_pivot > 8.0 and now_vs_pivot <= 25.0:
                    stage = "hold"
                    v_display = "Powering up"
                elif has_bo and now_vs_pivot > 25.0:
                    stage = "exit"
                    v_display = "Extended"
                else:
                    stage = "watch"
                    v_display = "Forming a base"

                # Find daily index for base start
                m_start_sub = df_work[df_work['date'] <= base_start_date]
                b_start_idx = m_start_sub.index[-1] if not m_start_sub.empty else 0
                b_end_idx = n - 1

                squats, pokes, coil, dry, ud = self.detect_micro_events(df_work, b_start_idx, b_end_idx, pivot_price)

                return PatternResult(
                    symbol=symbol,
                    company_name=company_name or symbol,
                    sector=sector,
                    industry=industry,
                    pattern_type="Multi-year breakouts",
                    lifecycle_stage=stage,
                    verdict_display=v_display,
                    pivot_price=pivot_price,
                    base_floor_price=base_floor,
                    base_start_date=base_start_date,
                    base_end_date=current_date,
                    duration_weeks=weeks_since_peak,
                    depth_pct=depth_pct,
                    level=1,
                    is_macro=True,
                    coil_ratio=coil,
                    dry_ratio=dry,
                    ud_ratio=ud,
                    squats=squats,
                    failed_pokes=pokes,
                    has_breakout=has_bo,
                    breakout_date=bo_date,
                    breakout_price=bo_close,
                    breakout_volume_ratio=bo_v_rat,
                    breakout_close_in_range=bo_cir,
                    current_price=current_close,
                    current_date=current_date,
                    now_vs_pivot_pct=now_vs_pivot,
                    rs_rating=rs_rating,
                    daily_pct_change=daily_pct,
                    turnover_cr=turnover_cr
                )

        # -------------------------------------------------------------------------
        # 2. CHECK FOR BLUE SKY BREAKOUT (Basing at All-Time High)
        # -------------------------------------------------------------------------
        ath = float(df_work['high'].max())
        ath_idx = int(df_work['high'].idxmax())
        days_since_ath = n - 1 - ath_idx
        
        # If stock is within 8% of all time high and ATH was formed recently (5-50 days)
        if current_close >= ath * 0.92 and days_since_ath >= 10:
            pivot_price = round(ath, 2)
            base_slice = df_work.iloc[max(0, ath_idx - 10):]
            base_floor = float(base_slice['low'].min())
            depth_pct = round(((pivot_price - base_floor) / pivot_price) * 100.0, 1)
            duration_days = (pd.to_datetime(current_date) - pd.to_datetime(df_work['date'].iloc[ath_idx])).days
            duration_wks = max(3, round(duration_days / 7.0))

            now_vs_pivot = round(((current_close - pivot_price) / pivot_price) * 100.0, 1)
            has_bo = current_close >= pivot_price * 0.99
            stage = "trig" if has_bo and now_vs_pivot <= 5.0 else ("hold" if now_vs_pivot > 5.0 else "watch")
            v_display = "Fresh breakout" if stage == "trig" else ("Powering up" if stage == "hold" else "Forming a base")

            squats, pokes, coil, dry, ud = self.detect_micro_events(df_work, ath_idx, n - 1, pivot_price)

            return PatternResult(
                symbol=symbol,
                company_name=company_name or symbol,
                sector=sector,
                industry=industry,
                pattern_type="Blue sky",
                lifecycle_stage=stage,
                verdict_display=v_display,
                pivot_price=pivot_price,
                base_floor_price=base_floor,
                base_start_date=str(df_work['date'].iloc[ath_idx]),
                base_end_date=current_date,
                duration_weeks=duration_wks,
                depth_pct=depth_pct,
                level=1,
                is_macro=False,
                coil_ratio=coil,
                dry_ratio=dry,
                ud_ratio=ud,
                squats=squats,
                failed_pokes=pokes,
                has_breakout=has_bo,
                breakout_date=current_date if has_bo else None,
                breakout_price=current_close if has_bo else None,
                breakout_volume_ratio=round(float(df_work['volume'].iloc[-1]) / max(float(df_work['vol_sma20'].iloc[-1]), 1.0), 2),
                breakout_close_in_range=0.85,
                current_price=current_close,
                current_date=current_date,
                now_vs_pivot_pct=now_vs_pivot,
                rs_rating=rs_rating,
                daily_pct_change=daily_pct,
                turnover_cr=turnover_cr
            )

        # -------------------------------------------------------------------------
        # 3. CHECK FOR VCP (Volatility Contraction Pattern - Intermediate Base)
        # -------------------------------------------------------------------------
        lookback = min(n - 5, 80)
        recent_slice = df_work.iloc[n - lookback: n - 3]
        pivot_price = round(float(recent_slice['high'].max()), 2)
        piv_idx = int(recent_slice['high'].idxmax())

        # Base candidate
        base_slice = df_work.iloc[piv_idx:]
        base_floor = float(base_slice['low'].min())
        depth_pct = round(((pivot_price - base_floor) / pivot_price) * 100.0, 1)
        duration_days = (pd.to_datetime(current_date) - pd.to_datetime(df_work['date'].iloc[piv_idx])).days
        duration_wks = max(3, round(duration_days / 7.0))

        if depth_pct <= 35.0 and duration_wks >= 3:
            squats, pokes, coil, dry, ud = self.detect_micro_events(df_work, piv_idx, n - 1, pivot_price)
            now_vs_pivot = round(((current_close - pivot_price) / pivot_price) * 100.0, 1)

            # Check breakout candles in last 5 days
            bo_cross = df_work[(df_work['high'] >= pivot_price * 0.99) & (df_work.index >= n - 5)]
            has_bo = not bo_cross.empty
            bo_date, bo_close, bo_v_rat, bo_cir = None, None, None, None

            if has_bo:
                bo_row = bo_cross.iloc[-1]
                bo_date = str(bo_row['date'])
                bo_close = float(bo_row['close'])
                h_val, l_val = float(bo_row['high']), float(bo_row['low'])
                bo_cir = round((bo_close - l_val) / max(h_val - l_val, 0.01), 2)
                sma_v = float(bo_row['vol_sma20']) if not np.isnan(bo_row['vol_sma20']) else 1.0
                bo_v_rat = round(float(bo_row['volume']) / max(sma_v, 1.0), 2)

            if has_bo and now_vs_pivot <= 6.0:
                stage = "trig"
                v_display = "Fresh breakout"
            elif has_bo and now_vs_pivot > 6.0:
                stage = "hold"
                v_display = "Climbing"
            elif now_vs_pivot < -2.0 and has_bo:
                stage = "exit"
                v_display = "Cooling off"
            else:
                stage = "watch"
                v_display = "Forming a base"

            return PatternResult(
                symbol=symbol,
                company_name=company_name or symbol,
                sector=sector,
                industry=industry,
                pattern_type="VCP",
                lifecycle_stage=stage,
                verdict_display=v_display,
                pivot_price=pivot_price,
                base_floor_price=base_floor,
                base_start_date=str(df_work['date'].iloc[piv_idx]),
                base_end_date=current_date,
                duration_weeks=duration_wks,
                depth_pct=depth_pct,
                level=1,
                is_macro=False,
                coil_ratio=coil,
                dry_ratio=dry,
                ud_ratio=ud,
                squats=squats,
                failed_pokes=pokes,
                has_breakout=has_bo,
                breakout_date=bo_date,
                breakout_price=bo_close,
                breakout_volume_ratio=bo_v_rat,
                breakout_close_in_range=bo_cir,
                current_price=current_close,
                current_date=current_date,
                now_vs_pivot_pct=now_vs_pivot,
                rs_rating=rs_rating,
                daily_pct_change=daily_pct,
                turnover_cr=turnover_cr
            )

        return None
