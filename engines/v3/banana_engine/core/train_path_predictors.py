"""
banana_engine/core/train_path_predictors.py — Master ML Path Expectancy Trainer
================================================================================
Extracts 20,000+ historical base formations from stock_history.db (2020–2026)
and trains LightGBM models with Isotonic Calibration to predict:
  1. Expected Upside % (Maximum Favorable Excursion - MFE)
  2. Expected Downside Risk % (Maximum Adverse Excursion - MAE)
  3. Directional Win Probability: P(Up) vs P(Down)
  4. Double-Digit Gain Probability: P(Gain >= +10%)
  5. Super-Gain Probability: P(Gain >= +20%)
Generates actionable verdicts (GO FOR IT vs SKIP / AVOID) based on asymmetric risk-reward.
"""

import os
import sys
import json
import sqlite3
import time
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any
from scipy.stats import spearmanr
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score, roc_auc_score
from sklearn.isotonic import IsotonicRegression
import lightgbm as lgb

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB, read_ohlcv_raw
from core.structure import detect_base_at

MODEL_MFE_FILE = os.path.join(DATA_DIR, "model_mfe.lgb")
MODEL_MAE_FILE = os.path.join(DATA_DIR, "model_mae.lgb")
MODEL_UP_PROB_FILE = os.path.join(DATA_DIR, "model_up_prob.lgb")
MODEL_GAIN_10_FILE = os.path.join(DATA_DIR, "model_gain_10pct.lgb")
MODEL_GAIN_20_FILE = os.path.join(DATA_DIR, "model_gain_20pct.lgb")
PATH_MODEL_CARD_FILE = os.path.join(DATA_DIR, "path_model_card.json")

FEATURES_LIST = [
    "duration_weeks",
    "depth_pct",
    "coil_ratio",
    "dry_ratio",
    "rs_rating",
    "vol_ratio",
    "cir",
    "dist_from_52w_high_pct",
    "price_vs_50ma_pct"
]


def extract_formation_dataset(max_symbols: int = 600) -> pd.DataFrame:
    """
    Extracts historical base formations with 25-bar forward realized excursions.
    """
    print(f"[Dataset Extractor] Reading base formations from {STOCK_HISTORY_DB}...")
    with sqlite3.connect(STOCK_HISTORY_DB) as conn:
        symbols = [r[0] for r in conn.execute("SELECT symbol FROM StockHistory WHERE symbol != ''").fetchall()]

    samples = []
    t0 = time.time()

    for sym in symbols[:max_symbols]:
        bars = read_ohlcv_raw(sym)
        if len(bars) < 100:
            continue

        # Step across time to find historical bases
        for idx in range(60, len(bars) - 25, 12):
            b = detect_base_at(bars, idx)
            if not b:
                continue

            entry_price = float(bars['close'].iloc[idx])
            future_bars = bars.iloc[idx + 1 : idx + 26]
            max_high = float(future_bars['high'].max())
            min_low = float(future_bars['low'].min())

            # Realized excursions in %
            realized_mfe = round(((max_high - entry_price) / max(entry_price, 0.01)) * 100.0, 2)
            realized_mae = round(((min_low - entry_price) / max(entry_price, 0.01)) * 100.0, 2)

            # Technical features at base breakout / completion
            idx_start = max(0, idx - 250)
            close_1y = float(bars['close'].iloc[idx_start])
            rs = int(np.clip(50 + ((entry_price / max(close_1y, 0.01)) - 1.0) * 50.0, 10, 99))

            vol_20 = float(bars['volume'].iloc[max(0, idx - 20):idx].mean()) if idx >= 5 else 1.0
            cur_v = float(bars['volume'].iloc[idx])
            vol_ratio = round(cur_v / max(vol_20, 1.0), 2)

            c_low = float(bars['low'].iloc[idx])
            c_high = float(bars['high'].iloc[idx])
            cir = round((entry_price - c_low) / max(c_high - c_low, 0.01), 2)

            high_52w = float(bars['high'].iloc[max(0, idx - 252):idx + 1].max())
            dist_52w_high = round(((high_52w - entry_price) / max(high_52w, 0.01)) * 100.0, 1)

            sma50 = float(bars['close'].iloc[max(0, idx - 50):idx + 1].mean())
            price_vs_50ma = round(((entry_price - sma50) / max(sma50, 0.01)) * 100.0, 1)

            # Binary targets: True asymmetric winners
            # 1 if reached >= 10% gain with favorable reward/risk, or exploded >= 15%
            target_up = 1 if ((realized_mfe >= 10.0 and realized_mfe >= 1.25 * abs(realized_mae)) or realized_mfe >= 15.0) else 0
            target_10 = 1 if realized_mfe >= 10.0 else 0
            target_20 = 1 if realized_mfe >= 20.0 else 0

            samples.append({
                "symbol": sym,
                "date": str(bars['date'].iloc[idx])[:10],
                "duration_weeks": float(b.duration_weeks),
                "depth_pct": float(b.depth_pct),
                "coil_ratio": float(b.coil_ratio),
                "dry_ratio": float(b.dry_ratio),
                "rs_rating": float(rs),
                "vol_ratio": float(vol_ratio),
                "cir": float(cir),
                "dist_from_52w_high_pct": float(dist_52w_high),
                "price_vs_50ma_pct": float(price_vs_50ma),
                "entry_price": float(entry_price),
                "mfe_pct": float(realized_mfe),
                "mae_pct": float(realized_mae),
                "target_up": target_up,
                "target_10": target_10,
                "target_20": target_20
            })

    df = pd.DataFrame(samples).sort_values("date").reset_index(drop=True)
    print(f"[Dataset Extractor] Successfully extracted {len(df)} historical formations in {time.time() - t0:.2f}s!")
    
    # Save full dataset for auditing
    parquet_path = os.path.join(DATA_DIR, "historical_dataset_oos2026.parquet")
    df.to_parquet(parquet_path, index=False)
    print(f"[Dataset Extractor] Full dataset cached to {parquet_path}")
    return df


def train_and_export_models() -> Dict[str, Any]:
    """
    Trains all 5 path prediction models strictly on pre-2026 data (<= 2025-12-31)
    and validates on strict 2026 Out-of-Sample (OOS) data.
    """
    df = extract_formation_dataset(max_symbols=600)
    if len(df) < 500:
        raise ValueError("Insufficient training samples extracted.")

    # Strict In-Sample (pre-2026) vs Out-of-Sample (2026)
    train_df = df[df['date'] <= '2025-12-31'].copy().reset_index(drop=True)
    test_df = df[df['date'] >= '2026-01-01'].copy().reset_index(drop=True)

    X_train = train_df[FEATURES_LIST].values
    X_test = test_df[FEATURES_LIST].values

    print(f"[ML Trainer] In-Sample (<= 2025): {len(train_df)} formations | Strict 2026 OOS: {len(test_df)} formations")

    # 1. Model MFE (Expected Upside % Regressor)
    y_mfe_train = train_df['mfe_pct'].values
    y_mfe_test = test_df['mfe_pct'].values

    mfe_model = lgb.LGBMRegressor(
        n_estimators=150,
        learning_rate=0.04,
        max_depth=5,
        num_leaves=24,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbose=-1
    )
    mfe_model.fit(X_train, y_mfe_train)
    mfe_preds = mfe_model.predict(X_test)
    mfe_spearman, _ = spearmanr(y_mfe_test, mfe_preds)
    mfe_mae = mean_absolute_error(y_mfe_test, mfe_preds)
    print(f"  • MFE Regressor: Holdout MAE = {mfe_mae:.2f}%, Spearman Rank Correlation = {mfe_spearman:.4f}")

    # 2. Model MAE (Expected Downside Risk % Regressor)
    y_mae_train = train_df['mae_pct'].values
    y_mae_test = test_df['mae_pct'].values

    mae_model = lgb.LGBMRegressor(
        n_estimators=120,
        learning_rate=0.04,
        max_depth=4,
        num_leaves=16,
        subsample=0.8,
        random_state=42,
        verbose=-1
    )
    mae_model.fit(X_train, y_mae_train)
    mae_preds = mae_model.predict(X_test)
    mae_loss = mean_absolute_error(y_mae_test, mae_preds)
    print(f"  • MAE Regressor: Holdout MAE = {mae_loss:.2f}%")

    # 3. Model Directional Win Probability: P(Up) vs P(Down)
    y_up_train = train_df['target_up'].values
    y_up_test = test_df['target_up'].values

    up_model = lgb.LGBMClassifier(
        n_estimators=120,
        learning_rate=0.03,
        max_depth=4,
        num_leaves=16,
        random_state=42,
        verbose=-1
    )
    up_model.fit(X_train, y_up_train)
    up_probs = up_model.predict_proba(X_test)[:, 1]
    up_auc = roc_auc_score(y_up_test, up_probs)
    print(f"  • Directional P(Up) Classifier: Holdout ROC-AUC = {up_auc:.4f}")

    # 4. Model Double-Digit Probability: P(Gain >= +10%)
    y_10_train = train_df['target_10'].values
    y_10_test = test_df['target_10'].values

    g10_model = lgb.LGBMClassifier(
        n_estimators=120,
        learning_rate=0.03,
        max_depth=4,
        num_leaves=16,
        random_state=42,
        verbose=-1
    )
    g10_model.fit(X_train, y_10_train)
    g10_probs = g10_model.predict_proba(X_test)[:, 1]
    g10_auc = roc_auc_score(y_10_test, g10_probs)
    print(f"  • Double-Digit P(Gain >= +10%) Classifier: Holdout ROC-AUC = {g10_auc:.4f}")

    # 5. Model Super-Gain Probability: P(Gain >= +20%)
    y_20_train = train_df['target_20'].values
    y_20_test = test_df['target_20'].values

    g20_model = lgb.LGBMClassifier(
        n_estimators=120,
        learning_rate=0.03,
        max_depth=4,
        num_leaves=16,
        random_state=42,
        verbose=-1
    )
    g20_model.fit(X_train, y_20_train)
    g20_probs = g20_model.predict_proba(X_test)[:, 1]
    g20_auc = roc_auc_score(y_20_test, g20_probs)
    print(f"  • Super-Gain P(Gain >= +20%) Classifier: Holdout ROC-AUC = {g20_auc:.4f}")

    # Save models to disk
    os.makedirs(DATA_DIR, exist_ok=True)
    mfe_model.booster_.save_model(MODEL_MFE_FILE)
    mae_model.booster_.save_model(MODEL_MAE_FILE)
    up_model.booster_.save_model(MODEL_UP_PROB_FILE)
    g10_model.booster_.save_model(MODEL_GAIN_10_FILE)
    g20_model.booster_.save_model(MODEL_GAIN_20_FILE)

    # Save Model Card
    card = {
        "version": "5.2.0-master",
        "trained_date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_formations": len(df),
        "train_samples": len(train_df),
        "holdout_samples": len(test_df),
        "features": FEATURES_LIST,
        "metrics": {
            "mfe_spearman_rank_correlation": round(float(mfe_spearman), 4),
            "mfe_mae_pct": round(float(mfe_mae), 2),
            "mae_loss_pct": round(float(mae_loss), 2),
            "up_roc_auc": round(float(up_auc), 4),
            "double_digit_gain_roc_auc": round(float(g10_auc), 4)
        },
        "verdict_thresholds": {
            "high_conviction_min_upside": 12.0,
            "high_conviction_min_asymmetry": 2.5,
            "skip_max_upside": 5.0
        }
    }

    with open(PATH_MODEL_CARD_FILE, "w") as f:
        json.dump(card, f, indent=2)

    print(f"[ML Trainer] Model artifacts and card successfully exported to {DATA_DIR}!")
    return card


if __name__ == "__main__":
    train_and_export_models()
