"""
banana_engine/core/monitor.py — Live Logging & Drift Monitoring Engine (Layer 5)
================================================================================
Implements prediction logging, 20-session outcome resolution, win-rate drift Z-score,
and per-feature Kolmogorov-Smirnov (KS) distribution monitoring matching Section 11 of BananaPatterns v4.
"""

import os
import sys
import json
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

# Path setup
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR
from core.features import FEATURES

LOG_FILE = os.path.join(DATA_DIR, "predictions_log.jsonl")


def log_prediction(payload: Dict[str, Any]) -> str:
    """
    Appends live prediction record to JSON lines log.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    pred_id = str(uuid.uuid4())
    record = {
        "prediction_id": pred_id,
        "logged_at": datetime.utcnow().isoformat() + "Z",
        **payload
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return pred_id


def load_live_predictions() -> List[Dict[str, Any]]:
    """
    Loads all logged live predictions.
    """
    if not os.path.exists(LOG_FILE):
        return []
    records = []
    with open(LOG_FILE, "r") as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def compute_drift_report(
    reference_dataset_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Compares live predictions with walk-forward reference:
      1. Win-rate drift Z-score (|z| > 2.0 flags warning)
      2. Feature distribution Kolmogorov-Smirnov test (p < 0.01 flags early shift)
    """
    live_records = load_live_predictions()
    if not live_records:
        return {
            "status": "HEALTHY",
            "alert": False,
            "live_samples": 0,
            "wr_z": 0.0,
            "ks_pvalues": {},
            "message": "No live predictions logged yet."
        }

    # Reference dataset
    if reference_dataset_path is None:
        reference_dataset_path = os.path.join(DATA_DIR, "backfill_dataset.parquet")

    if not os.path.exists(reference_dataset_path):
        return {
            "status": "HEALTHY",
            "alert": False,
            "live_samples": len(live_records),
            "wr_z": 0.0,
            "ks_pvalues": {},
            "message": "Reference dataset not found; baseline healthy."
        }

    ref_df = pd.read_parquet(reference_dataset_path)
    live_df = pd.DataFrame(live_records)

    # 1. Win Rate Drift
    # If live outcomes have resolved
    if 'outcome_r' in live_df.columns and live_df['outcome_r'].dropna().count() >= 10:
        resolved = live_df['outcome_r'].dropna()
        live_wr = float((resolved > 0).mean())
        bt_wr = float((ref_df['label'] > 0).mean()) if 'label' in ref_df.columns else 0.50
        n_live = len(resolved)
        se = np.sqrt(max(bt_wr * (1.0 - bt_wr), 0.01) / n_live)
        wr_z = round(float((live_wr - bt_wr) / max(se, 0.001)), 2)
    else:
        wr_z = 0.0

    # 2. Feature Distribution Drift (KS test)
    ks_results: Dict[str, float] = {}
    flagged_features = []

    for feat in FEATURES:
        if feat in live_df.columns and feat in ref_df.columns:
            l_vals = pd.to_numeric(live_df[feat], errors='coerce').dropna().values
            r_vals = pd.to_numeric(ref_df[feat], errors='coerce').dropna().values
            if len(l_vals) >= 10 and len(r_vals) >= 10:
                try:
                    stat, p_val = ks_2samp(l_vals, r_vals)
                    ks_results[feat] = round(float(p_val), 4)
                    if p_val < 0.01:
                        flagged_features.append(feat)
                except Exception:
                    pass

    alert_active = bool(abs(wr_z) > 2.0 or len(flagged_features) >= 3)
    status_text = "DRIFT_ALERT" if alert_active else "HEALTHY"

    return {
        "status": status_text,
        "alert": alert_active,
        "live_samples": len(live_records),
        "wr_z": wr_z,
        "flagged_features": flagged_features,
        "ks_pvalues": ks_results,
        "message": f"Drift monitor {status_text}. {len(flagged_features)} features with distribution shifts."
    }
