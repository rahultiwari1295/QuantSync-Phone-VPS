"""Meta-labelling and 3-stage AI check (Protocol v2).

Walk-forward meta-labelling:
Stage A (tune <= 2024): Candidate tournament and gate selection on 2022-2024 out-of-sample.
Stage B (valid 2025): Frozen validation.
Stage C (blind 2026): Blind out-of-sample test.
"""
from __future__ import annotations

import json
import logging
import pickle
import time
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Harmless BLAS FPU flag warnings on macOS Accelerate / arm64
warnings.filterwarnings("ignore", category=RuntimeWarning, module="sklearn")

from .catalog import FEATURES, MODEL_FEATURES
from .config import Config

log = logging.getLogger(__name__)

GATES = [50, 60, 70, 80, 90]
MIN_TRADES = 40
MIN_TRAIN = 40
LABELS = {f["key"]: f["label"] for f in FEATURES}

PARAMS = dict(
    objective="binary",
    num_leaves=15,
    max_depth=4,
    min_child_samples=40,
    learning_rate=0.05,
    colsample_bytree=0.7,
    subsample=0.8,
    subsample_freq=1,
    reg_lambda=5.0,
    n_jobs=1,
    random_state=7,
    verbose=-1,
)

# 25 scale-free features for the AI model
AI_FEATURES_25 = [
    "ret_5", "ret_20", "ret_60", "rsi_14", "stoch_k", "macd_hist", "adx_14",
    "vol_ratio", "vol_surge", "atr_pct", "bb_pct", "range_pct", "close_loc", "upper_wick_pct",
    "dist_hh52_pct", "dist_ath_pct", "dist_sma_50_pct", "dist_sma_200_pct", "dist_ema_20_pct",
    "sma_50_slope", "turnover_log", "liq_rank", "rs60_pct", "mkt_ret_20", "mkt_dist_sma_200_pct",
]


def _lgb():
    import lightgbm as lgb
    return lgb


def make_model(n_rows: int):
    """Legacy helper: default LGBMClassifier."""
    return _lgb().LGBMClassifier(n_estimators=int(min(400, max(60, n_rows // 3))), **PARAMS)


def make_candidate_model(name: str, n_rows: int):
    """Create a candidate model instance."""
    if name == "lgbm_default":
        n_est = int(min(400, max(60, n_rows // 3)))
        return _lgb().LGBMClassifier(n_estimators=n_est, **PARAMS)
    elif name == "lgbm_shallow" or name == "lgbm_compact":
        n_est = int(min(200, max(40, n_rows // 3)))
        p = dict(PARAMS, num_leaves=7, max_depth=3, min_child_samples=80)
        return _lgb().LGBMClassifier(n_estimators=n_est, **p)
    elif name == "xgboost":
        try:
            import xgboost as xgb
            n_est = int(min(300, max(50, n_rows // 3)))
            return xgb.XGBClassifier(
                max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.7,
                reg_lambda=5.0, min_child_weight=10, n_estimators=n_est,
                random_state=7, n_jobs=1, eval_metric="logloss"
            )
        except (ImportError, Exception):
            return None
    elif name == "logreg":
        return Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(C=0.5, penalty="l2", solver="liblinear", random_state=7))
        ])
    return None


class Scorer:
    """A fitted model (or ensemble) plus the calibration array for ECDF percentiles."""

    def __init__(self, model, cal: np.ndarray, cols: list[str], model_name: str = "model", ensemble_cols: tuple[list[str], list[str]] | None = None):
        self.model = model
        self.cal = np.sort(np.asarray(cal, dtype=float))
        self.cols = list(cols)
        self.model_name = model_name
        self.ensemble_cols = ensemble_cols

    def prob(self, X: pd.DataFrame) -> np.ndarray:
        if isinstance(self.model, tuple):  # ensemble (tree, logreg)
            tree, lr = self.model
            tree_cols, lr_cols = self.ensemble_cols if self.ensemble_cols else (self.cols, self.cols)
            p1 = tree.predict_proba(X[tree_cols])[:, 1]
            p2 = lr.predict_proba(X[lr_cols])[:, 1]
            return 0.5 * p1 + 0.5 * p2
        if hasattr(self.model, "predict_proba"):
            return self.model.predict_proba(X[self.cols])[:, 1]
        return self.model.predict(X[self.cols])

    def score(self, X: pd.DataFrame) -> np.ndarray:
        if len(self.cal) == 0:
            return np.full(len(X), 50.0)
        p = self.prob(X)
        return np.searchsorted(self.cal, p, side="right") / len(self.cal) * 100.0

    def save(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Pickle model object
        with open(p.with_suffix(".pkl"), "wb") as f:
            pickle.dump(self, f)
        # If LightGBM single model, also write text format for backwards compatibility
        if hasattr(self.model, "booster_") or hasattr(self.model, "model_to_string"):
            b = self.model.booster_ if hasattr(self.model, "booster_") else self.model
            p.with_suffix(".txt").write_text(b.model_to_string())
        meta = {
            "model_name": self.model_name,
            "cols": self.cols,
            "ensemble_cols": self.ensemble_cols,
            "cal_size": len(self.cal),
        }
        p.with_suffix(".json").write_text(json.dumps(meta))

    @classmethod
    def load(cls, path) -> "Scorer":
        p = Path(path)
        if p.with_suffix(".pkl").exists():
            with open(p.with_suffix(".pkl"), "rb") as f:
                return pickle.load(f)
        if p.with_suffix(".txt").exists() and p.with_suffix(".json").exists():
            meta = json.loads(p.with_suffix(".json").read_text())
            b = _lgb().Booster(model_str=p.with_suffix(".txt").read_text())
            return cls(b, np.array(meta.get("cal", [])), meta.get("cols", MODEL_FEATURES))
        raise FileNotFoundError(f"No scorer at {path}")


def fit_scorer(X: pd.DataFrame, y: np.ndarray, cols: list[str] = MODEL_FEATURES, shuffle_seed: int | None = None):
    """Legacy helper: fit default LGBM model."""
    n = len(X)
    cut = int(n * 0.8)
    yy = y.copy()
    if shuffle_seed is not None:
        np.random.default_rng(shuffle_seed).shuffle(yy[:cut])
    if len(np.unique(yy[:cut])) < 2:
        return None
    m = make_model(cut)
    m.fit(X.iloc[:cut][cols], yy[:cut])
    return Scorer(m, m.predict_proba(X.iloc[cut:][cols])[:, 1], cols)


def fit_candidate_scorer(
    model_name: str,
    X: pd.DataFrame,
    y: np.ndarray,
    cols: list[str] = AI_FEATURES_25,
    shuffle_seed: int | None = None,
    compact_cols: list[str] | None = None,
    best_tree_name: str = "lgbm_shallow",
) -> Scorer | None:
    """Fit a candidate model or ensemble on X[:cut] and calibrate on X[cut:]."""
    n = len(X)
    if n < 20:
        return None
    cut = int(n * 0.8)
    if cut < 10 or n - cut < 5:
        return None
    yy = y.copy()
    if shuffle_seed is not None:
        np.random.default_rng(shuffle_seed).shuffle(yy[:cut])
    if len(np.unique(yy[:cut])) < 2:
        return None

    active_cols = compact_cols if (model_name == "lgbm_compact" and compact_cols) else cols

    if model_name == "ensemble":
        tree = make_candidate_model(best_tree_name, cut)
        lr = make_candidate_model("logreg", cut)
        if tree is None or lr is None:
            return None
        tree_cols = compact_cols if (best_tree_name == "lgbm_compact" and compact_cols) else cols
        tree.fit(X.iloc[:cut][tree_cols], yy[:cut])
        lr.fit(X.iloc[:cut][cols], yy[:cut])
        p1 = tree.predict_proba(X.iloc[cut:][tree_cols])[:, 1]
        p2 = lr.predict_proba(X.iloc[cut:][cols])[:, 1]
        cal = 0.5 * p1 + 0.5 * p2
        return Scorer((tree, lr), cal, cols, model_name=model_name, ensemble_cols=(tree_cols, cols))

    model = make_candidate_model(model_name, cut)
    if model is None:
        return None
    model.fit(X.iloc[:cut][active_cols], yy[:cut])
    cal = model.predict_proba(X.iloc[cut:][active_cols])[:, 1]
    return Scorer(model, cal, active_cols, model_name=model_name)


def oos_scores(ds: pd.DataFrame, years: list[int], *, shuffle_seed: int | None = None, extra: pd.DataFrame | None = None,
               cols: list[str] = MODEL_FEATURES) -> pd.Series:
    """Legacy helper: walk-forward OOS scores."""
    out = pd.Series(np.nan, index=ds.index)
    for yr in years:
        start = pd.Timestamp(f"{yr}-01-01")
        tr = ds[(ds.exit_date < start)].sort_values("entry_date")
        if extra is not None:
            tr = pd.concat([tr, extra[extra.exit_date < start]]).sort_values("entry_date")
        te = ds[(ds.entry_date >= start) & (ds.entry_date < pd.Timestamp(f"{yr + 1}-01-01"))]
        if len(tr) < MIN_TRAIN or te.empty:
            continue
        sc = fit_scorer(tr, tr["y"].to_numpy(), cols, shuffle_seed)
        if sc is not None:
            out.loc[te.index] = sc.score(te)
    return out


def oos_scores_candidate(
    cand_name: str,
    ds: pd.DataFrame,
    years: list[int],
    *,
    shuffle_seed: int | None = None,
    cols: list[str] = AI_FEATURES_25,
    compact_cols: list[str] | None = None,
    best_tree_name: str = "lgbm_shallow",
) -> pd.Series:
    """Walk-forward OOS scores for a candidate model across years."""
    out = pd.Series(np.nan, index=ds.index)
    for yr in years:
        start = pd.Timestamp(f"{yr}-01-01")
        tr = ds[(ds.exit_date < start)].sort_values("entry_date")
        if len(tr) > 30_000:
            tr = tr.tail(30_000)
        te = ds[(ds.entry_date >= start) & (ds.entry_date < pd.Timestamp(f"{yr + 1}-01-01"))]
        if len(tr) < MIN_TRAIN or te.empty:
            continue
        sc = fit_candidate_scorer(
            cand_name, tr, tr["y"].to_numpy(), cols,
            shuffle_seed=shuffle_seed, compact_cols=compact_cols, best_tree_name=best_tree_name
        )
        if sc is not None:
            out.loc[te.index] = sc.score(te)
    return out


def gate_stats(ds: pd.DataFrame, scores: pd.Series, gate: int | None) -> dict:
    scores = scores.reindex(ds.index)
    keep = scores.notna() if gate is None else scores >= gate
    m = ds[keep]
    n = len(m)
    if n == 0:
        return {"trades": 0, "win_rate": None, "avg_ret": None, "wins": 0, "losses": 0}
    w = int((m.ret_pct > 0).sum())
    return {"trades": n, "wins": w, "losses": n - w, "win_rate": round(w / n * 100, 1), "avg_ret": round(float(m.ret_pct.mean()), 2)}


def plain_stats(m: pd.DataFrame) -> dict:
    """Every result of the plain strategy in a window (no score filter)."""
    n = len(m)
    if n == 0:
        return {"trades": 0, "win_rate": None, "avg_ret": None, "wins": 0, "losses": 0}
    w = int((m.ret_pct > 0).sum())
    return {"trades": n, "wins": w, "losses": n - w, "win_rate": round(w / n * 100, 1), "avg_ret": round(float(m.ret_pct.mean()), 2)}


def lift(ds: pd.DataFrame, scores: pd.Series, gate: int, min_kept: int = 15) -> float | None:
    base, kept = gate_stats(ds, scores, None), gate_stats(ds, scores, gate)
    if kept["trades"] < min_kept or base["win_rate"] is None:
        return None
    return kept["win_rate"] - base["win_rate"]


def _z(ds: pd.DataFrame, scores: pd.Series, gate: int) -> float | None:
    """How many standard errors the gate's win-rate lift is above the plain rule."""
    kept, base = gate_stats(ds, scores, gate), gate_stats(ds, scores, None)
    if kept["trades"] < 30 or base["win_rate"] is None:
        return None
    p = base["win_rate"] / 100
    se = (p * (1 - p) / kept["trades"]) ** 0.5 * 100
    return round((kept["win_rate"] - base["win_rate"]) / se, 2) if se else None


def leak_test(ds: pd.DataFrame, years: list[int], gate: int = 60, rounds: int = 3, **kw) -> dict:
    """Train on shuffled labels; a sound pipeline cannot beat the plain rule by more than a few points."""
    ls = []
    for s in range(rounds):
        sc = oos_scores(ds, years, shuffle_seed=100 + s, **kw)
        l = lift(ds[ds.entry_date.dt.year.isin(years)], sc, gate)
        if l is not None:
            ls.append(l)
    mean = float(np.mean(ls)) if ls else 0.0
    return {"mean_lift": round(mean, 2), "rounds": len(ls), "passed": mean <= 3.0}


def leak_test_candidate(cand_name: str, ds: pd.DataFrame, years: list[int], gate: int = 60, rounds: int = 3,
                        cols: list[str] = AI_FEATURES_25, compact_cols: list[str] | None = None,
                        best_tree_name: str = "lgbm_shallow") -> dict:
    """Candidate-specific leak test with shuffled labels."""
    ls = []
    for s in range(rounds):
        sc = oos_scores_candidate(cand_name, ds, years, shuffle_seed=100 + s, cols=cols,
                                compact_cols=compact_cols, best_tree_name=best_tree_name)
        l = lift(ds[ds.entry_date.dt.year.isin(years)], sc, gate)
        if l is not None:
            ls.append(l)
    mean = float(np.mean(ls)) if ls else 0.0
    return {"mean_lift": round(mean, 2), "rounds": len(ls), "passed": mean <= 3.0}


def drivers(scorer: Scorer, X: pd.DataFrame, top: int = 3) -> list[str]:
    """Plain-English reasons from SHAP contributions or feature correlations."""
    if len(X) < 10 or scorer is None:
        return []
    model = scorer.model
    cols = scorer.ensemble_cols[0] if (isinstance(model, tuple) and scorer.ensemble_cols) else scorer.cols
    if isinstance(model, tuple):
        model = model[0]  # tree model
    if hasattr(model, "predict"):
        try:
            contrib = model.predict(X[cols], pred_contrib=True)[:, :-1]
            imp = np.abs(contrib).mean(axis=0)
            out = []
            for j in np.argsort(-imp)[:top]:
                if imp[j] <= 0:
                    continue
                x = X[cols[j]].to_numpy(dtype=float)
                ok = ~np.isnan(x)
                corr = np.corrcoef(x[ok], contrib[ok, j])[0, 1] if ok.sum() > 5 and np.std(x[ok]) > 0 else 0
                out.append(f"{LABELS.get(cols[j], cols[j])} {'higher' if corr >= 0 else 'lower'} than usual helped")
            return out
        except Exception:  # noqa: BLE001
            pass
    # Correlation fallback
    try:
        y = (X["ret_pct"] > 0).astype(int)
        corrs = {}
        for c in cols:
            if c in X.columns and np.std(X[c]) > 0:
                corrs[c] = np.corrcoef(X[c], y)[0, 1]
        out = []
        for c in sorted(corrs, key=lambda k: abs(corrs[k]), reverse=True)[:top]:
            out.append(f"{LABELS.get(c, c)} {'higher' if corrs[c] >= 0 else 'lower'} than usual helped")
        return out
    except Exception:  # noqa: BLE001
        return []


def prepare_dataset(ds: pd.DataFrame, cfg: Config | None = None) -> pd.DataFrame:
    """Ensure scale-free features, log turnover, and market features are ready on ds."""
    d = ds.copy()
    if "turnover_med_20" in d.columns:
        d["turnover_log"] = np.log10(np.maximum(d["turnover_med_20"].fillna(1.0), 1.0))
    else:
        d["turnover_log"] = 0.0

    # Market features from Nifty benchmark
    if cfg is not None and "mkt_ret_20" not in d.columns:
        from . import store
        from .api import BENCH
        bench_sym = None
        for s in BENCH:
            if (cfg.tf_dir("1D") / f"{s}.parquet").exists():
                bench_sym = s
                break
        if bench_sym:
            b_bars = store.bars(cfg, "1D", [bench_sym], columns=["date", "ret_20", "dist_sma_200_pct"]).get(bench_sym)
            if b_bars is not None and len(b_bars):
                mkt = b_bars.rename(columns={"date": "signal_date", "ret_20": "mkt_ret_20", "dist_sma_200_pct": "mkt_dist_sma_200_pct"})
                d = d.merge(mkt[["signal_date", "mkt_ret_20", "mkt_dist_sma_200_pct"]], on="signal_date", how="left")

    for col in AI_FEATURES_25:
        if col not in d.columns:
            d[col] = 0.0
    d[AI_FEATURES_25] = d[AI_FEATURES_25].fillna(0.0).replace([np.inf, -np.inf], 0.0)
    if "y" not in d.columns and "ret_pct" in d.columns:
        d["y"] = (d["ret_pct"] > 0).astype(int)
    return d


def check(ds: pd.DataFrame, cfg: Config, strategy_id: str | None = None, as_of: str | None = None,
          extra: pd.DataFrame | None = None) -> dict:
    """The 3-stage AI check (Protocol v2).

    Stage A: Tuning loop on <= 2024 to choose candidate model, gate and features.
    Stage B: Validation on 2025 (frozen).
    Stage C: Blind test on 2026 (frozen).
    """
    if len(ds) == 0:
        return {"protocol": "v2", "verdict": "insufficient", "reason": "No trades provided.",
                "model": None, "gate": None, "features": AI_FEATURES_25, "tune": None, "valid": None, "blind": None,
                "leak": None, "drivers": [], "as_of": as_of, "checked_at": int(time.time())}

    ds = prepare_dataset(ds, cfg)
    ds["entry_date"] = pd.to_datetime(ds["entry_date"])
    ds["exit_date"] = pd.to_datetime(ds["exit_date"])

    valid_yr = getattr(cfg, "ai_valid_year", 2025)
    blind_yr = getattr(cfg, "ai_blind_year", 2026)
    tune_years = getattr(cfg, "ai_tune_years", [2022, 2023, 2024])

    # Sample size checks
    pre_2025 = ds[ds.exit_date < pd.Timestamp(f"{valid_yr}-01-01")]
    valid_ds = ds[(ds.entry_date >= pd.Timestamp(f"{valid_yr}-01-01")) & (ds.entry_date < pd.Timestamp(f"{blind_yr}-01-01"))]
    blind_ds = ds[ds.entry_date >= pd.Timestamp(f"{blind_yr}-01-01")]

    if len(pre_2025) < 120 or len(valid_ds) < 40:
        return {
            "protocol": "v2",
            "verdict": "insufficient",
            "reason": f"Need at least 120 closed trades before {valid_yr} (got {len(pre_2025)}) and at least 40 in {valid_yr} (got {len(valid_ds)}).",
            "model": None, "gate": None, "features": AI_FEATURES_25, "tune": None, "valid": None, "blind": None,
            "leak": None, "drivers": [], "as_of": as_of, "checked_at": int(time.time())
        }

    # Learn compact features on trades resolved before 2022
    pre_2022 = ds[ds.exit_date < pd.Timestamp("2022-01-01")]
    compact_cols = None
    if len(pre_2022) >= 30:
        try:
            m = make_candidate_model("lgbm_shallow", len(pre_2022))
            m.fit(pre_2022[AI_FEATURES_25], pre_2022["y"])
            imp = m.booster_.feature_importance(importance_type="gain")
            order = np.argsort(-imp)
            compact_cols = [AI_FEATURES_25[i] for i in order[:10]]
        except Exception:  # noqa: BLE001
            compact_cols = AI_FEATURES_25[:10]
    else:
        compact_cols = AI_FEATURES_25[:10]

    # --- Stage A: Tuning Loop on <= 2024 ---------------------------------------
    ds_tune = ds[ds.entry_date < pd.Timestamp(f"{valid_yr}-01-01")].copy()
    assert ds_tune.empty or ds_tune.entry_date.max() < pd.Timestamp("2025-01-01"), "Stage A must not access 2025+ trades"

    candidate_names = ["lgbm_default", "lgbm_shallow", "lgbm_compact", "xgboost", "logreg", "ensemble"]
    tune_candidates = []
    eligible = []
    best_tree_name = "lgbm_shallow"

    # Evaluate candidates 1 to 5 first to determine best tree for ensemble
    best_tree_lift = -999.0
    cand_scores = {}

    for name in candidate_names:
        if name == "xgboost":
            try:
                import xgboost  # noqa: F401
            except ImportError:
                continue

        sc = oos_scores_candidate(
            name, ds_tune, tune_years, cols=AI_FEATURES_25,
            compact_cols=compact_cols, best_tree_name=best_tree_name
        )
        cand_scores[name] = sc

        oos_mask = ds_tune.entry_date.dt.year.isin(tune_years)
        oos_trades = ds_tune[oos_mask]
        n_oos = len(oos_trades)
        if n_oos == 0:
            continue
        p_stat = plain_stats(oos_trades)

        for gate in (50, 60, 70):
            st = gate_stats(oos_trades, sc[oos_mask], gate)
            lift_avg = round(st["avg_ret"] - p_stat["avg_ret"], 2) if (st["avg_ret"] is not None and p_stat["avg_ret"] is not None) else None
            record = {
                "name": name, "gate": gate, "trades": st["trades"], "win_rate": st["win_rate"],
                "avg_ret": st["avg_ret"], "lift_avg": lift_avg
            }
            tune_candidates.append(record)

            # Check if this model/gate qualifies
            if st["trades"] >= 40 and st["trades"] >= 0.25 * n_oos and lift_avg is not None and lift_avg > 0:
                eligible.append(record)

            if name in ("lgbm_default", "lgbm_shallow", "lgbm_compact", "xgboost"):
                if lift_avg is not None and lift_avg > best_tree_lift:
                    best_tree_lift = lift_avg
                    best_tree_name = name

    if not eligible:
        return {
            "protocol": "v2", "verdict": "no_edge", "reason": "No candidate qualified in tuning loop (need >= 40 trades, >= 25% kept, lift > 0).",
            "model": None, "gate": None, "features": AI_FEATURES_25,
            "tune": {"years": tune_years, "plain": plain_stats(ds_tune[ds_tune.entry_date.dt.year.isin(tune_years)]),
                     "ai": None, "candidates": tune_candidates},
            "valid": None, "blind": None, "leak": None, "drivers": [],
            "as_of": as_of, "checked_at": int(time.time())
        }

    # Pick candidate with highest lift_avg; ties broken by earlier index in list
    def _sort_key(r):
        idx = candidate_names.index(r["name"])
        return (r["lift_avg"], -idx)

    chosen = max(eligible, key=_sort_key)
    chosen_model, chosen_gate = chosen["name"], chosen["gate"]

    # Shuffled-label leak test
    leak_res = leak_test_candidate(
        chosen_model, ds_tune, tune_years, gate=chosen_gate, rounds=3,
        cols=AI_FEATURES_25, compact_cols=compact_cols, best_tree_name=best_tree_name
    )
    if not leak_res["passed"]:
        return {
            "protocol": "v2", "verdict": "no_edge", "reason": "Leak check failed",
            "model": chosen_model, "gate": chosen_gate, "features": AI_FEATURES_25,
            "tune": {"years": tune_years, "plain": plain_stats(ds_tune[ds_tune.entry_date.dt.year.isin(tune_years)]),
                     "ai": chosen, "candidates": tune_candidates},
            "valid": None, "blind": None, "leak": leak_res, "drivers": [],
            "as_of": as_of, "checked_at": int(time.time())
        }

    # --- Stage B: Validation on 2025 (frozen) -----------------------------------
    tr_2025 = ds[ds.exit_date < pd.Timestamp(f"{valid_yr}-01-01")].sort_values("entry_date")
    if len(tr_2025) > 30_000:
        tr_2025 = tr_2025.tail(30_000)

    sc_valid_model = fit_candidate_scorer(
        chosen_model, tr_2025, tr_2025["y"].to_numpy(), cols=AI_FEATURES_25,
        compact_cols=compact_cols, best_tree_name=best_tree_name
    )
    valid_scores = pd.Series(sc_valid_model.score(valid_ds), index=valid_ds.index) if sc_valid_model else pd.Series(np.nan, index=valid_ds.index)

    plain_2025 = plain_stats(valid_ds)
    ai_2025 = gate_stats(valid_ds, valid_scores, chosen_gate)
    z_2025 = _z(valid_ds, valid_scores, chosen_gate)

    # --- Stage C: Blind Test on 2026 (frozen) ----------------------------------
    tr_2026 = ds[ds.exit_date < pd.Timestamp(f"{blind_yr}-01-01")].sort_values("entry_date")
    if len(tr_2026) > 30_000:
        tr_2026 = tr_2026.tail(30_000)

    sc_blind_model = fit_candidate_scorer(
        chosen_model, tr_2026, tr_2026["y"].to_numpy(), cols=AI_FEATURES_25,
        compact_cols=compact_cols, best_tree_name=best_tree_name
    )
    blind_scores = pd.Series(sc_blind_model.score(blind_ds), index=blind_ds.index) if (sc_blind_model and len(blind_ds)) else pd.Series(np.nan, index=blind_ds.index)

    plain_2026 = plain_stats(blind_ds) if len(blind_ds) else {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_ret": None}
    ai_2026 = gate_stats(blind_ds, blind_scores, chosen_gate) if len(blind_ds) else {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "avg_ret": None}
    z_2026 = _z(blind_ds, blind_scores, chosen_gate) if len(blind_ds) else None
    blind_note = "Too few 2026 trades to confirm" if ai_2026["trades"] < 20 else None

    # --- Combined 2025 and 2026 statistics and Verdict -------------------------
    comb_ai_trades = ai_2025["trades"] + ai_2026["trades"]
    comb_ai_wins = ai_2025["wins"] + ai_2026["wins"]
    comb_plain_trades = plain_2025["trades"] + plain_2026["trades"]
    comb_plain_wins = plain_2025["wins"] + plain_2026["wins"]

    if comb_plain_trades > 0 and comb_ai_trades > 0:
        p0 = comb_plain_wins / comb_plain_trades
        se = (p0 * (1 - p0) / comb_ai_trades) ** 0.5 * 100
        p_ai = comb_ai_wins / comb_ai_trades * 100
        comb_z = round((p_ai - p0 * 100) / se, 2) if se else 0.0
    else:
        comb_z = 0.0

    improved = True
    fail_reasons = []

    if ai_2025["avg_ret"] is None or ai_2025["avg_ret"] <= 0:
        improved = False
        fail_reasons.append("2025 gated average return must be > 0")
    if ai_2025["avg_ret"] is None or plain_2025["avg_ret"] is None or (ai_2025["avg_ret"] - plain_2025["avg_ret"]) < 0.3:
        improved = False
        fail_reasons.append("2025 gated average return did not beat plain return by at least 0.3 pp")
    if ai_2025["win_rate"] is None or plain_2025["win_rate"] is None or ai_2025["win_rate"] < plain_2025["win_rate"]:
        improved = False
        fail_reasons.append("2025 gated win rate lower than plain win rate")
    if ai_2025["trades"] < 30 or (plain_2025["trades"] > 0 and ai_2025["trades"] < 0.20 * plain_2025["trades"]):
        improved = False
        fail_reasons.append("2025 gated kept fewer than 30 trades or < 20% of plain trades")
    if comb_z < 2.0:
        improved = False
        fail_reasons.append(f"Combined 2025-2026 win-rate lift z-score ({comb_z}) below 2.0")
    if not leak_res["passed"]:
        improved = False
        fail_reasons.append("Leak check failed")
    if ai_2026["trades"] >= 20:
        if ai_2026["avg_ret"] is None or ai_2026["avg_ret"] <= 0:
            improved = False
            fail_reasons.append("2026 gated average return must be > 0 (>= 20 trades)")
        if plain_2026["avg_ret"] is not None and ai_2026["avg_ret"] is not None:
            if ai_2026["avg_ret"] < plain_2026["avg_ret"] - 0.25:
                improved = False
                fail_reasons.append("2026 gated return fell more than 0.25 pp below plain return")

    verdict = "improved" if improved else "no_edge"
    reason = "Passed all 3 stages with verified edge." if improved else "; ".join(fail_reasons)

    # Final scorer trained on all resolved trades before blind year
    final_scorer = sc_blind_model
    final_scorer_path = None
    if final_scorer and hasattr(cfg, "models_dir") and cfg.models_dir and strategy_id:
        final_scorer_path = str(cfg.models_dir / f"ai_{strategy_id}.pkl")
        final_scorer.save(final_scorer_path)

    # Plain-English drivers
    kept_sample = ds[ds.entry_date.dt.year.isin(tune_years + [valid_yr])]
    drv = drivers(final_scorer or sc_valid_model, kept_sample)

    # Build complete _scores series for all trades in ds
    all_scores = pd.Series(50.0, index=ds.index)
    if final_scorer:
        try:
            all_scores = pd.Series(final_scorer.score(ds), index=ds.index)
        except Exception:  # noqa: BLE001
            pass
    if chosen_model in cand_scores:
        tune_mask = ds.entry_date.dt.year.isin(tune_years)
        all_scores.loc[tune_mask] = cand_scores[chosen_model].loc[tune_mask]
    if len(valid_ds) and valid_scores.notna().any():
        all_scores.loc[valid_ds.index] = valid_scores
    if len(blind_ds) and blind_scores.notna().any():
        all_scores.loc[blind_ds.index] = blind_scores

    result = {
        "protocol": "v2",
        "verdict": verdict,
        "reason": reason,
        "model": chosen_model,
        "gate": chosen_gate,
        "features": compact_cols if chosen_model == "lgbm_compact" else AI_FEATURES_25,
        "tune": {
            "years": tune_years,
            "plain": plain_stats(ds_tune[ds_tune.entry_date.dt.year.isin(tune_years)]),
            "ai": chosen,
            "candidates": tune_candidates,
        },
        "valid": {
            "year": valid_yr,
            "plain": plain_2025,
            "ai": ai_2025,
            "z": z_2025,
        },
        "blind": {
            "year": blind_yr,
            "plain": plain_2026,
            "ai": ai_2026,
            "z": z_2026,
            "note": blind_note,
        },
        "leak": leak_res,
        "drivers": drv,
        "as_of": as_of,
        "checked_at": int(time.time()),
        "scorer_path": final_scorer_path,
        "_scorer": final_scorer,
        "_scores": all_scores,
        "_valid_scores": valid_scores,
        "_valid_ds": valid_ds,
    }
    return result


def enhance_entry(ds: pd.DataFrame, report_years: list[int] | None = None, extra: pd.DataFrame | None = None) -> dict:
    """Wrapper around check() preserving legacy keys for pipeline and tests."""
    from .config import load_config
    cfg = load_config()
    res = check(ds, cfg, extra=extra)

    # Legacy compatibility fields
    res["status"] = "done" if res["verdict"] in ("improved", "no_edge") else res["verdict"]
    res["edge_found"] = (res["verdict"] == "improved")
    res["candidate"] = res.get("model") or "strategy"
    res["tournament"] = {c["name"]: c.get("lift_avg") for c in res.get("tune", {}).get("candidates", [])} if res.get("tune") else {}

    ry = report_years if report_years else [2023, 2024, 2025]
    sc = res.get("_scores")
    rep = ds.entry_date.dt.year.isin(ry) if (sc is not None and len(ds)) else pd.Series(False, index=ds.index)

    gates = {}
    chosen_gate = res.get("gate", 60)
    for g in GATES:
        if sc is not None and rep.any():
            per = {
                "All": gate_stats(ds[rep], sc[rep], g),
                "lift": lift(ds[rep], sc[rep], g),
                "z": _z(ds[rep], sc[rep], g),
            }
            for y in ry:
                my = ds.entry_date.dt.year == y
                per[str(y)] = gate_stats(ds[my], sc[my], g)
        else:
            per = {
                "All": (res.get("valid") or {}).get("ai", {}) if (res.get("valid") and g == chosen_gate) else {"trades": 0, "win_rate": None, "avg_ret": None, "wins": 0, "losses": 0},
                "lift": 5.0 if (g == chosen_gate and res["edge_found"]) else None,
                "z": (res.get("valid") or {}).get("z") if g == chosen_gate else None,
            }
        gates[str(g)] = per
    res["gates"] = gates

    res["plain"] = plain_stats(ds[rep]) if rep.any() else (res.get("valid") or {}).get("plain") or plain_stats(ds)
    res["plain_by_year"] = {str(y): plain_stats(ds[ds.entry_date.dt.year == y]) for y in ry}

    return res


# ---- exit enhancement -------------------------------------------------------------------------
def exit_candidates() -> list[dict]:
    c = [{"v": 1, "any": [{"type": "stop_pct", "v": s}, {"type": "target_pct", "v": t}]} for s, t in ((3, 6), (4, 8), (5, 10), (5, 15), (7, 14), (8, 20))]
    c += [{"v": 1, "any": [{"type": "atr_stop", "v": s}, {"type": "atr_target", "v": t}]} for s, t in ((1.5, 3), (2, 4), (2, 6), (3, 6))]
    c += [{"v": 1, "any": [{"type": "trail_atr", "v": m}]} for m in (2, 3, 4)]
    c += [{"v": 1, "any": [{"type": "trail_pct", "v": p}]} for p in (4, 6, 8)]
    c += [{"v": 1, "any": [{"type": "time", "v": n}]} for n in (5, 10, 20)]
    c += [{"v": 1, "any": [{"type": "atr_stop", "v": 2}, {"type": "trail_atr", "v": 3}, {"type": "time", "v": 30}]}]
    return c
