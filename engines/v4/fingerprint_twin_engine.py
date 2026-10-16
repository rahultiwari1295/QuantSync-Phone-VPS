#!/usr/bin/env python3
"""
fingerprint_twin_engine.py
--------------------------
QuantSync Neural Pattern DNA & Historical Twin Retrieval Engine.
Indexes 100,000+ historical trade setups from 2019 to 2024 across 2,170+ Indian equities.

Features:
- Instant (<5ms) Vector Similarity Search using normalized 64-D Pattern Fingerprints.
- Empirical Bayesian Outcome Distribution from Top-K Historical Twins:
  * Empirical Win Rate (% that hit +7% before -8%)
  * Expected Maximum Upside (MFE %)
  * Expected Drawdown Floor (MAE %)
  * Velocity (Days to Peak Gain)
- Top-K Historical Twin Dossier with chart dates, patterns, and realized trajectory.
"""

import os
import sys
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
VPS_DATA_DIR = os.path.abspath(os.path.join(MODULE_DIR, "..", ".."))

EMBEDDINGS_PATH = os.path.join(VPS_DATA_DIR, "models", "v4_fingerprint_embeddings.npy")
METADATA_PATH = os.path.join(VPS_DATA_DIR, "models", "v4_fingerprint_metadata.parquet")


class PatternFingerprintEngine:
    def __init__(self, embeddings_path: str = EMBEDDINGS_PATH, metadata_path: str = METADATA_PATH):
        self.embeddings_path = embeddings_path
        self.metadata_path = metadata_path
        self.embeddings: Optional[np.ndarray] = None
        self.metadata: Optional[pd.DataFrame] = None
        self._is_loaded = False

    def load_index(self) -> bool:
        """Load the historical fingerprint vector matrix and metadata into memory."""
        if not os.path.exists(self.embeddings_path) or not os.path.exists(self.metadata_path):
            print(f"[WARN] Fingerprint index files not found at {self.embeddings_path}")
            return False

        try:
            self.embeddings = np.load(self.embeddings_path)
            self.embeddings = np.nan_to_num(self.embeddings, nan=0.0, posinf=1.0, neginf=-1.0)
            self.metadata = pd.read_parquet(self.metadata_path)
            # Ensure unit-normalization for cosine similarity
            norms = np.linalg.norm(self.embeddings, axis=1, keepdims=True)
            norms[norms < 1e-6] = 1.0
            self.embeddings = self.embeddings / norms
            self._is_loaded = True
            print(f"[+] Loaded Fingerprint Memory Bank: {len(self.metadata)} historical setups ({self.embeddings.shape[1]}-D vectors).")
            return True
        except Exception as e:
            print(f"[ERROR] Failed loading fingerprint index: {e}")
            return False

    def find_historical_twins(
        self,
        candidate_vector: np.ndarray,
        top_k: int = 20,
        min_similarity: float = 0.70,
        exclude_symbol: Optional[str] = None
    ) -> Dict:
        """
        Finds the top-K closest historical twins from the 2019-2024 database.
        Returns empirical outcome distribution and details of the top twins.
        """
        if not self._is_loaded:
            if not self.load_index():
                return {"error": "Index not loaded"}

        # Normalize candidate vector with NaN protection
        vec = np.nan_to_num(candidate_vector.flatten().astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec = vec / norm
        else:
            return {"error": "Zero vector"}

        # Fast BLAS Dot-product (Cosine similarity) across all setups
        scores = np.dot(self.embeddings, vec)

        # Optional: Mask out candidate itself if inspecting past data
        if exclude_symbol:
            sym_mask = (self.metadata["symbol"] != exclude_symbol).values
            scores = np.where(sym_mask, scores, -1.0)

        # Get top-K indices
        top_indices = np.argsort(scores)[::-1][:top_k]
        top_scores = scores[top_indices]

        # Filter by min_similarity
        valid_mask = top_scores >= min_similarity
        valid_indices = top_indices[valid_mask]
        valid_scores = top_scores[valid_mask]

        if len(valid_indices) == 0:
            # Fallback to top 5 even if below min_similarity
            valid_indices = top_indices[:5]
            valid_scores = top_scores[:5]

        # Extract twin slice
        twins_df = self.metadata.iloc[valid_indices].copy()
        twins_df["similarity"] = valid_scores

        # Compute Empirical Outcome Statistics
        win_rate = float(twins_df["label_win_20d"].mean() * 100.0)
        avg_mfe = float(twins_df["label_mfe_pct"].mean())
        median_mfe = float(twins_df["label_mfe_pct"].median())
        avg_mae = float(twins_df["label_mae_pct"].mean())
        median_mae = float(twins_df["label_mae_pct"].median())
        avg_ret_20d = float(twins_df["label_ret_20d"].mean())
        avg_bars_to_mfe = float(twins_df["label_bars_to_mfe"].mean())

        # Top 5 most prominent twins for UI display
        dossier_list = []
        for _, row in twins_df.head(5).iterrows():
            dossier_list.append({
                "symbol": str(row["symbol"]),
                "date": str(row["date"]),
                "entry_price": float(row["entry_price"]),
                "pattern_type": str(row.get("pattern_type", "NONE")),
                "similarity_score": round(float(row["similarity"]), 3),
                "similarity_pct": f"{float(row['similarity']) * 100:.1f}%",
                "realized_win": bool(row["label_win_20d"] == 1),
                "mfe_gain_pct": round(float(row["label_mfe_pct"]), 1),
                "mae_drawdown_pct": round(float(row["label_mae_pct"]), 1),
                "bars_to_peak": int(row["label_bars_to_mfe"]),
                "final_return_pct": round(float(row["label_ret_20d"]), 1),
            })

        return {
            "total_twins_analyzed": len(twins_df),
            "top_similarity": round(float(valid_scores[0]), 3) if len(valid_scores) > 0 else 0.0,
            "empirical_win_rate_pct": round(win_rate, 1),
            "expected_upside_mfe_pct": round(median_mfe, 1),
            "expected_drawdown_mae_pct": round(median_mae, 1),
            "expected_days_to_target": round(avg_bars_to_mfe, 1),
            "expected_20d_return_pct": round(avg_ret_20d, 1),
            "historical_twins": dossier_list,
        }
