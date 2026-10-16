"""
QuantSync Daily V2 Trade Health & State Machine Engine
------------------------------------------------------
Evaluates active open positions on every daily candle close.
Categorizes positions into HEALTHY, WARNING, or INVALIDATED states
with strict precedence: Hard Stop -> Health Invalidation -> MFE Trailing -> Warning Tightening.
"""

from typing import Dict, List, Any, Tuple, Optional
from daily_v2.config import HEALTH_CONFIG


class TradeHealthState:
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    INVALIDATED = "INVALIDATED"
    EXITED = "EXITED"


class DailyV2HealthEngine:

    @staticmethod
    def evaluate_position_health(
        trade: Dict[str, Any],
        today_candle: Dict[str, Any],
        indicators: Dict[str, Any],
        idx: int,
        days_held: int
    ) -> Dict[str, Any]:
        """
        Evaluates the health of an active trade at bar `idx` and returns state + exit decision.
        """
        c = float(today_candle['c'])
        h = float(today_candle['h'])
        l = float(today_candle['l'])
        entry_price = float(trade['entry_price'])

        # Update MFE and MAE
        current_mfe = max(float(trade.get('mfe_pct', 0.0)), (h - entry_price) / max(1e-4, entry_price) * 100.0)
        current_mae = min(float(trade.get('mae_pct', 0.0)), (l - entry_price) / max(1e-4, entry_price) * 100.0)
        current_roi = (c - entry_price) / max(1e-4, entry_price) * 100.0

        e20 = float(indicators['ema20'][idx])
        e50 = float(indicators['ema50'][idx])
        rsi = float(indicators['rsi14'][idx])

        # Exit Precedence Hierarchy

        # 1. Hard Risk Stop (-8.0%)
        if current_mae <= HEALTH_CONFIG['HARD_STOP_LOSS_PCT'] or current_roi <= HEALTH_CONFIG['HARD_STOP_LOSS_PCT']:
            return {
                'health_state': TradeHealthState.INVALIDATED,
                'action': 'EXIT',
                'exit_reason': f"HARD_STOP_LOSS (Loss={min(current_mae, current_roi):.2f}% <= {HEALTH_CONFIG['HARD_STOP_LOSS_PCT']}%)",
                'mfe_pct': round(current_mfe, 2),
                'mae_pct': round(current_mae, 2),
                'roi_pct': round(current_roi, 2),
                'exit_price': c
            }

        # 2. Structural Health Invalidation (Daily Close below 50 EMA)
        if c < e50 and days_held >= 3:
            return {
                'health_state': TradeHealthState.INVALIDATED,
                'action': 'EXIT',
                'exit_reason': f"STRUCTURE_INVALIDATION (Close {c:.2f} < Daily 50 EMA {e50:.2f})",
                'mfe_pct': round(current_mfe, 2),
                'mae_pct': round(current_mae, 2),
                'roi_pct': round(current_roi, 2),
                'exit_price': c
            }

        # 3. Dynamic MFE Trailing Profit Protection
        # If compounder expansion (MFE >= 35%), lock 75% of peak MFE
        if current_mfe >= 35.0:
            trailing_floor = current_mfe * HEALTH_CONFIG['MFE_TRAIL_CAPTURE_PCT']
            if current_roi <= trailing_floor:
                return {
                    'health_state': TradeHealthState.INVALIDATED,
                    'action': 'EXIT',
                    'exit_reason': f"COMPOUNDER_MFE_TRAILING_EXIT (Captured {current_roi:.1f}% vs peak {current_mfe:.1f}%)",
                    'mfe_pct': round(current_mfe, 2),
                    'mae_pct': round(current_mae, 2),
                    'roi_pct': round(current_roi, 2),
                    'exit_price': c
                }

        # If moderate expansion (MFE >= 15%), lock 60% of peak MFE
        elif current_mfe >= HEALTH_CONFIG['MFE_EXPANSION_HURDLE']:
            trailing_floor = current_mfe * HEALTH_CONFIG['MODERATE_WINNER_TRAIL']
            if current_roi <= trailing_floor:
                return {
                    'health_state': TradeHealthState.INVALIDATED,
                    'action': 'EXIT',
                    'exit_reason': f"MODERATE_MFE_TRAILING_EXIT (Captured {current_roi:.1f}% vs peak {current_mfe:.1f}%)",
                    'mfe_pct': round(current_mfe, 2),
                    'mae_pct': round(current_mae, 2),
                    'roi_pct': round(current_roi, 2),
                    'exit_price': c
                }

        # 4. Early Warning Breakdown (Cut early at -5.0% within first 5 days if momentum fails)
        if days_held <= 5 and current_mae <= HEALTH_CONFIG['EARLY_WARNING_MAE_PCT'] and current_mfe < 3.0:
            return {
                'health_state': TradeHealthState.INVALIDATED,
                'action': 'EXIT',
                'exit_reason': f"EARLY_WARNING_CUT (Initial breakdown at {current_mae:.2f}% without upside)",
                'mfe_pct': round(current_mfe, 2),
                'mae_pct': round(current_mae, 2),
                'roi_pct': round(current_roi, 2),
                'exit_price': c
            }

        # 5. Warning State Monitoring (Non-terminal: tighten risk / monitor for recovery)
        is_warning = False
        warning_notes = []

        if c <= e20:
            is_warning = True
            warning_notes.append("Close <= Daily 20 EMA")
        if rsi < HEALTH_CONFIG['WARNING_RSI_THRESHOLD']:
            is_warning = True
            warning_notes.append(f"RSI {rsi:.1f} < {HEALTH_CONFIG['WARNING_RSI_THRESHOLD']}")

        if is_warning:
            return {
                'health_state': TradeHealthState.WARNING,
                'action': 'HOLD_WARNING',
                'exit_reason': None,
                'warning_detail': "; ".join(warning_notes),
                'mfe_pct': round(current_mfe, 2),
                'mae_pct': round(current_mae, 2),
                'roi_pct': round(current_roi, 2),
                'exit_price': None
            }

        # 6. Healthy State (Hold for expansion)
        return {
            'health_state': TradeHealthState.HEALTHY,
            'action': 'HOLD_HEALTHY',
            'exit_reason': None,
            'warning_detail': None,
            'mfe_pct': round(current_mfe, 2),
            'mae_pct': round(current_mae, 2),
            'roi_pct': round(current_roi, 2),
            'exit_price': None
        }
