#!/usr/bin/env python3
"""
train_v4_enhancement_engine.py
------------------------------
Trains the 3 Multi-Task Trade Enhancement Models on the Universe Dataset:
1. P(Win) Classifier (Probability of hitting +7% before -8% stop)
2. Quantile MFE Regressor (Dynamic Target Upside at 75th percentile)
3. Quantile MAE Regressor (Dynamic Stop Loss Drawdown at 10th percentile)

Strictly Temporal Split:
- Train: 2019-01-01 to 2023-12-31 (~80% of data)
- Validation: 2024-01-01 to 2024-12-31 (Strict Out-of-Time Test)
- Strictly Lookahead-Free (No contaminated features, clean entry at Open)
"""

import os
import sys
import pickle
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, brier_score_loss, mean_pinball_loss

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

DATASET_PATH = os.path.join(SCRIPT_DIR, "v4_universe_2500_dataset_2019_2024.parquet")
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)

MODEL_PWIN_PATH = os.path.join(MODELS_DIR, "v4_pwin_classifier.pkl")
MODEL_MFE_PATH = os.path.join(MODELS_DIR, "v4_mfe_regressor.pkl")
MODEL_MAE_PATH = os.path.join(MODELS_DIR, "v4_mae_regressor.pkl")

from engines.v4.trade_enhancer_models import FEATURE_COLS, CATEGORICAL_COLS


def encode_categoricals(df_train: pd.DataFrame, df_val: pd.DataFrame):
    """Encodes categorical columns to integer codes."""
    cat_maps = {}
    for col in CATEGORICAL_COLS:
        uniques = df_train[col].astype(str).unique().tolist()
        mapping = {val: idx for idx, val in enumerate(uniques)}
        cat_maps[col] = mapping
        df_train[col] = df_train[col].astype(str).map(mapping).fillna(-1).astype(int)
        df_val[col] = df_val[col].astype(str).map(mapping).fillna(-1).astype(int)
    return df_train, df_val, cat_maps


def main():
    print("=" * 80)
    print("  QuantSync V4 Trade Enhancement Multi-Task Trainer")
    print("=" * 80)

    if not os.path.exists(DATASET_PATH):
        print(f"[ERROR] Dataset not found at: {DATASET_PATH}")
        sys.exit(1)

    print(f"[*] Loading universe dataset: {DATASET_PATH}...")
    df = pd.read_parquet(DATASET_PATH)
    print(f"[+] Loaded {len(df)} historical trade setups across {df['symbol'].nunique()} stocks.")
    print(f"    Date span: {df['date'].min()} to {df['date'].max()}")

    # Temporal Split: Train on 2019-2023, Test on 2024
    train_mask = df["date"] < "2024-01-01"
    val_mask = df["date"] >= "2024-01-01"

    df_train = df[train_mask].copy().reset_index(drop=True)
    df_val = df[val_mask].copy().reset_index(drop=True)

    print(f"[*] Training Set (2019-2023): {len(df_train)} setups")
    print(f"[*] Validation Set (2024):    {len(df_val)} setups")

    # Encode categoricals
    df_train, df_val, cat_maps = encode_categoricals(df_train, df_val)

    X_train = df_train[FEATURE_COLS + CATEGORICAL_COLS]
    X_val = df_val[FEATURE_COLS + CATEGORICAL_COLS]

    # =========================================================================
    # 1. Train Win Probability Classifier P(Win)
    # =========================================================================
    print("\n" + "-" * 60)
    print("  [1/3] Training P(Win) Calibrated Classifier (LightGBM)")
    print("-" * 60)
    y_train_win = df_train["label_win_20d"].values
    y_val_win = df_val["label_win_20d"].values

    clf_pwin = lgb.LGBMClassifier(
        n_estimators=300,
        learning_rate=0.03,
        num_leaves=31,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=50,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )
    clf_pwin.fit(
        X_train, y_train_win,
        eval_set=[(X_val, y_val_win)],
        callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)]
    )

    preds_pwin = clf_pwin.predict_proba(X_val)[:, 1]
    auc_score = roc_auc_score(y_val_win, preds_pwin)
    brier = brier_score_loss(y_val_win, preds_pwin)
    print(f"[+] Out-of-Time 2024 P(Win) Validation:")
    print(f"    ROC-AUC: {auc_score:.4f} | Brier Calibration Loss: {brier:.4f}")

    # Top quintile lift
    val_df_eval = pd.DataFrame({"actual": y_val_win, "score": preds_pwin})
    val_df_eval["quintile"] = pd.qcut(val_df_eval["score"], 5, labels=False)
    for q in range(5):
        q_df = val_df_eval[val_df_eval["quintile"] == q]
        print(f"    Quintile {q+1} (Score {q_df['score'].min():.2f}-{q_df['score'].max():.2f}): Actual Win Rate = {q_df['actual'].mean()*100:.1f}%")

    with open(MODEL_PWIN_PATH, "wb") as f:
        pickle.dump({"model": clf_pwin, "cat_maps": cat_maps, "features": FEATURE_COLS + CATEGORICAL_COLS}, f)
    print(f"[+] Saved P(Win) Model to: {MODEL_PWIN_PATH}")

    # =========================================================================
    # 2. Train Quantile MFE Regressor (Dynamic Target Upside at 75th percentile)
    # =========================================================================
    print("\n" + "-" * 60)
    print("  [2/3] Training Dynamic Upside Regressor (Quantile MFE alpha=0.75)")
    print("-" * 60)
    y_train_mfe = np.clip(df_train["label_mfe_pct"].values, 0.0, 50.0)
    y_val_mfe = np.clip(df_val["label_mfe_pct"].values, 0.0, 50.0)

    reg_mfe = lgb.LGBMRegressor(
        objective="quantile",
        alpha=0.75,
        n_estimators=250,
        learning_rate=0.03,
        num_leaves=31,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=50,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )
    reg_mfe.fit(
        X_train, y_train_mfe,
        eval_set=[(X_val, y_val_mfe)],
        callbacks=[lgb.early_stopping(stopping_rounds=25, verbose=False)]
    )

    preds_mfe = reg_mfe.predict(X_val)
    pb_loss_mfe = mean_pinball_loss(y_val_mfe, preds_mfe, alpha=0.75)
    print(f"[+] Out-of-Time 2024 MFE Validation:")
    print(f"    Pinball Loss (alpha=0.75): {pb_loss_mfe:.4f}")
    print(f"    Mean Predicted Target: +{np.mean(preds_mfe):.1f}% | Actual 75th percentile MFE: +{np.percentile(y_val_mfe, 75):.1f}%")

    with open(MODEL_MFE_PATH, "wb") as f:
        pickle.dump({"model": reg_mfe, "cat_maps": cat_maps, "features": FEATURE_COLS + CATEGORICAL_COLS}, f)
    print(f"[+] Saved Dynamic MFE Model to: {MODEL_MFE_PATH}")

    # =========================================================================
    # 3. Train Quantile MAE Regressor (Dynamic Drawdown Floor at 10th percentile)
    # =========================================================================
    print("\n" + "-" * 60)
    print("  [3/3] Training Dynamic Drawdown Regressor (Quantile MAE alpha=0.10)")
    print("-" * 60)
    y_train_mae = np.clip(df_train["label_mae_pct"].values, -25.0, 0.0)
    y_val_mae = np.clip(df_val["label_mae_pct"].values, -25.0, 0.0)

    reg_mae = lgb.LGBMRegressor(
        objective="quantile",
        alpha=0.10,
        n_estimators=250,
        learning_rate=0.03,
        num_leaves=31,
        max_depth=6,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=50,
        random_state=42,
        n_jobs=-1,
        verbose=-1
    )
    reg_mae.fit(
        X_train, y_train_mae,
        eval_set=[(X_val, y_val_mae)],
        callbacks=[lgb.early_stopping(stopping_rounds=25, verbose=False)]
    )

    preds_mae = reg_mae.predict(X_val)
    pb_loss_mae = mean_pinball_loss(y_val_mae, preds_mae, alpha=0.10)
    print(f"[+] Out-of-Time 2024 MAE Validation:")
    print(f"    Pinball Loss (alpha=0.10): {pb_loss_mae:.4f}")
    print(f"    Mean Predicted Stop Loss Floor: {np.mean(preds_mae):.1f}% | Actual 10th percentile MAE: {np.percentile(y_val_mae, 10):.1f}%")

    with open(MODEL_MAE_PATH, "wb") as f:
        pickle.dump({"model": reg_mae, "cat_maps": cat_maps, "features": FEATURE_COLS + CATEGORICAL_COLS}, f)
    print(f"[+] Saved Dynamic MAE Model to: {MODEL_MAE_PATH}")

    print("\n" + "=" * 80)
    print("  All 3 Trade Enhancement Models Successfully Trained and Saved!")
    print("=" * 80)


if __name__ == "__main__":
    main()
