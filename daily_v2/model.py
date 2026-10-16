"""
QuantSync Daily V2 Model Loader & Inference Engine (Frozen Snapshot Version)
----------------------------------------------------------------------------
Loads the frozen XGBoost model artifact and Platt calibrator, verifies feature ordering,
evaluates calibrated probability, and applies negation filter checks.
"""

import os
import json
import pickle
import numpy as np
import xgboost as xgb
from typing import Dict, List, Any, Tuple, Optional
from sklearn.linear_model import LogisticRegression


class RobustPlattCalibrator:
    def __init__(self):
        self.lr = LogisticRegression(C=1.0, solver='liblinear', max_iter=200)

    def fit(self, raw_probs: np.ndarray, y: np.ndarray):
        X = np.clip(raw_probs, 1e-6, 1.0 - 1e-6).reshape(-1, 1)
        self.lr.fit(X, y)
        return self

    def predict_proba(self, raw_probs: np.ndarray) -> np.ndarray:
        X = np.clip(raw_probs, 1e-6, 1.0 - 1e-6).reshape(-1, 1)
        return self.lr.predict_proba(X)[:, 1]


class RobustPlattUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if name == 'RobustPlattCalibrator':
            return RobustPlattCalibrator
        return super().find_class(module, name)


from daily_v2.config import (
    FROZEN_MODEL_PATH,
    FEATURE_NAMES_PATH,
    FINAL_CONFIG_PATH,
    DAILY_V2_ML_THRESHOLD,
    DAILY_CAPACITY_LIMIT,
    NEGATION_RULES,
    DAILY_V2_VERSION
)
from daily_v2.features import FEATURE_NAMES, vector_from_features


class DailyV2ModelManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DailyV2ModelManager, cls).__new__(cls)
            cls._instance._init_model()
        return cls._instance

    def _init_model(self):
        if not os.path.exists(FROZEN_MODEL_PATH):
            raise FileNotFoundError(f"Frozen Daily V2 model not found at {FROZEN_MODEL_PATH}")

        self.model = xgb.XGBClassifier()
        self.model.load_model(FROZEN_MODEL_PATH)
        self.calibrator = None

        with open(FEATURE_NAMES_PATH, 'r') as f:
            self.expected_features = json.load(f)

        if os.path.exists(FINAL_CONFIG_PATH):
            with open(FINAL_CONFIG_PATH, 'r') as f:
                self.final_config = json.load(f)
        else:
            self.final_config = {}

        self.threshold = DAILY_V2_ML_THRESHOLD
        self.capacity_limit = DAILY_CAPACITY_LIMIT
        self.version = DAILY_V2_VERSION

    def check_negation_rules(self, feat_dict: Dict[str, float]) -> Tuple[bool, Optional[str]]:
        """
        Evaluates evidence-based negation failure rules before ML gating.
        """
        atr_pct = feat_dict.get('atr14_pct', 0.0)
        if atr_pct > NEGATION_RULES['MAX_ATR_PCT']:
            return True, f"NEGATION: Hyper-volatility churn (ATR%={atr_pct:.2f}% > {NEGATION_RULES['MAX_ATR_PCT']}%)"

        dist_52w = feat_dict.get('dist_52w_high_pct', 0.0)
        if dist_52w > NEGATION_RULES['MAX_DIST_52W_HIGH_PCT']:
            return True, f"NEGATION: Trapped under heavy overhead supply (Dist52W={dist_52w:.1f}% > {NEGATION_RULES['MAX_DIST_52W_HIGH_PCT']}%)"

        if NEGATION_RULES.get('REQUIRE_NIFTY_EMA50', False):
            nifty_above50 = feat_dict.get('nifty_above_ema50', 1.0)
            if nifty_above50 == 0.0:
                return True, "NEGATION: Severe market regime breakdown (Nifty under 50 EMA)"

        return False, None

    def predict_candidate(self, feat_dict: Dict[str, float]) -> Dict[str, Any]:
        """
        Predicts calibrated probability and determines initial floor qualification.
        """
        is_negated, neg_reason = self.check_negation_rules(feat_dict)
        if is_negated:
            return {
                'raw_score': 0.0,
                'calibrated_probability': 0.0,
                'threshold': self.threshold,
                'decision': 'REJECT',
                'rejection_reason': neg_reason,
                'model_version': self.version
            }

        vec = [vector_from_features(feat_dict, self.expected_features)]
        X = np.array(vec)

        raw_score = float(self.model.predict_proba(X)[0, 1])
        if self.calibrator is not None:
            calib_p = float(self.calibrator.predict_proba(np.array([raw_score]))[0])
        else:
            calib_p = raw_score

        decision = 'ENTER' if calib_p >= self.threshold else 'REJECT'
        rejection_reason = None if decision == 'ENTER' else f"Probability P={calib_p:.4f} < Threshold {self.threshold:.2f}"

        return {
            'raw_score': round(raw_score, 4),
            'calibrated_probability': round(calib_p, 4),
            'threshold': self.threshold,
            'decision': decision,
            'rejection_reason': rejection_reason,
            'model_version': self.version
        }


def get_daily_v2_model() -> DailyV2ModelManager:
    return DailyV2ModelManager()
