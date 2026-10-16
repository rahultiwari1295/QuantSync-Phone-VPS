"""
banana_engine/core/portfolio_sharpe.py — Marginal Portfolio Sharpe Allocator (v5)
================================================================================
Implements institutional portfolio risk optimization:
  1. 180-Day Vasicek Shrunk Beta: beta_shrunk = 0.70 * beta_180d + 0.30 * 1.0
  2. 120-Day Shrunk Covariance (Ledoit-Wolf diagonal target)
  3. Marginal Sharpe Allocation (Delta Sharpe):
     Evaluates portfolio Sharpe before and after adding candidate setup.
     Yields diversification bonus (size_multiplier > 1.0) or concentration haircut (size_multiplier < 1.0).
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from core.data_layer import read_ohlcv_raw


_RETURNS_CACHE: Dict[str, pd.Series] = {}


def get_daily_returns(symbol: str, as_of_date: str, lookback_bars: int = 180) -> pd.Series:
    """
    Returns percentage daily returns for symbol up to as_of_date.
    """
    cache_key = f"{symbol.upper()}_{as_of_date}_{lookback_bars}"
    if cache_key in _RETURNS_CACHE:
        return _RETURNS_CACHE[cache_key]

    df = read_ohlcv_raw(symbol, end_date=as_of_date)
    if len(df) < 30:
        return pd.Series(dtype=float)

    recent = df.tail(lookback_bars + 1).copy()
    ret = recent['close'].pct_change().dropna()
    _RETURNS_CACHE[cache_key] = ret
    return ret


def compute_180d_shrunk_beta(
    symbol: str,
    as_of_date: str,
    benchmark_symbol: str = "RELIANCE",
    lookback: int = 180
) -> float:
    """
    Computes 180-day market beta shrunk toward 1.0:
      beta_shrunk = 0.70 * beta_sample + 0.30 * 1.0
    """
    sym_ret = get_daily_returns(symbol, as_of_date, lookback_bars=lookback)
    mkt_ret = get_daily_returns(benchmark_symbol, as_of_date, lookback_bars=lookback)

    if len(sym_ret) < 40 or len(mkt_ret) < 40:
        return 1.0

    # Align by length
    n = min(len(sym_ret), len(mkt_ret))
    r_s = sym_ret.tail(n).values
    r_m = mkt_ret.tail(n).values

    var_m = float(np.var(r_m))
    if var_m < 1e-8:
        return 1.0

    cov_sm = float(np.cov(r_s, r_m)[0, 1])
    sample_beta = cov_sm / var_m

    # Vasicek / Bayesian shrinkage toward grand mean 1.0
    beta_shrunk = float(0.70 * sample_beta + 0.30 * 1.0)
    return round(float(np.clip(beta_shrunk, 0.20, 2.50)), 2)


def compute_shrunk_covariance(
    symbols: List[str],
    as_of_date: str,
    lookback: int = 120,
    delta: float = 0.20
) -> Tuple[np.ndarray, List[str]]:
    """
    Computes sample covariance with Ledoit-Wolf diagonal shrinkage:
      Sigma_shrunk = (1 - delta) * S + delta * diag(S)
    """
    valid_symbols = []
    series_dict = {}

    for sym in symbols:
        ret = get_daily_returns(sym, as_of_date, lookback_bars=lookback)
        if len(ret) >= 30:
            valid_symbols.append(sym)
            series_dict[sym] = ret

    if not valid_symbols:
        return np.eye(1), ["CASH"]

    # Minimum common length
    min_len = min(len(s) for s in series_dict.values())
    ret_matrix = np.column_stack([series_dict[s].tail(min_len).values for s in valid_symbols])

    # Sample covariance (annualized 252 days)
    sample_cov = np.cov(ret_matrix, rowvar=False) * 252.0
    if sample_cov.ndim == 0:
        sample_cov = np.array([[float(sample_cov)]])

    # Diagonal target
    diag_target = np.diag(np.diag(sample_cov))

    # Shrinkage
    shrunk_cov = (1.0 - delta) * sample_cov + delta * diag_target
    return shrunk_cov, valid_symbols


def calculate_marginal_sharpe(
    candidate_symbol: str,
    candidate_expected_return: float,
    candidate_weight: float = 0.08,
    current_positions: Optional[List[Dict[str, Any]]] = None,
    as_of_date: str = "2024-05-01",
    risk_free_rate: float = 0.065
) -> Dict[str, Any]:
    """
    Computes the marginal Sharpe change (Delta Sharpe) of adding the candidate setup:
      Delta Sharpe = Sharpe(Portfolio + Candidate) - Sharpe(Portfolio)
    Generates institutional sizing multiplier.
    """
    if current_positions is None:
        current_positions = []

    sym_clean = candidate_symbol.upper().strip()
    shrunk_beta = compute_180d_shrunk_beta(sym_clean, as_of_date)

    # If portfolio is empty, evaluate candidate against cash baseline
    if not current_positions:
        # Volatility estimate from candidate returns
        c_ret = get_daily_returns(sym_clean, as_of_date, lookback_bars=120)
        c_vol = float(np.std(c_ret) * np.sqrt(252.0)) if len(c_ret) > 10 else 0.25
        base_sharpe = 0.0
        new_sharpe = max(0.0, (candidate_expected_return - risk_free_rate) / max(c_vol, 0.05))
        delta_sharpe = round(new_sharpe - base_sharpe, 3)

        return {
            "existing_sharpe": 0.0,
            "new_sharpe": round(new_sharpe, 2),
            "delta_sharpe": delta_sharpe,
            "shrunk_beta_180d": shrunk_beta,
            "diversification_multiplier": 1.0,
            "avg_holding_correlation": 0.0,
            "portfolio_action": "INITIATE_FIRST_POSITION"
        }

    # Construct existing portfolio weights and returns
    held_symbols = [p['symbol'].upper().strip() for p in current_positions if p.get('symbol')]
    held_weights = np.array([float(p.get('weight', 0.08)) for p in current_positions])
    held_exp_rets = np.array([float(p.get('expected_return', 0.15)) for p in current_positions])

    # Normalize existing weights
    tot_w = np.sum(held_weights)
    if tot_w > 0:
        w_exist = held_weights / tot_w
    else:
        w_exist = np.ones(len(held_symbols)) / len(held_symbols)

    cov_matrix_exist, valid_held = compute_shrunk_covariance(held_symbols, as_of_date)
    # Existing portfolio metrics
    var_exist = float(w_exist.T @ cov_matrix_exist @ w_exist)
    vol_exist = float(np.sqrt(max(var_exist, 1e-6)))
    ret_exist = float(np.sum(w_exist * held_exp_rets))
    sharpe_exist = (ret_exist - risk_free_rate) / max(vol_exist, 0.05)

    # Combined portfolio with candidate
    all_symbols = valid_held + [sym_clean]
    cov_all, valid_all = compute_shrunk_covariance(all_symbols, as_of_date)

    cand_w = candidate_weight
    # Scale down existing weights to accommodate candidate
    w_new = np.zeros(len(valid_all))
    for i, s in enumerate(valid_all):
        if s == sym_clean:
            w_new[i] = cand_w
        else:
            # find original weight
            idx_in_held = valid_held.index(s)
            w_new[i] = w_exist[idx_in_held] * (1.0 - cand_w)

    # Expected returns vector
    r_all = np.zeros(len(valid_all))
    for i, s in enumerate(valid_all):
        if s == sym_clean:
            r_all[i] = candidate_expected_return
        else:
            idx_in_held = valid_held.index(s)
            r_all[i] = held_exp_rets[idx_in_held]

    var_new = float(w_new.T @ cov_all @ w_new)
    vol_new = float(np.sqrt(max(var_new, 1e-6)))
    ret_new = float(np.sum(w_new * r_all))
    sharpe_new = (ret_new - risk_free_rate) / max(vol_new, 0.05)

    delta_sharpe = round(sharpe_new - sharpe_exist, 3)

    # Average correlation with existing holdings
    corrs = []
    cand_ret = get_daily_returns(sym_clean, as_of_date, lookback_bars=90)
    for s in valid_held:
        h_ret = get_daily_returns(s, as_of_date, lookback_bars=90)
        if len(cand_ret) > 20 and len(h_ret) > 20:
            n_min = min(len(cand_ret), len(h_ret))
            c_val = np.corrcoef(cand_ret.tail(n_min), h_ret.tail(n_min))[0, 1]
            if not np.isnan(c_val):
                corrs.append(float(c_val))

    avg_corr = round(float(np.mean(corrs)), 2) if corrs else 0.45

    # Determine institutional sizing multiplier based on Delta Sharpe & Correlation
    if delta_sharpe >= 0.05 and avg_corr < 0.35:
        mult = 1.20  # High diversification bonus
        action = "OVERWEIGHT (High Marginal Sharpe + Low Correlation)"
    elif delta_sharpe >= 0.01:
        mult = 1.05  # Slight diversification boost
        action = "NORMAL (Accretive to Portfolio Sharpe)"
    elif delta_sharpe >= -0.03:
        mult = 1.00  # Neutral
        action = "NORMAL (Neutral to Portfolio Sharpe)"
    elif avg_corr > 0.70:
        mult = 0.65  # Heavy correlation penalty
        action = "UNDERWEIGHT (High Correlation with Existing Holdings)"
    else:
        mult = 0.80  # Mild correlation haircut
        action = "UNDERWEIGHT (Dilutive to Portfolio Sharpe)"

    return {
        "existing_sharpe": round(sharpe_exist, 2),
        "new_sharpe": round(sharpe_new, 2),
        "delta_sharpe": delta_sharpe,
        "shrunk_beta_180d": shrunk_beta,
        "avg_holding_correlation": avg_corr,
        "diversification_multiplier": mult,
        "portfolio_action": action
    }


if __name__ == "__main__":
    print("Testing Portfolio Marginal Sharpe Allocator...")
    current_pos = [
        {"symbol": "TCS", "weight": 0.10, "expected_return": 0.18},
        {"symbol": "INFY", "weight": 0.10, "expected_return": 0.17}
    ]

    # Test candidate from same sector (WIPRO - should have high correlation / lower delta sharpe)
    res_tech = calculate_marginal_sharpe("WIPRO", candidate_expected_return=0.16, current_positions=current_pos)
    print("\nCandidate WIPRO (Tech Sector):")
    print(json.dumps(res_tech, indent=2))

    # Test candidate from uncorrelated sector (SUNPHARMA)
    res_pharma = calculate_marginal_sharpe("SUNPHARMA", candidate_expected_return=0.22, current_positions=current_pos)
    print("\nCandidate SUNPHARMA (Pharma Sector):")
    print(json.dumps(res_pharma, indent=2))
