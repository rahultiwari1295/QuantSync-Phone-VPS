"""
vps_data/engines/v2/live_scanner.py
===================================
Production Point-in-Time Daily Scanner for QuantSync-V2.
Directly scans stock_history.db using the 5 frozen strategy detectors
and ensemble models (LGB + XGB) with Zero Lookahead.
Zero external dependencies.
"""

import os
import sys
import json
import pickle
import sqlite3
import datetime
from typing import Dict, List, Any, Optional, Set
import numpy as np
import pandas as pd

_V2_DIR = os.path.dirname(os.path.abspath(__file__))
_V11_DIR = _V2_DIR  # Compatibility alias
_ENGINES_DIR = os.path.dirname(_V2_DIR)
_PROD_ROOT = os.path.dirname(_ENGINES_DIR)

if _V2_DIR not in sys.path:
    sys.path.insert(0, _V2_DIR)

# Local imports from V2 signals
try:
    from engines.v2.signals.p1_rs_leader import detect_rs_leader
    from engines.v2.signals.p2_apex_base import detect_apex_base
    from engines.v2.signals.p4_power_earnings_gap import detect_power_earnings_gap
    from engines.v2.signals.p5_high_tight_flag import detect_high_tight_flag
    from engines.v2.signals.p9_stage2_base_breakout import detect_stage2_base_breakout
except ImportError:
    from signals.p1_rs_leader import detect_rs_leader
    from signals.p2_apex_base import detect_apex_base
    from signals.p4_power_earnings_gap import detect_power_earnings_gap
    from signals.p5_high_tight_flag import detect_high_tight_flag
    from signals.p9_stage2_base_breakout import detect_stage2_base_breakout

MODEL_DIR = os.path.join(_V2_DIR, "models")
CONFIG_DIR = os.path.join(_V2_DIR, "config")

STRATEGY_NAMES = {
    "APEX_BASE": "Apex",
    "HIGH_TIGHT_FLAG": "Flag",
    "STAGE2_BASE": "Breakout",
    "POWER_EARNINGS_GAP": "Gap",
    "RS_LEADER": "Leader",
}

SECTOR_KEYWORDS = {
    ("BANK", "FIN", "CAP", "HOLD", "SEC"): "Financial Services",
    ("TECH", "SOFT", "INFO", "DIGI"): "Information Technology",
    ("PHARM", "LAB", "BIO", "HEALTH"): "Healthcare & Pharma",
    ("ENG", "IND", "STEEL", "POWER", "ELECT"): "Industrial Manufacturing",
    ("AUTO", "MOT", "TYRE", "GEAR"): "Automotive",
    ("OIL", "GAS", "PETRO", "ENERGY"): "Energy & Petrochemicals",
}


def resolve_sector(symbol: str) -> str:
    s_u = symbol.upper()
    for keys, sec in SECTOR_KEYWORDS.items():
        if any(k in s_u for k in keys):
            return sec
    return f"Sector_{abs(hash(symbol)) % 25}"


class V11LiveScanner:
    """
    Scans daily candles from stock_history.db at EOD (e.g. 15:30 IST) to generate
    high-conviction Buy candidates for the next trading session.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(_PROD_ROOT, "Data", "stock_history.db")
        self._models_loaded = False
        self._models: Dict[str, Any] = {}
        self._feature_cols: Dict[str, List[str]] = {}
        self._regime_df: Optional[pd.DataFrame] = None

    def _ensure_models_loaded(self):
        """Loads all 5 pairs of LGB + XGB ensemble models and their feature lists."""
        if self._models_loaded:
            return

        strats = ["apex_base", "high_tight_flag", "power_earnings_gap", "stage2_base_breakout", "rs_leader"]
        for s in strats:
            lgb_p = os.path.join(MODEL_DIR, f"lgb_{s}.pkl")
            xgb_p = os.path.join(MODEL_DIR, f"xgb_{s}.pkl")

            if not os.path.exists(lgb_p) or not os.path.exists(xgb_p):
                continue

            with open(lgb_p, "rb") as f:
                l_obj = pickle.load(f)
            with open(xgb_p, "rb") as f:
                x_obj = pickle.load(f)

            lgb_m = l_obj["model"] if isinstance(l_obj, dict) else l_obj
            xgb_m = x_obj["model"] if isinstance(x_obj, dict) else x_obj

            f_cols = None
            if isinstance(l_obj, dict) and "feature_cols" in l_obj:
                f_cols = l_obj["feature_cols"]
            elif hasattr(lgb_m, "feature_name_"):
                f_cols = lgb_m.feature_name_
            else:
                json_p = os.path.join(MODEL_DIR, f"features_{s}.json")
                if os.path.exists(json_p):
                    with open(json_p, "r") as jf:
                        f_cols = json.load(jf)

            self._models[s] = {"lgb": lgb_m, "xgb": xgb_m}
            self._feature_cols[s] = f_cols or []

        self._models_loaded = True

    def _ensure_regime_df(self):
        if self._regime_df is not None:
            return
        if not os.path.exists(self.db_path):
            return
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT rawCandles FROM StockHistory WHERE symbol='NIFTY' OR symbol='NIFTY100' LIMIT 1"
                ).fetchone()
                if not row or not row[0]:
                    return
                candles = json.loads(row[0])
                df = pd.DataFrame(candles).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close"})
                df["date_str"] = df["date"].astype(str).str[:10]
                for col in ["open", "high", "low", "close"]:
                    df[col] = df[col].astype(float)
                df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
                df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
                tr = np.maximum(
                    df["high"] - df["low"],
                    np.maximum(abs(df["high"] - df["close"].shift(1)), abs(df["low"] - df["close"].shift(1)))
                )
                df["atr14"] = tr.rolling(14).mean()
                df["atr_pct"] = df["atr14"] / df["close"] * 100.0
                df["ret_5d"] = df["close"].pct_change(5) * 100.0
                self._regime_df = df
        except Exception:
            pass

    def get_market_regime(self, date_str: str) -> Dict[str, Any]:
        """
        Dynamically classifies market regime from NIFTY daily candles in stock_history.db:
        - BULL_CALM: NIFTY > EMA50 > EMA200 and ATR% <= 1.2%
        - BULL_VOLATILE: NIFTY > EMA200 and (NIFTY <= EMA50 or ATR% > 1.2%)
        - BEAR_CALM: NIFTY <= EMA200 and 5d return >= -2.0%
        - BEAR_VOLATILE: NIFTY <= EMA200 and (5d return < -2.0% or ATR% > 1.5%)
        """
        default_res = {"regime": "BULL_VOLATILE", "close": 0.0, "ema50": 0.0, "ema200": 0.0, "atr_pct": 0.8}
        self._ensure_regime_df()
        if self._regime_df is None or len(self._regime_df) < 50:
            return default_res

        try:
            sub = self._regime_df[self._regime_df["date_str"] <= str(date_str)[:10]]
            if len(sub) < 50:
                return default_res

            last_row = sub.iloc[-1]
            c = float(last_row["close"])
            e50 = float(last_row["ema50"])
            e200 = float(last_row["ema200"])
            atr_p = float(last_row["atr_pct"])
            r5 = float(last_row["ret_5d"])

            if c > e200:
                regime = "BULL_CALM" if (c > e50 and atr_p <= 1.2) else "BULL_VOLATILE"
            else:
                regime = "BEAR_VOLATILE" if (r5 < -2.0 or atr_p > 1.5) else "BEAR_CALM"

            return {
                "regime": regime,
                "close": round(c, 2),
                "ema50": round(e50, 2),
                "ema200": round(e200, 2),
                "atr_pct": round(atr_p, 2),
                "ret_5d": round(r5, 2),
            }
        except Exception:
            return default_res

    def scan(
        self,
        target_date: Optional[str] = None,
        active_symbols: Optional[Set[str]] = None,
        min_probability: float = 0.55,
    ) -> List[Dict[str, Any]]:
        """
        Runs point-in-time scanning across all tradeable stocks for target_date.
        """
        self._ensure_models_loaded()
        active_symbols = active_symbols or set()

        if not os.path.exists(self.db_path):
            return []

        # Read NIFTY data for regime & RS calculation
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            n_row = cur.execute(
                "SELECT rawCandles FROM StockHistory WHERE symbol='NIFTY' OR symbol='NIFTY100' LIMIT 1"
            ).fetchone()
            nifty_df = None
            if n_row and n_row[0]:
                n_c = json.loads(n_row[0])
                nifty_df = pd.DataFrame(n_c).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close"})
                nifty_df["date"] = nifty_df["date"].astype(str).str[:10]
                for col in ["open", "high", "low", "close"]:
                    nifty_df[col] = nifty_df[col].astype(float)

            # Determine scanning date
            if not target_date:
                if nifty_df is not None and not nifty_df.empty:
                    target_date = nifty_df["date"].iloc[-1]
                else:
                    target_date = datetime.date.today().strftime("%Y-%m-%d")

            # Fetch all stock candles (filtered strictly by isAlgoEligible = 1)
            rows = cur.execute("SELECT symbol, rawCandles FROM StockHistory WHERE isAlgoEligible = 1").fetchall()

        regime_info = self.get_market_regime(target_date)
        current_regime = regime_info.get("regime", "BULL_VOLATILE")

        candidates: List[Dict[str, Any]] = []

        for sym, raw_candles_str in rows:
            if not raw_candles_str or sym in active_symbols or sym in ["NIFTY", "NIFTY100", "NIFTY 50", "NIFTY 100"]:
                continue

            try:
                c_list = json.loads(raw_candles_str)
                if not isinstance(c_list, list) or len(c_list) < 50:
                    continue

                df_stock = pd.DataFrame(c_list).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
                df_stock["date"] = df_stock["date"].astype(str).str[:10]

                # Find bar index matching target_date
                match_indices = df_stock[df_stock["date"] == target_date].index
                if len(match_indices) == 0:
                    continue
                bar_idx = match_indices[0]

                for col in ["open", "high", "low", "close", "volume"]:
                    df_stock[col] = df_stock[col].astype(float)

                c_arr = df_stock["close"].values
                o_arr = df_stock["open"].values
                h_arr = df_stock["high"].values
                l_arr = df_stock["low"].values
                v_arr = df_stock["volume"].values

                # Shared indicators
                v_s20 = pd.Series(v_arr).rolling(20, min_periods=5).mean().values
                tr_s = np.maximum(
                    h_arr - l_arr,
                    np.maximum(
                        np.abs(h_arr - np.roll(c_arr, 1)),
                        np.abs(l_arr - np.roll(c_arr, 1))
                    )
                )
                tr_s[0] = h_arr[0] - l_arr[0]
                atr14 = pd.Series(tr_s).rolling(14, min_periods=5).mean().values
                sma150 = pd.Series(c_arr).rolling(150, min_periods=30).mean().values

                sec = resolve_sector(sym)

                # -------------------------------------------------------------
                # 1. Evaluate APEX_BASE
                # -------------------------------------------------------------
                sig_apex = detect_apex_base(df_stock, bar_idx)
                if sig_apex:
                    cand = self._score_candidate("apex_base", "APEX_BASE", sym, sec, target_date, sig_apex, current_regime, min_probability)
                    if cand:
                        candidates.append(cand)

                # -------------------------------------------------------------
                # 2. Evaluate RS_LEADER
                # -------------------------------------------------------------
                sig_rs = detect_rs_leader(df_stock, bar_idx, nifty_df=nifty_df)
                if sig_rs:
                    cand = self._score_candidate("rs_leader", "RS_LEADER", sym, sec, target_date, sig_rs, current_regime, min_probability)
                    if cand:
                        candidates.append(cand)

                # -------------------------------------------------------------
                # 3. Evaluate HIGH_TIGHT_FLAG
                # -------------------------------------------------------------
                sig_htf = detect_high_tight_flag(c_arr, o_arr, h_arr, l_arr, v_arr, v_s20, atr14, bar_idx)
                if sig_htf:
                    cand = self._score_candidate("high_tight_flag", "HIGH_TIGHT_FLAG", sym, sec, target_date, sig_htf, current_regime, min_probability)
                    if cand:
                        candidates.append(cand)

                # -------------------------------------------------------------
                # 4. Evaluate POWER_EARNINGS_GAP
                # -------------------------------------------------------------
                sig_peg = detect_power_earnings_gap(c_arr, o_arr, h_arr, l_arr, v_arr, v_s20, atr14, bar_idx)
                if sig_peg:
                    cand = self._score_candidate("power_earnings_gap", "POWER_EARNINGS_GAP", sym, sec, target_date, sig_peg, current_regime, min_probability)
                    if cand:
                        candidates.append(cand)

                # -------------------------------------------------------------
                # 5. Evaluate STAGE2_BASE
                # -------------------------------------------------------------
                sig_stg2 = detect_stage2_base_breakout(c_arr, o_arr, h_arr, l_arr, v_arr, v_s20, sma150, atr14, bar_idx)
                if sig_stg2:
                    cand = self._score_candidate("stage2_base_breakout", "STAGE2_BASE", sym, sec, target_date, sig_stg2, current_regime, min_probability)
                    if cand:
                        candidates.append(cand)

            except Exception:
                continue

        # Sort candidates: highest ensemble score first
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates

    def _score_candidate(
        self,
        model_key: str,
        strat_id: str,
        symbol: str,
        sector: str,
        date_str: str,
        signal_dict: Dict[str, Any],
        regime: str,
        min_probability: float,
    ) -> Optional[Dict[str, Any]]:
        """Scores candidate feature vector through LGB + XGB ensemble."""
        models = self._models.get(model_key)
        feat_cols = self._feature_cols.get(model_key, [])
        if not models or not feat_cols:
            return None

        raw_features = signal_dict.get("features", {})
        feat_vals = [raw_features.get(col, 0.0) for col in feat_cols]
        df_feat = pd.DataFrame([feat_vals], columns=feat_cols).fillna(0.0)

        p_lgb = float(models["lgb"].predict_proba(df_feat)[0][1])
        p_xgb = float(models["xgb"].predict_proba(df_feat)[0][1])
        ens_prob = 0.5 * p_lgb + 0.5 * p_xgb

        if ens_prob < min_probability:
            return None

        # Determine visual star conviction rating
        if ens_prob >= 0.65:
            stars = "★★★★★"
        elif ens_prob >= 0.60:
            stars = "★★★★"
        else:
            stars = "★★★"

        ep = float(signal_dict["entry_price"])
        sp = float(signal_dict["stop_price"])
        tp = round(float(signal_dict.get("target_price", ep * 1.15)), 2)

        return {
            "symbol": symbol,
            "strategy_id": strat_id,
            "strategy_name": STRATEGY_NAMES.get(strat_id, strat_id),
            "entry_price": ep,
            "stop_price": sp,
            "target_price": tp,
            "risk_pct": signal_dict.get("risk_pct", round((ep - sp) / ep * 100.0, 2)),
            "score": round(ens_prob, 4),
            "stars": stars,
            "action_badge": "BUY_CANDIDATE",
            "regime": regime,
            "sector": sector,
            "date": date_str,
            "features": raw_features,
        }


# Compatibility aliases
QuantSyncV2LiveScanner = V11LiveScanner
V2LiveScanner = V11LiveScanner

