"""
core/ml_engine_v2.py — Production ML Engine v2 Inference Interface
==================================================================
Consolidated production class MLEngineV2:
- Loads 4 pattern models, 1 meta classifier, 3 path regressors, and the analog library.
- Routes candidates through pattern-specific classifiers.
- Computes multi-timeframe alignment and analog retrieval features.
- Applies adaptive sizing with analog agreement multiplier.
- Dynamic exit rule assignment (Runner, Standard, Tight, Skip).
- SHAP feature explainability for top predictions.
"""

import os
import json
import numpy as np
import pandas as pd
import lightgbm as lgb
import shap
from typing import Dict, List, Any, Optional

class MLEngineV2:
    def __init__(self, model_dir: Optional[str] = None):
        if model_dir is None:
            # Try default paths
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            p1 = os.path.join(base_dir, "data/ml_engine_v2")
            p2 = os.path.abspath("data/ml_engine_v2")
            model_dir = p1 if os.path.exists(p1) else p2
            
        self.model_dir = model_dir
        self.models = {}
        self.regressors = {}
        self.historical_shapes = None
        self.historical_shapes_norm = None
        self.historical_outcomes = None
        self.meta_features = []
        self.explainer = None
        
        self.load_models()
        self.load_index()

    def load_models(self):
        patterns = ['HTF', 'BASE', '52W_HIGH', 'DARVAS']
        for p in patterns:
            path = os.path.join(self.model_dir, f"pattern_{p}.lgb")
            if os.path.exists(path):
                self.models[p] = lgb.Booster(model_file=path)
                
        meta_path = os.path.join(self.model_dir, "meta_model.lgb")
        if os.path.exists(meta_path):
            self.models['meta'] = lgb.Booster(model_file=meta_path)
            
        for tgt in ['mfe_r', 'mae_r', 'days_to_peak']:
            path = os.path.join(self.model_dir, f"regressor_{tgt}.lgb")
            if os.path.exists(path):
                self.regressors[tgt] = lgb.Booster(model_file=path)
                
        feat_path = os.path.join(self.model_dir, "feature_list.json")
        if os.path.exists(feat_path):
            with open(feat_path, "r") as f:
                data = json.load(f)
                self.meta_features = data.get("meta_features", [])

    def load_index(self):
        s_path = os.path.join(self.model_dir, "historical_shapes.npy")
        o_path = os.path.join(self.model_dir, "historical_outcomes.npy")
        if os.path.exists(s_path) and os.path.exists(o_path):
            self.historical_shapes = np.load(s_path)
            self.historical_outcomes = np.load(o_path)
            norms = np.linalg.norm(self.historical_shapes, axis=1, keepdims=True) + 1e-9
            self.historical_shapes_norm = self.historical_shapes / norms

    def shape_embedding(self, closes: np.ndarray, signal_idx: int, lookback: int = 40, n_points: int = 32) -> np.ndarray:
        if signal_idx < lookback or len(closes) <= signal_idx:
            return np.zeros(n_points, dtype=np.float32)
        seg = closes[signal_idx - lookback : signal_idx + 1]
        x_old = np.linspace(0, 1, len(seg))
        x_new = np.linspace(0, 1, n_points)
        resampled = np.interp(x_new, x_old, seg)
        lo, hi = float(np.min(resampled)), float(np.max(resampled))
        if hi - lo < 1e-6:
            return np.zeros(n_points, dtype=np.float32)
        return ((resampled - lo) / (hi - lo)).astype(np.float32)

    def analog_features(self, shape_vector: np.ndarray, k: int = 20) -> Dict[str, float]:
        if self.historical_shapes_norm is None:
            return {
                "analog_win_rate": 0.5, "analog_mean_r": 0.0, "analog_weighted_r": 0.0,
                "analog_best_r": 0.0, "analog_worst_r": 0.0, "analog_std_r": 2.0, "analog_top_similarity": 0.5
            }
        q_norm = shape_vector / (np.linalg.norm(shape_vector) + 1e-9)
        sims = np.dot(self.historical_shapes_norm, q_norm)
        top_k_idx = np.argsort(-sims)[:k]
        outcomes = self.historical_outcomes[top_k_idx]
        top_sims = np.maximum(sims[top_k_idx], 0.001)
        weights = top_sims / np.sum(top_sims)
        
        return {
            "analog_win_rate": float((outcomes > 0).mean()),
            "analog_mean_r": float(outcomes.mean()),
            "analog_weighted_r": float(np.sum(outcomes * weights)),
            "analog_best_r": float(outcomes.max()),
            "analog_worst_r": float(outcomes.min()),
            "analog_std_r": float(outcomes.std()),
            "analog_top_similarity": float(top_sims[0])
        }

    def compute_size(self, score: float, analog_std: float) -> float:
        if score >= 0.75:
            base = 1.25
        elif score >= 0.65:
            base = 1.00
        elif score >= 0.55:
            base = 0.75
        else:
            base = 0.50
            
        if analog_std < 1.5:
            multiplier = 1.20
        elif analog_std < 2.5:
            multiplier = 1.00
        else:
            multiplier = 0.70
            
        size = base * multiplier
        return float(min(max(size, 0.25), 1.50))

    def assign_exit_rule(self, feature_row: pd.Series, entry: float, stop: float) -> Dict[str, Any]:
        r = entry - stop
        if 'mfe_r' in self.regressors and 'days_to_peak' in self.regressors:
            X = pd.DataFrame([feature_row])
            cols = self.regressors['mfe_r'].feature_name()
            # Align features
            for c in cols:
                if c not in X.columns:
                    X[c] = 0.0
            pred_mfe = float(self.regressors['mfe_r'].predict(X[cols])[0])
            pred_days = float(self.regressors['days_to_peak'].predict(X[cols])[0])
        else:
            pred_mfe = 3.0
            pred_days = 10.0
            
        if pred_mfe >= 4.0:
            return {
                "type": "runner",
                "initial_stop": stop,
                "breakeven_trigger_r": 1.5,
                "trail_trigger_r": 2.5,
                "trail_method": "20ema",
                "time_stop_bars": int(pred_days * 1.5),
                "target": None
            }
        elif pred_mfe >= 2.5:
            return {
                "type": "standard",
                "initial_stop": stop,
                "breakeven_trigger_r": 1.0,
                "target": round(entry + 0.7 * pred_mfe * r, 2),
                "time_stop_bars": int(pred_days * 1.3)
            }
        elif pred_mfe >= 1.8:
            return {
                "type": "tight",
                "initial_stop": stop,
                "breakeven_trigger_r": 0.8,
                "target": round(entry + 1.5 * r, 2),
                "time_stop_bars": int(pred_days)
            }
        else:
            return {
                "type": "skip",
                "reason": "predicted_mfe_too_low"
            }

    def explain(self, feature_dict: Dict[str, Any], top_n: int = 3) -> List[Dict[str, Any]]:
        """Returns top SHAP features in plain English."""
        if 'meta' not in self.models:
            return []
            
        if self.explainer is None:
            self.explainer = shap.TreeExplainer(self.models['meta'])
            
        cols = self.models['meta'].feature_name()
        row = pd.DataFrame([{c: feature_dict.get(c, 0.0) for c in cols}])
        shap_values = self.explainer.shap_values(row)
        
        # Binary LightGBM gives shap_values as array or list
        vals = shap_values[1][0] if isinstance(shap_values, list) else shap_values[0]
        top_indices = np.argsort(-np.abs(vals))[:top_n]
        
        explanations = []
        for idx in top_indices:
            feat_name = cols[idx]
            val = vals[idx]
            feat_val = row[feat_name].iloc[0]
            direction = "positive catalyst" if val > 0 else "drag / resistance"
            explanations.append({
                "feature": feat_name,
                "impact": round(float(val), 4),
                "value": round(float(feat_val), 4) if isinstance(feat_val, (int, float, np.number)) else str(feat_val),
                "direction": direction
            })
        return explanations
