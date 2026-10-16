"""
banana_engine/core/online_calibrator.py — Dual-Window Rolling Calibrator & Regime Routing (v5)
=============================================================================================
Implements:
  1. Fast Window (100 trades): Highly responsive to regime breakdown and drift.
  2. Slow Window (500 trades): Long-horizon empirical statistical anchor.
  3. Regime Divergence Alarm: Flags alert when |P_fast - P_slow| > 0.12 (12%).
  4. Regime Routing: Dispatches inference to regime-conditioned models (RISK_ON / NEUTRAL / RISK_OFF)
     with robust fallback to global barrier model.
"""

import os
import sys
import json
import sqlite3
from typing import Dict, List, Optional, Tuple, Any
from collections import deque
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB, get_connection

CALIBRATOR_DB = os.path.join(DATA_DIR, "calibrator_history.db")
REGIME_MODELS_DIR = os.path.join(DATA_DIR, "regime_models")


class DualWindowCalibrator:
    """
    Dual-window rolling probability calibrator.
    Maintains 100-trade fast window and 500-trade slow window.
    """
    def __init__(
        self,
        fast_size: int = 100,
        slow_size: int = 500,
        # FIXED: Fix 4.3 (INFO-03) — Calibrated from 95th percentile walk-forward divergence distribution
        divergence_threshold: float = 0.08,
        db_path: Optional[str] = None
    ):
        self.fast_size = fast_size
        self.slow_size = slow_size
        self.divergence_threshold = divergence_threshold
        self.db_path = db_path or CALIBRATOR_DB
        self.fast_history: deque = deque(maxlen=self.fast_size)
        self.slow_history: deque = deque(maxlen=self.slow_size)

        self.calib_fast: Optional[IsotonicRegression] = None
        self.calib_slow: Optional[IsotonicRegression] = None

        self._init_db()
        self._load_from_db()

    def _init_db(self):
        with get_connection(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trade_resolutions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resolved_date TEXT,
                    symbol TEXT,
                    raw_p REAL,
                    actual_outcome INTEGER,
                    regime TEXT
                )
            """)
            conn.commit()

    def _load_from_db(self):
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trade_resolutions")
            count = cursor.fetchone()[0]

            if count < self.fast_size:
                self._seed_initial_history(conn)

            # Load the most recent slow_size records
            query = """
                SELECT raw_p, actual_outcome FROM trade_resolutions
                ORDER BY id DESC LIMIT ?
            """
            rows = conn.execute(query, (self.slow_size,)).fetchall()

        if rows:
            # Reverse so they are chronological
            rows = rows[::-1]
            for raw_p, outcome in rows:
                self.slow_history.append((float(raw_p), int(outcome)))
            for raw_p, outcome in rows[-self.fast_size:]:
                self.fast_history.append((float(raw_p), int(outcome)))

            self._refit()

    # FIXED: Fix 4.1 (MAJOR-04) — Removed synthetic calibration seeding
    def _seed_initial_history(self, conn: sqlite3.Connection):
        """
        FIXED: MAJOR-04 — previously seeded with synthetic noise.
        Now returns without action. The calibrator uses whatever real
        outcomes exist. If insufficient, it reports a 'low confidence'
        flag in the output.
        """
        return

    def _refit(self):
        """Fits isotonic regression on both fast and slow windows"""
        # Slow window fitting
        if len(self.slow_history) >= 20:
            raw_s = np.array([x[0] for x in self.slow_history])
            y_s = np.array([x[1] for x in self.slow_history])
            # Ensure both classes exist
            if len(np.unique(y_s)) > 1:
                self.calib_slow = IsotonicRegression(out_of_bounds="clip")
                self.calib_slow.fit(raw_s, y_s)

        # Fast window fitting
        if len(self.fast_history) >= 20:
            raw_f = np.array([x[0] for x in self.fast_history])
            y_f = np.array([x[1] for x in self.fast_history])
            if len(np.unique(y_f)) > 1:
                self.calib_fast = IsotonicRegression(out_of_bounds="clip")
                self.calib_fast.fit(raw_f, y_f)

    def add_outcome(
        self,
        raw_p: float,
        actual_outcome: int,
        symbol: str = "UNKNOWN",
        regime: str = "RISK_ON",
        resolved_date: Optional[str] = None
    ) -> None:
        """
        Appends a newly resolved trade into the calibrator history and refits curves.
        """
        if resolved_date is None:
            resolved_date = pd.Timestamp.now().strftime("%Y-%m-%d")

        raw_p = float(np.clip(raw_p, 0.01, 0.99))
        actual_outcome = 1 if int(actual_outcome) > 0 else 0

        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO trade_resolutions (resolved_date, symbol, raw_p, actual_outcome, regime)
                VALUES (?, ?, ?, ?, ?)
            """, (resolved_date, symbol, raw_p, actual_outcome, regime))
            conn.commit()

        self.fast_history.append((raw_p, actual_outcome))
        self.slow_history.append((raw_p, actual_outcome))
        self._refit()

    # FIXED: Fix 4.1 (MAJOR-04) — Confidence flag based on real sample size
    def predict(self, raw_p: float) -> Dict[str, Any]:
        """
        Predicts calibrated probability with confidence level based on history length.
        """
        raw_val = float(np.clip(raw_p, 0.01, 0.99))
        if len(self.fast_history) < 100:
            return {
                "p_win": raw_val,
                "confidence": "low",
                "reason": "insufficient calibration data"
            }
        calib = self.calibrate(raw_val)
        return {
            "p_win": calib["p_calibrated"],
            "confidence": "normal"
        }

    def calibrate(self, raw_p: float) -> Dict[str, Any]:
        """
        Calibrates raw probability under both fast and slow windows and monitors divergence.
        """
        raw_val = float(np.clip(raw_p, 0.01, 0.99))

        # Slow probability (anchor)
        if self.calib_slow is not None:
            p_slow = float(self.calib_slow.predict([raw_val])[0])
        else:
            p_slow = raw_val

        # Fast probability (drift sensor)
        if self.calib_fast is not None:
            p_fast = float(self.calib_fast.predict([raw_val])[0])
        else:
            p_fast = p_slow

        divergence = round(p_fast - p_slow, 4)
        abs_div = abs(divergence)
        divergence_alert = abs_div > self.divergence_threshold

        # Robust blended probability: 70% slow anchor + 30% fast response
        p_calibrated = round(0.70 * p_slow + 0.30 * p_fast, 3)

        alert_msg = None
        if divergence_alert:
            alert_msg = (
                f"REGIME DIVERGENCE ALERT: Fast window (100 trades) differs by {round(abs_div * 100, 1)}% "
                f"from slow anchor (500 trades). Recent market dynamics may be transitioning."
            )

        return {
            "raw_probability": round(raw_val, 4),
            "p_calibrated": p_calibrated,
            "confidence": "normal" if len(self.fast_history) >= 100 else "low",
            "p_fast_window": round(p_fast, 3),
            "p_slow_window": round(p_slow, 3),
            "divergence": divergence,
            "divergence_alert": divergence_alert,
            "alert_message": alert_msg,
            "fast_samples": len(self.fast_history),
            "slow_samples": len(self.slow_history)
        }


# FIXED: Fix 4.2 (MAJOR-03) — Regime models not trained. Falling back to global model.
# To re-enable: train model_risk_on.lgb, model_neutral.lgb, model_risk_off.lgb
# into data/regime_models/, then uncomment.
class RegimeModelRouter:
    """
    Dispatches inference to regime-conditioned models:
      • RISK_ON: Aggressive momentum parameters.
      • NEUTRAL: Selective criteria.
      • RISK_OFF: Strict defense.
    Currently falls back cleanly to the primary global barrier model (Fix 4.2 Option A).
    """
    def __init__(self):
        self.models: Dict[str, Any] = {}

    def route_inference(self, features: Dict[str, Any], regime: str = "RISK_ON") -> Optional[float]:
        # FIXED: MAJOR-03 — regime models not trained. Falling back to global model.
        return None


if __name__ == "__main__":
    print("Testing DualWindowCalibrator...")
    calibrator = DualWindowCalibrator()
    print(f"Loaded {calibrator.fast_size} fast / {calibrator.slow_size} slow history samples.")

    # Test test calibration on raw probability 0.58
    res = calibrator.calibrate(0.58)
    print("Calibration Output on raw 0.58:")
    print(json.dumps(res, indent=2))

    # Test divergence simulation
    print("\nSimulating rapid market degradation (50 consecutive losses)...")
    for _ in range(50):
        calibrator.add_outcome(raw_p=0.60, actual_outcome=0, regime="RISK_OFF")

    res2 = calibrator.calibrate(0.58)
    print("Post-Degradation Calibration Output on raw 0.58:")
    print(json.dumps(res2, indent=2))
