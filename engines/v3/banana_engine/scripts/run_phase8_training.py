"""
scripts/run_phase8_training.py — Full walk-forward retraining & model card generation (Phase 8)
"""
import os
import sys
import json
import pandas as pd
import numpy as np

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.brain import train_walk_forward, save_artifacts, FEATURES, MODEL_CARD_FILE, MODEL_FILE
from core.data_layer import DATA_DIR

dataset_path = os.path.join(DATA_DIR, "backfill_dataset.parquet")
if not os.path.exists(dataset_path):
    print(f"Dataset not found at {dataset_path}")
    sys.exit(1)

df = pd.read_parquet(dataset_path)
print(f"Loaded dataset: {len(df)} rows from {df['date'].min()} to {df['date'].max()}")
print(f"RS rating: std={df['rs_rating'].std():.2f}, min={df['rs_rating'].min()}, max={df['rs_rating'].max()}")
print(f"Label mean: {df['label'].mean():.3f}")

print("\n--- Running Walk-Forward Three-Way Training ---")
model, iso, meta = train_walk_forward(df, features=FEATURES, label="label")

print("\n--- Training Results ---")
print(f"Mean AUC: {meta.get('mean_auc'):.4f}")
print(f"Calibration Brier Score: {meta.get('calibration_brier_score'):.4f}")
print("Per-fold details:")
for f in meta.get("folds", []):
    print(f"  Fold {f['fold']}: {f['test_start']} to {f['test_end']} (N={f['test_size']}), AUC={f['auc']:.4f}")

print("\nTop 10 Feature Importances:")
sorted_feats = sorted(meta.get("feature_importances", {}).items(), key=lambda x: x[1], reverse=True)[:10]
for f, imp in sorted_feats:
    print(f"  {f}: {imp}")

save_artifacts(model, iso, meta, dataset=df)
print("\nModel artifacts saved successfully.")

with open(MODEL_CARD_FILE, "r") as f:
    card = json.load(f)
print("\nGenerated Model Card:")
print(json.dumps(card, indent=2))
