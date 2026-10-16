"""
QuantSync Daily V2 Trading Engine (Frozen Snapshot Version: DAILY_V2_FROZEN_2026_08_30_V1)
------------------------------------------------------------------------------------------
Orchestrates end-of-day execution for Daily V2:
1. Health evaluation & exits for existing active positions
2. Strategy evaluation across all 9 surviving strategies
3. PIT feature extraction & ML inference (Calibrated P >= 0.50)
4. Strict Symbol Deduplication: ONE Stock = ONE Active Opportunity (highest P wins, competing metadata saved)
5. Daily Top 1% Ranking within each trading day
6. Active Position Blocking (No Pyramiding)
7. Same-Day Close execution & SQLite persistence
"""

import logging
import json
import numpy as np
from typing import Dict, List, Any, Optional
from datetime import datetime

from daily_v2.config import (
    DAILY_V2_VERSION,
    DAILY_V2_ML_THRESHOLD,
    DAILY_CAPACITY_LIMIT,
    ACTIVE_STRATEGIES
)
from daily_v2.primitives import (
    calculate_stock_indicators,
    compute_nifty_real_market_features
)
from daily_v2.strategies import evaluate_all_daily_v2_strategies
from daily_v2.features import extract_point_in_time_features
from daily_v2.model import DailyV2ModelManager, get_daily_v2_model
from daily_v2.health import DailyV2HealthEngine
from daily_v2.storage import DailyV2Storage

logger = logging.getLogger("DailyV2Engine")

STRATEGY_PRECEDENCE = {
    "Breakout": 1,
    "Alpha": 2,
    "CarterSqueeze": 3,
    "Decouple": 4,
    "Snapback": 5,
    "TurtleSoup": 6,
    "DipBuyer": 7,
    "WyckoffSpring": 8,
    "NR7": 9,
    "HyperCoil": 10
}


class DailyV2Engine:
    def __init__(
        self,
        storage: Optional[DailyV2Storage] = None,
        model_manager: Optional[DailyV2ModelManager] = None,
        dry_run: bool = False
    ):
        self.storage = storage or DailyV2Storage()
        self.model_manager = model_manager or get_daily_v2_model()
        self.dry_run = dry_run

    def run_daily_v2_for_date(
        self,
        target_date: str,
        phase: str = "DECISION",
        market_data: Optional[Dict[str, Any]] = None,
        dry_run: Optional[bool] = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        CANONICAL MULTI-PHASE DAILY V2 STATE TRANSITION FUNCTION.
        Single source of truth for historical replay, initial population, and live execution.
        Supported phases:
        - 'DECISION': 15:15 IST cutoff. Evaluates exits for entry_date < T, scores candidates, opens Top 3 Max trades.
        - 'FINALIZE': 15:45 IST EOD. Reconciles EOD prices, evaluates exits for entry_date < T, finalizes state.
        """
        if dry_run is not None:
            self.dry_run = dry_run

        if market_data is None:
            from run_daily_cycle import load_vps_market_data
            market_data = load_vps_market_data()

        return self.run_daily_cycle(market_data=market_data, target_date=target_date, phase=phase, force=force)

    def run_daily_cycle(
        self,
        market_data: Dict[str, Any],
        target_date: Optional[str] = None,
        phase: str = "DECISION",
        force: bool = False
    ) -> Dict[str, Any]:
        # Idempotency Guard: Safe NO-OP if requested phase already committed for target_date
        if not self.dry_run and not force and target_date:
            if self.storage.is_phase_committed(target_date, phase):
                logger.info(f"Phase ({target_date}, {phase}) is already committed in DB. Returning idempotent NO-OP.")
                run_info = self.storage.get_cycle_run(target_date, phase)
                return {
                    'status': 'ALREADY_COMMITTED',
                    'cycle_version': DAILY_V2_VERSION,
                    'dry_run': self.dry_run,
                    'target_date': target_date,
                    'phase': phase,
                    'run_info': run_info,
                    'new_entries': [],
                    'qualified_entries': [],
                    'exits': [],
                    'exits_executed': [],
                    'active_positions_count': len(self.storage.get_active_trades())
                }

        # Normalize input market_data format
        if 'stock_candles_map' in market_data:
            stock_map = market_data['stock_candles_map']
            nifty_candles = market_data.get('nifty_candles') or stock_map.get('NIFTY', stock_map.get('RELIANCE', []))
        else:
            stock_map = market_data
            n_raw = market_data.get('NIFTY', market_data.get('RELIANCE', []))
            nifty_candles = n_raw.get('candles', []) if isinstance(n_raw, dict) else n_raw

        if not nifty_candles:
            for s_val in stock_map.values():
                c_list = s_val.get('candles', []) if isinstance(s_val, dict) else s_val
                if isinstance(c_list, list) and len(c_list) >= 60:
                    nifty_candles = c_list
                    break

        # Point-in-time slicing for NIFTY if target_date provided
        if target_date and nifty_candles:
            n_idx = -1
            for i, c in enumerate(nifty_candles):
                if str(c['t'])[:10] <= target_date:
                    n_idx = i
                else:
                    break
            if n_idx >= 0:
                nifty_candles = nifty_candles[:n_idx + 1]

        nifty_feature_map = compute_nifty_real_market_features(nifty_candles)
        cycle_eval_date = target_date or (nifty_candles[-1]['t'][:10] if nifty_candles else 'N/A')

        # -------------------------------------------------------------
        # STEP 1: Process Exits & Health Updates for Active Positions
        # -------------------------------------------------------------
        active_trades = self.storage.get_active_trades()
        exits_executed = []
        health_updates = []

        for trade in active_trades:
            sym = trade['symbol']
            entry_d = trade['entry_date']

            # SAME-DAY ENTRY PROTECTION (CRITICAL INVARIANT):
            # A position created on date T MUST NOT be eligible for exit on date T!
            # It becomes exit-eligible on date T+1 (entry_date < target_date).
            if target_date and entry_d >= target_date:
                continue

            stock_data = stock_map.get(sym)
            if stock_data is None:
                continue

            raw_c = stock_data.get('candles', []) if isinstance(stock_data, dict) else stock_data
            if not isinstance(raw_c, list) or len(raw_c) < 60:
                continue

            # Point-in-time slice for active trade
            if isinstance(stock_data, dict) and 'indicators' in stock_data:
                candles = stock_data['candles']
                indicators = stock_data['indicators']
                if target_date:
                    t_idx = -1
                    for i, c in enumerate(candles):
                        if str(c['t'])[:10] <= target_date:
                            t_idx = i
                        else:
                            break
                    if t_idx < 59:
                        continue
                    current_idx = t_idx
                else:
                    current_idx = len(candles) - 1
            else:
                if target_date:
                    t_idx = -1
                    for i, c in enumerate(raw_c):
                        if str(c['t'])[:10] <= target_date:
                            t_idx = i
                        else:
                            break
                    if t_idx < 59:
                        continue
                    candles = raw_c[:t_idx + 1]
                else:
                    candles = raw_c
                indicators = calculate_stock_indicators(candles)
                current_idx = len(candles) - 1

            today_candle = candles[current_idx]
            eval_date = today_candle['t'][:10]

            # Calculate actual holding period
            try:
                d1 = datetime.strptime(entry_d, "%Y-%m-%d")
                d2 = datetime.strptime(eval_date, "%Y-%m-%d")
                days_held = max(1, (d2 - d1).days)
            except Exception:
                days_held = max(1, trade.get('holding_days', 0) + 1)

            health_res = DailyV2HealthEngine.evaluate_position_health(
                trade=trade,
                today_candle=today_candle,
                indicators=indicators,
                idx=current_idx,
                days_held=days_held
            )

            if health_res['action'] == 'EXIT':
                exits_executed.append({
                    'trade_id': trade['id'],
                    'symbol': sym,
                    'strategy_id': trade['strategy_id'],
                    'exit_price': health_res['exit_price'],
                    'exit_reason': health_res['exit_reason'],
                    'roi_pct': health_res['roi_pct'],
                    'holding_days': days_held
                })
                if not self.dry_run:
                    self.storage.close_trade(trade['id'], health_res, eval_date, days_held)
                logger.info(f"EXIT EXECUTED: {sym} ({trade['strategy_id']}) ROI={health_res['roi_pct']:+.2f}% | Reason={health_res['exit_reason']}")
            else:
                health_updates.append({
                    'trade_id': trade['id'],
                    'symbol': sym,
                    'health_state': health_res['health_state'],
                    'roi_pct': health_res['roi_pct']
                })
                if not self.dry_run:
                    self.storage.update_trade_health(trade['id'], health_res, days_held)

        # -------------------------------------------------------------
        # STEP 2: Evaluate All 9 Strategies & Generate Raw Candidates
        # (Executed in DECISION phase; FINALIZE phase reconciles without opening new entries)
        # -------------------------------------------------------------
        raw_candidates_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        raw_candidates_count = 0

        if phase == "DECISION":
            for sym, stock_data in stock_map.items():
                if sym == 'NIFTY':
                    continue
                raw_c = stock_data.get('candles', []) if isinstance(stock_data, dict) else stock_data
                if not isinstance(raw_c, list) or len(raw_c) < 60:
                    continue

                # Point-in-time slice
                if isinstance(stock_data, dict) and 'indicators' in stock_data:
                    candles = stock_data['candles']
                    indicators = stock_data['indicators']
                    if target_date:
                        t_idx = -1
                        for i, c in enumerate(candles):
                            if str(c['t'])[:10] <= target_date:
                                t_idx = i
                            else:
                                break
                        if t_idx < 59:
                            continue
                        current_idx = t_idx
                    else:
                        current_idx = len(candles) - 1
                else:
                    if target_date:
                        t_idx = -1
                        for i, c in enumerate(raw_c):
                            if str(c['t'])[:10] <= target_date:
                                t_idx = i
                            else:
                                break
                        if t_idx < 59:
                            continue
                        candles = raw_c[:t_idx + 1]
                    else:
                        candles = raw_c
                    indicators = calculate_stock_indicators(candles)
                    current_idx = len(candles) - 1

                eval_date = candles[current_idx]['t'][:10]

                # In historical replay or date-specific mode, only evaluate setups if stock traded on target_date
                if target_date and eval_date != target_date:
                    continue
                cycle_eval_date = eval_date

                cands = evaluate_all_daily_v2_strategies(
                    sym, candles, indicators, current_idx, nifty_feature_map, include_hypercoil=False
                )
                raw_candidates_count += len(cands)

                for cand in cands:
                    strat_id = cand['strategy_id']
                    if strat_id not in ACTIVE_STRATEGIES:
                        continue

                    feat_dict = extract_point_in_time_features(sym, candles, indicators, current_idx, strat_id, nifty_feature_map)
                    ml_pred = self.model_manager.predict_candidate(feat_dict)
                    p_val = ml_pred['calibrated_probability']

                    cand_audit = {
                        'symbol': sym,
                        'strategy_id': strat_id,
                        'date': eval_date,
                        'competing_strategies': [],
                        **ml_pred
                    }

                    if sym not in raw_candidates_by_symbol:
                        raw_candidates_by_symbol[sym] = []

                    raw_candidates_by_symbol[sym].append({
                        'cand': cand,
                        'pred': ml_pred,
                        'features': feat_dict,
                        'p_val': p_val,
                        'strat_rank': STRATEGY_PRECEDENCE.get(strat_id, 99),
                        'eval_date': eval_date,
                        'cand_audit': cand_audit
                    })

            cands = evaluate_all_daily_v2_strategies(
                sym, candles, indicators, current_idx, nifty_feature_map, include_hypercoil=False
            )
            raw_candidates_count += len(cands)

            for cand in cands:
                strat_id = cand['strategy_id']
                if strat_id not in ACTIVE_STRATEGIES:
                    continue

                feat_dict = extract_point_in_time_features(sym, candles, indicators, current_idx, strat_id, nifty_feature_map)
                ml_pred = self.model_manager.predict_candidate(feat_dict)
                p_val = ml_pred['calibrated_probability']

                cand_audit = {
                    'symbol': sym,
                    'strategy_id': strat_id,
                    'date': eval_date,
                    'competing_strategies': [],
                    **ml_pred
                }

                if sym not in raw_candidates_by_symbol:
                    raw_candidates_by_symbol[sym] = []

                raw_candidates_by_symbol[sym].append({
                    'cand': cand,
                    'pred': ml_pred,
                    'features': feat_dict,
                    'p_val': p_val,
                    'strat_rank': STRATEGY_PRECEDENCE.get(strat_id, 99),
                    'eval_date': eval_date,
                    'cand_audit': cand_audit
                })

        # -------------------------------------------------------------
        # STEP 3: Deduplicate to 1 Best Opportunity Per Stock
        # -------------------------------------------------------------
        unique_opportunities: List[Dict[str, Any]] = []

        for sym, cand_list in raw_candidates_by_symbol.items():
            # Highest P, then strategy precedence
            cand_list.sort(key=lambda x: (x['p_val'], -x['strat_rank']), reverse=True)
            best_cand = cand_list[0]
            competing = [c['cand']['strategy_id'] for c in cand_list[1:]]
            best_cand['cand_audit']['competing_strategies'] = competing
            best_cand['competing_strategies'] = competing
            unique_opportunities.append(best_cand)

        total_unique_opps = len(unique_opportunities)

        # -------------------------------------------------------------
        # STEP 4: Apply Gate 2B Post-ML Filters (P >= 0.60, DI+ > DI-, Vol >= 1.2x)
        # -------------------------------------------------------------
        qualifying_opps = []
        for c in unique_opportunities:
            feat = c.get('features') or c['pred'].get('features', {})
            p_val = c['p_val']
            cand_audit = c['cand_audit']

            if p_val < DAILY_V2_ML_THRESHOLD:
                cand_audit['decision'] = 'REJECT'
                cand_audit['rejection_reason'] = f"ML_FLOOR_REJECT: P={p_val:.4f} < {DAILY_V2_ML_THRESHOLD}"
                if not self.dry_run:
                    self.storage.record_candidate(cand_audit)
                continue

            # Gate 2B: DI+ > DI-
            plus_di = feat.get('plus_di14', 0.0)
            minus_di = feat.get('minus_di14', 0.0)
            if plus_di <= minus_di:
                cand_audit['decision'] = 'REJECT'
                cand_audit['rejection_reason'] = f"NEGATION: Negative directional momentum (DI+={plus_di:.1f} <= DI-={minus_di:.1f})"
                if not self.dry_run:
                    self.storage.record_candidate(cand_audit)
                continue

            # Gate 2B: Volume Surge >= 1.2x
            vol_ratio = feat.get('vol_ratio_20d', 0.0)
            if vol_ratio < 1.20:
                cand_audit['decision'] = 'REJECT'
                cand_audit['rejection_reason'] = f"NEGATION: Insufficient accumulation volume (VolRatio20={vol_ratio:.2f}x < 1.20x)"
                if not self.dry_run:
                    self.storage.record_candidate(cand_audit)
                continue

            qualifying_opps.append(c)

        # -------------------------------------------------------------
        # STEP 5 & 6: Rank Qualifying Opportunities & Select Top 3 Max
        # -------------------------------------------------------------
        qualifying_opps.sort(key=lambda x: x['p_val'], reverse=True)

        selected_for_entry = []
        if qualifying_opps:
            selected_for_entry = qualifying_opps[:DAILY_CAPACITY_LIMIT]

        # Record rank-filtered-out candidates
        for c in qualifying_opps[len(selected_for_entry):]:
            c['cand_audit']['decision'] = 'REJECT'
            c['cand_audit']['rejection_reason'] = f"RANK_FILTER: Exceeded Daily Top {DAILY_CAPACITY_LIMIT} Max"
            if not self.dry_run:
                self.storage.record_candidate(c['cand_audit'])

        # -------------------------------------------------------------
        # STEP 7 & 8: Active Position Blocking & Same-Day Close Entry
        # -------------------------------------------------------------
        qualified_entries = []

        for winner in selected_for_entry:
            sym = winner['cand']['symbol']
            winner_audit = winner['cand_audit']
            competing_strategies = winner['competing_strategies']

            # Check if symbol is already ACTIVE (ONE STOCK = ONE POSITION)
            if self.storage.is_symbol_active(sym):
                winner_audit['decision'] = 'REJECT'
                winner_audit['rejection_reason'] = 'SIGNAL_WHILE_POSITION_ACTIVE'
                if not self.dry_run:
                    self.storage.record_candidate(winner_audit)
                logger.info(f"RE-ENTRY BLOCKED: {sym} is already ACTIVE. Candidate marked SIGNAL_WHILE_POSITION_ACTIVE.")
                continue

            if not self.dry_run:
                self.storage.record_candidate(winner_audit)

            cand = winner['cand']
            ml_pred = winner['pred']
            eval_date = winner['eval_date']

            trade_record = {
                'strategy_id': cand['strategy_id'],
                'strategy_family': cand.get('strategy_family', 'TREND'),
                'symbol': sym,
                'signal_date': eval_date,
                'entry_date': eval_date,
                'entry_price': cand['entry_price'],
                'raw_score': ml_pred['raw_score'],
                'calibrated_probability': ml_pred['calibrated_probability'],
                'also_triggered_by': competing_strategies,
                'model_version': ml_pred['model_version']
            }

            trade_id = None
            if not self.dry_run:
                trade_id = self.storage.create_trade(trade_record)

            if trade_id is not None or self.dry_run:
                qualified_entries.append({
                    'trade_id': trade_id,
                    **trade_record
                })
                logger.info(f"QUALIFIED ENTRY (Daily Top 1%): {sym} ({cand['strategy_id']}) at ₹{cand['entry_price']:.2f} | P={ml_pred['calibrated_probability']:.4f} | Also: {competing_strategies}")

        status_str = "DECISION_COMMITTED" if phase == "DECISION" else "EOD_FINALIZED"
        if not self.dry_run and cycle_eval_date != 'N/A':
            self.storage.record_cycle_run(
                target_date=cycle_eval_date,
                phase=phase,
                new_entries=len(qualified_entries),
                exits=len(exits_executed),
                active_count=len(self.storage.get_active_trades()),
                status=status_str
            )

        logger.info(f"Daily V2 [{phase}] Cycle Complete: {len(qualified_entries)} New Entries (from {total_unique_opps} unique opps), {len(exits_executed)} Exits, {len(health_updates)} Active Positions.")

        return {
            'status': status_str,
            'phase': phase,
            'cycle_version': DAILY_V2_VERSION,
            'dry_run': self.dry_run,
            'target_date': cycle_eval_date,
            'raw_candidates_evaluated': raw_candidates_count,
            'unique_opportunities': total_unique_opps,
            'new_entries': qualified_entries,
            'qualified_entries': qualified_entries,
            'exits': exits_executed,
            'exits_executed': exits_executed,
            'active_positions_count': len(self.storage.get_active_trades())
        }


def run_daily_v2_for_date(
    target_date: str,
    phase: str = "DECISION",
    market_data: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    db_path: Optional[str] = None,
    force: bool = False,
    dispatch_alerts: bool = False
) -> Dict[str, Any]:
    """
    TOP-LEVEL CANONICAL MULTI-PHASE DAILY V2 FUNCTION.
    Single source of truth across historical replay, database population, and live execution.
    Phases: 'DECISION' (15:15 IST) and 'FINALIZE' (15:45 IST).
    """
    storage = DailyV2Storage(db_path) if db_path else DailyV2Storage()
    engine = DailyV2Engine(storage=storage, dry_run=dry_run)
    res = engine.run_daily_v2_for_date(target_date=target_date, phase=phase, market_data=market_data, dry_run=dry_run, force=force)

    if dispatch_alerts and not dry_run and res.get('status') != 'ALREADY_COMMITTED':
        from daily_v2.alerts import send_buy_alert, send_exit_alert, send_daily_summary_alert
        for ex in res.get('exits', []):
            try:
                send_exit_alert(ex)
            except Exception as e:
                logger.error(f"Error sending exit alert: {e}")
        for tr in res.get('new_entries', []):
            try:
                send_buy_alert(tr)
            except Exception as e:
                logger.error(f"Error sending buy alert: {e}")

    return res

