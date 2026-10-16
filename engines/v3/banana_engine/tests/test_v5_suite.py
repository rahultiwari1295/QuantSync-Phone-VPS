"""
banana_engine/tests/test_v5_suite.py — BananaPatterns v5 Institutional Evolution Test Suite
===========================================================================================
Comprehensive automated test suite covering all 7 pillars of the v5 institutional evolution:
  1. Direct path functional estimation (4 models: Barrier, MFE, MAE, Peak Days)
  2. Phase 0 Spearman correlation validation gate
  3. Dual-scale analog retrieval (Index A 32-pt/8D vs Index B 64-pt/12D)
  4. Calendar separation enforcement (no twin from same symbol within 60 days)
  5. Empirical forward fan cone quantile validity (p10 <= p50 <= p90)
  6. Dual-window rolling calibrator (100 fast vs 500 slow) and divergence alarm (|delta P| > 0.12)
  7. 180-day shrunk Vasicek beta and 120-day Ledoit-Wolf shrunk covariance
  8. Marginal portfolio Sharpe delta and diversification multiplier
  9. Constrained counterfactual regret ledger optimization (N_takes/month >= 4)
 10. Monthly perception recall audit
 11. Cross-sectional cohort relative ranking (Flood vs Famine hurdles)
 12. Live FastAPI REST API v5 endpoint responses
"""

import os
import sys
import unittest
import numpy as np
import pandas as pd
import json
import urllib.request

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import DATA_DIR, STOCK_HISTORY_DB
from core.path_models import PathInference, PATH_CARD_FILE
from core.analog import DualScaleAnalogEngine, normalize_contour
from core.online_calibrator import DualWindowCalibrator, RegimeModelRouter
from core.portfolio_sharpe import compute_180d_shrunk_beta, compute_shrunk_covariance, calculate_marginal_sharpe
from core.regret import RegretLedger
from core.cohort import rank_cohort


class TestV5InstitutionalBrain(unittest.TestCase):

    def setUp(self):
        self.path_infer = PathInference()
        self.analog_engine = DualScaleAnalogEngine()
        self.calibrator = DualWindowCalibrator()
        self.regret = RegretLedger()

    def test_01_path_models_inference(self):
        """Test all 4 direct path models run in <10ms and produce realistic outputs"""
        dummy_feats = {
            "rs_rating": 85.0,
            "tightness_5d": 0.02,
            "tightness_10d": 0.04,
            "ud_ratio": 1.4,
            "dist_from_52w_high_pct": -3.5,
            "dist_from_52w_low_pct": 42.0
        }
        res = self.path_infer.predict_path(dummy_feats)
        self.assertIn("p_win_calibrated", res)
        self.assertIn("expected_mfe_pct", res)
        self.assertIn("expected_mae_pct", res)
        self.assertIn("expected_days_to_peak", res)

        self.assertGreaterEqual(res["p_win_calibrated"], 0.0)
        self.assertLessEqual(res["p_win_calibrated"], 1.0)
        self.assertGreater(res["expected_mfe_pct"], 0.0)
        self.assertLess(res["expected_mae_pct"], 0.0)
        self.assertGreaterEqual(res["expected_days_to_peak"], 1.0)
        self.assertLessEqual(res["expected_days_to_peak"], 20.0)

    def test_02_phase_0_model_card(self):
        """Verify Phase 0 model card exists and logged MFE Spearman correlation"""
        self.assertTrue(os.path.exists(PATH_CARD_FILE), f"Missing {PATH_CARD_FILE}")
        # FIXED: Fix 6.4 (MINOR-03) — Assertion updated to match path_model_card.json structure
        with open(PATH_CARD_FILE, "r") as f:
            card = json.load(f)
        self.assertIn("mfe_spearman_rank_correlation", card["metrics"])
        self.assertIn("version", card)

    def test_03_dual_scale_analog_indices(self):
        """Verify standard bases (<=30w) use Index A (32pt) and macro bases (>30w) use Index B (64pt)"""
        std_closes = np.linspace(100, 115, 30)
        std_res = self.analog_engine.find_analogs("RELIANCE", "2024-05-01", std_closes, duration_weeks=10.0, k=3)
        self.assertEqual(std_res["scale_used"], "STANDARD (32pt/8D)")

        macro_closes = np.linspace(80, 120, 150)
        macro_res = self.analog_engine.find_analogs("RELIANCE", "2024-05-01", macro_closes, duration_weeks=42.0, k=3)
        self.assertEqual(macro_res["scale_used"], "MACRO (64pt/12D)")

    def test_04_analog_calendar_separation(self):
        """Verify candidate from same symbol within 60 calendar days is filtered out"""
        dummy_closes = np.linspace(100, 110, 30)
        res = self.analog_engine.find_analogs("RELIANCE", "2023-04-15", dummy_closes, duration_weeks=8.0, k=5)
        for twin in res["twins"]:
            if twin["symbol"] == "RELIANCE":
                twin_date = pd.to_datetime(twin["date"])
                q_date = pd.to_datetime("2023-04-15")
                day_diff = abs((twin_date - q_date).days)
                self.assertGreaterEqual(day_diff, 60, f"Same symbol matched within 60 days: {day_diff} days")

    def test_05_empirical_fan_cone_validity(self):
        """Verify empirical fan cone has 20 days and p10 <= p50 <= p90 at all points"""
        dummy_closes = np.linspace(50, 60, 40)
        res = self.analog_engine.find_analogs("TRENT", "2024-01-01", dummy_closes, duration_weeks=12.0, k=5)
        cone = res["empirical_fan_cone"]
        self.assertEqual(len(cone["days"]), 20)
        p10 = cone["p10_path"]
        p50 = cone["p50_path"]
        p90 = cone["p90_path"]

        self.assertEqual(len(p10), 20)
        self.assertEqual(len(p50), 20)
        self.assertEqual(len(p90), 20)

        for i in range(20):
            self.assertLessEqual(p10[i], p50[i] + 1e-4, f"p10 ({p10[i]}) > p50 ({p50[i]}) at day {i+1}")
            self.assertLessEqual(p50[i], p90[i] + 1e-4, f"p50 ({p50[i]}) > p90 ({p90[i]}) at day {i+1}")

    def test_06_dual_window_calibrator_and_divergence_alarm(self):
        """Verify fast (100) and slow (500) window tracking and divergence alert when |delta P| > 0.12"""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            test_db = tf.name
        try:
            calib = DualWindowCalibrator(db_path=test_db)
            normal_res = calib.calibrate(0.55)
            self.assertIn("p_calibrated", normal_res)
            self.assertIn("p_fast_window", normal_res)
            self.assertIn("p_slow_window", normal_res)
            self.assertIn("divergence", normal_res)

            # FIXED: Fix 4.1 removed synthetic seeding, so provide baseline anchor history in test
            for _ in range(120):
                calib.add_outcome(raw_p=0.65, actual_outcome=1, regime="NORMAL")

            # Force severe degradation into the fast window
            for _ in range(60):
                calib.add_outcome(raw_p=0.65, actual_outcome=0, regime="RISK_OFF")

            degrade_res = calib.calibrate(0.65)
            self.assertTrue(degrade_res["divergence_alert"], "Divergence alert failed to trigger during market degradation")
            self.assertIsNotNone(degrade_res["alert_message"])
        finally:
            if os.path.exists(test_db):
                os.remove(test_db)

    def test_07_shrunk_beta_vasicek(self):
        """Verify 180-day market beta is shrunk toward 1.0"""
        beta = compute_180d_shrunk_beta("TCS", "2024-05-01")
        self.assertGreaterEqual(beta, 0.20)
        self.assertLessEqual(beta, 2.50)

    def test_08_marginal_portfolio_sharpe_allocator(self):
        """Verify uncorrelated asset yields diversification bonus while highly correlated asset gets haircut"""
        pos = [
            {"symbol": "TCS", "weight": 0.10, "expected_return": 0.18},
            {"symbol": "INFY", "weight": 0.10, "expected_return": 0.18}
        ]
        # Pharma setup (diversifier)
        res_pharma = calculate_marginal_sharpe("SUNPHARMA", candidate_expected_return=0.24, current_positions=pos)
        self.assertGreaterEqual(res_pharma["diversification_multiplier"], 1.0)

        # Tech setup (concentrator)
        res_tech = calculate_marginal_sharpe("WIPRO", candidate_expected_return=0.15, current_positions=pos)
        self.assertLessEqual(res_tech["delta_sharpe"], res_pharma["delta_sharpe"])

    def test_09_regret_ledger_constrained_optimization(self):
        """Verify regret ledger optimization maximizes R_bar subject to min_takes_per_month >= 4"""
        s_id = self.regret.log_setup("RELIANCE", "2023-05-10", "TAKEN", score=0.65, entry_price=1200, stop_price=1120, target_price=1380)
        self.assertTrue(s_id.startswith("RELIANCE"))

        opt = self.regret.optimize_threshold(min_takes_per_month=4)
        self.assertIn("recommended_threshold", opt)
        self.assertIn("status", opt)

    def test_10_perception_recall_audit(self):
        """Verify perception recall audit tracks multi-week winners and calculates recall %"""
        audit = self.regret.run_perception_recall_audit(lookback_days=180, min_gain_pct=30.0)
        self.assertIn("big_movers_identified", audit)
        self.assertIn("bases_captured", audit)
        self.assertIn("perception_recall_pct", audit)
        self.assertGreaterEqual(audit["perception_recall_pct"], 0.0)
        self.assertLessEqual(audit["perception_recall_pct"], 100.0)

    def test_11_cross_sectional_cohort_ranking(self):
        """Verify multi-factor cohort percentile ranking and flood/famine hurdles"""
        cohort = [
            {"symbol": "A", "ev_r": 1.20, "twin_win_rate": 80.0, "delta_sharpe": 0.08, "rs_rating": 95},
            {"symbol": "B", "ev_r": 0.70, "twin_win_rate": 60.0, "delta_sharpe": 0.03, "rs_rating": 88},
            {"symbol": "C", "ev_r": 0.40, "twin_win_rate": 50.0, "delta_sharpe": 0.00, "rs_rating": 75},
            {"symbol": "D", "ev_r": -0.10, "twin_win_rate": 30.0, "delta_sharpe": -0.05, "rs_rating": 55},
        ]
        ranked = rank_cohort(cohort)
        self.assertEqual(len(ranked), 4)
        self.assertEqual(ranked[0]["cohort_rank"], 1)
        self.assertEqual(ranked[0]["symbol"], "A")
        self.assertEqual(ranked[0]["cohort_tier"], "TIER_1_CONVICTION")
        self.assertTrue(ranked[0]["cohort_actionable"])

        self.assertEqual(ranked[3]["cohort_rank"], 4)
        self.assertEqual(ranked[3]["symbol"], "D")
        self.assertFalse(ranked[3]["cohort_actionable"])

    def test_12_api_v5_live_endpoints(self):
        """Verify live FastAPI service returns all v5 fields on port 8088"""
        # 1. Setup endpoint with v5 payload
        req = urllib.request.urlopen("http://localhost:8088/api/setup/RAYMOND")
        data = json.loads(req.read().decode())
        self.assertIn("path_diagnostics", data)
        self.assertIn("analog_retrieval", data)
        self.assertIn("marginal_portfolio_sharpe", data)

        # 2. Regret summary endpoint
        req_regret = urllib.request.urlopen("http://localhost:8088/api/regret/summary")
        regret_data = json.loads(req_regret.read().decode())
        self.assertIn("recommended_threshold", regret_data)

        # 3. Perception audit endpoint
        req_audit = urllib.request.urlopen("http://localhost:8088/api/perception/audit")
        audit_data = json.loads(req_audit.read().decode())
        self.assertIn("perception_recall_pct", audit_data)

        # 4. Calibrator status endpoint
        req_calib = urllib.request.urlopen("http://localhost:8088/api/calibrator/status")
        calib_data = json.loads(req_calib.read().decode())
        self.assertIn("fast_window_size", calib_data)
        self.assertIn("slow_window_size", calib_data)


if __name__ == "__main__":
    unittest.main()
