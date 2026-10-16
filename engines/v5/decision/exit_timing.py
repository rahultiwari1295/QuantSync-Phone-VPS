"""
quantsync_v11/decision/exit_timing.py
=====================================
Layer 6: Exit Timing Model.

Replaces hyper-sensitive mechanical EMA10/EMA20 trailing stops that historically
cut winners prematurely (average PnL of only ₹53 or negative).

- Evaluates daily state vector for open positions:
  days_held, unrealized_r, dist_to_stop, dist_to_target,
  volume vs entry volume, price vs EMA10/20/50, regime, fingerprint MFE remaining.
- Predicts P(reach target barrier | current state).
- Only signals exit if P(target) drops below calibrated threshold.
- Invariable Rule: Initial structural hard stop is enforced outside this model and is NEVER overridden.
"""

from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
import lightgbm as lgb


STATE_FEATURES = [
    "days_held",
    "unrealized_r",
    "dist_to_stop_pct",
    "dist_to_target_pct",
    "vol_vs_entry_vol",
    "price_vs_ema10",
    "price_vs_ema20",
    "price_vs_ema50",
    "regime_id",
    "fingerprint_remaining_mfe_est",
]


def build_state_vector(
    days_held: int,
    unrealized_r: float,
    current_price: float,
    stop_price: float,
    target_price: float,
    current_volume: float,
    entry_volume: float,
    ema10: float,
    ema20: float,
    ema50: float,
    regime_id: int,
    fingerprint_est: float = 2.0,
) -> Dict[str, float]:
    """
    Builds canonical daily state vector for an active trade.
    """
    vol_ratio = current_volume / entry_volume if entry_volume > 0 else 1.0
    dist_stop = (current_price - stop_price) / current_price if current_price > 0 else 0.0
    dist_target = (target_price - current_price) / current_price if current_price > 0 else 0.0

    p_ema10 = (current_price / ema10 - 1.0) if ema10 > 0 else 0.0
    p_ema20 = (current_price / ema20 - 1.0) if ema20 > 0 else 0.0
    p_ema50 = (current_price / ema50 - 1.0) if ema50 > 0 else 0.0

    return {
        "days_held": float(days_held),
        "unrealized_r": float(unrealized_r),
        "dist_to_stop_pct": float(dist_stop),
        "dist_to_target_pct": float(dist_target),
        "vol_vs_entry_vol": float(vol_ratio),
        "price_vs_ema10": float(p_ema10),
        "price_vs_ema20": float(p_ema20),
        "price_vs_ema50": float(p_ema50),
        "regime_id": float(regime_id),
        "fingerprint_remaining_mfe_est": float(fingerprint_est),
    }


def train_exit_model(state_df: pd.DataFrame, labels: pd.Series, purged_splits: List[Any]) -> lgb.LGBMClassifier:
    """
    Trains exit timing model on historical holding states.
    Labels come from the unified triple_barrier_label function (1 = reached target, 0 = stopped/timed out).
    """
    model = lgb.LGBMClassifier(
        objective="binary",
        max_depth=4,
        n_estimators=120,
        learning_rate=0.05,
        subsample=0.8,
        verbose=-1,
        random_state=42,
    )
    # Fit on the latest purged train split
    train_idx = list(purged_splits)[-1][0]
    feature_cols = [c for c in STATE_FEATURES if c in state_df.columns]
    model.fit(state_df[feature_cols].iloc[train_idx], labels.iloc[train_idx])
    return model


def should_exit(
    model: Optional[lgb.LGBMClassifier],
    state_vector: Dict[str, float],
    threshold: float = 0.35,
) -> bool:
    """
    Returns True if model predicts that probability of reaching profit target
    has decayed below `threshold`.
    Hard stop is enforced strictly outside this model.
    """
    if model is None:
        return False

    ordered_features = [[state_vector.get(k, 0.0) for k in STATE_FEATURES]]
    df_state = pd.DataFrame(ordered_features, columns=STATE_FEATURES)
    p_target = float(model.predict_proba(df_state)[0][1])
    return p_target < threshold
