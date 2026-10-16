# Fold 2 Inversion Hypothesis

## Fold 2 Summary Statistics
- **Test Period:** 2023-07-13 to 2023-12-04 ($N = 26$ test samples)
- **AUC:** 0.3846 (model ranked losing setups higher than winning setups)
- **Base Rate:** 50.0% (13 positives, 13 negatives)
- **Top 20 Predicted Setups Win Rate:** 40.0%
- **Bottom Predicted Setups Win Rate:** 50.0%
- **Market Benchmark:** Nifty 50 $+6.40\%$, Market Breadth $78.6\%$ positive

## Hypothesis: Mega-Cap Rotation Churn and Relative Strength Collapse

Fold 2 inverted because the model suffered from **relative strength regime mismatch and rotational churn in large-cap leadership**. During the test period (July to December 2023), market leadership rotated violently out of heavyweight frontline IT and private banking (which dominate this cohort sample, e.g., TCS, INFY, WIPRO, HDFCBANK) into broader market cyclicals, public sector enterprises, and mid-caps. As a consequence, the average `rs_rating` of test setups dropped sharply from $51.81 \pm 17.88$ in Fold 1 down to $33.85 \pm 12.56$ in Fold 2 (the lowest among all folds), and base compression degraded (`coil_ratio` fell to $0.92$, reflecting loose, non-contracting consolidation). The booster—trained on prior bull-leg regimes where high volume breakout signals in large caps yielded reliable momentum continuation—assigned its highest probabilities ($P_{\text{win}}$) to low-RS large-cap breakouts (e.g., TCS, SBIN, HDFCBANK) that repeatedly squatted and reversed within 20 bars, while lower-scored candidates in other sectors unexpectedly achieved targets. This failure highlights that without conditioning on cross-sectional market regime and sector leadership (the deferred regime models), single-model LightGBM overfits to historical leader patterns during active market rotational shifts.
