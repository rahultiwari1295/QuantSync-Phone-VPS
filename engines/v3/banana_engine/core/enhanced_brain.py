"""
banana_engine/core/enhanced_brain.py — Institutional ML Brain & Failure Filter (v5)
===================================================================================
Trained on 49 features:
  - 38 Base Domain Features
  - 6 Smart Money Accumulation Features
  - 5 Cross-Sectional Cascade Momentum Features

Includes:
  1. Primary LightGBM Success Predictor with Isotonic Calibration
  2. Failure Anatomy Negative Filter (predicts high-probability traps)
  3. Plain-language SHAP Phrase Templates for new institutional features
"""

import os
import sys
import json
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, brier_score_loss
import shap

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR
from core.features import FEATURES
from core.brain import PHRASES
from core.enhanced_features import ENHANCED_FEATURES, SMART_MONEY_FEATURES, CASCADE_FEATURES

ENHANCED_DATA_DIR = os.path.join(DATA_DIR, "enhanced_v5")
MODEL_V5_FILE = os.path.join(ENHANCED_DATA_DIR, "model_v5.lgb")
FAILURE_MODEL_FILE = os.path.join(ENHANCED_DATA_DIR, "failure_model.lgb")
CALIB_RAW_FILE = os.path.join(ENHANCED_DATA_DIR, "calib_raw.npy")
CALIB_Y_FILE = os.path.join(ENHANCED_DATA_DIR, "calib_y.npy")
MODEL_CARD_FILE = os.path.join(ENHANCED_DATA_DIR, "model_card_v5.json")

# Enhanced SHAP phrase templates for institutional additions
ENHANCED_PHRASES: Dict[str, Dict[str, Tuple[str, Any]]] = dict(PHRASES)

ENHANCED_PHRASES.update({
    "obv_divergence_score": {
        "bullish": ("Stealth institutional accumulation: OBV rising while price bases.",
                    lambda v: v is not None and not np.isnan(v) and v > 1.0),
        "bearish": ("Stealth distribution: OBV declining despite firm price.",
                    lambda v: v is not None and not np.isnan(v) and v < -1.0),
    },
    "ad_line_curvature": {
        "accelerating": ("Urgent institutional buying: Accumulation/Distribution curve is convex upward.",
                         lambda v: v is not None and not np.isnan(v) and v > 1.5),
    },
    "close_position_drift": {
        "buying_dips": ("Price consistently closes in the upper half of daily range through base.",
                        lambda v: v is not None and not np.isnan(v) and v > 1.0),
    },
    "large_bar_bias": {
        "heavy_buying": ("Top volume days during base were overwhelmingly green (> 60%).",
                         lambda v: v is not None and not np.isnan(v) and v >= 0.6),
        "heavy_selling": ("Top volume days during base were dominated by distribution selling.",
                          lambda v: v is not None and not np.isnan(v) and v <= 0.3),
    },
    "price_volume_divergence": {
        "exhaustion": ("Supply exhaustion: Selling volume dried up completely on down-swings.",
                       lambda v: v is not None and not np.isnan(v) and v > 0.2),
    },
    "accumulation_score": {
        "elite": ("Institutional accumulation footprint is in the top decile (> 70).",
                  lambda v: v is not None and not np.isnan(v) and v >= 70.0),
        "poor": ("Institutional footprint shows lack of stealth sponsorship (< 40).",
                 lambda v: v is not None and not np.isnan(v) and v < 40.0),
    },
    "sector_breakout_count_10d": {
        "strong_cluster": ("Sector momentum wave active: Multiple industry peers broke out recently.",
                           lambda v: v is not None and not np.isnan(v) and v >= 3.0),
        "isolated": ("Lone breakout: No sector peers confirming this move.",
                     lambda v: v is not None and not np.isnan(v) and v == 0.0),
    },
    "cascade_position": {
        "sweet_spot": ("Ideal wave entry: Positioned 2nd to 5th in the sector cascade wave.",
                       lambda v: v is not None and not np.isnan(v) and 2.0 <= v <= 5.0),
        "late": ("Late stage cascade: 6th+ stock in sector to break out, elevated exhaustion risk.",
                 lambda v: v is not None and not np.isnan(v) and v >= 6.0),
    },
    "market_cascade_breadth": {
        "broad": ("Broad market bull participation across 4+ distinct sectors.",
                  lambda v: v is not None and not np.isnan(v) and v >= 4.0),
    },
    "cascade_velocity": {
        "accelerating": ("Sector breakout velocity is accelerating rapidly.",
                         lambda v: v is not None and not np.isnan(v) and v > 0.0),
    }
})


def train_enhanced_models(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    features: List[str] = ENHANCED_FEATURES
) -> Dict[str, Any]:
    """
    Trains:
      1. Primary LightGBM success classifier on 49 features
      2. Failure Mode classifier (predicting immediate rejection)
      3. Fits Isotonic probability calibrator on test fold
    """
    os.makedirs(ENHANCED_DATA_DIR, exist_ok=True)

    tr = train_df.dropna(subset=features + ["label"]).copy()
    te = test_df.dropna(subset=features + ["label"]).copy()

    print(f"[Enhanced Brain] Training set: {len(tr)} samples | Test set: {len(te)} samples")

    # 1. Primary Success Model
    lgb_params = dict(
        objective="binary",
        metric="auc",
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=20,
        max_depth=5,
        min_child_samples=25,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.85,
        reg_alpha=0.2,
        reg_lambda=1.0,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )

    primary_model = lgb.LGBMClassifier(**lgb_params)
    primary_model.fit(
        tr[features], tr["label"]
    )

    raw_test_preds = primary_model.predict_proba(te[features])[:, 1]
    auc_score = float(roc_auc_score(te["label"], raw_test_preds))

    # Fit Isotonic Calibrator
    iso = IsotonicRegression(out_of_bounds="clip").fit(raw_test_preds, te["label"])
    calib_probs = iso.predict(raw_test_preds)
    brier_score = float(brier_score_loss(te["label"], calib_probs))

    # 2. Failure Mode Model (Predicting immediate rejection or false volume)
    # Target: 1 if immediate rejection or false volume, 0 otherwise
    target_col = "fail_immediate_rejection" if "fail_immediate_rejection" in tr.columns else "is_failure"
    
    fail_params = dict(
        objective="binary",
        metric="auc",
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=16,
        max_depth=4,
        min_child_samples=30,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_alpha=0.5,
        reg_lambda=2.0,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )
    failure_model = lgb.LGBMClassifier(**fail_params)
    failure_model.fit(
        tr[features], tr[target_col]
    )
    raw_fail_preds = failure_model.predict_proba(te[features])[:, 1]
    try:
        fail_auc = float(roc_auc_score(te[target_col], raw_fail_preds))
    except Exception:
        fail_auc = 0.50

    # Feature importances
    primary_feat_imp = {f: int(imp) for f, imp in zip(features, primary_model.feature_importances_)}
    sorted_imp = sorted(primary_feat_imp.items(), key=lambda x: -x[1])

    # Save artifacts
    primary_model.booster_.save_model(MODEL_V5_FILE)
    failure_model.booster_.save_model(FAILURE_MODEL_FILE)
    np.save(CALIB_RAW_FILE, getattr(iso, "X_thresholds_", np.array([0.0, 1.0])))
    np.save(CALIB_Y_FILE, getattr(iso, "y_thresholds_", np.array([0.0, 1.0])))

    card = {
        "version": "5.0.0-institutional",
        "training_window": {"train": "2020-01-01 to 2024-12-31", "test": "2025-01-01 to 2026-09"},
        "samples": {"train": len(tr), "test": len(te)},
        "features": features,
        "n_features": len(features),
        "primary_auc": round(auc_score, 4),
        "primary_brier": round(brier_score, 4),
        "failure_auc": round(fail_auc, 4),
        "top_features": sorted_imp[:15]
    }
    with open(MODEL_CARD_FILE, "w") as f:
        json.dump(card, f, indent=2)

    print(f"[Enhanced Brain] Primary AUC: {auc_score:.4f} | Brier: {brier_score:.4f} | Failure Model AUC: {fail_auc:.4f}")
    print(f"[Enhanced Brain] Saved artifacts to {ENHANCED_DATA_DIR}")

    return {
        "primary_model": primary_model,
        "failure_model": failure_model,
        "calibrator": iso,
        "auc": auc_score,
        "brier": brier_score,
        "fail_auc": fail_auc,
        "top_features": sorted_imp[:15]
    }


class EnhancedBrainInference:
    """
    Sub-millisecond inference for v5 Enhanced Engine.
    Combines calibrated success probability with negative failure filter.
    """
    def __init__(self):
        self.primary_booster: Optional[lgb.Booster] = None
        self.failure_booster: Optional[lgb.Booster] = None
        self.raw_thresh: Optional[np.ndarray] = None
        self.y_thresh: Optional[np.ndarray] = None
        self.load()

    def load(self) -> bool:
        if os.path.exists(MODEL_V5_FILE):
            self.primary_booster = lgb.Booster(model_file=MODEL_V5_FILE)
            if os.path.exists(FAILURE_MODEL_FILE):
                self.failure_booster = lgb.Booster(model_file=FAILURE_MODEL_FILE)
            try:
                self.raw_thresh = np.load(CALIB_RAW_FILE)
                self.y_thresh = np.load(CALIB_Y_FILE)
            except Exception:
                self.raw_thresh = np.array([0.0, 1.0])
                self.y_thresh = np.array([0.0, 1.0])
            return True
        return False

    def predict_p_win(self, features_dict: Dict[str, Any]) -> float:
        """Returns calibrated P(win) in [0.05, 0.95]."""
        if self.primary_booster is None:
            return 0.50

        row = [features_dict.get(f, np.nan) for f in ENHANCED_FEATURES]
        X = np.array([row], dtype=float)
        raw_prob = float(self.primary_booster.predict(X)[0])

        if self.raw_thresh is not None and self.y_thresh is not None:
            p_cal = float(np.interp(raw_prob, self.raw_thresh, self.y_thresh))
        else:
            p_cal = raw_prob

        return float(np.clip(p_cal, 0.05, 0.95))

    def predict_p_failure(self, features_dict: Dict[str, Any]) -> float:
        """Returns predicted probability of immediate failure / bull trap."""
        if self.failure_booster is None:
            return 0.20

        row = [features_dict.get(f, np.nan) for f in ENHANCED_FEATURES]
        X = np.array([row], dtype=float)
        p_fail = float(self.failure_booster.predict(X)[0])
        return float(np.clip(p_fail, 0.0, 1.0))

    def is_setup_approved(
        self,
        features_dict: Dict[str, Any],
        max_failure_prob: float = 0.35,
        min_p_win: float = 0.38
    ) -> Tuple[bool, str]:
        """
        Dual-gate filter:
          1. Sufficient P(win)
          2. Passes Failure Anatomy negative filter
        """
        p_win = self.predict_p_win(features_dict)
        p_fail = self.predict_p_failure(features_dict)

        if p_fail > max_failure_prob:
            return False, f"REJECTED: High failure trap risk (P_fail={p_fail:.2f} > {max_failure_prob:.2f})"
        if p_win < min_p_win:
            return False, f"REJECTED: Low win probability (P_win={p_win:.2f} < {min_p_win:.2f})"

        return True, "APPROVED"
