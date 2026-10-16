#!/usr/bin/env python3
"""
trade_enhancer_models.py
------------------------
QuantSync V4 Multi-Task Trade Enhancement Models.

Builds and serves 3 institutional ML models:
1. P(Win) Calibrated Classifier: Probability that trade hits +7% target before -8% stop loss.
2. Dynamic Upside Regressor (Quantile MFE at 75th percentile): Predicts realistic profit ceiling.
3. Dynamic Drawdown Regressor (Quantile MAE at 10th percentile): Predicts maximum noise floor.
4. Trade Decision & Kelly Optimizer: Calculates EV, Risk-Reward, Dynamic Target/Stop, and Position Size.
"""

import os
import sys
import pickle
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VPS_DATA_DIR = os.path.abspath(os.path.join(MODULE_DIR, "..", ".."))

MODEL_PWIN_PATH = os.path.join(VPS_DATA_DIR, "models", "v4_pwin_classifier.pkl")
MODEL_MFE_PATH = os.path.join(VPS_DATA_DIR, "models", "v4_mfe_regressor.pkl")
MODEL_MAE_PATH = os.path.join(VPS_DATA_DIR, "models", "v4_mae_regressor.pkl")

# Clean, strictly lookahead-free features
FEATURE_COLS = [
    # Microstructure
    "bar_return_pct", "close_in_range", "upper_wick_pct", "lower_tail_pct",
    "body_to_range_ratio", "gap_up_pct", "turnover_cr",
    # Volume dynamics
    "vol_ratio_20d", "vol_ratio_5d", "vol_ratio_60d", "vol_trend_5d",
    "vol_acceleration", "dryup_bars_in_10",
    # Trend & Oscillators
    "rsi_14", "rsi_trend_5d", "atr_14_pct",
    "dist_ema20_pct", "dist_ema50_pct", "dist_ema200_pct",
    "dist_52w_high_pct", "dist_52w_low_pct",
    "prior_60d_return_pct", "prior_120d_return_pct", "stage_2_confirmed",
    # Pattern DNA
    "has_classical_pattern", "pattern_confidence", "pattern_quality_score",
    "pattern_rr_ratio", "pattern_breakout_dist_pct", "pattern_inval_dist_pct",
    "pattern_target_dist_pct", "pattern_is_bullish", "competing_pattern_count",
    # Champion Breakout
    "has_champ_pattern", "champ_cir", "champ_vol_ratio", "champ_upper_wick",
    "champ_risk_pct", "is_double_confluence",
    # Fibonacci
    "fib_has_structure", "fib_impulse_pct", "fib_pullback_pct",
    "fib_dist_1000_pct", "fib_dist_1272_pct", "fib_dist_1618_pct", "fib_structure_age_weeks",
    # Macro & Regime
    "nifty_dist_ema20", "nifty_dist_ema50", "nifty_dist_ema200",
    "nifty_ret_5d", "nifty_ret_10d", "nifty_ret_20d", "nifty_rsi14", "nifty_atr_pct",
    "midcap_dist_ema50", "dual_index_bull",
    # Seasonality
    "month_of_year", "day_of_week"
]

CATEGORICAL_COLS = [
    "sector", "ema_alignment", "pattern_type", "pattern_lifecycle",
    "champ_pattern", "nifty_regime", "midcap_regime", "quarter"
]


class TradeEnhancerEngine:
    def __init__(
        self,
        pwin_path: str = MODEL_PWIN_PATH,
        mfe_path: str = MODEL_MFE_PATH,
        mae_path: str = MODEL_MAE_PATH
    ):
        self.pwin_path = pwin_path
        self.mfe_path = mfe_path
        self.mae_path = mae_path
        self.pwin_bundle: Optional[Dict] = None
        self.mfe_bundle: Optional[Dict] = None
        self.mae_bundle: Optional[Dict] = None
        self._is_loaded = False

    def load_models(self) -> bool:
        """Load trained models and category encoders."""
        if not (os.path.exists(self.pwin_path) and os.path.exists(self.mfe_path) and os.path.exists(self.mae_path)):
            print(f"[WARN] Trade enhancement models not yet trained.")
            return False

        try:
            with open(self.pwin_path, "rb") as f:
                self.pwin_bundle = pickle.load(f)
            with open(self.mfe_path, "rb") as f:
                self.mfe_bundle = pickle.load(f)
            with open(self.mae_path, "rb") as f:
                self.mae_bundle = pickle.load(f)
            self._is_loaded = True
            print("[+] Loaded all 3 Trade Enhancement Models (P_Win, MFE Quantile, MAE Quantile).")
            return True
        except Exception as e:
            print(f"[ERROR] Failed loading models: {e}")
            return False

    def _prepare_feature_row(self, raw_features: Dict, cat_maps: Dict) -> pd.DataFrame:
        """Encodes features into model-ready single-row DataFrame."""
        row = {}
        for col in FEATURE_COLS:
            val = raw_features.get(col, 0.0)
            row[col] = float(val) if val is not None else 0.0

        for col in CATEGORICAL_COLS:
            raw_val = str(raw_features.get(col, "Unknown"))
            mapping = cat_maps.get(col, {})
            code = mapping.get(raw_val, -1)
            row[col] = code

        return pd.DataFrame([row])

    def enhance_trade(self, raw_features: Dict, entry_price: float) -> Dict:
        """
        Enhances a candidate trade setup by computing:
        - Win Probability P(Win)
        - Dynamic Target % & Price (based on predicted MFE)
        - Dynamic Stop Loss % & Price (based on predicted MAE)
        - Expected Value (EV)
        - Half-Kelly Position Sizing Multiplier
        - AI Conviction Grade
        """
        if not self._is_loaded:
            if not self.load_models():
                # Safe baseline heuristics if models are not loaded
                return {
                    "ml_ready": False,
                    "p_win": 0.50,
                    "target_pct": 7.0,
                    "target_price": round(entry_price * 1.07, 2),
                    "stop_loss_pct": 5.0,
                    "stop_loss_price": round(entry_price * 0.95, 2),
                    "expected_value_pct": 1.0,
                    "risk_reward_ratio": 1.4,
                    "size_multiplier": 1.0,
                    "conviction_grade": "NEUTRAL",
                }

        cat_maps = self.pwin_bundle.get("cat_maps", {})
        X = self._prepare_feature_row(raw_features, cat_maps)

        # 1. Predict Win Probability
        model_pwin = self.pwin_bundle["model"]
        p_win = float(model_pwin.predict_proba(X)[0][1])

        # 2. Predict Dynamic Target (MFE 75th percentile)
        model_mfe = self.mfe_bundle["model"]
        pred_mfe = float(model_mfe.predict(X)[0])
        # Guardrails: Min target +5.0%, Max realistic target +25.0%
        target_pct = float(np.clip(pred_mfe, 5.0, 25.0))

        # 3. Predict Dynamic Stop Loss (MAE 10th percentile - noise floor)
        model_mae = self.mae_bundle["model"]
        pred_mae = float(model_mae.predict(X)[0])  # typically negative (e.g. -4.5%)
        # Guardrails: Tightest stop -3.5%, Widest stop -8.5%
        stop_pct = float(np.clip(abs(pred_mae), 3.5, 8.5))

        # 4. Target & Stop Prices
        target_price = round(entry_price * (1.0 + target_pct / 100.0), 2)
        stop_loss_price = round(entry_price * (1.0 - stop_pct / 100.0), 2)

        # 5. Risk-to-Reward Ratio & Expected Value
        rr_ratio = round(target_pct / stop_pct, 2)
        p_loss = 1.0 - p_win
        ev_pct = round((p_win * target_pct) - (p_loss * stop_pct), 2)

        # 6. Half-Kelly Bet Sizing
        b = target_pct / (stop_pct + 1e-9)
        kelly_fraction = (p_win * b - p_loss) / (b + 1e-9)
        half_kelly = 0.5 * kelly_fraction
        # Sizing multiplier between 0.25x and 2.0x
        size_multiplier = round(float(np.clip(1.0 + half_kelly, 0.25, 2.0)), 2)

        # 7. AI Conviction Grade
        if ev_pct >= 4.5 and p_win >= 0.58 and rr_ratio >= 1.6:
            conviction_grade = "ELITE"
        elif ev_pct >= 2.0 and p_win >= 0.52 and rr_ratio >= 1.3:
            conviction_grade = "STRONG"
        elif ev_pct >= 0.0:
            conviction_grade = "NEUTRAL"
        else:
            conviction_grade = "AVOID"

        return {
            "ml_ready": True,
            "p_win": round(p_win, 3),
            "p_win_pct": f"{round(p_win * 100, 1)}%",
            "target_pct": round(target_pct, 1),
            "target_price": target_price,
            "stop_loss_pct": round(stop_pct, 1),
            "stop_loss_price": stop_loss_price,
            "risk_reward_ratio": rr_ratio,
            "expected_value_pct": ev_pct,
            "size_multiplier": size_multiplier,
            "conviction_grade": conviction_grade,
        }
