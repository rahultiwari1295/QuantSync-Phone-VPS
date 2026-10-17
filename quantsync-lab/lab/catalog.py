"""Single source of truth for what users can build rules from (also served as catalog.json)."""
from __future__ import annotations

from . import FSV
from .facts import FACT_FEATURES
from .signals import SIGNALS

F = lambda key, label, group, kind="number", unit="": {"key": key, "label": label, "group": group, "kind": kind, "unit": unit}

FEATURES: list[dict] = [
    F("close", "Close", "Price", unit="₹"), F("open", "Open", "Price", unit="₹"),
    F("high", "High", "Price", unit="₹"), F("low", "Low", "Price", unit="₹"),
    F("volume", "Volume", "Volume"), F("turnover", "Traded value", "Volume", unit="₹"),
    F("vol_sma_20", "Avg volume (20)", "Volume"), F("vol_ratio", "Volume vs 20-bar avg", "Volume", unit="x"), F("vol_surge", "Volume vs prior 20-bar avg", "Volume", unit="x"),
    F("ret_1", "Change 1 bar", "Momentum", unit="%"), F("ret_5", "Change 5 bars", "Momentum", unit="%"),
    F("ret_20", "Change 20 bars", "Momentum", unit="%"),
    F("rsi_14", "RSI (14)", "Momentum"), F("rsi_ema_10", "RSI (14) · EMA 10", "Momentum"), F("rsi_sma_10", "RSI (14) · SMA 10", "Momentum"),
    F("cci_20", "CCI (20)", "Momentum"), F("stoch_k", "Stochastic %K (14)", "Momentum"),
    F("macd", "MACD", "Momentum"), F("macd_signal", "MACD signal", "Momentum"), F("macd_hist", "MACD histogram", "Momentum"),
    F("adx_14", "ADX (14)", "Trend"), F("pdi_14", "+DI (14)", "Trend"), F("ndi_14", "-DI (14)", "Trend"),
    *[F(f"sma_{n}", f"SMA {n}", "Moving averages", unit="₹") for n in (10, 20, 50, 100, 150, 200)],
    *[F(f"ema_{n}", f"EMA {n}", "Moving averages", unit="₹") for n in (9, 10, 20, 21, 50, 100, 200)],
    F("dist_sma_50_pct", "Distance from SMA 50", "Trend", unit="%"), F("dist_sma_200_pct", "Distance from SMA 200", "Trend", unit="%"),
    F("atr_14", "ATR (14)", "Volatility", unit="₹"), F("atr_pct", "ATR % of price", "Volatility", unit="%"),
    F("bb_upper", "Bollinger upper", "Volatility", unit="₹"), F("bb_lower", "Bollinger lower", "Volatility", unit="₹"),
    F("bb_pct", "Bollinger %B", "Volatility"), F("bb_width_pct", "Bollinger width", "Volatility", unit="%"), F("range_pct", "Bar range", "Volatility", unit="%"),
    F("hh_20", "Prior 20-bar high", "Levels", unit="₹"), F("ll_20", "Prior 20-bar low", "Levels", unit="₹"),
    F("hh_52", "Prior 52-wk high", "Levels", unit="₹"), F("ll_52", "Prior 52-wk low", "Levels", unit="₹"),
    F("dist_hh52_pct", "Distance from 52-wk high", "Levels", unit="%"), F("dist_hh20_pct", "Distance from prior 20-bar high", "Levels", unit="%"),
    F("gap_pct", "Gap vs prior close", "Candle", unit="%"), F("body_pct", "Candle body", "Candle", unit="%"),
    F("close_loc", "Close within bar range", "Candle", unit="%"), F("upper_wick_pct", "Upper wick", "Candle", unit="%"),
    F("is_green", "Green candle", "Candle", kind="bool"), F("is_inside_bar", "Inside bar", "Candle", kind="bool"),
    F("ret_60", "Change 60 bars", "Momentum", unit="%"), F("ret_120", "Change 120 bars", "Momentum", unit="%"), F("ret_250", "Change 250 bars", "Momentum", unit="%"),
    F("ath", "All-time high", "Levels", unit="₹"), F("dist_ath_pct", "Distance from all-time high", "Levels", unit="%"),
    F("dist_ll52_pct", "Distance above 52-wk low", "Levels", unit="%"), F("dist_ema_20_pct", "Distance from EMA 20", "Trend", unit="%"),
    F("sma_50_slope", "SMA 50 slope (10 bars)", "Trend", unit="%"), F("sma_200_slope", "SMA 200 slope (20 bars)", "Trend", unit="%"),
    F("vol_sma_50", "Avg volume (50)", "Volume"), F("vol_ratio_50", "Volume vs 50-bar avg", "Volume", unit="x"),
    F("up_streak", "Up days in a row", "Candle"), F("down_streak", "Down days in a row", "Candle"),
    F("is_nr7", "Narrowest range of 7 bars", "Candle", kind="bool"),
    *[F(k, label, "QuantSync signals", kind="bool") for k, label in SIGNALS.items()],
    *FACT_FEATURES,
]
FEATURE_KEYS = {f["key"] for f in FEATURES}
BOOL_KEYS = {f["key"] for f in FEATURES if f["kind"] == "bool"}
STRING_KEYS = {"symbol"}
# columns the model may use as inputs (scale-free ones only; raw price levels are excluded on purpose)
MODEL_FEATURES = [
    "ret_1", "ret_5", "ret_20", "rsi_14", "stoch_k", "macd_hist", "adx_14", "vol_ratio", "atr_pct", "bb_pct",
    "range_pct", "dist_hh52_pct", "dist_sma_50_pct", "dist_sma_200_pct", "gap_pct", "body_pct",
]
OPERATORS = [
    {"op": ">", "label": "is above"}, {"op": ">=", "label": "is at least"}, {"op": "<", "label": "is below"},
    {"op": "<=", "label": "is at most"}, {"op": "==", "label": "equals"}, {"op": "!=", "label": "is not"},
    {"op": "between", "label": "is between"}, {"op": "crosses_above", "label": "crosses above"},
    {"op": "crosses_below", "label": "crosses below"}, {"op": "is_true", "label": "is true"}, {"op": "in", "label": "is one of"},
]
EXIT_TYPES = [
    {"type": "stop_pct", "label": "Stop at % below entry", "unit": "%"},
    {"type": "target_pct", "label": "Target at % above entry", "unit": "%"},
    {"type": "atr_stop", "label": "Stop at ATR multiple", "unit": "x ATR"},
    {"type": "atr_target", "label": "Target at ATR multiple", "unit": "x ATR"},
    {"type": "trail_atr", "label": "Trail by ATR multiple", "unit": "x ATR"},
    {"type": "trail_pct", "label": "Trail by %", "unit": "%"},
    {"type": "time", "label": "Exit after N bars", "unit": "bars"},
    {"type": "rule", "label": "Exit when a rule is true", "unit": ""},
]


def catalog() -> dict:
    return {"fsv": FSV, "features": FEATURES, "operators": OPERATORS, "exits": EXIT_TYPES,
            "timeframes": ["1D", "1W"], "limits": {"max_conditions": 12, "max_depth": 3, "max_lag": 20}}
