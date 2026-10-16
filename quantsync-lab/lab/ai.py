"""Meta-labelling: the user's rule makes the trades, a LightGBM model learns which of them tend to work.

Walk-forward only: a trade in year Y is scored by a model trained on trades that had fully resolved before
Jan 1 of Y. Candidates are chosen on 2020-2022 and reported on 2023+. Scores are ECDF percentiles (0-100)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .catalog import FEATURES, MODEL_FEATURES

GATES = [50, 60, 70, 80, 90]
MIN_TRADES = 40
MIN_TRAIN = 40
LABELS = {f["key"]: f["label"] for f in FEATURES}
PARAMS = dict(objective="binary", num_leaves=15, max_depth=4, min_child_samples=40, learning_rate=0.05,
              colsample_bytree=0.7, subsample=0.8, subsample_freq=1, reg_lambda=5.0, n_jobs=1,
              random_state=7, verbose=-1)


def _lgb():
    import lightgbm as lgb
    return lgb


def make_model(n_rows: int):
    # fewer trees on small samples; the 400-tree ceiling is the design value
    return _lgb().LGBMClassifier(n_estimators=int(min(400, max(60, n_rows // 3))), **PARAMS)


class Scorer:
    """A fitted model plus the ECDF that turns probabilities into 0-100 scores."""

    def __init__(self, model, cal: np.ndarray, cols: list[str]):
        self.model, self.cal, self.cols = model, np.sort(cal), cols

    def prob(self, X: pd.DataFrame) -> np.ndarray:
        if hasattr(self.model, "predict_proba"):
            return self.model.predict_proba(X[self.cols])[:, 1]
        return self.model.predict(X[self.cols])

    def save(self, path) -> None:
        import json
        from pathlib import Path
        b = self.model.booster_ if hasattr(self.model, "booster_") else self.model
        Path(path).with_suffix(".txt").write_text(b.model_to_string())
        Path(path).with_suffix(".json").write_text(json.dumps({"cal": self.cal.tolist(), "cols": self.cols}))

    @classmethod
    def load(cls, path) -> "Scorer":
        import json
        from pathlib import Path
        meta = json.loads(Path(path).with_suffix(".json").read_text())
        b = _lgb().Booster(model_str=Path(path).with_suffix(".txt").read_text())
        return cls(b, np.array(meta["cal"]), meta["cols"])

    def score(self, X: pd.DataFrame) -> np.ndarray:
        return np.searchsorted(self.cal, self.prob(X), side="right") / len(self.cal) * 100


def fit_scorer(X: pd.DataFrame, y: np.ndarray, cols: list[str] = MODEL_FEATURES, shuffle_seed: int | None = None):
    """X must be sorted by time. Last 20 % is held out only to build the ECDF."""
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


def oos_scores(ds: pd.DataFrame, years: list[int], *, shuffle_seed: int | None = None, extra: pd.DataFrame | None = None,
               cols: list[str] = MODEL_FEATURES) -> pd.Series:
    """ds: closed trades with entry_date, exit_date, y and feature columns. Returns score aligned to ds.index (NaN if unscored)."""
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
    """How many standard errors the gate's win-rate lift is above the plain rule (guards against best-of-five luck)."""
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


def drivers(scorer: Scorer, X: pd.DataFrame, top: int = 3) -> list[str]:
    """Plain-English reasons from LightGBM's built-in SHAP contributions (no extra dependency)."""
    if len(X) < 10:
        return []
    contrib = scorer.model.predict(X[scorer.cols], pred_contrib=True)[:, :-1]
    imp = np.abs(contrib).mean(axis=0)
    out = []
    for j in np.argsort(-imp)[:top]:
        if imp[j] <= 0:
            continue
        x = X[scorer.cols[j]].to_numpy(dtype=float)
        ok = ~np.isnan(x)
        corr = np.corrcoef(x[ok], contrib[ok, j])[0, 1] if ok.sum() > 5 and np.std(x[ok]) > 0 else 0
        out.append(f"{LABELS.get(scorer.cols[j], scorer.cols[j])} {'higher' if corr >= 0 else 'lower'} than usual helped")
    return out


def pick_candidate(ds: pd.DataFrame, cfg_select=(2020, 2021, 2022), extra: pd.DataFrame | None = None) -> tuple[str, dict]:
    """Candidate tournament on the selection window only. Returns (name, {name: lift})."""
    sel = ds[ds.entry_date.dt.year.isin(cfg_select)]
    res = {}
    for name, ex in (("strategy", None), ("pooled", extra)):
        if name == "pooled" and ex is None:
            continue
        l = lift(sel, oos_scores(ds, list(cfg_select), extra=ex), 60)
        res[name] = l
    ok = {k: v for k, v in res.items() if v is not None}
    best = max(ok, key=ok.get) if ok else "strategy"
    return best, res


def enhance_entry(ds: pd.DataFrame, report_years: list[int], extra: pd.DataFrame | None = None) -> dict:
    """Full entry-gate analysis. ds = closed trades (entry/exit dates, ret_pct, y, features)."""
    if len(ds) < MIN_TRADES:
        return {"status": "insufficient", "reason": f"Only {len(ds)} past trades; at least {MIN_TRADES} are needed."}
    cand, tour = pick_candidate(ds, extra=extra)
    ex = extra if cand == "pooled" else None
    all_years = [2020, 2021, 2022] + report_years
    sc = oos_scores(ds, all_years, extra=ex)
    rep = ds.entry_date.dt.year.isin(report_years)
    plain_all = {"trades": int(rep.sum()), "win_rate": round(float((ds[rep].ret_pct > 0).mean() * 100), 1) if rep.any() else None,
                 "avg_ret": round(float(ds[rep].ret_pct.mean()), 2) if rep.any() else None}
    plain_by_year = {str(y): plain_stats(ds[ds.entry_date.dt.year == y]) for y in report_years}
    gates = {}
    for g in GATES:
        per = {"All": gate_stats(ds[rep], sc[rep], g)}
        for y in report_years:
            my = ds.entry_date.dt.year == y
            per[str(y)] = gate_stats(ds[my], sc[my], g)
        sel = ds[rep & (sc >= g)]
        per["months"] = [{"month": k, "trades": int(len(v))} for k, v in sel.groupby(sel.entry_date.dt.strftime("%Y-%m"))]
        per["lift"] = lift(ds[rep], sc[rep], g)
        per["z"] = _z(ds[rep], sc[rep], g)
        gates[str(g)] = per
    leak = leak_test(ds, all_years, extra=ex)
    best_z = max([gv["z"] for gv in gates.values() if gv["z"] is not None], default=None)
    sel_lift = tour.get(cand)
    final = fit_scorer(ds.sort_values("entry_date"), ds.sort_values("entry_date")["y"].to_numpy())
    kept = ds[sc >= 60]
    return {"status": "done", "candidate": cand, "tournament": tour, "plain": plain_all, "plain_by_year": plain_by_year, "gates": gates, "leak": leak,
            "edge_found": bool(best_z is not None and best_z >= 2.5 and sel_lift is not None and sel_lift > 0 and leak["passed"]),
            "drivers": drivers(final, kept if len(kept) >= 10 else ds) if final else [],
            "_scores": sc, "_scorer": final}


# ---- exit enhancement -------------------------------------------------------------------------
def exit_candidates() -> list[dict]:
    c = [{"v": 1, "any": [{"type": "stop_pct", "v": s}, {"type": "target_pct", "v": t}]} for s, t in ((3, 6), (4, 8), (5, 10), (5, 15), (7, 14), (8, 20))]
    c += [{"v": 1, "any": [{"type": "atr_stop", "v": s}, {"type": "atr_target", "v": t}]} for s, t in ((1.5, 3), (2, 4), (2, 6), (3, 6))]
    c += [{"v": 1, "any": [{"type": "trail_atr", "v": m}]} for m in (2, 3, 4)]
    c += [{"v": 1, "any": [{"type": "trail_pct", "v": p}]} for p in (4, 6, 8)]
    c += [{"v": 1, "any": [{"type": "time", "v": n}]} for n in (5, 10, 20)]
    c += [{"v": 1, "any": [{"type": "atr_stop", "v": 2}, {"type": "trail_atr", "v": 3}, {"type": "time", "v": 30}]}]
    return c
