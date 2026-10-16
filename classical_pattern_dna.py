#!/usr/bin/env python3
"""
ClassicalPatternAnalystDNA
==========================
Engine-Agnostic Classical Chart Pattern Perception & Geometry Engine.

Designed as an independent, modular capability:
- Usable by ANY engine (V2, V3, V4, V5, V6, or DNA Engine).
- Decouples geometric discovery from engine-specific decision logic.
- Detects the primary classical chart patterns:
    1. FALLING_WEDGE (Bullish compression reversal)
    2. SYMMETRICAL_TRIANGLE (Apex coil continuation / reversal)
    3. BULLISH_RECTANGLE (Parallel consolidation breakout)
    4. INVERSE_HEAD_AND_SHOULDERS (Three-trough reversal)
    5. CUP_AND_HANDLE (Parabolic rounding bowl + shallow handle)
    6. ASCENDING_TRIANGLE (Flat resistance ceiling + ascending support)
    + Complementary structures: RISING_WEDGE, DESCENDING_TRIANGLE,
      HEAD_AND_SHOULDERS, DOUBLE_BOTTOM, DOUBLE_TOP, BULL_FLAG.

Emits standardized `DNAPatternResult` objects complete with exact
Matplotlib / SVG `chart_directives` for seamless institutional visual rendering.
"""

import os
import sys
import math
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd


@dataclass
class PatternAnchor:
    label: str
    date: str
    price: float
    index: int


@dataclass
class DNAPatternResult:
    pattern_type: str                  # e.g. "FALLING_WEDGE"
    pattern_name: str                  # e.g. "Falling Wedge · Bullish Apex Breakout"
    stance: str                        # "BULLISH", "BEARISH", "NEUTRAL"
    action_title: str                  # e.g. "Buy Breakout (Tight Apex)"
    lifecycle_state: str               # "FORMING", "NEAR_CONFIRMATION", "CONFIRMED", "TARGET_ACHIEVED", "INVALIDATED"
    confidence: float                  # 0.0 to 1.0 (combined geometry & confirmation)
    quality_score: float               # 0.0 to 1.0
    breakout_level: float              # Trigger price / Neckline
    invalidation_level: float          # Stop loss floor / ceiling
    target_price: float                # Measured move upside / downside
    risk_reward_ratio: float           # e.g. 2.5
    raw_geometry: Dict[str, Any]       # Measured slopes, heights, spreads
    normalized_geometry: Dict[str, float]  # ATR & scale invariant metrics
    evidence: List[str]                # Bullet points for human reasoning
    anchors: List[PatternAnchor]       # Key geometric anchor points
    chart_directives: Dict[str, Any]   # Directives for Matplotlib / Canvas renderer
    
    # Separation of Concerns (Institutional Three-Tier Structure)
    historical_pattern: Optional[Dict[str, Any]] = None  # Full historical pattern context (neckline, base dates, measured target)
    pattern_status: str = "CONFIRMED"                    # Pattern status (Target Achieved, Fresh Breakout, Forming, etc.)
    market_structure: str = "Trend Continuation"         # Current market structure (e.g. Bullish Trend Continuation)
    trading_signal: str = "Neutral"                      # Current actionable signal (e.g. Target Achieved · Do Not Chase)
    competing_candidates: List[str] = field(default_factory=list)
    trade_call: Optional[Dict[str, Any]] = None          # validated state/grade/plan/track-record from common.trade_rules

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_type": self.pattern_type,
            "pattern_name": self.pattern_name,
            "stance": self.stance,
            "action_title": self.action_title,
            "lifecycle_state": self.lifecycle_state,
            "confidence": round(self.confidence, 3),
            "quality_score": round(self.quality_score, 3),
            "breakout_level": round(self.breakout_level, 2),
            "invalidation_level": round(self.invalidation_level, 2),
            "target_price": round(self.target_price, 2),
            "risk_reward_ratio": round(self.risk_reward_ratio, 2),
            "raw_geometry": self.raw_geometry,
            "normalized_geometry": self.normalized_geometry,
            "evidence": self.evidence,
            "anchors": [{"label": a.label, "date": a.date, "price": a.price, "index": a.index} for a in self.anchors],
            "chart_directives": self.chart_directives,
            "historical_pattern": self.historical_pattern,
            "pattern_status": self.pattern_status,
            "market_structure": self.market_structure,
            "trading_signal": self.trading_signal,
            "competing_candidates": self.competing_candidates,
            "trade_call": self.trade_call,
        }


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorized aggregation of daily OHLCV into standard Weekly bars (ending Friday).
    Includes latest partial week up to the most recent daily close.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    clean = df.copy()
    if 'Date' not in clean.columns and 't' in clean.columns:
        clean['Date'] = pd.to_datetime(clean['t'])
    else:
        clean['Date'] = pd.to_datetime(clean['Date'])
    
    col_map = {'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'}
    for k, v in col_map.items():
        if k in clean.columns and v not in clean.columns:
            clean[v] = clean[k]

    clean = clean.sort_values('Date').set_index('Date')
    w_df = clean.resample('W-FRI').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna(subset=['Close']).reset_index()

    # Append live unclosed week if trading happened past last Friday
    if len(clean) > 0 and len(w_df) > 0:
        last_daily_date = clean.index[-1]
        if last_daily_date > w_df['Date'].iloc[-1]:
            tail_df = clean.loc[clean.index > w_df['Date'].iloc[-1]]
            if len(tail_df) > 0:
                partial_week = pd.DataFrame([{
                    'Date': last_daily_date,
                    'Open': float(tail_df['Open'].iloc[0]),
                    'High': float(tail_df['High'].max()),
                    'Low': float(tail_df['Low'].min()),
                    'Close': float(tail_df['Close'].iloc[-1]),
                    'Volume': float(tail_df['Volume'].sum())
                }])
                w_df = pd.concat([w_df, partial_week], ignore_index=True)
    return w_df


def resample_to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorized aggregation of daily OHLCV into standard Monthly bars (Month-End).
    Includes latest partial month up to the most recent daily close.
    """
    if df is None or len(df) == 0:
        return pd.DataFrame()
    clean = df.copy()
    if 'Date' not in clean.columns and 't' in clean.columns:
        clean['Date'] = pd.to_datetime(clean['t'])
    else:
        clean['Date'] = pd.to_datetime(clean['Date'])
    
    col_map = {'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'}
    for k, v in col_map.items():
        if k in clean.columns and v not in clean.columns:
            clean[v] = clean[k]

    clean = clean.sort_values('Date').set_index('Date')
    m_df = clean.resample('ME').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna(subset=['Close']).reset_index()

    # Append live unclosed month if trading happened past last month end
    if len(clean) > 0 and len(m_df) > 0:
        last_daily_date = clean.index[-1]
        if last_daily_date > m_df['Date'].iloc[-1]:
            tail_df = clean.loc[clean.index > m_df['Date'].iloc[-1]]
            if len(tail_df) > 0:
                partial_month = pd.DataFrame([{
                    'Date': last_daily_date,
                    'Open': float(tail_df['Open'].iloc[0]),
                    'High': float(tail_df['High'].max()),
                    'Low': float(tail_df['Low'].min()),
                    'Close': float(tail_df['Close'].iloc[-1]),
                    'Volume': float(tail_df['Volume'].sum())
                }])
                m_df = pd.concat([m_df, partial_month], ignore_index=True)
    return m_df


class ClassicalPatternAnalystDNA:
    """
    Engine-Agnostic Classical Pattern Perception System.
    Analyzes any standard OHLCV DataFrame (columns: Date, Open, High, Low, Close, Volume).
    """

    def __init__(self, atr_window: int = 14, min_bars: int = 30):
        self.atr_window = atr_window
        self.min_bars = min_bars

    # ──────────────────────────────────────────────────────────────────────────
    # Core Analysis Entry Point
    # ──────────────────────────────────────────────────────────────────────────
    def analyze(self, df: pd.DataFrame, custom_atr: Optional[float] = None, timeframe: str = "DAILY") -> Optional[DNAPatternResult]:
        """
        Scans OHLCV dataframe and extracts the primary pattern candidate.
        Supports timeframe='DAILY', 'WEEKLY', or 'MONTHLY'.
        Returns None if no classical pattern meets minimum geometric validity.
        """
        if df is None:
            return None

        tf_upper = (timeframe or "DAILY").upper()
        if tf_upper == "WEEKLY":
            clean_df = resample_to_weekly(df)
            if len(clean_df) > 156:  # up to 3 years of weekly bars
                clean_df = clean_df.iloc[-156:].reset_index(drop=True)
            min_req_bars = 20
        elif tf_upper == "MONTHLY":
            clean_df = resample_to_monthly(df)
            if len(clean_df) > 60:   # up to 5 years of monthly bars
                clean_df = clean_df.iloc[-60:].reset_index(drop=True)
            min_req_bars = 18
        else:
            clean_df = self._standardize_df(df)
            if len(clean_df) > 260:
                clean_df = clean_df.iloc[-260:].reset_index(drop=True)
            min_req_bars = self.min_bars

        if clean_df is None or len(clean_df) < min_req_bars:
            return None

        n = len(clean_df)
        atr = custom_atr if (custom_atr and custom_atr > 0) else self._compute_atr(clean_df)
        atr = max(atr, 0.01)

        closes = clean_df['Close'].values
        curr_close = float(closes[-1])
        dates = [d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10] for d in clean_df['Date']]

        # Extract swing extrema using multi-window ZigZag
        swing_highs, swing_lows = self._extract_swings(clean_df, dates)

        candidates: List[DNAPatternResult] = []

        # 1. Falling Wedge (Bullish Reversal) & Rising Wedge
        candidates.extend(self._detect_wedges(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 2. Triangles (Ascending, Symmetrical, Descending)
        candidates.extend(self._detect_triangles(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 3. Rectangles (Bullish Rectangle / Box Channel)
        candidates.extend(self._detect_rectangles(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 4. Inverse Head & Shoulders & Head & Shoulders
        candidates.extend(self._detect_head_and_shoulders(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 5. Cup & Handle
        candidates.extend(self._detect_cup_and_handle(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 6. Double Bottom & Double Top
        candidates.extend(self._detect_double_formations(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 7. Bull Flag
        candidates.extend(self._detect_bull_flags(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates))

        # 8. Multi-Year Generational Base (3-5Y Accumulation Saucer & Multi-Year Flat Base)
        if tf_upper in ("MONTHLY", "WEEKLY"):
            candidates.extend(self._detect_multi_year_saucer(clean_df, swing_highs, swing_lows, atr, n, curr_close, dates, is_weekly=(tf_upper == "WEEKLY")))

        if not candidates:
            return None

        # Only consider active, actionable setups (fresh confirmation, coiling near breakout, or forming)
        active_candidates = [
            c for c in candidates
            if c.lifecycle_state in ("CONFIRMED", "NEAR_CONFIRMATION", "FORMING")
        ]
        if not active_candidates:
            # If all candidates achieved target or completed, return the best completed setup for factual reporting
            completed = [c for c in candidates if c.lifecycle_state == "TARGET_ACHIEVED"]
            if completed:
                completed.sort(key=lambda x: x.confidence, reverse=True)
                return completed[0]
            return None

        def _score(c: DNAPatternResult) -> float:
            state_mult = 1.30 if c.lifecycle_state == "CONFIRMED" else (1.15 if c.lifecycle_state == "NEAR_CONFIRMATION" else 0.90)
            order_mult = 1.08 if c.pattern_type in ("BULLISH_RECTANGLE", "FALLING_WEDGE", "SYMMETRICAL_TRIANGLE", "ASCENDING_TRIANGLE", "INVERSE_HEAD_AND_SHOULDERS", "CUP_AND_HANDLE") else 1.0
            return (c.confidence * 0.50 + c.quality_score * 0.50) * state_mult * order_mult

        active_candidates.sort(key=_score, reverse=True)

        primary = active_candidates[0]
        # Extra guard: FORMING double-bottoms with borderline quality must clear a
        # higher quality bar so they don't crowd out real patterns on every chart.
        if (primary.pattern_type == "DOUBLE_BOTTOM"
                and primary.lifecycle_state == "FORMING"
                and primary.quality_score < 0.72):
            # Try next best candidate that clears the bar
            for alt in active_candidates[1:]:
                if not (alt.pattern_type == "DOUBLE_BOTTOM"
                        and alt.lifecycle_state == "FORMING"
                        and alt.quality_score < 0.72):
                    primary = alt
                    break
            else:
                # No acceptable alternative — suppress pattern entirely
                return None
        primary.competing_candidates = [c.pattern_type for c in active_candidates[1:4]]
        self._apply_trade_rules(primary, clean_df, tf_upper)
        return primary

    def _apply_trade_rules(self, res: DNAPatternResult, clean_df: pd.DataFrame, tf_upper: str) -> None:
        """Replace 'CONFIRMED = last close is above the level' with the tested state machine (common.trade_rules).
        Geometry (confidence / quality_score / levels) is untouched; only the lifecycle and the trade plan change."""
        try:
            if res.stance != "BULLISH" or res.lifecycle_state in ("INVALIDATED", "TARGET_ACHIEVED"):
                return
            from common import trade_rules as _tr
            call = _tr.evaluate_arrays(clean_df["Open"].to_numpy(float), clean_df["High"].to_numpy(float),
                                       clean_df["Low"].to_numpy(float), clean_df["Close"].to_numpy(float),
                                       clean_df["Volume"].to_numpy(float) if "Volume" in clean_df else np.ones(len(clean_df)),
                                       res.breakout_level, tf_upper)
            res.trade_call = call
            m = _tr.to_legacy(call)
            if m:
                res.lifecycle_state = m["lifecycle"]
                res.pattern_status = m["status"]
                res.trading_signal = m["signal"]
                res.action_title = m["title"]
                plan = call.get("plan")
                if plan and call.get("action") == "BUY":
                    res.target_price = plan["target"]
                    res.invalidation_level = plan["stop"]
                    res.risk_reward_ratio = plan["reward_risk"]
        except Exception:
            res.trade_call = None            # never let the new layer break pattern detection

    def detect_all(self, df: pd.DataFrame) -> List[DNAPatternResult]:
        """Returns all recognized candidates sorted by conviction."""
        if df is None or len(df) < self.min_bars:
            return []
        clean_df = self._standardize_df(df)
        if len(clean_df) > 260:
            clean_df = clean_df.iloc[-260:].reset_index(drop=True)
        n = len(clean_df)
        atr = max(self._compute_atr(clean_df), 0.01)
        closes = clean_df['Close'].values
        curr_close = float(closes[-1])
        dates = [d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10] for d in clean_df['Date']]

        sh, sl = self._extract_swings(clean_df, dates)
        candidates = []
        candidates.extend(self._detect_wedges(clean_df, sh, sl, atr, n, curr_close, dates))
        candidates.extend(self._detect_triangles(clean_df, sh, sl, atr, n, curr_close, dates))
        candidates.extend(self._detect_rectangles(clean_df, sh, sl, atr, n, curr_close, dates))
        candidates.extend(self._detect_head_and_shoulders(clean_df, sh, sl, atr, n, curr_close, dates))
        candidates.extend(self._detect_cup_and_handle(clean_df, sh, sl, atr, n, curr_close, dates))
        candidates.extend(self._detect_double_formations(clean_df, sh, sl, atr, n, curr_close, dates))
        candidates.extend(self._detect_bull_flags(clean_df, sh, sl, atr, n, curr_close, dates))

        def _score(c: DNAPatternResult) -> float:
            state_mult = 1.30 if c.lifecycle_state == "CONFIRMED" else (1.15 if c.lifecycle_state == "NEAR_CONFIRMATION" else 0.90)
            return (c.confidence * 0.50 + c.quality_score * 0.50) * state_mult

        candidates.sort(key=_score, reverse=True)
        return candidates

    # ──────────────────────────────────────────────────────────────────────────
    # Helper: Data Hygiene & ATR
    # ──────────────────────────────────────────────────────────────────────────
    def _standardize_df(self, df: pd.DataFrame) -> pd.DataFrame:
        cdf = df.copy()
        col_map = {c: c.capitalize() for c in cdf.columns}
        cdf = cdf.rename(columns=col_map)
        if 'Date' not in cdf.columns and 'Datetime' in cdf.columns:
            cdf['Date'] = cdf['Datetime']
        cdf['Date'] = pd.to_datetime(cdf['Date'])
        cdf = cdf.sort_values('Date').reset_index(drop=True)
        for col in ['Open', 'High', 'Low', 'Close']:
            cdf[col] = cdf[col].astype(float)
        if 'Volume' in cdf.columns:
            cdf['Volume'] = cdf['Volume'].astype(float)
        return cdf

    def _compute_atr(self, df: pd.DataFrame) -> float:
        if len(df) < 2:
            return 1.0
        h = df['High'].values
        l = df['Low'].values
        c = df['Close'].values
        tr1 = h[1:] - l[1:]
        tr2 = np.abs(h[1:] - c[:-1])
        tr3 = np.abs(l[1:] - c[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        w = min(self.atr_window, len(tr))
        return float(np.mean(tr[-w:])) if w > 0 else 1.0

    def _extract_swings(self, df: pd.DataFrame, dates: List[str]) -> Tuple[List[Dict], List[Dict]]:
        highs = df['High'].values
        lows = df['Low'].values
        sh_dict, sl_dict = {}, {}
        # Multi-window ZigZag: k=4 (tactical) and k=8 (macro base)
        windows = [4]
        if len(df) >= 70:
            windows.append(8)
            
        for k in windows:
            for i in range(k, len(df) - k):
                if highs[i] == max(highs[i - k:i + k + 1]):
                    if i not in sh_dict or k > sh_dict[i].get('k', 0):
                        sh_dict[i] = {"idx": i, "price": float(highs[i]), "date": dates[i], "macro": (k >= 8), "k": k}
                if lows[i] == min(lows[i - k:i + k + 1]):
                    if i not in sl_dict or k > sl_dict[i].get('k', 0):
                        sl_dict[i] = {"idx": i, "price": float(lows[i]), "date": dates[i], "macro": (k >= 8), "k": k}
                        
        sh = sorted(sh_dict.values(), key=lambda x: x['idx'])
        sl = sorted(sl_dict.values(), key=lambda x: x['idx'])
        return sh, sl

    def _determine_market_structure(self, df: pd.DataFrame, curr_close: float) -> str:
        """Determines macro trend & structural regime from EMA hierarchy."""
        if df is None or len(df) < 20:
            return "Consolidation / Range Structure"
        c = df['Close']
        ema20 = float(c.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1]) if len(c) >= 50 else ema20
        ema200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1]) if len(c) >= 150 else ema50

        if curr_close > ema20 > ema50 > ema200:
            return "Strong Bullish Trend Continuation (20/50/200 EMAs Stacked Upward)"
        elif curr_close > ema50 and ema50 > ema200:
            return "Bullish Stage 2 Trend (Above 50 & 200 EMA)"
        elif curr_close < ema20 < ema50 < ema200:
            return "Downtrend / Distribution (20/50/200 EMAs Stacked Downward)"
        elif curr_close > ema200 and curr_close < ema50:
            return "Macro Uptrend Pullback / Base Retest"
        else:
            return "Consolidation / Range-Bound Structure"

    def _determine_lifecycle_and_signal(
        self,
        curr_close: float,
        breakout_level: float,
        invalidation_level: float,
        target_price: float,
        stance: str,
        base_action_title: str,
        df: Optional[pd.DataFrame] = None
    ) -> Tuple[str, str, str, str, float]:
        """
        Enforces separation of concerns across lifecycle, status, signal, and action title.
        Prevents misleading 'Buy Breakout' signals when target has already been achieved or surpassed.
        Returns: (lifecycle_state, pattern_status, trading_signal, action_title, confidence)
        """
        if stance == "BULLISH":
            if curr_close >= target_price:
                return (
                    "TARGET_ACHIEVED",
                    "Breakout Completed · Target Achieved",
                    "TARGET ACHIEVED · EXTENDED (DO NOT CHASE / TRAIL STOP)",
                    "Target Achieved · Extended",
                    0.92
                )
            elif curr_close > breakout_level * 1.035:
                return (
                    "EXTENDED",
                    "Post-Breakout Run In Progress",
                    "POST-BREAKOUT RUN (HOLD / TRAIL STOP)",
                    "Post-Breakout Run",
                    0.78
                )
            elif curr_close > breakout_level:
                return (
                    "CONFIRMED",
                    "Fresh Breakout Confirmed",
                    f"BUY {base_action_title.upper()}",
                    base_action_title,
                    0.86
                )
            elif curr_close >= breakout_level * 0.985:
                return (
                    "NEAR_CONFIRMATION",
                    "Approaching Breakout Level",
                    "WATCHLIST · AWAIT BREAKOUT",
                    "Awaiting Breakout",
                    0.78
                )
            elif curr_close < invalidation_level:
                return (
                    "INVALIDATED",
                    "Pattern Invalidated (Stop Breached)",
                    "PATTERN FAILED · STEP ASIDE",
                    "Pattern Invalidated",
                    0.20
                )
            else:
                return (
                    "FORMING",
                    "Pattern Base Forming",
                    "MONITOR EVOLUTION (NO SIGNAL)",
                    "Base Forming",
                    0.68
                )
        else:
            # Bearish
            if curr_close <= target_price:
                return (
                    "TARGET_ACHIEVED",
                    "Breakdown Completed · Downside Target Met",
                    "DOWNSIDE TARGET MET · COVER SHORTS",
                    "Target Achieved · Downside",
                    0.90
                )
            elif curr_close < breakout_level * 0.965:
                return (
                    "EXTENDED",
                    "Post-Breakdown Decline In Progress",
                    "POST-BREAKDOWN TREND (TRAIL SHORT STOP)",
                    "Post-Breakdown Decline",
                    0.78
                )
            elif curr_close < breakout_level:
                return (
                    "CONFIRMED",
                    "Fresh Breakdown Confirmed",
                    f"SELL {base_action_title.upper()}",
                    base_action_title,
                    0.84
                )
            elif curr_close <= breakout_level * 1.015:
                return (
                    "NEAR_CONFIRMATION",
                    "Testing Breakdown Floor",
                    "WATCHLIST · AWAIT BREAKDOWN",
                    "Awaiting Breakdown",
                    0.75
                )
            elif curr_close > invalidation_level:
                return (
                    "INVALIDATED",
                    "Pattern Invalidated (Ceiling Breached)",
                    "PATTERN FAILED · COVER SHORTS",
                    "Pattern Invalidated",
                    0.20
                )
            else:
                return (
                    "FORMING",
                    "Distribution Top Forming",
                    "MONITOR TOPPING (NO SIGNAL)",
                    "Top Forming",
                    0.65
                )

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Wedges: Falling Wedge & Rising Wedge
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_wedges(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []
        if len(sh) < 2 or len(sl) < 2:
            return results

        # Check last 3 or last 2 swing points
        h_pts = sh[-3:] if len(sh) >= 3 else sh[-2:]
        l_pts = sl[-3:] if len(sl) >= 3 else sl[-2:]

        h_first, h_last = h_pts[0], h_pts[-1]
        l_first, l_last = l_pts[0], l_pts[-1]

        # FALLING WEDGE: Both upper resistance and lower support are downward sloping,
        # but upper resistance drops faster (steeper negative slope) than lower support, converging to an apex.
        upper_slope = (h_last['price'] - h_first['price']) / max(h_last['idx'] - h_first['idx'], 1)
        lower_slope = (l_last['price'] - l_first['price']) / max(l_last['idx'] - l_first['idx'], 1)

        if upper_slope < 0 and lower_slope < 0 and upper_slope < lower_slope:
            spread_start = h_first['price'] - l_first['price']
            spread_end = h_last['price'] - l_last['price']
            if spread_end < spread_start and spread_start > 0:
                convergence = spread_end / max(spread_start, 0.01)
                breakout_level = round(h_last['price'], 2)
                invalidation_level = round(l_last['price'] - (atr * 0.6), 2)
                measured_move = spread_start
                target_price = round(breakout_level + measured_move, 2)
                rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

                if curr_close > breakout_level:
                    state = "CONFIRMED"
                    conf = 0.88
                elif curr_close >= breakout_level * 0.985:
                    state = "NEAR_CONFIRMATION"
                    conf = 0.78
                elif curr_close < invalidation_level:
                    state = "INVALIDATED"
                    conf = 0.25
                else:
                    state = "FORMING"
                    conf = 0.68

                quality = round(max(0.1, min(0.98, 1.0 - (convergence * 0.7))), 2)

                # Directives for DNA Matplotlib & Interactive Canvas
                directives = {
                    "pattern_family": "WEDGE",
                    "trendlines": [
                        {
                            "type": "resistance",
                            "p1": {"date": h_first['date'], "price": h_first['price']},
                            "p2": {"date": h_last['date'], "price": h_last['price']},
                            "color": "#EF4444",
                            "label": "Falling Wedge Resistance (Upper Ray)"
                        },
                        {
                            "type": "support",
                            "p1": {"date": l_first['date'], "price": l_first['price']},
                            "p2": {"date": l_last['date'], "price": l_last['price']},
                            "color": "#10B981",
                            "label": "Falling Wedge Support (Lower Ray)"
                        }
                    ],
                    "horizontal_levels": [
                        {"price": breakout_level, "color": "#3B82F6", "style": "dashed", "label": "Breakout Pivot Trigger"},
                        {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Measured Move Target"},
                        {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Stop Loss"}
                    ],
                    "annotation_box": f"Falling Wedge · Apex Convergence {(1.0-convergence)*100:.0f}%"
                }

                anchors = [
                    PatternAnchor("Upper Anchor 1", h_first['date'], h_first['price'], h_first['idx']),
                    PatternAnchor("Upper Anchor 2", h_last['date'], h_last['price'], h_last['idx']),
                    PatternAnchor("Lower Anchor 1", l_first['date'], l_first['price'], l_first['idx']),
                    PatternAnchor("Lower Anchor 2", l_last['date'], l_last['price'], l_last['idx'])
                ]

                results.append(DNAPatternResult(
                    pattern_type="FALLING_WEDGE",
                    pattern_name="Falling Wedge · Bullish Apex Breakout",
                    stance="BULLISH",
                    action_title="Buy Breakout (Apex Squeeze)",
                    lifecycle_state=state,
                    confidence=conf,
                    quality_score=quality,
                    breakout_level=breakout_level,
                    invalidation_level=invalidation_level,
                    target_price=target_price,
                    risk_reward_ratio=rr,
                    raw_geometry={"upper_slope": round(upper_slope, 4), "lower_slope": round(lower_slope, 4), "spread_start": round(spread_start, 2), "spread_end": round(spread_end, 2)},
                    normalized_geometry={"convergence_ratio": round(convergence, 3), "spread_atr": round(spread_start / atr, 2)},
                    evidence=[
                        f"Converging descending boundary rails with spread contraction of {(1.0 - convergence)*100:.1f}%.",
                        f"Upper line falling faster (slope {upper_slope:.2f}) than lower support (slope {lower_slope:.2f}).",
                        f"Clear breakout pivot at ₹{breakout_level} targeting ₹{target_price}."
                    ],
                    anchors=anchors,
                    chart_directives=directives
                ))

        # RISING WEDGE: Bearish breakdown setup
        elif upper_slope > 0 and lower_slope > 0 and lower_slope > upper_slope:
            spread_start = h_first['price'] - l_first['price']
            spread_end = h_last['price'] - l_last['price']
            if spread_end < spread_start and spread_start > 0:
                convergence = spread_end / max(spread_start, 0.01)
                breakdown_level = round(l_last['price'], 2)
                invalidation_level = round(h_last['price'] + (atr * 0.6), 2)
                target_price = round(breakdown_level - spread_start, 2)
                rr = round(abs(curr_close - target_price) / max(abs(invalidation_level - curr_close), 0.01), 2)

                state = "CONFIRMED" if curr_close < breakdown_level else "FORMING"
                results.append(DNAPatternResult(
                    pattern_type="RISING_WEDGE",
                    pattern_name="Rising Wedge · Bearish Exhaustion Coil",
                    stance="BEARISH",
                    action_title="Avoid / Protect Profits",
                    lifecycle_state=state,
                    confidence=0.75 if state == "CONFIRMED" else 0.62,
                    quality_score=round(max(0.1, 1.0 - convergence), 2),
                    breakout_level=breakdown_level,
                    invalidation_level=invalidation_level,
                    target_price=target_price,
                    risk_reward_ratio=rr,
                    raw_geometry={"upper_slope": round(upper_slope, 4), "lower_slope": round(lower_slope, 4)},
                    normalized_geometry={"convergence_ratio": round(convergence, 3)},
                    evidence=["Ascending converging boundaries with diminishing volume momentum."],
                    anchors=[PatternAnchor("H1", h_first['date'], h_first['price'], h_first['idx']), PatternAnchor("L1", l_first['date'], l_first['price'], l_first['idx'])],
                    chart_directives={
                        "pattern_family": "WEDGE",
                        "trendlines": [
                            {"type": "resistance", "p1": {"date": h_first['date'], "price": h_first['price']}, "p2": {"date": h_last['date'], "price": h_last['price']}, "color": "#EF4444", "label": "Rising Wedge Ceiling"},
                            {"type": "support", "p1": {"date": l_first['date'], "price": l_first['price']}, "p2": {"date": l_last['date'], "price": l_last['price']}, "color": "#F59E0B", "label": "Rising Wedge Floor"}
                        ]
                    }
                ))

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Triangles: Ascending, Symmetrical, Descending
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_triangles(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []
        if len(sh) < 2 or len(sl) < 2:
            return results

        h1, h2 = sh[-2], sh[-1]
        l1, l2 = sl[-2], sl[-1]

        # ASCENDING TRIANGLE: Flat horizontal ceiling (resistance) + Ascending swing lows
        res_diff = abs(h1['price'] - h2['price'])
        floor_rise = l2['price'] - l1['price']
        is_flat_ceiling = (res_diff <= min(atr * 0.5, h1['price'] * 0.010))
        has_higher_lows = (floor_rise >= max(atr * 0.65, l1['price'] * 0.012))

        if is_flat_ceiling and has_higher_lows:
            res_ceiling = round(max(h1['price'], h2['price']), 2)
            height = res_ceiling - l1['price']
            breakout_level = res_ceiling
            invalidation_level = round(l2['price'] - (atr * 0.4), 2)
            target_price = round(res_ceiling + height, 2)
            rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

            if curr_close > breakout_level:
                state = "CONFIRMED"
                conf = 0.89
            elif curr_close >= breakout_level * 0.988:
                state = "NEAR_CONFIRMATION"
                conf = 0.82
            elif curr_close < invalidation_level:
                state = "INVALIDATED"
                conf = 0.20
            else:
                state = "FORMING"
                conf = 0.70

            quality = round(max(0.2, 1.0 - (res_diff / max(atr, 0.01)) * 0.5), 2)

            directives = {
                "pattern_family": "TRIANGLE",
                "trendlines": [
                    {
                        "type": "resistance",
                        "p1": {"date": h1['date'], "price": res_ceiling},
                        "p2": {"date": dates[-1], "price": res_ceiling},
                        "color": "#EF4444",
                        "label": f"Flat Resistance Ceiling (₹{res_ceiling})"
                    },
                    {
                        "type": "support",
                        "p1": {"date": l1['date'], "price": l1['price']},
                        "p2": {"date": l2['date'], "price": l2['price']},
                        "color": "#10B981",
                        "label": "Ascending Support Rail"
                    }
                ],
                "horizontal_levels": [
                    {"price": breakout_level, "color": "#3B82F6", "style": "solid", "label": "Ascending Triangle Breakout Level"},
                    {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Measured Move Target"},
                    {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Stop Loss Floor"}
                ],
                "annotation_box": f"Ascending Triangle · Ceiling ₹{res_ceiling}"
            }

            results.append(DNAPatternResult(
                pattern_type="ASCENDING_TRIANGLE",
                pattern_name="Ascending Triangle · Pressure Against Ceiling",
                stance="BULLISH",
                action_title="Buy Breakout (Flat Ceiling Punch)",
                lifecycle_state=state,
                confidence=conf,
                quality_score=quality,
                breakout_level=breakout_level,
                invalidation_level=invalidation_level,
                target_price=target_price,
                risk_reward_ratio=rr,
                raw_geometry={"res_ceiling": res_ceiling, "l1": l1['price'], "l2": l2['price'], "height": round(height, 2)},
                normalized_geometry={"height_atr": round(height / atr, 2), "ceiling_flatness_ratio": round(res_diff / atr, 2)},
                evidence=[
                    f"Strong horizontal resistance ceiling tested multiple times near ₹{res_ceiling}.",
                    f"Buyers steadily raising the floor with higher swing lows (₹{l1['price']:.1f} -> ₹{l2['price']:.1f}).",
                    f"Classic volatility compression pushing price directly into breakout trigger at ₹{breakout_level}."
                ],
                anchors=[
                    PatternAnchor("Resistance 1", h1['date'], h1['price'], h1['idx']),
                    PatternAnchor("Resistance 2", h2['date'], h2['price'], h2['idx']),
                    PatternAnchor("Ascending Low 1", l1['date'], l1['price'], l1['idx']),
                    PatternAnchor("Ascending Low 2", l2['date'], l2['price'], l2['idx'])
                ],
                chart_directives=directives
            ))

        # SYMMETRICAL TRIANGLE: Lower Highs + Higher Lows converging symmetrically
        if h2['price'] < h1['price'] and l2['price'] > l1['price']:
            spread_start = h1['price'] - l1['price']
            spread_end = h2['price'] - l2['price']
            if 0 < spread_end < spread_start:
                convergence = spread_end / max(spread_start, 0.01)
                breakout_level = round(h2['price'], 2)
                invalidation_level = round(l2['price'], 2)
                target_price = round(breakout_level + spread_start, 2)
                rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

                if curr_close > breakout_level:
                    state = "CONFIRMED"
                    conf = 0.85
                elif curr_close >= breakout_level * 0.988:
                    state = "NEAR_CONFIRMATION"
                    conf = 0.76
                elif curr_close < invalidation_level:
                    state = "INVALIDATED"
                    conf = 0.20
                else:
                    state = "FORMING"
                    conf = 0.65

                quality = round(max(0.2, 1.0 - convergence), 2)

                directives = {
                    "pattern_family": "TRIANGLE",
                    "trendlines": [
                        {
                            "type": "resistance",
                            "p1": {"date": h1['date'], "price": h1['price']},
                            "p2": {"date": h2['date'], "price": h2['price']},
                            "color": "#EF4444",
                            "label": "Descending Triangle Upper Boundary"
                        },
                        {
                            "type": "support",
                            "p1": {"date": l1['date'], "price": l1['price']},
                            "p2": {"date": l2['date'], "price": l2['price']},
                            "color": "#10B981",
                            "label": "Ascending Triangle Lower Boundary"
                        }
                    ],
                    "horizontal_levels": [
                        {"price": breakout_level, "color": "#3B82F6", "style": "dashed", "label": "Symmetrical Breakout Level"},
                        {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Measured Move Target"},
                        {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Stop Loss Level"}
                    ],
                    "annotation_box": f"Symmetrical Triangle · Spread Contraction {(1.0-convergence)*100:.0f}%"
                }

                results.append(DNAPatternResult(
                    pattern_type="SYMMETRICAL_TRIANGLE",
                    pattern_name="Symmetrical Triangle · Coiled Energy Coil",
                    stance="BULLISH",
                    action_title="Buy Breakout on Volume",
                    lifecycle_state=state,
                    confidence=conf,
                    quality_score=quality,
                    breakout_level=breakout_level,
                    invalidation_level=invalidation_level,
                    target_price=target_price,
                    risk_reward_ratio=rr,
                    raw_geometry={"h1": h1['price'], "h2": h2['price'], "l1": l1['price'], "l2": l2['price'], "spread_start": round(spread_start, 2)},
                    normalized_geometry={"convergence_ratio": round(convergence, 3), "spread_atr": round(spread_start / atr, 2)},
                    evidence=[
                        f"Both boundaries converging toward apex with a {(1.0-convergence)*100:.1f}% contraction.",
                        f"Lower high at ₹{h2['price']:.1f} and higher low at ₹{l2['price']:.1f} coiling energy.",
                        f"Breakout trigger at ₹{breakout_level} with target at ₹{target_price}."
                    ],
                    anchors=[
                        PatternAnchor("Peak 1", h1['date'], h1['price'], h1['idx']),
                        PatternAnchor("Peak 2", h2['date'], h2['price'], h2['idx']),
                        PatternAnchor("Trough 1", l1['date'], l1['price'], l1['idx']),
                        PatternAnchor("Trough 2", l2['date'], l2['price'], l2['idx'])
                    ],
                    chart_directives=directives
                ))

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Rectangles: Bullish Rectangle / Box Channel
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_rectangles(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []
        if len(sh) < 2 or len(sl) < 2:
            return results

        h1, h2 = sh[-2], sh[-1]
        l1, l2 = sl[-2], sl[-1]

        top_diff = abs(h1['price'] - h2['price'])
        bot_diff = abs(l1['price'] - l2['price'])

        # Both top and bottom flat within 2% or 0.8x ATR
        is_top_flat = (top_diff <= max(atr * 0.85, h1['price'] * 0.02))
        is_bot_flat = (bot_diff <= max(atr * 0.85, l1['price'] * 0.02))

        if is_top_flat and is_bot_flat:
            res = round(max(h1['price'], h2['price']), 2)
            sup = round(min(l1['price'], l2['price']), 2)
            box_height = res - sup

            if box_height >= atr * 1.2:
                breakout_level = res
                invalidation_level = round(sup - (atr * 0.5), 2)
                target_price = round(res + box_height, 2)
                rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

                if curr_close > breakout_level:
                    state = "CONFIRMED"
                    conf = 0.87
                elif curr_close >= breakout_level * 0.988:
                    state = "NEAR_CONFIRMATION"
                    conf = 0.79
                elif curr_close < sup:
                    state = "INVALIDATED"
                    conf = 0.22
                else:
                    state = "FORMING"
                    conf = 0.68

                quality = round(max(0.2, 1.0 - ((top_diff + bot_diff) / (2 * max(atr, 0.01)))), 2)

                directives = {
                    "pattern_family": "RECTANGLE",
                    "trendlines": [
                        {
                            "type": "resistance",
                            "p1": {"date": h1['date'], "price": res},
                            "p2": {"date": dates[-1], "price": res},
                            "color": "#EF4444",
                            "label": f"Rectangle Upper Boundary (₹{res})"
                        },
                        {
                            "type": "support",
                            "p1": {"date": l1['date'], "price": sup},
                            "p2": {"date": dates[-1], "price": sup},
                            "color": "#10B981",
                            "label": f"Rectangle Floor Support (₹{sup})"
                        }
                    ],
                    "horizontal_levels": [
                        {"price": breakout_level, "color": "#3B82F6", "style": "solid", "label": "Box Breakout Ceiling"},
                        {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Measured Box Target"},
                        {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Box Floor Invalidation"}
                    ],
                    "annotation_box": f"Bullish Rectangle · Range ₹{sup} to ₹{res}"
                }

                results.append(DNAPatternResult(
                    pattern_type="BULLISH_RECTANGLE",
                    pattern_name="Bullish Rectangle · Horizontal Consolidation Box",
                    stance="BULLISH",
                    action_title="Buy Box Breakout",
                    lifecycle_state=state,
                    confidence=conf,
                    quality_score=quality,
                    breakout_level=breakout_level,
                    invalidation_level=invalidation_level,
                    target_price=target_price,
                    risk_reward_ratio=rr,
                    raw_geometry={"res": res, "sup": sup, "box_height": round(box_height, 2)},
                    normalized_geometry={"height_atr": round(box_height / atr, 2), "depth_pct": round(box_height / res * 100, 1)},
                    evidence=[
                        f"Parallel horizontal bounds between ₹{sup} and ₹{res}.",
                        f"Repeated structural tests validating the institutional accumulation floor.",
                        f"Measured move upside projection of ₹{box_height:.1f} targeting ₹{target_price}."
                    ],
                    anchors=[
                        PatternAnchor("Ceiling Touch 1", h1['date'], h1['price'], h1['idx']),
                        PatternAnchor("Ceiling Touch 2", h2['date'], h2['price'], h2['idx']),
                        PatternAnchor("Floor Touch 1", l1['date'], l1['price'], l1['idx']),
                        PatternAnchor("Floor Touch 2", l2['date'], l2['price'], l2['idx'])
                    ],
                    chart_directives=directives
                ))

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Inverse Head & Shoulders & Normal Head & Shoulders
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_head_and_shoulders(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []

        # INVERSE HEAD & SHOULDERS: Inverted Left Shoulder, deeper Head trough, higher Right Shoulder
        if len(sl) >= 3:
            for i in range(len(sl) - 2):
                ls, hd, rs = sl[i], sl[i + 1], sl[i + 2]
                if (n - 1) - rs['idx'] > 35:
                    continue  # pattern must be reasonably recent

                # Head must be distinctly lower than both shoulders
                head_prominence = min(ls['price'], rs['price']) - hd['price']
                if head_prominence >= atr * 0.70 and hd['price'] < ls['price'] and hd['price'] < rs['price']:
                    shoulder_diff = abs(ls['price'] - rs['price'])
                    shoulder_ratio = shoulder_diff / ls['price']

                    if shoulder_ratio <= 0.08:  # shoulders within 8% symmetry
                        neckline = float(df['High'].iloc[ls['idx']:rs['idx'] + 1].max())
                        neckline = round(neckline, 2)
                        head_depth = neckline - hd['price']
                        breakout_level = neckline
                        invalidation_level = round(rs['price'] - (atr * 0.5), 2)
                        target_price = round(neckline + head_depth, 2)
                        rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

                        # Check if target was already achieved post right shoulder
                        post_rs_max_high = float(df['High'].iloc[rs['idx']:].max())
                        if post_rs_max_high >= target_price or curr_close >= target_price:
                            state = "TARGET_ACHIEVED"
                            conf = 0.95
                        elif curr_close > breakout_level:
                            state = "CONFIRMED"
                            conf = 0.90
                        elif curr_close >= breakout_level * 0.985:
                            state = "NEAR_CONFIRMATION"
                            conf = 0.81
                        elif curr_close < invalidation_level:
                            state = "INVALIDATED"
                            conf = 0.20
                        else:
                            state = "FORMING"
                            conf = 0.70

                        quality = round(max(0.2, 1.0 - shoulder_ratio * 4.0), 2)

                        stance = "NEUTRAL" if state in ("TARGET_ACHIEVED", "INVALIDATED") else "BULLISH"
                        action_title = "Target Achieved · Profit Booked" if state == "TARGET_ACHIEVED" else ("Buy Neckline Breakout" if state in ("CONFIRMED", "NEAR_CONFIRMATION") else "Watch Neckline Resistance")

                        directives = {
                            "pattern_family": "HEAD_AND_SHOULDERS",
                            "trendlines": [
                                {
                                    "type": "neckline",
                                    "p1": {"date": ls['date'], "price": neckline},
                                    "p2": {"date": dates[-1], "price": neckline},
                                    "color": "#3B82F6",
                                    "label": f"Inverse H&S Neckline (₹{neckline})"
                                }
                            ],
                            "horizontal_levels": [
                                {"price": breakout_level, "color": "#3B82F6", "style": "solid", "label": "Neckline Breakout Level"},
                                {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Measured Head Depth Target"},
                                {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Right Shoulder Invalidation"}
                            ],
                            "annotation_box": f"Inverse Head & Shoulders · Neckline ₹{neckline}"
                        }

                        results.append(DNAPatternResult(
                            pattern_type="INVERSE_HEAD_AND_SHOULDERS",
                            pattern_name="Inverse Head & Shoulders · Reversal Trough",
                            stance=stance,
                            action_title=action_title,
                            lifecycle_state=state,
                            confidence=conf,
                            quality_score=quality,
                            breakout_level=breakout_level,
                            invalidation_level=invalidation_level,
                            target_price=target_price,
                            risk_reward_ratio=rr,
                            raw_geometry={"ls": ls['price'], "head": hd['price'], "rs": rs['price'], "neckline": neckline},
                            normalized_geometry={"head_prominence_atr": round(head_prominence / atr, 2), "shoulder_symmetry_pct": round(shoulder_ratio * 100, 1)},
                            evidence=[
                                f"Inverted three-trough bottom with central head trough at ₹{hd['price']:.1f}.",
                                f"Left shoulder (₹{ls['price']:.1f}) and right shoulder (₹{rs['price']:.1f}) demonstrate {100 - shoulder_ratio*100:.1f}% symmetry.",
                                f"Neckline resistance established at ₹{neckline}, projecting measured target ₹{target_price}."
                            ],
                            anchors=[
                                PatternAnchor("Left Shoulder", ls['date'], ls['price'], ls['idx']),
                                PatternAnchor("Head", hd['date'], hd['price'], hd['idx']),
                                PatternAnchor("Right Shoulder", rs['date'], rs['price'], rs['idx'])
                            ],
                            chart_directives=directives
                        ))

        # NORMAL HEAD & SHOULDERS: Bearish distribution
        if len(sh) >= 3:
            for i in range(len(sh) - 2):
                ls, hd, rs = sh[i], sh[i + 1], sh[i + 2]
                if (n - 1) - rs['idx'] > 35:
                    continue
                head_prominence = hd['price'] - max(ls['price'], rs['price'])
                if head_prominence >= atr * 0.70 and hd['price'] > ls['price'] and hd['price'] > rs['price']:
                    shoulder_ratio = abs(ls['price'] - rs['price']) / ls['price']
                    if shoulder_ratio <= 0.08:
                        neckline = float(df['Low'].iloc[ls['idx']:rs['idx'] + 1].min())
                        breakdown_level = round(neckline, 2)
                        invalidation_level = round(hd['price'], 2)
                        target_price = round(neckline - (hd['price'] - neckline), 2)
                        rr = round(abs(curr_close - target_price) / max(abs(invalidation_level - curr_close), 0.01), 2)

                        state = "CONFIRMED" if curr_close < breakdown_level else "FORMING"
                        results.append(DNAPatternResult(
                            pattern_type="HEAD_AND_SHOULDERS",
                            pattern_name="Head & Shoulders · Distribution Top",
                            stance="BEARISH",
                            action_title="Protect Profits / Exit",
                            lifecycle_state=state,
                            confidence=0.82 if state == "CONFIRMED" else 0.65,
                            quality_score=round(max(0.2, 1.0 - shoulder_ratio * 4.0), 2),
                            breakout_level=breakdown_level,
                            invalidation_level=invalidation_level,
                            target_price=target_price,
                            risk_reward_ratio=rr,
                            raw_geometry={"ls": ls['price'], "head": hd['price'], "rs": rs['price'], "neckline": neckline},
                            normalized_geometry={"head_prominence_atr": round(head_prominence / atr, 2)},
                            evidence=["Classical 3-peak topping formation with breakdown below neckline."],
                            anchors=[PatternAnchor("LS", ls['date'], ls['price'], ls['idx']), PatternAnchor("Head", hd['date'], hd['price'], hd['idx']), PatternAnchor("RS", rs['date'], rs['price'], rs['idx'])],
                            chart_directives={
                                "pattern_family": "HEAD_AND_SHOULDERS",
                                "trendlines": [{"type": "neckline", "p1": {"date": ls['date'], "price": neckline}, "p2": {"date": dates[-1], "price": neckline}, "color": "#EF4444", "label": "H&S Neckline Support"}]
                            }
                        ))

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Cup & Handle
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_cup_and_handle(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []
        if len(sh) < 2 or len(sl) < 1:
            return results

        # Rim 1 -> Rounding Bowl Trough -> Rim 2 -> Shallow Handle Consolidation
        for i in range(len(sh) - 1):
            r1, r2 = sh[i], sh[i + 1]
            cup_span = r2['idx'] - r1['idx']
            if 14 <= cup_span <= 75:
                rim_diff = abs(r1['price'] - r2['price'])
                if rim_diff <= max(atr * 1.2, r1['price'] * 0.045):
                    # Intervening low is the bowl bottom
                    inter_lows = [l for l in sl if r1['idx'] < l['idx'] < r2['idx']]
                    if inter_lows:
                        trough = min(inter_lows, key=lambda x: x['price'])
                        cup_depth = max(r1['price'], r2['price']) - trough['price']
                        rim_level = round(max(r1['price'], r2['price']), 2)

                        handle_len = (n - 1) - r2['idx']
                        if 3 <= handle_len <= 25:
                            handle_lows = df['Low'].iloc[r2['idx']:].values
                            handle_pullback = rim_level - min(handle_lows)
                            handle_retrace = handle_pullback / max(cup_depth, 0.01)

                            # Handle must not retrace more than 50% of the cup depth
                            if handle_retrace <= 0.50:
                                breakout_level = rim_level
                                invalidation_level = round(rim_level - (cup_depth * 0.45), 2)
                                target_price = round(rim_level + cup_depth, 2)
                                rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

                                if curr_close > breakout_level:
                                    state = "CONFIRMED"
                                    conf = 0.92
                                elif curr_close >= breakout_level * 0.985:
                                    state = "NEAR_CONFIRMATION"
                                    conf = 0.84
                                elif curr_close < invalidation_level:
                                    state = "INVALIDATED"
                                    conf = 0.20
                                else:
                                    state = "FORMING"
                                    conf = 0.72

                                quality = round(max(0.2, 1.0 - (handle_retrace * 0.7)), 2)

                                directives = {
                                    "pattern_family": "CUP_AND_HANDLE",
                                    "trendlines": [
                                        {
                                            "type": "resistance",
                                            "p1": {"date": r1['date'], "price": rim_level},
                                            "p2": {"date": dates[-1], "price": rim_level},
                                            "color": "#D97706",
                                            "label": f"Cup Rim Resistance (₹{rim_level})"
                                        }
                                    ],
                                    "horizontal_levels": [
                                        {"price": breakout_level, "color": "#3B82F6", "style": "solid", "label": "Cup Breakout Rim Level"},
                                        {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Cup Depth Upside Target"},
                                        {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Handle Invalidation Floor"}
                                    ],
                                    "cup_curve": {
                                        "start_idx": r1['idx'],
                                        "bottom_idx": trough['idx'],
                                        "end_idx": r2['idx'],
                                        "rim_price": rim_level,
                                        "bottom_price": trough['price']
                                    },
                                    "annotation_box": f"Cup & Handle · Depth {cup_depth/rim_level*100:.1f}% · Handle Retrace {handle_retrace*100:.0f}%"
                                }

                                results.append(DNAPatternResult(
                                    pattern_type="CUP_AND_HANDLE",
                                    pattern_name="Cup & Handle · High Tight Continuation",
                                    stance="BULLISH",
                                    action_title="Buy Breakout (Rim Expansion)",
                                    lifecycle_state=state,
                                    confidence=conf,
                                    quality_score=quality,
                                    breakout_level=breakout_level,
                                    invalidation_level=invalidation_level,
                                    target_price=target_price,
                                    risk_reward_ratio=rr,
                                    raw_geometry={"r1": r1['price'], "r2": r2['price'], "trough": trough['price'], "cup_depth": round(cup_depth, 2), "handle_pullback": round(handle_pullback, 2)},
                                    normalized_geometry={"cup_depth_atr": round(cup_depth / atr, 2), "handle_retrace_ratio": round(handle_retrace, 3)},
                                    evidence=[
                                        f"Textbook U-shaped rounding accumulation bowl spanning {cup_span} trading sessions.",
                                        f"Symmetric rim alignment within {(rim_diff/r1['price'])*100:.1f}% variance at ₹{rim_level}.",
                                        f"Constructive shallow handle consolidation retracing only {handle_retrace*100:.1f}% of cup depth.",
                                        f"Upside measured move target ₹{target_price}."
                                    ],
                                    anchors=[
                                        PatternAnchor("Left Rim", r1['date'], r1['price'], r1['idx']),
                                        PatternAnchor("Cup Bottom", trough['date'], trough['price'], trough['idx']),
                                        PatternAnchor("Right Rim", r2['date'], r2['price'], r2['idx'])
                                    ],
                                    chart_directives=directives
                                ))
                                break
        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 6. Double Bottoms & Double Tops (Strict Structural Intervening Peak/Valley)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_double_formations(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []
        market_struct = self._determine_market_structure(df, curr_close)
        
        # ── Double Bottom (W formation) ──────────────────────────────────────
        # P1 = first significant swing low
        # P2 = intervening swing high strictly between P1 and P3 (Neckline)
        # P3 = second significant swing low
        for i in range(len(sl) - 1):
            for j in range(i + 1, min(i + 5, len(sl))):
                l1, l2 = sl[i], sl[j]
                span = l2['idx'] - l1['idx']
                # Meaningful base width: between 10 and 110 trading bars
                if not (10 <= span <= 110):
                    continue

                # Recency gate: P3 (second bottom) must be within the last 30 bars
                # so we only surface actionable, recent patterns — not stale history.
                if l2['idx'] < n - 30:
                    continue

                diff = abs(l1['price'] - l2['price'])
                min_bottom = min(l1['price'], l2['price'])
                # Trough symmetry: within 3.5% or 1.25 ATR (tightened from 4.5%)
                if diff > max(atr * 1.25, min_bottom * 0.035):
                    continue

                # Intervening Peak P2 strictly between l1 and l2
                inter_highs = [h for h in sh if l1['idx'] < h['idx'] < l2['idx']]
                if not inter_highs:
                    sub_highs = df['High'].values[l1['idx']:l2['idx']+1]
                    if len(sub_highs) > 0:
                        max_val = float(np.max(sub_highs))
                        max_idx = int(np.argmax(sub_highs)) + l1['idx']
                        peak = {"idx": max_idx, "price": max_val, "date": dates[max_idx]}
                    else:
                        continue
                else:
                    peak = max(inter_highs, key=lambda x: x['price'])

                # ── Quality gate: both bottoms must be meaningful swing lows ──
                # Require at least one macro-scale swing low (k=8 window) to ensure
                # we are looking at a genuine structural base, not noise.
                if not (l1.get('macro') or l2.get('macro')):
                    continue

                neckline = round(float(peak['price']), 2)
                avg_bottom = (l1['price'] + l2['price']) / 2.0
                depth = neckline - avg_bottom
                # Minimum depth for a meaningful W reversal (raised to 2.2 ATR)
                if depth < max(atr * 2.2, avg_bottom * 0.06):
                    continue

                # Prior trend: price must have descended into l1 (reversal requirement)
                # Require at least a 10% drop from the preceding high into P1
                prior_start = max(0, l1['idx'] - 40)
                prior_highs = df['High'].values[prior_start:l1['idx']]
                if len(prior_highs) > 0 and float(np.max(prior_highs)) < l1['price'] * 1.10:
                    continue

                breakout = neckline
                invalidation = round(min_bottom - (atr * 0.5), 2)
                # Measured Move Target: neckline + (neckline - avg(P1, P3))
                target = round(neckline + depth, 2)
                rr = round(abs(target - curr_close) / max(abs(curr_close - invalidation), 0.01), 2)

                lifecycle, status_text, signal_text, act_title, conf = self._determine_lifecycle_and_signal(
                    curr_close=curr_close,
                    breakout_level=breakout,
                    invalidation_level=invalidation,
                    target_price=target,
                    stance="BULLISH",
                    base_action_title="Buy Neckline Breakout",
                    df=df
                )

                sym_score = max(0.2, 1.0 - (diff / max(atr, 0.01)) * 0.4)
                macro_bonus = 0.25 if (l1.get('macro') and l2.get('macro')) else (0.10 if (l1.get('macro') or l2.get('macro')) else 0.0)
                quality = round(min(0.98, sym_score * 0.5 + min(depth / (atr * 3.0), 1.0) * 0.25 + macro_bonus), 2)

                anchors = [
                    PatternAnchor("Bottom 1 (P1)", l1['date'], l1['price'], l1['idx']),
                    PatternAnchor("Intervening Peak (P2 Neckline)", peak['date'], neckline, peak['idx']),
                    PatternAnchor("Bottom 2 (P3)", l2['date'], l2['price'], l2['idx'])
                ]

                directives = {
                    "pattern_family": "DOUBLE_BOTTOM",
                    "trendlines": [
                        {
                            "type": "neckline",
                            "p1": {"date": peak['date'], "price": neckline},
                            "p2": {"date": dates[-1], "price": neckline},
                            "color": "#3B82F6",
                            "label": f"W Neckline (Intervening Peak) ₹{neckline:.1f}"
                        },
                        {
                            "type": "support",
                            "p1": {"date": l1['date'], "price": min_bottom},
                            "p2": {"date": dates[-1], "price": min_bottom},
                            "color": "#10B981",
                            "label": f"Base Floor ₹{min_bottom:.1f}"
                        }
                    ],
                    "horizontal_levels": [
                        {"price": neckline, "color": "#3B82F6", "style": "dashed", "label": "Neckline Trigger"},
                        {"price": target, "color": "#10B981", "style": "dotted", "label": "Measured Move Target"},
                        {"price": invalidation, "color": "#EF4444", "style": "dotted", "label": "Stop Loss Floor"}
                    ],
                    "annotation_box": f"W-Reversal Base · Width {span} bars · Depth ₹{depth:.1f}",
                    "market_structure": market_struct,
                    "trading_signal": signal_text
                }

                results.append(DNAPatternResult(
                    pattern_type="DOUBLE_BOTTOM",
                    pattern_name="Double Bottom · W-Reversal Base",
                    stance="BULLISH",
                    action_title=act_title,
                    lifecycle_state=lifecycle,
                    confidence=conf,
                    quality_score=quality,
                    breakout_level=breakout,
                    invalidation_level=invalidation,
                    target_price=target,
                    risk_reward_ratio=rr,
                    raw_geometry={
                        "p1_bottom1": l1['price'],
                        "p2_intervening_peak": neckline,
                        "p3_bottom2": l2['price'],
                        "pattern_depth": round(depth, 2),
                        "base_width_bars": span
                    },
                    normalized_geometry={
                        "depth_atr": round(depth / atr, 2),
                        "trough_diff_pct": round((diff / min_bottom) * 100, 2)
                    },
                    evidence=[
                        f"Classical W-reversal base: P1 at ₹{l1['price']:.1f} ({l1['date']}) and P3 at ₹{l2['price']:.1f} ({l2['date']}).",
                        f"Intervening peak P2 firmly establishes W neckline at ₹{neckline} ({peak['date']}).",
                        f"Measured move target: ₹{neckline} + ₹{depth:.1f} depth = ₹{target}.",
                        f"Structure: {market_struct} | Status: {status_text}."
                    ],
                    anchors=anchors,
                    chart_directives=directives,
                    historical_pattern={
                        "pattern": "Double Bottom · W-Reversal Base",
                        "bottom_1": {"date": l1['date'], "price": l1['price']},
                        "intervening_peak_neckline": {"date": peak['date'], "price": neckline},
                        "bottom_2": {"date": l2['date'], "price": l2['price']},
                        "measured_move_target": target,
                        "base_width_days": span
                    },
                    pattern_status=status_text,
                    market_structure=market_struct,
                    trading_signal=signal_text
                ))

        # ── Double Top (M formation) ─────────────────────────────────────────
        # P1 = first swing high
        # P2 = intervening swing low strictly between P1 and P3 (Neckline floor)
        # P3 = second swing high
        for i in range(len(sh) - 1):
            for j in range(i + 1, min(i + 5, len(sh))):
                h1, h2 = sh[i], sh[j]
                span = h2['idx'] - h1['idx']
                if not (10 <= span <= 110):
                    continue

                # Recency gate: P3 (second top) must be within the last 30 bars
                if h2['idx'] < n - 30:
                    continue

                diff = abs(h1['price'] - h2['price'])
                max_peak = max(h1['price'], h2['price'])
                # Tightened peak symmetry: 3.5% (from 4.5%)
                if diff > max(atr * 1.25, max_peak * 0.035):
                    continue

                inter_lows = [l for l in sl if h1['idx'] < l['idx'] < h2['idx']]
                if not inter_lows:
                    sub_lows = df['Low'].values[h1['idx']:h2['idx']+1]
                    if len(sub_lows) > 0:
                        min_val = float(np.min(sub_lows))
                        min_idx = int(np.argmin(sub_lows)) + h1['idx']
                        valley = {"idx": min_idx, "price": min_val, "date": dates[min_idx]}
                    else:
                        continue
                else:
                    valley = min(inter_lows, key=lambda x: x['price'])

                neckline = round(float(valley['price']), 2)
                avg_peak = (h1['price'] + h2['price']) / 2.0
                height = avg_peak - neckline
                if height < max(atr * 1.8, avg_peak * 0.05):
                    continue

                breakdown = neckline
                invalidation = round(max_peak + (atr * 0.5), 2)
                target = round(neckline - height, 2)
                rr = round(abs(curr_close - target) / max(abs(invalidation - curr_close), 0.01), 2)

                lifecycle, status_text, signal_text, act_title, conf = self._determine_lifecycle_and_signal(
                    curr_close=curr_close,
                    breakout_level=breakdown,
                    invalidation_level=invalidation,
                    target_price=target,
                    stance="BEARISH",
                    base_action_title="Book Profits / Step Aside",
                    df=df
                )

                sym_score = max(0.2, 1.0 - (diff / max(atr, 0.01)) * 0.4)
                quality = round(min(0.98, sym_score * 0.6 + min(height / (atr * 3.0), 1.0) * 0.3), 2)

                anchors = [
                    PatternAnchor("Peak 1 (P1)", h1['date'], h1['price'], h1['idx']),
                    PatternAnchor("Intervening Valley (P2 Neckline)", valley['date'], neckline, valley['idx']),
                    PatternAnchor("Peak 2 (P3)", h2['date'], h2['price'], h2['idx'])
                ]

                directives = {
                    "pattern_family": "DOUBLE_TOP",
                    "trendlines": [
                        {
                            "type": "neckline",
                            "p1": {"date": valley['date'], "price": neckline},
                            "p2": {"date": dates[-1], "price": neckline},
                            "color": "#EF4444",
                            "label": f"M Neckline (Intervening Floor) ₹{neckline:.1f}"
                        },
                        {
                            "type": "resistance",
                            "p1": {"date": h1['date'], "price": h1['price']},
                            "p2": {"date": h2['date'], "price": h2['price']},
                            "color": "#EF4444",
                            "label": f"Top Resistance Ceiling ₹{max_peak:.1f}"
                        }
                    ],
                    "horizontal_levels": [
                        {"price": breakdown, "color": "#EF4444", "style": "dashed", "label": "Neckline Floor"},
                        {"price": target, "color": "#EF4444", "style": "dotted", "label": "Downside Target"},
                        {"price": invalidation, "color": "#3B82F6", "style": "dotted", "label": "Invalidation Ceiling"}
                    ],
                    "annotation_box": f"Double Top · Span {span} bars · Height ₹{height:.1f}",
                    "market_structure": market_struct,
                    "trading_signal": signal_text
                }

                results.append(DNAPatternResult(
                    pattern_type="DOUBLE_TOP",
                    pattern_name="Double Top · M-Distribution Top",
                    stance="BEARISH",
                    action_title=act_title,
                    lifecycle_state=lifecycle,
                    confidence=conf,
                    quality_score=quality,
                    breakout_level=breakdown,
                    invalidation_level=invalidation,
                    target_price=target,
                    risk_reward_ratio=rr,
                    raw_geometry={
                        "p1_peak1": h1['price'],
                        "p2_intervening_valley": neckline,
                        "p3_peak2": h2['price'],
                        "pattern_height": round(height, 2),
                        "top_span_bars": span
                    },
                    normalized_geometry={
                        "height_atr": round(height / atr, 2),
                        "peak_diff_pct": round((diff / max_peak) * 100, 2)
                    },
                    evidence=[
                        f"Double Top distribution: P1 at ₹{h1['price']:.1f} and P3 at ₹{h2['price']:.1f}.",
                        f"Intervening valley P2 establishes M neckline floor at ₹{neckline}.",
                        f"Downside measured target ₹{target}.",
                        f"Structure: {market_struct} | Status: {status_text}."
                    ],
                    anchors=anchors,
                    chart_directives=directives,
                    historical_pattern={
                        "pattern": "Double Top · M-Distribution Top",
                        "peak_1": {"date": h1['date'], "price": h1['price']},
                        "intervening_valley_neckline": {"date": valley['date'], "price": neckline},
                        "peak_2": {"date": h2['date'], "price": h2['price']},
                        "measured_downside_target": target,
                        "span_bars": span
                    },
                    pattern_status=status_text,
                    market_structure=market_struct,
                    trading_signal=signal_text
                ))
        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 7. Bull Flag
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_bull_flags(self, df: pd.DataFrame, sh: List[Dict], sl: List[Dict], atr: float, n: int, curr_close: float, dates: List[str]) -> List[DNAPatternResult]:
        results = []
        closes = df['Close'].values
        for i in range(15, n - 2):
            pole_start_idx = max(0, i - 10)
            pole_height = closes[i] - closes[pole_start_idx]
            if pole_height / atr >= 3.0 and pole_height / closes[pole_start_idx] >= 0.05:
                flag_len = (n - 1) - i
                if 3 <= flag_len <= 18:
                    flag_highs = df['High'].iloc[i:].values
                    flag_lows = df['Low'].iloc[i:].values
                    flag_retrace = max(flag_highs) - min(flag_lows)
                    retrace_ratio = flag_retrace / pole_height

                    if retrace_ratio <= 0.52:
                        upper_bound = float(np.max(flag_highs[:-1])) if len(flag_highs) > 1 else float(flag_highs[0])
                        lower_bound = float(np.min(flag_lows))
                        breakout_level = round(upper_bound, 2)
                        invalidation_level = round(lower_bound - (atr * 0.4), 2)
                        target_price = round(breakout_level + pole_height, 2)
                        rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

                        state = "CONFIRMED" if curr_close > breakout_level else ("NEAR_CONFIRMATION" if curr_close >= breakout_level * 0.988 else "FORMING")
                        results.append(DNAPatternResult(
                            pattern_type="BULL_FLAG",
                            pattern_name="Bull Flag · High Momentum Consolidation",
                            stance="BULLISH",
                            action_title="Buy Flag Breakout",
                            lifecycle_state=state,
                            confidence=0.88 if state == "CONFIRMED" else 0.72,
                            quality_score=round(max(0.2, 1.0 - retrace_ratio * 1.2), 2),
                            breakout_level=breakout_level,
                            invalidation_level=invalidation_level,
                            target_price=target_price,
                            risk_reward_ratio=rr,
                            raw_geometry={"pole_height": round(pole_height, 2), "flag_len": flag_len},
                            normalized_geometry={"pole_atr": round(pole_height / atr, 2), "retrace_ratio": round(retrace_ratio, 3)},
                            evidence=[f"Explosive impulse flagpole gaining ₹{pole_height:.1f} ({pole_height/atr:.1f}x ATR).", f"Orderly shallow flag consolidation retracing {retrace_ratio*100:.1f}%."],
                            anchors=[PatternAnchor("Pole Start", dates[pole_start_idx], closes[pole_start_idx], pole_start_idx), PatternAnchor("Pole Peak", dates[i], closes[i], i)],
                            chart_directives={
                                "pattern_family": "FLAG",
                                "trendlines": [
                                    {"type": "resistance", "p1": {"date": dates[i], "price": upper_bound}, "p2": {"date": dates[-1], "price": upper_bound}, "color": "#10B981", "label": "Flag Upper Boundary"}
                                ]
                            }
                        ))
        return results

    # ──────────────────────────────────────────────────────────────────────────
    # 8. Multi-Year Generational Base (3-5Y Accumulation Saucer & Flat Base)
    # ──────────────────────────────────────────────────────────────────────────
    def _detect_multi_year_saucer(
        self,
        df: pd.DataFrame,
        sh: List[Dict],
        sl: List[Dict],
        atr: float,
        n: int,
        curr_close: float,
        dates: List[str],
        is_weekly: bool = False
    ) -> List[DNAPatternResult]:
        """
        Detects 2 to 5-Year Generational Accumulation Saucer / Flat Base Breakouts.
        - For Monthly: looks for bases lasting 24 to 60+ months (2-5 years).
        - For Weekly: looks for multi-quarter bases lasting 40 to 104+ weeks (1-2 years).
        """
        results = []
        min_bars_base = 25 if is_weekly else 16
        if n < min_bars_base:
            return results

        # Find candidate multi-year ceiling established earlier in history
        min_age = 20 if is_weekly else 14
        past_highs = [h for h in sh if (n - 1) - h['idx'] >= min_age]
        if not past_highs:
            half_n = max(1, n // 2)
            max_idx = int(df['High'].iloc[:half_n].idxmax())
            past_highs = [{'price': float(df['High'].iloc[max_idx]), 'idx': max_idx, 'date': dates[max_idx]}]

        for h in past_highs:
            ceiling_price = h['price']
            ceiling_idx = h['idx']
            bars_since = (n - 1) - ceiling_idx
            if bars_since < min_age:
                continue

            sub_lows = df['Low'].iloc[ceiling_idx:n].values
            if len(sub_lows) == 0:
                continue
            base_floor = float(np.min(sub_lows))
            base_floor_idx = ceiling_idx + int(np.argmin(sub_lows))
            base_depth = ceiling_price - base_floor
            depth_pct = base_depth / ceiling_price

            # Valid multi-year base: depth between 15% and 68%
            if not (0.15 <= depth_pct <= 0.68):
                continue

            # Floor must not be right at the end (must have had time to round out)
            if (n - 1) - base_floor_idx < (8 if is_weekly else 3):
                continue

            breakout_level = round(ceiling_price, 2)
            invalidation_level = round(ceiling_price - (base_depth * 0.35), 2)
            target_price = round(ceiling_price + (base_depth * 0.618), 2)
            rr = round(abs(target_price - curr_close) / max(abs(curr_close - invalidation_level), 0.01), 2)

            post_breakout_high = float(df['High'].iloc[ceiling_idx:].max())
            if post_breakout_high >= target_price or curr_close >= target_price:
                state = "TARGET_ACHIEVED"
                conf = 0.95
            elif curr_close > breakout_level:
                state = "CONFIRMED"
                conf = 0.92
            elif curr_close >= breakout_level * 0.96:
                state = "NEAR_CONFIRMATION"
                conf = 0.85
            elif curr_close < invalidation_level:
                state = "INVALIDATED"
                conf = 0.20
            else:
                state = "FORMING"
                conf = 0.75

            duration_years = round((bars_since * 7 / 365) if is_weekly else (bars_since * 30.4 / 365), 1)
            stance = "NEUTRAL" if state in ("TARGET_ACHIEVED", "INVALIDATED") else "BULLISH"
            action_title = (
                "Target Achieved · Profit Booked" if state == "TARGET_ACHIEVED" else (
                    "Buy Multi-Year Breakout" if state in ("CONFIRMED", "NEAR_CONFIRMATION") else "Watch Multi-Year Ceiling"
                )
            )

            directives = {
                "pattern_family": "MULTI_YEAR_BASE",
                "trendlines": [
                    {
                        "type": "resistance",
                        "p1": {"date": h['date'], "price": ceiling_price},
                        "p2": {"date": dates[-1], "price": ceiling_price},
                        "color": "#F59E0B",
                        "label": f"{duration_years}Y Base Ceiling (₹{ceiling_price})"
                    }
                ],
                "horizontal_levels": [
                    {"price": breakout_level, "color": "#F59E0B", "style": "solid", "label": f"{duration_years}-Year Breakout Pivot"},
                    {"price": target_price, "color": "#10B981", "style": "dotted", "label": "Measured 1.618 Target"},
                    {"price": invalidation_level, "color": "#EF4444", "style": "dotted", "label": "Base Floor Invalidation"}
                ],
                "annotation_box": f"Multi-Year Generational Base ({duration_years}Y) · Pivot ₹{ceiling_price}"
            }

            results.append(DNAPatternResult(
                pattern_type="MULTI_YEAR_BASE",
                pattern_name=f"Multi-Year Base · {duration_years}Y Accumulation Breakout",
                stance=stance,
                action_title=action_title,
                lifecycle_state=state,
                confidence=conf,
                quality_score=round(min(0.98, 0.70 + (duration_years * 0.05)), 2),
                breakout_level=breakout_level,
                invalidation_level=invalidation_level,
                target_price=target_price,
                risk_reward_ratio=rr,
                raw_geometry={"ceiling": ceiling_price, "floor": base_floor, "depth_pct": round(depth_pct * 100, 1), "bars_since": bars_since, "duration_years": duration_years},
                normalized_geometry={"duration_years": duration_years, "base_depth_atr": round(base_depth / atr, 2)},
                evidence=[
                    f"Massive {duration_years}-year secular accumulation base spanning {bars_since} {'weeks' if is_weekly else 'months'}.",
                    f"Prior multi-year ceiling at ₹{ceiling_price:.1f} tested after {depth_pct*100:.1f}% rounding correction to ₹{base_floor:.1f}.",
                    f"Measured 1.618 Fibonacci expansion projecting long-term target at ₹{target_price}."
                ],
                anchors=[
                    PatternAnchor(f"{duration_years}Y Ceiling", h['date'], ceiling_price, ceiling_idx),
                    PatternAnchor("Base Floor", dates[base_floor_idx], base_floor, base_floor_idx)
                ],
                chart_directives=directives
            ))
            break

        return results

    # ──────────────────────────────────────────────────────────────────────────
    # Multi-Timeframe Scan Entry Point
    # ──────────────────────────────────────────────────────────────────────────
    def analyze_multi_timeframe(self, df: pd.DataFrame) -> Dict[str, Optional[DNAPatternResult]]:
        """
        Executes pattern scans simultaneously across DAILY, WEEKLY, and MONTHLY bars.
        """
        return {
            "DAILY": self.analyze(df, timeframe="DAILY"),
            "WEEKLY": self.analyze(df, timeframe="WEEKLY"),
            "MONTHLY": self.analyze(df, timeframe="MONTHLY")
        }
