import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import KFold

DATA_PATH = "QuantSync_Lines/banana_engine/data/ml_engine_v2_candidates.parquet"
df = pd.read_parquet(DATA_PATH)
train = df[df['year'] <= 2024].copy().reset_index(drop=True)

feature_cols = [
    'chase_pct', 'upper_wick_pct', 'lower_wick_pct', 'body_pct', 'cir', 'is_bull_body',
    'bar_ret', 'gap_pct', 'vol_ratio_20', 'vol_ratio_50', 'volume_slope_20',
    'ud_ratio_20', 'dry_up_ratio', 'dist_sma20', 'dist_sma50', 'dist_sma200',
    'sma50_slope_20', 'sma200_slope_20', 'ema_stack_score', 'ath_dist_pct',
    'dist_from_52w_high', 'dist_from_52w_low', 'is_htf', 'is_base', 'is_52w',
    'is_darvas', 'base_depth_pct', 'base_duration_bars', 'flag_pole_height_pct',
    'flag_duration_bars', 'risk_pct', 'r_ratio_available', 'w_uptrend',
    'w_sma_slope', 'w_rs_20', 'w_atr_pct', 'w_vol_ratio', 'w_base_depth',
    'w_base_tightness', 'w_position_52w', 'mtf_alignment', 'analog_win_rate',
    'analog_mean_r', 'analog_weighted_r', 'analog_best_r', 'analog_worst_r',
    'analog_std_r', 'analog_top_similarity'
]

# Let's train 5-fold CV models
oof_preds = np.zeros(len(train))
kf = KFold(n_splits=5, shuffle=False)

for trn_idx, val_idx in kf.split(train):
    X_tr, y_tr = train.loc[trn_idx, feature_cols], train.loc[trn_idx, 'label']
    X_va = train.loc[val_idx, feature_cols]
    clf = lgb.LGBMClassifier(
        n_estimators=150, learning_rate=0.03, num_leaves=31,
        subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1
    )
    clf.fit(X_tr, y_tr)
    oof_preds[val_idx] = clf.predict_proba(X_va)[:, 1]

train['pred_prob'] = oof_preds

# Let's test a sweep of target R: 1.8R, 2.0R, 2.2R, 2.5R, 3.0R
# And tranche splits: e.g. 50% at 2.0R, 50% at 3.0R
# For exactly top 10% (1,136 trades = 227/yr) and top 11% (1,250 trades = 250/yr)

print("=== SWEEP OVER TARGET R FOR 225-250 TRADES/YR (Top 10-11% ML Rank) ===")
for pct in [9, 10, 11]:
    cutoff = np.percentile(train['pred_prob'], 100 - pct)
    sub = train[train['pred_prob'] >= cutoff].copy()
    n_yr = len(sub) / 5.0
    print(f"\n--- Top {pct}% ({len(sub)} trades, {n_yr:.0f} trades/yr) ---")
    
    for tgt in [1.8, 2.0, 2.2, 2.5, 3.0]:
        # Did it reach target before stop?
        # Note: if mfe_r >= tgt and label == 1 (meaning it reached at least 2.0R before stop)
        if tgt <= 2.0:
            won = (sub['mfe_r'] >= tgt) & (sub['mae_r'] > -1.0)
            # Actually, label == 1 means reached 2R before stop, so if tgt <= 2, it definitely won if label == 1 or mfe >= tgt before stop
            won = (sub['label'] == 1) | ((sub['mfe_r'] >= tgt) & (sub['pnl_r'] > 0))
        else:
            won = (sub['label'] == 1) & (sub['mfe_r'] >= tgt)
            
        wr = won.mean() * 100.0
        payoff = tgt
        # Loss is -1.0R
        pf = (wr / 100.0 * payoff) / ((1.0 - wr / 100.0) * 1.0)
        ann_r = (len(sub) / 5.0) * (wr / 100.0 * payoff - (1.0 - wr / 100.0) * 1.0)
        print(f"  Target {tgt:.1f}R: WR = {wr:5.1f}% | Payoff = {payoff:.1f}:1 | PF = {pf:4.2f} | Ann R = +{ann_r:5.1f}R")

    # What about Tranche 50/50: 50% @ 2.0R, 50% @ 3.0R?
    w_t1 = (sub['label'] == 1) # reached 2R
    w_t2 = (sub['label'] == 1) & (sub['mfe_r'] >= 3.0) # reached 3R
    # Total R per trade:
    # If didn't reach 2R: -1.0R
    # If reached 2R but not 3R: +1.0R (0.5 * 2.0 + 0.5 * 0 if breakeven, or 0.5 * 2.0 - 0.5 * 1.0 = +0.5R)
    # If reached 3R: 0.5 * 2.0 + 0.5 * 3.0 = +2.5R
    r_no_be = np.where(w_t2, 2.5, np.where(w_t1, 0.5, -1.0))
    wr_tranche = (r_no_be > 0).mean() * 100.0
    wins_r = r_no_be[r_no_be > 0].sum()
    loss_r = abs(r_no_be[r_no_be <= 0].sum())
    pf_tranche = wins_r / loss_r if loss_r > 0 else 0
    ann_r_tranche = r_no_be.sum() / 5.0
    print(f"  50/50 Tranche (2R/3R): WR = {wr_tranche:5.1f}% | PF = {pf_tranche:4.2f} | Ann R = +{ann_r_tranche:5.1f}R")
