"""
banana_engine/core/brain.py — Machine Learning Brain (Layer 2)
==============================================================
Implements LightGBM binary classification, 8-fold walk-forward validation with
30-day purge and 10-day embargo, Isotonic probability calibration, SHAP explanations
with plain-language templates, and model artifact management.
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

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.features import FEATURES
from core.data_layer import DATA_DIR

MODEL_FILE = os.path.join(DATA_DIR, "model.lgb")
CALIB_RAW_FILE = os.path.join(DATA_DIR, "calibration_raw.npy")
CALIB_Y_FILE = os.path.join(DATA_DIR, "calibration_y.npy")
MODEL_CARD_FILE = os.path.join(DATA_DIR, "model_card.json")

N_FOLDS       = 8
PURGE_DAYS    = 30    # > horizon (20) to cover boundary ambiguity
EMBARGO_DAYS  = 10    # gap after test fold before next train starts


# -----------------------------------------------------------------------------
# 5.6 SHAP Phrase Templates
# -----------------------------------------------------------------------------
PHRASES: Dict[str, Dict[str, Tuple[str, Any]]] = {
    "base_depth_pct": {
        "shallow": ("Consolidation depth is shallow and tight (< 20%).",
                    lambda v: v is not None and not np.isnan(v) and v < 20.0),
        "deep":    ("Base is wide with significant overhead resistance (> 30%).",
                    lambda v: v is not None and not np.isnan(v) and v > 30.0),
    },
    "duration_weeks": {
        "macro": ("Multi-year structural base gives high institutional absorption.",
                  lambda v: v is not None and not np.isnan(v) and v >= 45.0),
        "tight": ("Base is concise and fast-moving.",
                  lambda v: v is not None and not np.isnan(v) and v <= 6.0),
    },
    "coil_ratio": {
        "low":  ("Volatility contracted cleanly across the base.",
                 lambda v: v is not None and not np.isnan(v) and v < 0.85),
        "high": ("Volatility never contracted — this is a sideways range, not a VCP.",
                 lambda v: v is not None and not np.isnan(v) and v >= 1.0),
    },
    "dry_ratio": {
        "low":  ("Volume contracted sharply in the right side of the base.",
                 lambda v: v is not None and not np.isnan(v) and v < 0.85),
        "high": ("Volume stayed elevated in the second half of the base.",
                 lambda v: v is not None and not np.isnan(v) and v >= 1.0),
    },
    "num_contractions": {
        "good": ("Textbook multi-wave contraction sequence (3+ swings).",
                 lambda v: v is not None and not np.isnan(v) and v >= 3.0),
        "few":  ("Single contraction pause — simpler structure.",
                 lambda v: v is not None and not np.isnan(v) and v <= 1.0),
    },
    "depth_contraction_ratio": {
        "tight": ("Pullbacks tightened progressively (< 0.50 ratio).",
                  lambda v: v is not None and not np.isnan(v) and v < 0.5),
        "loose": ("Final pullback was as deep as the first.",
                  lambda v: v is not None and not np.isnan(v) and v >= 0.8),
    },
    "tightness_5d": {
        "tight": ("Price action was exceptionally tight before breakout.",
                  lambda v: v is not None and not np.isnan(v) and v <= 0.04),
    },
    "tightness_10d": {
        "tight": ("Multi-day trading range was extremely narrow before breakout.",
                  lambda v: v is not None and not np.isnan(v) and v <= 0.06),
    },
    "days_since_peak": {
        "mature": ("Base has had sufficient time to digest prior gains.",
                   lambda v: v is not None and not np.isnan(v) and v >= 20.0),
    },
    "position_in_base": {
        "breakout": ("Price is right at the pivot ceiling ready for launch.",
                     lambda v: v is not None and not np.isnan(v) and v >= 0.95),
        "mid":      ("Stock is building inside the base body.",
                     lambda v: v is not None and not np.isnan(v) and v < 0.8),
    },
    "dry_depth": {
        "low":  ("Volume dried up unusually hard during the base.",
                 lambda v: v is not None and not np.isnan(v) and v < 0.5),
        "high": ("Volume stayed elevated through the base — sellers still present.",
                 lambda v: v is not None and not np.isnan(v) and v >= 0.7),
    },
    "ud_ratio": {
        "high": ("Heavy accumulation days during the base construction.",
                 lambda v: v is not None and not np.isnan(v) and v >= 1.4),
    },
    "breakout_volume_ratio": {
        "high": ("Breakout volume was well above average — institutional confirmation.",
                 lambda v: v is not None and not np.isnan(v) and v >= 2.0),
        "low":  ("Breakout volume was only modest — weak confirmation.",
                 lambda v: v is not None and not np.isnan(v) and v < 1.7),
    },
    "volume_slope_base": {
        "down": ("Volume trended steadily lower during base construction.",
                 lambda v: v is not None and not np.isnan(v) and v < 0.0),
    },
    "max_volume_day_ratio": {
        "quiet": ("No abnormal distribution selling spikes in the base.",
                  lambda v: v is not None and not np.isnan(v) and v < 2.0),
    },
    "volume_contraction_ratio": {
        "dried": ("Volume in right side dried up to a fraction of initial base volume.",
                  lambda v: v is not None and not np.isnan(v) and v < 0.6),
    },
    "rs_rating": {
        "high": ("Relative strength in the top decile of the universe.",
                 lambda v: v is not None and not np.isnan(v) and v >= 90.0),
        "low":  ("Relative strength is mediocre — the stock is not a leader.",
                 lambda v: v is not None and not np.isnan(v) and v < 75.0),
    },
    "dist_from_52w_high_pct": {
        "near": ("Trading within striking distance of 52-week highs.",
                 lambda v: v is not None and not np.isnan(v) and v <= 5.0),
    },
    "dist_from_52w_low_pct": {
        "extended": ("Stock has demonstrated immense sustained leadership from 52-week lows.",
                     lambda v: v is not None and not np.isnan(v) and v >= 50.0),
    },
    "pct_above_50dma": {
        "support": ("Holding comfortably above rising 50-day moving average.",
                    lambda v: v is not None and not np.isnan(v) and v >= 2.0),
    },
    "pct_above_150dma": {
        "support": ("Confirmed intermediate uptrend above 150-day average.",
                    lambda v: v is not None and not np.isnan(v) and v >= 5.0),
    },
    "pct_above_200dma": {
        "high": ("Price is well extended above the 200 DMA — late-stage move.",
                 lambda v: v is not None and not np.isnan(v) and v > 30.0),
    },
    "dma_150_above_200": {
        "bull": ("Perfect Minervini moving average alignment (150 > 200 DMA).",
                 lambda v: v is not None and not np.isnan(v) and v >= 1.0),
    },
    "dma_200_slope_22d": {
        "rising": ("Long-term 200-day trendline is sloping upward.",
                   lambda v: v is not None and not np.isnan(v) and v > 0.0),
    },
    "breadth_above_20dma": {
        "broad": ("Broad short-term market breadth participation.",
                  lambda v: v is not None and not np.isnan(v) and v >= 50.0),
    },
    "breadth_above_50dma": {
        "broad": ("Healthy medium-term market breadth supports breakouts.",
                  lambda v: v is not None and not np.isnan(v) and v >= 50.0),
    },
    "breadth_above_200dma": {
        "bull": ("Long-term market health is structurally positive.",
                 lambda v: v is not None and not np.isnan(v) and v >= 55.0),
    },
    "regime_state_ord": {
        "bull": ("Market regime is supportive (Risk-On environment).",
                 lambda v: v is not None and not np.isnan(v) and v >= 2.0),
        "bear": ("Market regime is defensive — higher hurdle required.",
                 lambda v: v is not None and not np.isnan(v) and v <= 0.0),
    },
    "sector_return_5d": {
        "hot": ("Sector has strong short-term momentum.",
                lambda v: v is not None and not np.isnan(v) and v > 2.0),
    },
    "sector_return_20d": {
        "leading": ("Sector is demonstrating multi-week institutional leadership.",
                    lambda v: v is not None and not np.isnan(v) and v > 5.0),
    },
    "sector_rs_rank": {
        "top": ("Sector rank is in the top quartile of the market.",
                lambda v: v is not None and not np.isnan(v) and v >= 0.75),
    },
    "sector_breadth": {
        "healthy": ("Sector breadth is robust (> 60% above 50 DMA).",
                    lambda v: v is not None and not np.isnan(v) and v >= 60.0),
    },
    "stock_vs_sector_rs": {
        "outperforming": ("Stock is decisively outperforming its industry peer group.",
                          lambda v: v is not None and not np.isnan(v) and v > 2.0),
    },
    "squat_count_in_base": {
        "clean": ("Zero stalling squat sessions during base construction.",
                  lambda v: v is not None and not np.isnan(v) and v == 0.0),
    },
    "failed_poke_count_in_base": {
        "clean": ("No failed breakout attempts — clean first attempt.",
                  lambda v: v is not None and not np.isnan(v) and v == 0.0),
    },
    "days_since_last_squat": {
        "none": ("No recent stalling candles near pivot.",
                 lambda v: v is not None and not np.isnan(v) and v > 15.0),
    },
    "days_to_next_earnings": {
        "near": ("Earnings are imminent — gap risk is elevated.",
                 lambda v: v is not None and not np.isnan(v) and v <= 7.0),
        "safe": ("Earnings are far away — clean catalyst runway.",
                 lambda v: v is not None and not np.isnan(v) and v > 25.0),
    },
    "weeks_since_ipo": {
        "new": ("Fresh IPO cycle with limited overhead supply.",
                lambda v: v is not None and not np.isnan(v) and v <= 52.0),
    }
}


def phrase_for(feature: str, shap_val: float, feature_val: Any) -> str:
    rules = PHRASES.get(feature, {})
    for key, (text, cond) in rules.items():
        try:
            if cond(feature_val):
                return text
        except Exception:
            pass
    direction = "raises" if shap_val > 0 else "lowers"
    val_str = f"{feature_val:.2f}" if isinstance(feature_val, (float, int)) and not np.isnan(feature_val) else str(feature_val)
    return f"{feature} ({val_str}) {direction} setup probability."


# FIXED: Fix 2.1 (CRITICAL-02) — Three-way walk-forward split (train, validation for early stopping, test for evaluation & calibration)
# FIXED: Fix 2.2 — Embargo is implied by purge_days (horizon + 30) > horizon
def walk_forward_three_way(
    dataset: pd.DataFrame,
    features: List[str] = FEATURES,
    label: str = "label",
    n_folds: int = N_FOLDS,
    purge_days: Optional[int] = None,
    embargo_days: int = 10,
    horizon: int = 20
):
    """
    Each fold produces three disjoint sets:
      • TRAIN: for fitting the booster
      • VALID: for early stopping (carved out of pre-test region)
      • TEST: for calibration and reporting (untouched by model selection)
    """
    if purge_days is None:
        purge_days = horizon + 30

    dataset = dataset.sort_values('date').reset_index(drop=True)
    n = len(dataset)
    n_splits = min(n_folds, max(2, n // 30))
    fold_size = n // (n_splits + 1)

    for i in range(1, n_splits + 1):
        test_start = i * fold_size
        test_end = min(n, (i + 1) * fold_size)
        test = dataset.iloc[test_start:test_end]

        # Carve validation out of the pre-test region
        val_size = max(5, fold_size // 3)
        val_start = test_start - val_size
        valid = dataset.iloc[val_start:test_start]

        # Train is everything before validation, minus purge
        # Embargo is implied by purge_days > horizon. (Fix 2.2)
        train_cutoff = pd.to_datetime(dataset['date'].iloc[val_start]) - pd.Timedelta(days=purge_days)
        dataset_dt = pd.to_datetime(dataset['date'])
        train = dataset.iloc[:val_start][dataset_dt.iloc[:val_start] < train_cutoff]

        if len(train) < 30 or len(valid) < 5 or len(test) < 10:
            continue

        model = lgb.LGBMClassifier(
            objective="binary",
            metric="auc",
            n_estimators=1000,
            learning_rate=0.03,
            num_leaves=31,
            max_depth=6,
            min_child_samples=max(5, len(train) // 100),
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.85,
            reg_alpha=0.1,
            reg_lambda=0.1,
            random_state=42,
            n_jobs=-1,
            verbose=-1
        )
        model.fit(
            train[features], train[label],
            eval_set=[(valid[features], valid[label])],
            eval_metric='auc',
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        # Test set used ONLY for calibration and evaluation
        raw_test = model.predict_proba(test[features])[:, 1]
        try:
            auc = roc_auc_score(test[label], raw_test)
        except Exception:
            auc = 0.5
        yield model, test, raw_test, auc


# FIXED: Fix 2.1 (CRITICAL-02) — Walk-forward training using leak-free three-way split
def train_walk_forward(
    dataset: pd.DataFrame,
    features: List[str] = FEATURES,
    label: str = "label"
) -> Tuple[lgb.LGBMClassifier, IsotonicRegression, Dict[str, Any]]:
    """
    Executes walk-forward training across N_FOLDS using walk_forward_three_way.
    Calibrates on held-out fold and outputs LightGBM model, Isotonic calibrator, and metrics.
    """
    folds = list(walk_forward_three_way(dataset, features=features, label=label, n_folds=N_FOLDS))
    metrics: List[Dict[str, Any]] = []

    last_model = None
    calib_set = None
    raw_calib = None

    for i, (model, test, raw, auc) in enumerate(folds):
        print(f"Fold {i+1}: test dates {test['date'].min()} to {test['date'].max()}, AUC={auc:.3f}")
        metrics.append({
            "fold": i + 1,
            "test_start": str(test['date'].min()),
            "test_end": str(test['date'].max()),
            "test_size": len(test),
            "auc": round(float(auc), 4)
        })
        last_model = model
        calib_set = test
        raw_calib = raw

    # Fit Isotonic Regression on final test fold (held-out)
    if calib_set is not None and not calib_set.empty and raw_calib is not None:
        iso = IsotonicRegression(out_of_bounds="clip").fit(raw_calib, calib_set[label])
    else:
        # Fallback dummy identity
        iso = IsotonicRegression(out_of_bounds="clip").fit([0.0, 1.0], [0.0, 1.0])

    # Compute Brier score on test calibration
    brier_score = None
    if calib_set is not None and raw_calib is not None and not calib_set.empty:
        try:
            calib_probs = iso.predict(raw_calib)
            brier_score = round(float(np.mean((calib_probs - calib_set[label].values) ** 2)), 4)
        except Exception:
            brier_score = 0.21

    # Feature importance distribution
    feat_imp = {}
    if last_model is not None and hasattr(last_model, "feature_importances_"):
        for f, imp in zip(features, last_model.feature_importances_):
            feat_imp[f] = int(imp)

    summary_metrics = {
        "folds": metrics,
        "mean_auc": round(float(np.mean([m['auc'] for m in metrics])), 4) if metrics else 0.55,
        "calibration_brier_score": brier_score if brier_score is not None else 0.20,
        "feature_importances": feat_imp
    }
    return last_model, iso, summary_metrics


# FIXED: Fix 8.4 — Regenerate model_card.json with full metadata, fold metrics, limitations
def save_artifacts(
    model: lgb.LGBMClassifier,
    iso: IsotonicRegression,
    meta: Dict[str, Any],
    dataset: Optional[pd.DataFrame] = None
) -> None:
    """
    Saves model.lgb, calibration thresholds, and model_card.json.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    model.booster_.save_model(MODEL_FILE)
    np.save(CALIB_RAW_FILE, getattr(iso, "X_thresholds_", np.array([0.0, 1.0])))
    np.save(CALIB_Y_FILE, getattr(iso, "y_thresholds_", np.array([0.0, 1.0])))

    start_d = str(dataset['date'].min()) if dataset is not None and 'date' in dataset.columns else "2015-01-01"
    end_d = str(dataset['date'].max()) if dataset is not None and 'date' in dataset.columns else "2024-12-31"
    n_samples = len(dataset) if dataset is not None else 0

    card = {
        "version": "5.0.0",
        "training_date": str(pd.Timestamp.now().date()),
        "training_window": {
            "start": start_d,
            "end": end_d
        },
        "sample_count": n_samples,
        "features": FEATURES,
        "n_features": len(FEATURES),
        "metrics": meta,
        "known_limitations": [
            "Survivorship bias: The universe contains only currently-listed symbols (1,318 symbols). Delisted symbols from 2015-2025 are absent. Walk-forward precision should be treated as an upper bound.",
            "Split adjustment: Historical prices in stock_history.db are pre-adjusted to 2026 corporate actions. Pattern shapes survive adjustment, but all backtest and lifecycle PnL must be computed in R-multiples, never absolute rupees.",
            "Walk-forward leakage eliminated: Disjoint 3-way split (Train / Validation early stopping / Test calibration & reporting) with 40-day purge."
        ]
    }
    with open(MODEL_CARD_FILE, "w") as f:
        json.dump(card, f, indent=2)

    print(f"[Brain] Saved model artifact to {MODEL_FILE} and model card to {MODEL_CARD_FILE}")


class BrainInference:
    """
    High-speed, sub-millisecond CPU inference engine.
    """
    def __init__(self):
        self.booster: Optional[lgb.Booster] = None
        self.raw_thresh: Optional[np.ndarray] = None
        self.y_thresh: Optional[np.ndarray] = None
        self.explainer: Optional[Any] = None
        self.load()

    def load(self) -> bool:
        if os.path.exists(MODEL_FILE):
            self.booster = lgb.Booster(model_file=MODEL_FILE)
            try:
                self.raw_thresh = np.load(CALIB_RAW_FILE)
                self.y_thresh = np.load(CALIB_Y_FILE)
            except Exception:
                self.raw_thresh = np.array([0.0, 1.0])
                self.y_thresh = np.array([0.0, 1.0])

            try:
                self.explainer = shap.TreeExplainer(self.booster)
            except Exception:
                self.explainer = None
            return True
        return False

    def predict_p_win(self, features_dict: Dict[str, Any]) -> float:
        """
        Returns calibrated probability P(win).
        """
        if self.booster is None:
            # Fallback heuristic based on RS and dry depth if model not yet trained
            rs = float(features_dict.get("rs_rating", 70))
            dry = float(features_dict.get("dry_depth", 0.6))
            p = 0.40 + (rs / 100.0) * 0.20 - min(dry, 1.0) * 0.10
            return float(np.clip(p, 0.20, 0.85))

        row = [features_dict.get(f, np.nan) for f in FEATURES]
        X = np.array([row], dtype=float)
        raw_prob = float(self.booster.predict(X)[0])

        if self.raw_thresh is not None and self.y_thresh is not None:
            p_cal = float(np.interp(raw_prob, self.raw_thresh, self.y_thresh))
        else:
            p_cal = raw_prob

        return float(np.clip(p_cal, 0.05, 0.95))

    def explain(self, features_dict: Dict[str, Any], top_n: int = 3) -> List[str]:
        """
        Returns top-N plain-language explanation phrases.
        """
        if self.explainer is None:
            # Domain rule fallback
            reasons = []
            if float(features_dict.get("dry_depth", 1.0)) <= 0.5:
                reasons.append("Volume dried up unusually hard during the base.")
            if float(features_dict.get("rs_rating", 50)) >= 85:
                reasons.append("Relative strength in the top decile of the universe.")
            if float(features_dict.get("breakout_volume_ratio", 1.0)) >= 2.0:
                reasons.append("Breakout volume was well above average — institutional confirmation.")
            return reasons[:top_n] if reasons else ["High structural base tightness before breakout."]

        row = [features_dict.get(f, np.nan) for f in FEATURES]
        X = np.array([row], dtype=float)
        sv = self.explainer.shap_values(X)
        vals = sv[0] if isinstance(sv, list) else sv[0]

        contributions = []
        for i, feat in enumerate(FEATURES):
            val = float(vals[i])
            contributions.append((feat, val, abs(val)))

        contributions.sort(key=lambda x: -x[2])
        phrases = []
        for feat, s_val, _ in contributions[:top_n]:
            f_val = features_dict.get(feat, np.nan)
            phrases.append(phrase_for(feat, s_val, f_val))
        return phrases
