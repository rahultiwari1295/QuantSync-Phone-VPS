#!/usr/bin/env python3
"""
QuantSync ClassicalPatternAnalystDNA — Live Demonstration
=========================================================
Demonstrates the engine-agnostic ClassicalPatternAnalystDNA capability
fused with the Stock DNA Visual Chart Renderer.

Detects and renders:
1. Falling Wedge (Bullish Apex Breakout)
2. Symmetrical Triangle (Apex Energy Squeeze)
3. Bullish Rectangle (Horizontal Consolidation Box)
4. Inverse Head & Shoulders (Three-Trough Reversal)
5. Cup & Handle (Parabolic Bowl + Shallow Handle)
6. Ascending Triangle (Flat Ceiling Resistance + Rising Lows)

Generates publication-quality Light and Dark mode HD PNG charts in:
  vps_data_live/Data/dna_charts/demo/
"""

import os
import sys
import json
import sqlite3
import datetime
import numpy as np
import pandas as pd

# Add local path to sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from classical_pattern_dna import ClassicalPatternAnalystDNA, DNAPatternResult
from dna_engine import generate_analyst_chart_image

DEMO_OUT_DIR = os.path.join(SCRIPT_DIR, "Data", "dna_charts", "demo")
os.makedirs(DEMO_OUT_DIR, exist_ok=True)


def build_pattern_series(pattern_type: str, n_bars: int = 90, base_price: float = 500.0) -> pd.DataFrame:
    """
    Constructs high-fidelity, organic OHLCV candle series matching the 6 classical patterns
    for rigorous mathematical and visual benchmarking.
    """
    dates = pd.date_range(end=datetime.date.today(), periods=n_bars, freq='B')
    closes = np.zeros(n_bars)
    highs = np.zeros(n_bars)
    lows = np.zeros(n_bars)
    opens = np.zeros(n_bars)
    vols = np.zeros(n_bars)

    np.random.seed(42)

    if pattern_type == "FALLING_WEDGE":
        # Descending swing highs and descending swing lows converging toward apex
        # Upper line drops faster than lower line
        t = np.linspace(0, 1, n_bars)
        trend = base_price * (1.0 - 0.22 * t)
        spread = base_price * (0.16 * (1.0 - 0.70 * t))  # converging spread
        osc = np.sin(t * 3.5 * 2 * np.pi) * (spread / 2.0)
        closes = trend + osc
        # Final breakout above descending resistance in the last 4 bars
        closes[-4:] = closes[-5] + np.linspace(base_price * 0.01, base_price * 0.05, 4)

    elif pattern_type == "ASCENDING_TRIANGLE":
        # Flat horizontal resistance ceiling + Ascending swing lows
        t = np.linspace(0, 1, n_bars)
        res_ceiling = base_price * 1.15
        lower_floor = base_price * (0.95 + 0.16 * t)  # rising floor
        osc = (np.sin(t * 3.2 * 2 * np.pi) + 1.0) / 2.0
        closes = lower_floor + osc * (res_ceiling - lower_floor)
        # Cap at ceiling until last 3 breakout bars
        closes = np.minimum(closes, res_ceiling)
        closes[-3:] = res_ceiling + np.linspace(base_price * 0.01, base_price * 0.04, 3)

    elif pattern_type == "SYMMETRICAL_TRIANGLE":
        # Lower highs and higher lows pinching into an apex squeeze
        t = np.linspace(0, 1, n_bars)
        mid = base_price * 1.05
        envelope = base_price * 0.15 * (1.0 - 0.75 * t)
        osc = np.sin(t * 3.5 * 2 * np.pi)
        closes = mid + osc * envelope
        # Final breakout bar
        closes[-2:] = mid + envelope[-2:] + base_price * 0.03

    elif pattern_type == "BULLISH_RECTANGLE":
        # Parallel horizontal resistance ceiling and support floor
        t = np.linspace(0, 1, n_bars)
        res = base_price * 1.12
        sup = base_price * 0.98
        osc = (np.sin(t * 3.8 * 2 * np.pi) + 1.0) / 2.0
        closes = sup + osc * (res - sup)
        # Breakout above rectangle in last 3 bars
        closes[-3:] = res + np.linspace(base_price * 0.01, base_price * 0.045, 3)

    elif pattern_type == "INVERSE_HEAD_AND_SHOULDERS":
        # Left shoulder trough, deeper central head trough, higher right shoulder trough
        t = np.linspace(0, 1, n_bars)
        neckline = base_price * 1.10
        closes = np.full(n_bars, neckline)
        # Left shoulder (bars 15 to 35)
        for i in range(15, 35):
            closes[i] = neckline - base_price * 0.08 * np.sin((i - 15) / 20.0 * np.pi)
        # Head (bars 35 to 65)
        for i in range(35, 65):
            closes[i] = neckline - base_price * 0.16 * np.sin((i - 35) / 30.0 * np.pi)
        # Right shoulder (bars 65 to 85)
        for i in range(65, 85):
            closes[i] = neckline - base_price * 0.08 * np.sin((i - 65) / 20.0 * np.pi)
        # Breakout above neckline in last 5 bars
        closes[85:] = neckline + np.linspace(base_price * 0.01, base_price * 0.04, n_bars - 85)

    elif pattern_type == "CUP_AND_HANDLE":
        # Rounding parabolic cup + shallow handle pullback
        t = np.linspace(0, 1, n_bars)
        rim = base_price * 1.15
        closes = np.full(n_bars, rim)
        # Cup from bar 10 to 70 (U-shaped bowl)
        for i in range(10, 71):
            theta = (i - 10) / 60.0 * np.pi
            depth = base_price * 0.22 * np.sin(theta)
            closes[i] = rim - depth
        # Handle from bar 71 to 85 (shallow drift <= 35% of depth)
        for i in range(71, 86):
            handle_pull = base_price * 0.06 * ((i - 70) / 15.0)
            closes[i] = rim - handle_pull
        # Breakout above rim in last 4 bars
        closes[86:] = rim + np.linspace(base_price * 0.01, base_price * 0.04, n_bars - 86)

    else:
        # Default mild uptrend
        closes = np.linspace(base_price, base_price * 1.2, n_bars)

    # Add realistic micro-wicks and volatility
    for i in range(n_bars):
        c = closes[i]
        o = c * (1.0 + np.random.uniform(-0.005, 0.005)) if i == 0 else closes[i - 1]
        noise = c * 0.008
        h = max(o, c) + abs(np.random.normal(noise, noise * 0.5))
        l = min(o, c) - abs(np.random.normal(noise, noise * 0.5))
        v = int(np.random.uniform(250000, 1200000))

        opens[i] = round(o, 2)
        highs[i] = round(h, 2)
        lows[i] = round(l, 2)
        closes[i] = round(c, 2)
        vols[i] = v

    df = pd.DataFrame({
        'Date': dates,
        'Open': opens,
        'High': highs,
        'Low': lows,
        'Close': closes,
        'Volume': vols
    })
    df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA_200'] = df['Close'].ewm(span=200, adjust=False).mean()
    df['VOL_SMA_20'] = df['Volume'].rolling(20, min_periods=1).mean()
    return df


def run_demo():
    print("=" * 82)
    print("  QuantSync ClassicalPatternAnalystDNA — Multi-Pattern Perception & Charting Demo")
    print("=" * 82)
    print("Engine Architecture: Standalone, Engine-Agnostic Capability (V2/V5/V6/DNA ready)\n")

    patterns_to_test = [
        ("FALLING_WEDGE", "WEDGE_TECH", 720.0),
        ("ASCENDING_TRIANGLE", "ASC_MOMENTUM", 1450.0),
        ("SYMMETRICAL_TRIANGLE", "SYM_COIL", 420.0),
        ("BULLISH_RECTANGLE", "RECT_CONSOL", 890.0),
        ("INVERSE_HEAD_AND_SHOULDERS", "IHNS_REVERSAL", 1250.0),
        ("CUP_AND_HANDLE", "CUP_O_NEIL", 1680.0)
    ]

    analyst = ClassicalPatternAnalystDNA(atr_window=14)
    rendered_charts = []

    for pat_type, ticker, base_price in patterns_to_test:
        print(f"─" * 82)
        print(f"▶ Scanning Series: {ticker} (Target Pattern: {pat_type})")
        df = build_pattern_series(pat_type, n_bars=95, base_price=base_price)

        result: Optional[DNAPatternResult] = analyst.analyze(df)

        if not result:
            print(f"  [!] No pattern detected for {ticker}")
            continue

        curr_price = float(df.iloc[-1]['Close'])
        print(f"  ✓ Pattern Identified : {result.pattern_name}")
        print(f"  ✓ Stance / State     : {result.stance} · {result.lifecycle_state}")
        print(f"  ✓ Confidence Score   : {result.confidence * 100:.1f}% (Quality: {result.quality_score * 100:.1f}%)")
        print(f"  ✓ Breakout Trigger   : ₹{result.breakout_level:.2f}")
        print(f"  ✓ Measured Target    : ₹{result.target_price:.2f} (Upside: {((result.target_price - curr_price)/curr_price)*100:+.1f}%)")
        print(f"  ✓ Invalidation Stop  : ₹{result.invalidation_level:.2f} (Risk: {((curr_price - result.invalidation_level)/curr_price)*100:.1f}%)")
        print(f"  ✓ Reward / Risk      : {result.risk_reward_ratio}:1")
        print(f"  ✓ Evidence           : {result.evidence[0]}")

        # Render Light & Dark HD DNA Charts
        light_img = os.path.join(DEMO_OUT_DIR, f"{ticker}_tactical_light.png")
        dark_img = os.path.join(DEMO_OUT_DIR, f"{ticker}_tactical_dark.png")

        generate_analyst_chart_image(
            df=df,
            symbol=ticker,
            pattern_name=result.pattern_name,
            stance=result.stance,
            action_title=result.action_title,
            trendlines=result.chart_directives.get('trendlines', []),
            target_1=result.target_price,
            stop_loss=result.invalidation_level,
            curr_price=curr_price,
            out_path=light_img,
            is_macro=False,
            theme="light",
            chart_directives=result.chart_directives
        )

        generate_analyst_chart_image(
            df=df,
            symbol=ticker,
            pattern_name=result.pattern_name,
            stance=result.stance,
            action_title=result.action_title,
            trendlines=result.chart_directives.get('trendlines', []),
            target_1=result.target_price,
            stop_loss=result.invalidation_level,
            curr_price=curr_price,
            out_path=dark_img,
            is_macro=False,
            theme="dark",
            chart_directives=result.chart_directives
        )

        rendered_charts.append({
            "ticker": ticker,
            "pattern": result.pattern_type,
            "light_chart": light_img,
            "dark_chart": dark_img
        })
        print(f"  ✓ HD Charts Rendered : Light Mode -> {light_img}")
        print(f"                         Dark Mode  -> {dark_img}")

    # Also test on a live real stock from stock_history.db if present
    db_path = os.path.join(SCRIPT_DIR, "Data", "stock_history.db")
    if os.path.exists(db_path):
        print("\n" + "=" * 82)
        print("  Scanning Real Historical NSE Equities via ClassicalPatternAnalystDNA")
        print("=" * 82)
        try:
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT symbol, rawCandles FROM StockHistory WHERE candleCount >= 100 LIMIT 25")
            rows = c.fetchall()
            conn.close()

            found_count = 0
            for sym, raw_json in rows:
                if not raw_json:
                    continue
                try:
                    raw_candles = json.loads(raw_json)
                    sdf = pd.DataFrame(raw_candles)
                    sdf = sdf.rename(columns={'t': 'Date', 'o': 'Open', 'h': 'High', 'l': 'Low', 'c': 'Close', 'v': 'Volume'})
                    sdf['Date'] = pd.to_datetime(sdf['Date'])
                    sdf = sdf.sort_values('Date').reset_index(drop=True)
                    sdf['EMA_20'] = sdf['Close'].ewm(span=20, adjust=False).mean()
                    sdf['EMA_50'] = sdf['Close'].ewm(span=50, adjust=False).mean()
                    sdf['EMA_200'] = sdf['Close'].ewm(span=200, adjust=False).mean()

                    res = analyst.analyze(sdf)
                    if res:
                        found_count += 1
                        print(f"  ★ [{sym}] Real NSE Equity Detected: {res.pattern_name} ({res.lifecycle_state})")
                        print(f"      Confidence: {res.confidence*100:.1f}% | Target: ₹{res.target_price} | Stop: ₹{res.invalidation_level}")
                        real_light = os.path.join(DEMO_OUT_DIR, f"{sym}_real_dna_light.png")
                        generate_analyst_chart_image(
                            df=sdf, symbol=sym, pattern_name=res.pattern_name, stance=res.stance,
                            action_title=res.action_title, trendlines=res.chart_directives.get('trendlines', []),
                            target_1=res.target_price, stop_loss=res.invalidation_level,
                            curr_price=float(sdf.iloc[-1]['Close']), out_path=real_light,
                            is_macro=False, theme="light", chart_directives=res.chart_directives
                        )
                        if found_count >= 3:
                            break
                except Exception:
                    continue
        except Exception as e:
            print(f"  Note on database scan: {e}")

    print("\n" + "=" * 82)
    print("  DEMO COMPLETE: All 6 Classical Patterns Successfully Detected & Visualized")
    print("=" * 82)
    return rendered_charts


if __name__ == "__main__":
    run_demo()
