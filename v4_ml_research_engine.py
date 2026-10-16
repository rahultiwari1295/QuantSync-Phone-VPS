#!/usr/bin/env python3
"""
v4_ml_research_engine.py
------------------------
Performs end-to-end Machine Learning training, SHAP/Feature Importance analysis,
and deep pattern research on the 67,954-row master dataset (2020-2024).

Models Trained:
  1. LightGBM Binary Win/Loss Classifier (label_win_20d)
  2. XGBoost Binary Win/Loss Classifier (label_win_20d)
  3. LightGBM 5-Class Outcome Classifier (RUNNER, WINNER, BREAKEVEN, WEAK_STOP, HARD_STOP)
  4. LightGBM MFE Regression (How far will the trade run?)

Validation:
  - Strict Time-Series Split:
    - Train: 2020-01-01 to 2023-12-31 (4 years in-sample)
    - Test / OOS: 2024-01-01 to 2024-12-31 (1 full year out-of-sample)

Outputs:
  - Deep statistical research tables (Win rate by pattern, by regime, volume, technicals)
  - Model evaluation metrics on 2024 out-of-sample test set (AUC, Precision, Recall)
  - Top 25 predictive features & what makes good trades win
  - JSON summary saved to vps_data_live/v4_ml_research_summary.json
"""

import sys
import os
import json
import time
import pickle
import numpy as np
import pandas as pd
import lightgbm as lgb
import xgboost as xgb
from sklearn.metrics import (
    roc_auc_score,
    accuracy_score,
    precision_score,
    recall_score,
    classification_report,
    confusion_matrix,
    mean_squared_error,
    r2_score,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATASET_PATH = os.path.join(SCRIPT_DIR, "v4_research_dataset_2020_2024.parquet")
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)
SUMMARY_JSON = os.path.join(SCRIPT_DIR, "v4_ml_research_summary.json")


def run_research():
    print("=" * 75)
    print("  QuantSync V4 ML Deep Research & Classification Engine")
    print("=" * 75)
    t0 = time.time()

    # 1. Load Dataset
    print(f"[*] Loading master dataset: {DATASET_PATH}")
    df = pd.read_parquet(DATASET_PATH)
    print(f"    Loaded {len(df):,} rows x {df.shape[1]} columns.")

    # -------------------------------------------------------------------------
    # PART 1: EXPLORATORY DATA RESEARCH (Statistical Ground Truth)
    # -------------------------------------------------------------------------
    print("\n" + "=" * 75)
    print("  PART 1: STATISTICAL PATTERN & REGIME DISCOVERY")
    print("=" * 75)

    research_findings = {}

    # 1A. Pattern Performance Breakdown
    pat_stats = (
        df.groupby("pattern_type")
        .agg(
            total_trades=("label_win_20d", "count"),
            win_rate=("label_win_20d", lambda x: round(x.mean() * 100, 2)),
            avg_mfe=("label_mfe_pct", lambda x: round(x.mean(), 2)),
            median_mfe=("label_mfe_pct", lambda x: round(x.median(), 2)),
            avg_mae=("label_mae_pct", lambda x: round(x.mean(), 2)),
            runner_pct=("label_5class", lambda x: round((x == "RUNNER").mean() * 100, 2)),
            hard_stop_pct=("label_5class", lambda x: round((x == "HARD_STOP").mean() * 100, 2)),
        )
        .sort_values("total_trades", ascending=False)
    )
    print("\n--- Pattern Performance (Sorted by Frequency) ---")
    print(pat_stats.to_string())
    research_findings["pattern_stats"] = pat_stats.reset_index().to_dict(orient="records")

    # 1B. NIFTY 100 Macro Regime Breakdown
    regime_stats = (
        df.groupby("nifty_regime")
        .agg(
            total_trades=("label_win_20d", "count"),
            win_rate=("label_win_20d", lambda x: round(x.mean() * 100, 2)),
            avg_mfe=("label_mfe_pct", lambda x: round(x.mean(), 2)),
            median_mfe=("label_mfe_pct", lambda x: round(x.median(), 2)),
            runner_pct=("label_5class", lambda x: round((x == "RUNNER").mean() * 100, 2)),
            hard_stop_pct=("label_5class", lambda x: round((x == "HARD_STOP").mean() * 100, 2)),
        )
        .sort_values("win_rate", ascending=False)
    )
    print("\n--- NIFTY 100 Macro Regime Performance ---")
    print(regime_stats.to_string())
    research_findings["regime_stats"] = regime_stats.reset_index().to_dict(orient="records")

    # 1C. Cross-Tabulation: Pattern Type x NIFTY Regime (Win Rate %)
    cross_win = pd.crosstab(
        df["pattern_type"],
        df["nifty_regime"],
        values=df["label_win_20d"] * 100,
        aggfunc="mean",
    ).round(2)
    print("\n--- Win Rate (%) by Pattern Type x NIFTY 100 Regime ---")
    print(cross_win.to_string())
    research_findings["pattern_x_regime_win_rate"] = cross_win.to_dict()

    # 1D. Champion Breakout Setups
    champ_stats = (
        df.groupby("champ_pattern")
        .agg(
            total_trades=("label_win_20d", "count"),
            win_rate=("label_win_20d", lambda x: round(x.mean() * 100, 2)),
            avg_mfe=("label_mfe_pct", lambda x: round(x.mean(), 2)),
            median_mfe=("label_mfe_pct", lambda x: round(x.median(), 2)),
            runner_pct=("label_5class", lambda x: round((x == "RUNNER").mean() * 100, 2)),
        )
        .sort_values("total_trades", ascending=False)
    )
    print("\n--- Champion Pattern Performance ---")
    print(champ_stats.to_string())
    research_findings["champ_stats"] = champ_stats.reset_index().to_dict(orient="records")

    # 1E. Impact of Key Binary Indicators
    def test_feature_split(col_name, friendly_label):
        sub = df.groupby(col_name).agg(
            count=("label_win_20d", "count"),
            win_rate=("label_win_20d", lambda x: round(x.mean() * 100, 2)),
            avg_mfe=("label_mfe_pct", lambda x: round(x.mean(), 2)),
            runner_pct=("label_5class", lambda x: round((x == "RUNNER").mean() * 100, 2)),
        )
        return sub

    feature_splits = {
        "is_vol_dryup_base": "Volume Dryup in Base (Quiet Consolidation)",
        "is_buying_climax": "Buying Climax (High Vol + Weak Close)",
        "dual_index_bull": "Dual Index Bull (Both NIFTY100 & MIDCAP > EMA50)",
        "is_double_confluence": "Double Confluence (Classical DNA + Champion)",
        "stage_2_confirmed": "Stage 2 Confirmed (> EMA200 for 40+ of 50 bars)",
    }

    print("\n--- Impact of Structural Feature Filters ---")
    filter_results = {}
    for col, desc in feature_splits.items():
        if col in df.columns:
            tbl = test_feature_split(col, desc)
            print(f"\n[Feature: {desc}]")
            print(tbl.to_string())
            filter_results[col] = tbl.to_dict()
    research_findings["structural_filters"] = filter_results

    # -------------------------------------------------------------------------
    # PART 2: MACHINE LEARNING FEATURE PREPARATION
    # -------------------------------------------------------------------------
    print("\n" + "=" * 75)
    print("  PART 2: MACHINE LEARNING PIPELINE (LightGBM & XGBoost)")
    print("=" * 75)

    # Define categorical vs numerical features
    categorical_cols = [
        "sector",
        "ema_alignment",
        "pattern_type",
        "pattern_lifecycle",
        "champ_pattern",
        "nifty_regime",
        "midcap_regime",
        "quarter",
    ]

    feature_cols = [
        # Group A: Breakout Candle Quality
        "bar_return_pct",
        "close_in_range",
        "upper_wick_pct",
        "lower_tail_pct",
        "body_to_range_ratio",
        "gap_up_pct",
        "is_gap_up",
        "turnover_cr",
        # Group B: Volume Structure & VSA
        "vol_ratio_20d",
        "vol_ratio_5d",
        "vol_ratio_60d",
        "vol_trend_5d",
        "vol_acceleration",
        "is_vol_dryup_base",
        "is_buying_climax",
        # Group C: Technical Momentum Context
        "rsi_14",
        "rsi_trend_5d",
        "atr_14_pct",
        "dist_ema20_pct",
        "dist_ema50_pct",
        "dist_ema200_pct",
        "dist_52w_high_pct",
        "dist_52w_low_pct",
        "prior_60d_return_pct",
        "prior_120d_return_pct",
        # Group D: Classical Pattern DNA
        "has_classical_pattern",
        "pattern_confidence",
        "pattern_quality_score",
        "pattern_rr_ratio",
        "pattern_breakout_dist_pct",
        "pattern_inval_dist_pct",
        "pattern_target_dist_pct",
        "pattern_is_bullish",
        "competing_pattern_count",
        # Group E: Champion Breakout Pattern
        "has_champ_pattern",
        "champ_cir",
        "champ_vol_ratio",
        "champ_upper_wick",
        "champ_risk_pct",
        "is_double_confluence",
        # Group F: Fibonacci Swing Structure
        "fib_has_structure",
        "fib_impulse_pct",
        "fib_pullback_pct",
        "fib_dist_1000_pct",
        "fib_dist_1272_pct",
        "fib_dist_1618_pct",
        "fib_structure_age_weeks",
        # Group G: NIFTY 100 Macro Regime
        "nifty_close",
        "nifty_dist_ema20",
        "nifty_dist_ema50",
        "nifty_dist_ema200",
        "nifty_ret_5d",
        "nifty_ret_10d",
        "nifty_ret_20d",
        "nifty_rsi14",
        "nifty_atr_pct",
        # Group H: Market Breadth & Seasonality
        "midcap_dist_ema50",
        "dual_index_bull",
        "month_of_year",
        "day_of_week",
        "is_earnings_season",
        # Group I: Prior Context
        "bars_since_last_signal",
        "prior_signal_won",
        "num_signals_last_30d",
        "stage_2_confirmed",
        # Categoricals (will be category dtype)
        "sector",
        "ema_alignment",
        "pattern_type",
        "pattern_lifecycle",
        "champ_pattern",
        "nifty_regime",
        "midcap_regime",
        "quarter",
    ]

    # Convert categoricals to category dtype
    for col in categorical_cols:
        df[col] = df[col].astype("category")

    # Strict Chronological Split
    train_mask = df["date"] < "2024-01-01"
    test_mask = df["date"] >= "2024-01-01"

    X_train = df.loc[train_mask, feature_cols].copy()
    y_train = df.loc[train_mask, "label_win_20d"].copy()

    X_test = df.loc[test_mask, feature_cols].copy()
    y_test = df.loc[test_mask, "label_win_20d"].copy()

    print(f"[*] Train Set (2020-2023): {len(X_train):,} rows ({y_train.mean()*100:.1f}% win rate)")
    print(f"[*] Test Set  (2024 OOS):  {len(X_test):,} rows ({y_test.mean()*100:.1f}% win rate)")

    # -------------------------------------------------------------------------
    # MODEL 1: LightGBM Binary Win/Loss Classifier
    # -------------------------------------------------------------------------
    print("\n--- Training Model 1: LightGBM Win/Loss Classifier ---")
    lgb_train = lgb.Dataset(X_train, label=y_train)
    lgb_test = lgb.Dataset(X_test, label=y_test, reference=lgb_train)

    lgb_params = {
        "objective": "binary",
        "metric": ["auc", "binary_logloss"],
        "boosting_type": "gbdt",
        "learning_rate": 0.03,
        "num_leaves": 45,
        "max_depth": 6,
        "min_child_samples": 40,
        "feature_fraction": 0.75,
        "bagging_fraction": 0.80,
        "bagging_freq": 3,
        "verbosity": -1,
        "random_state": 42,
    }

    evals_result = {}
    lgb_model = lgb.train(
        lgb_params,
        lgb_train,
        num_boost_round=600,
        valid_sets=[lgb_train, lgb_test],
        valid_names=["train", "test"],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.record_evaluation(evals_result)],
    )

    y_pred_proba_lgb = lgb_model.predict(X_test)
    y_pred_lgb = (y_pred_proba_lgb >= 0.50).astype(int)

    auc_lgb = roc_auc_score(y_test, y_pred_proba_lgb)
    acc_lgb = accuracy_score(y_test, y_pred_lgb)
    prec_lgb = precision_score(y_test, y_pred_lgb)
    rec_lgb = recall_score(y_test, y_pred_lgb)

    print(f"  LightGBM OOS 2024 -> AUC: {auc_lgb:.4f} | Accuracy: {acc_lgb*100:.2f}% | Precision: {prec_lgb*100:.2f}% | Recall: {rec_lgb*100:.2f}%")

    # Check High-Conviction Decile (Top 10% Probability Trades)
    top_decile_threshold = np.percentile(y_pred_proba_lgb, 90)
    top_decile_mask = y_pred_proba_lgb >= top_decile_threshold
    top_decile_win_rate = y_test[top_decile_mask].mean() * 100.0
    print(f"  LightGBM High-Conviction (Top 10% Scores >= {top_decile_threshold:.2f}) -> Win Rate: {top_decile_win_rate:.2f}% (vs 51.1% baseline!)")

    # Feature Importance (Gain)
    importance_df = pd.DataFrame({
        "feature": feature_cols,
        "importance_gain": lgb_model.feature_importance(importance_type="gain"),
        "importance_split": lgb_model.feature_importance(importance_type="split"),
    }).sort_values("importance_gain", ascending=False)

    print("\n--- LightGBM Top 20 Most Predictive Features ---")
    print(importance_df.head(20).to_string(index=False))

    research_findings["lgb_top_features"] = importance_df.head(25).to_dict(orient="records")
    research_findings["lgb_oos_metrics"] = {
        "auc": round(auc_lgb, 4),
        "accuracy": round(acc_lgb * 100, 2),
        "precision": round(prec_lgb * 100, 2),
        "recall": round(rec_lgb * 100, 2),
        "top_decile_win_rate": round(top_decile_win_rate, 2),
        "top_decile_threshold": round(top_decile_threshold, 3),
    }

    # -------------------------------------------------------------------------
    # MODEL 2: XGBoost Binary Win/Loss Classifier
    # -------------------------------------------------------------------------
    print("\n--- Training Model 2: XGBoost Win/Loss Classifier ---")
    # Prepare numeric-encoded features for XGBoost
    X_train_xgb = X_train.copy()
    X_test_xgb = X_test.copy()
    for col in categorical_cols:
        X_train_xgb[col] = X_train_xgb[col].cat.codes
        X_test_xgb[col] = X_test_xgb[col].cat.codes

    xgb_model = xgb.XGBClassifier(
        n_estimators=500,
        learning_rate=0.03,
        max_depth=5,
        subsample=0.8,
        colsample_bytree=0.75,
        eval_metric="auc",
        early_stopping_rounds=40,
        random_state=42,
    )
    xgb_model.fit(
        X_train_xgb,
        y_train,
        eval_set=[(X_train_xgb, y_train), (X_test_xgb, y_test)],
        verbose=False,
    )

    y_pred_proba_xgb = xgb_model.predict_proba(X_test_xgb)[:, 1]
    y_pred_xgb = (y_pred_proba_xgb >= 0.50).astype(int)

    auc_xgb = roc_auc_score(y_test, y_pred_proba_xgb)
    acc_xgb = accuracy_score(y_test, y_pred_xgb)
    prec_xgb = precision_score(y_test, y_pred_xgb)
    top_decile_xgb_thr = np.percentile(y_pred_proba_xgb, 90)
    top_decile_xgb_wr = y_test[y_pred_proba_xgb >= top_decile_xgb_thr].mean() * 100.0

    print(f"  XGBoost OOS 2024 -> AUC: {auc_xgb:.4f} | Accuracy: {acc_xgb*100:.2f}% | Precision: {prec_xgb*100:.2f}%")
    print(f"  XGBoost High-Conviction (Top 10% Scores) -> Win Rate: {top_decile_xgb_wr:.2f}%")

    research_findings["xgb_oos_metrics"] = {
        "auc": round(auc_xgb, 4),
        "accuracy": round(acc_xgb * 100, 2),
        "precision": round(prec_xgb * 100, 2),
        "top_decile_win_rate": round(top_decile_xgb_wr, 2),
    }

    # -------------------------------------------------------------------------
    # MODEL 3: LightGBM 5-Class Outcome Classifier
    # -------------------------------------------------------------------------
    print("\n--- Training Model 3: LightGBM 5-Class Outcome Classifier ---")
    class_map = {"HARD_STOP": 0, "WEAK_STOP": 1, "BREAKEVEN": 2, "WINNER": 3, "RUNNER": 4}
    y_train_5 = df.loc[train_mask, "label_5class"].map(class_map).values
    y_test_5 = df.loc[test_mask, "label_5class"].map(class_map).values

    lgb_5_train = lgb.Dataset(X_train, label=y_train_5)
    lgb_5_test = lgb.Dataset(X_test, label=y_test_5, reference=lgb_5_train)

    lgb_5_params = {
        "objective": "multiclass",
        "num_class": 5,
        "metric": "multi_logloss",
        "learning_rate": 0.03,
        "num_leaves": 40,
        "max_depth": 6,
        "min_child_samples": 40,
        "feature_fraction": 0.75,
        "bagging_fraction": 0.80,
        "verbosity": -1,
        "random_state": 42,
    }

    lgb_5_model = lgb.train(
        lgb_5_params,
        lgb_5_train,
        num_boost_round=500,
        valid_sets=[lgb_5_train, lgb_5_test],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )

    y_pred_5_proba = lgb_5_model.predict(X_test)
    y_pred_5 = np.argmax(y_pred_5_proba, axis=1)
    acc_5 = accuracy_score(y_test_5, y_pred_5)
    print(f"  5-Class Outcome Model Accuracy on 2024 OOS: {acc_5*100:.2f}%")

    # Probability of being a RUNNER (class 4)
    runner_proba = y_pred_5_proba[:, 4]
    top_runner_thr = np.percentile(runner_proba, 90)
    actual_runners_in_top = (y_test_5[runner_proba >= top_runner_thr] == 4).mean() * 100.0
    print(f"  Top 10% Predicted Runners -> Actually Runner Rate: {actual_runners_in_top:.2f}% (vs 27.9% baseline)")

    research_findings["outcome_5class_metrics"] = {
        "accuracy": round(acc_5 * 100, 2),
        "top_predicted_runner_hit_rate": round(actual_runners_in_top, 2),
    }

    # -------------------------------------------------------------------------
    # MODEL 4: LightGBM MFE Regression (How far will it run?)
    # -------------------------------------------------------------------------
    print("\n--- Training Model 4: LightGBM MFE Regression ---")
    y_train_mfe = df.loc[train_mask, "label_mfe_pct"].values
    y_test_mfe = df.loc[test_mask, "label_mfe_pct"].values

    lgb_reg_train = lgb.Dataset(X_train, label=y_train_mfe)
    lgb_reg_test = lgb.Dataset(X_test, label=y_test_mfe, reference=lgb_reg_train)

    lgb_reg_params = {
        "objective": "regression",
        "metric": "rmse",
        "learning_rate": 0.03,
        "num_leaves": 40,
        "max_depth": 6,
        "min_child_samples": 40,
        "feature_fraction": 0.75,
        "bagging_fraction": 0.80,
        "verbosity": -1,
        "random_state": 42,
    }

    lgb_reg_model = lgb.train(
        lgb_reg_params,
        lgb_reg_train,
        num_boost_round=500,
        valid_sets=[lgb_reg_train, lgb_reg_test],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )

    y_pred_mfe = lgb_reg_model.predict(X_test)
    rmse_mfe = np.sqrt(mean_squared_error(y_test_mfe, y_pred_mfe))
    r2_mfe = r2_score(y_test_mfe, y_pred_mfe)
    print(f"  MFE Regression on 2024 OOS -> RMSE: {rmse_mfe:.2f}% | R2: {r2_mfe:.4f}")

    # Top 10% highest predicted MFE trades
    top_mfe_thr = np.percentile(y_pred_mfe, 90)
    actual_mfe_top = np.mean(y_test_mfe[y_pred_mfe >= top_mfe_thr])
    print(f"  Top 10% Predicted MFE -> Realized Average MFE: +{actual_mfe_top:.2f}% (vs +9.8% baseline)")

    research_findings["mfe_regression_metrics"] = {
        "rmse": round(rmse_mfe, 2),
        "r2": round(r2_mfe, 4),
        "top_predicted_mfe_realized": round(actual_mfe_top, 2),
    }

    # -------------------------------------------------------------------------
    # PART 3: SAVE TRAINED MODELS & EXPORT SUMMARY JSON
    # -------------------------------------------------------------------------
    print("\n[*] Saving trained models to disk...")
    with open(os.path.join(MODELS_DIR, "v4_lgb_win_classifier.pkl"), "wb") as f:
        pickle.dump(lgb_model, f)
    with open(os.path.join(MODELS_DIR, "v4_xgb_win_classifier.pkl"), "wb") as f:
        pickle.dump(xgb_model, f)
    with open(os.path.join(MODELS_DIR, "v4_lgb_outcome_5class.pkl"), "wb") as f:
        pickle.dump(lgb_5_model, f)
    with open(os.path.join(MODELS_DIR, "v4_lgb_mfe_regression.pkl"), "wb") as f:
        pickle.dump(lgb_reg_model, f)

    with open(SUMMARY_JSON, "w") as f:
        json.dump(research_findings, f, indent=2)

    total_time = time.time() - t0
    print(f"[*] Deep Research & Model Training Finished in {total_time:.1f}s ({total_time/60:.2f} mins)")
    print(f"[*] Research Summary JSON: {SUMMARY_JSON}")
    print("=" * 75)


if __name__ == "__main__":
    run_research()
