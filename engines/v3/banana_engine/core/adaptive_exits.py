"""
banana_engine/core/adaptive_exits.py — Regime-Adaptive Trade Exit State Machine
==============================================================================
Replaces rigid static stop/target exits with a dynamic, path-dependent post-entry state machine.

State Machine Architecture:
  State 0: ENTRY_ZONE (Days 1 - 3)
    - Day 1 open gap-down > 3% -> IMMEDIATE_EXIT (cut instantly at open)
    - 3 consecutive closes below entry -> REJECTION_EXIT (prune failed follow-through early)
    - Else -> Transitions to MONITORING

  State 1: MONITORING (Days 4 - 20)
    - Early velocity: MFE > 1.5R within first 5 days -> Promoted to RUNNER
    - Adverse excursion: MAE > 1.0R -> TIGHT_EXIT (downside protection)
    - Dead capital: Day 10 with MFE < 0.5R -> STALE_EXIT (recycle capital)
    - Target reached -> TARGET_HIT
    - Stop reached -> STOP_HIT

  State 2: RUNNER (Compounder ride mode)
    - Trailing stop = max(breakeven, highest_close * (1 - trail_pct))
    - trail_pct adapts to regime: BULL=4%, NEUTRAL=6%, BEAR=8%
    - No premature time cap; lets compounders run until trailing stop triggers

  State 3: STALE_EXIT
    - Gracefully exits at market when trade fails to gain traction after 12-15 days

  State 4: TIGHT_EXIT
    - Tightens stop to min loss (max(initial_stop, entry - 0.5R))
    - Allows up to 5 additional days to recover or exit
"""

import os
import sys
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np


def simulate_adaptive_exit(
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    regime: str = "BULL",
    max_bars: int = 40
) -> Dict[str, Any]:
    """
    Executes bar-by-bar path evaluation of a trade using the Adaptive Exit State Machine.
    
    Returns:
      Dict with outcome, exit_price, exit_idx, bars_held, r_multiple, pnl_pct, max_mfe_r, max_mae_r
    """
    one_r = max(entry_price - stop_price, entry_price * 0.01)
    
    # Adaptive trailing parameters by regime
    regime_u = str(regime).upper()
    if "BULL" in regime_u or "RISK_ON" in regime_u:
        trail_pct = 0.04
    elif "BEAR" in regime_u or "RISK_OFF" in regime_u:
        trail_pct = 0.07
    else:
        trail_pct = 0.05

    current_state = "ENTRY_ZONE"
    current_stop = stop_price
    runner_high = entry_price
    state_history = [(0, current_state)]
    
    consecutive_down_closes = 0
    tight_exit_counter = 0

    max_mfe_r = 0.0
    max_mae_r = 0.0

    total_bars = len(bars)
    exit_idx = min(entry_idx + max_bars, total_bars - 1)
    exit_price = float(bars['close'].iloc[exit_idx])
    final_outcome = "TIME_EXIT"

    for step, j in enumerate(range(entry_idx + 1, total_bars)):
        days_in_trade = step + 1
        if days_in_trade > max_bars and current_state != "RUNNER":
            exit_idx = j
            exit_price = float(bars['close'].iloc[j])
            final_outcome = "TIME_EXIT"
            break

        o = float(bars['open'].iloc[j])
        h = float(bars['high'].iloc[j])
        l = float(bars['low'].iloc[j])
        c = float(bars['close'].iloc[j])

        # Track MFE and MAE in R-units
        current_mfe_r = (h - entry_price) / one_r
        current_mae_r = (entry_price - l) / one_r
        if current_mfe_r > max_mfe_r:
            max_mfe_r = current_mfe_r
        if current_mae_r > max_mae_r:
            max_mae_r = current_mae_r

        # -------------------------------------------------------------
        # STATE 0: ENTRY_ZONE (Days 1 to 3)
        # -------------------------------------------------------------
        if current_state == "ENTRY_ZONE":
            # Check immediate Day 1 gap-down
            if days_in_trade == 1:
                if (o - entry_price) / entry_price < -0.03:
                    exit_idx = j
                    exit_price = o
                    final_outcome = "IMMEDIATE_EXIT"
                    break

            # Check stop loss
            if l <= current_stop:
                exit_idx = j
                exit_price = min(o, current_stop)
                final_outcome = "STOP_HIT"
                break

            # Count consecutive closes below entry
            if c < entry_price:
                consecutive_down_closes += 1
            else:
                consecutive_down_closes = 0

            # 3 consecutive closes below entry indicates failed follow-through
            if consecutive_down_closes >= 3:
                exit_idx = j
                exit_price = c
                final_outcome = "REJECTION_EXIT"
                break

            # Transition out of Entry Zone
            if days_in_trade >= 3:
                current_state = "MONITORING"
                state_history.append((days_in_trade, current_state))

        # -------------------------------------------------------------
        # STATE 1: MONITORING (Days 4+)
        # -------------------------------------------------------------
        if current_state == "MONITORING":
            # Check stop loss
            if l <= current_stop:
                exit_idx = j
                exit_price = min(o, current_stop)
                final_outcome = "STOP_HIT"
                break

            # Promotion to RUNNER: high velocity early (> 1.5R gain in first 5 days or > 2.0R anytime)
            if (days_in_trade <= 6 and current_mfe_r >= 1.5) or current_mfe_r >= 2.0:
                current_state = "RUNNER"
                runner_high = h
                current_stop = max(current_stop, entry_price * 1.01)  # Lock in breakeven + fee buffer
                state_history.append((days_in_trade, current_state))

            # Degradation to TIGHT_EXIT: sharp adverse move (> 0.8R drawdown)
            elif current_mae_r >= 0.8:
                current_state = "TIGHT_EXIT"
                current_stop = max(current_stop, entry_price - (0.5 * one_r))
                tight_exit_counter = 0
                state_history.append((days_in_trade, current_state))

            # Dead money prune: 10 days with < 0.5R MFE
            elif days_in_trade >= 10 and max_mfe_r < 0.5:
                current_state = "STALE_EXIT"
                state_history.append((days_in_trade, current_state))

            # Standard target hit if not promoted to runner
            elif h >= target_price:
                exit_idx = j
                exit_price = target_price
                final_outcome = "TARGET_HIT"
                break

        # -------------------------------------------------------------
        # STATE 2: RUNNER (Trailing Compounder Ride)
        # -------------------------------------------------------------
        if current_state == "RUNNER":
            if h > runner_high:
                runner_high = h
                # Ratchet trailing stop higher
                new_trail = runner_high * (1.0 - trail_pct)
                if new_trail > current_stop:
                    current_stop = new_trail

            if l <= current_stop:
                exit_idx = j
                exit_price = min(o, current_stop)
                final_outcome = "TRAILING_STOP"
                break

        # -------------------------------------------------------------
        # STATE 3: STALE_EXIT (Pruning Dead Capital)
        # -------------------------------------------------------------
        elif current_state == "STALE_EXIT":
            # Exit at market close
            exit_idx = j
            exit_price = c
            final_outcome = "STALE_EXIT"
            break

        # -------------------------------------------------------------
        # STATE 4: TIGHT_EXIT (Controlled Damage Control)
        # -------------------------------------------------------------
        elif current_state == "TIGHT_EXIT":
            tight_exit_counter += 1
            if l <= current_stop:
                exit_idx = j
                exit_price = min(o, current_stop)
                final_outcome = "TIGHT_STOP_HIT"
                break

            # Target reduced to +1.0R to take small profit/scratch if bounce occurs
            mod_target = entry_price + (1.0 * one_r)
            if h >= mod_target:
                exit_idx = j
                exit_price = mod_target
                final_outcome = "TIGHT_TARGET_HIT"
                break

            if tight_exit_counter >= 5:
                exit_idx = j
                exit_price = c
                final_outcome = "TIGHT_TIMEOUT_EXIT"
                break

    # Calculate final trade statistics
    bars_held = exit_idx - entry_idx
    realized_r = round((exit_price - entry_price) / one_r, 3)
    pnl_pct = round((exit_price / entry_price - 1.0) * 100.0, 2)

    return {
        "outcome": final_outcome,
        "exit_price": round(exit_price, 2),
        "exit_idx": exit_idx,
        "bars_held": bars_held,
        "r_multiple": realized_r,
        "pnl_pct": pnl_pct,
        "max_mfe_r": round(max_mfe_r, 2),
        "max_mae_r": round(max_mae_r, 2),
        "state_history": state_history,
    }


def simulate_baseline_fixed_exit(
    bars: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    max_bars: int = 30
) -> Dict[str, Any]:
    """
    Executes standard baseline fixed exit:
      - Fixed stop loss
      - Fixed target
      - Trailing stop locks 5% at +15% and 15% at +25%
      - Max bars time exit
      - Conservative same-bar tie break: stop wins
    """
    one_r = max(entry_price - stop_price, entry_price * 0.01)
    stop_p = stop_price
    trailing_activated = False
    total_bars = len(bars)
    exit_idx = min(entry_idx + max_bars, total_bars - 1)
    exit_price = float(bars['close'].iloc[exit_idx])
    outcome = "TIME_EXIT"

    max_mfe_r = 0.0
    max_mae_r = 0.0

    for step, j in enumerate(range(entry_idx + 1, total_bars)):
        if step >= max_bars:
            exit_idx = j
            exit_price = float(bars['close'].iloc[j])
            outcome = "TIME_EXIT"
            break

        h = float(bars['high'].iloc[j])
        l = float(bars['low'].iloc[j])
        c = float(bars['close'].iloc[j])

        mfe_r = (h - entry_price) / one_r
        mae_r = (entry_price - l) / one_r
        if mfe_r > max_mfe_r:
            max_mfe_r = mfe_r
        if mae_r > max_mae_r:
            max_mae_r = mae_r

        # Conservative tie break: stop first
        if l <= stop_p:
            exit_idx = j
            exit_price = stop_p
            outcome = "TRAILING_STOP" if (trailing_activated and stop_p >= entry_price) else "STOP_HIT"
            break

        # Trailing stop adjustments
        if not trailing_activated and h >= entry_price * 1.15:
            stop_p = max(stop_p, entry_price * 1.05)
            trailing_activated = True
        if trailing_activated and h >= entry_price * 1.25:
            stop_p = max(stop_p, entry_price * 1.15)

        if h >= target_price:
            exit_idx = j
            exit_price = target_price
            outcome = "TARGET_HIT"
            break

    bars_held = exit_idx - entry_idx
    realized_r = round((exit_price - entry_price) / one_r, 3)
    pnl_pct = round((exit_price / entry_price - 1.0) * 100.0, 2)

    return {
        "outcome": outcome,
        "exit_price": round(exit_price, 2),
        "exit_idx": exit_idx,
        "bars_held": bars_held,
        "r_multiple": realized_r,
        "pnl_pct": pnl_pct,
        "max_mfe_r": round(max_mfe_r, 2),
        "max_mae_r": round(max_mae_r, 2),
    }
