"""The liquid universe: the ~500 most-traded stocks that get the heavy work (past tests, strategy tracking, AI scoring).

Why: a past test over all ~2,700 stocks is slow on a phone and most of the extra names trade too thinly to act on. Everything
cheap (screens, X-Ray, news, scans of today) still covers every stock. The flag is stored per stock in the `universe` table
(`free_backtest` = 1), so a paid tier can later switch the same work on for all stocks with no code change.

Rule (rebuilt monthly, `python -m lab.cli universe`):
  * listed for at least 250 bars, traded in the last 10 days, last close >= universe_min_price
  * ranked by median daily turnover (close x volume) over the snapshot window
  * top `free_universe_size` become free_backtest=1. Current members get a 20% rank bonus so the list does not churn.
  * added_on / removed_on are kept, so a past test can be explained and never silently drops a stock that later failed."""
from __future__ import annotations

import time
from datetime import date

import pandas as pd
import pyarrow.parquet as pq

from . import store
from .config import Config
from .db import tx

_cache: dict = {"t": 0.0, "v": None}


def build(cfg: Config, con, tf: str = "1D") -> dict:
    snap = store.snap_path(cfg, tf)
    if not snap.exists():
        store.build_snapshot(cfg, tf)
    df = pq.read_table(snap, columns=["symbol", "date", "close", "volume"]).to_pandas()
    as_of = df["date"].max()
    df["turn"] = df["close"].astype("float64") * df["volume"].astype("float64") / 1e7   # ₹ crore a day
    g = df.groupby("symbol")
    st = pd.DataFrame({"turnover_cr": g["turn"].median(), "last_close": g["close"].last(), "last_date": g["date"].max()})
    bars = {}
    for p in cfg.tf_dir(tf).glob("*.parquet"):
        try:
            bars[p.stem] = pq.ParquetFile(p).metadata.num_rows
        except Exception:  # noqa: BLE001
            continue
    st["bars"] = pd.Series(bars)
    ok = st[(st["bars"].fillna(0) >= 250) & (st["last_close"] >= cfg.universe_min_price) & (st["last_date"] >= as_of - pd.Timedelta(days=10))
            & (st["turnover_cr"] > 0)].copy()
    cur = {r[0] for r in con.execute("SELECT symbol FROM universe WHERE free_backtest=1")}
    ok["eff"] = ok["turnover_cr"] * [1.2 if s in cur else 1.0 for s in ok.index]
    ok = ok.sort_values("eff", ascending=False)
    ok["rank"] = range(1, len(ok) + 1)
    keep = set(ok.index[: int(cfg.free_universe_size)])
    today, now = date.today().isoformat(), time.time()
    added = removed = 0
    with tx(con):
        known = {r[0]: r for r in con.execute("SELECT symbol, free_backtest, added_on FROM universe")}
        for sym, r in st.iterrows():
            flag = 1 if sym in keep else 0
            was = known.get(sym)
            add_on = (was["added_on"] if was else None)
            rem_on = None
            if flag and (not was or not was["free_backtest"]):
                add_on, added = today, added + 1
            if was and was["free_backtest"] and not flag:
                rem_on, removed = today, removed + 1
            rank = int(ok.loc[sym, "rank"]) if sym in ok.index else None
            con.execute("""INSERT INTO universe(symbol,free_backtest,liquidity_rank,turnover_cr,last_close,bars,added_on,removed_on,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET free_backtest=excluded.free_backtest, liquidity_rank=excluded.liquidity_rank,
                turnover_cr=excluded.turnover_cr, last_close=excluded.last_close, bars=excluded.bars,
                added_on=COALESCE(excluded.added_on, universe.added_on), removed_on=COALESCE(excluded.removed_on, CASE WHEN excluded.free_backtest=1 THEN NULL ELSE universe.removed_on END),
                updated_at=excluded.updated_at""",
                        (sym, flag, rank, round(float(r["turnover_cr"]), 2), float(r["last_close"]), int(r["bars"]) if pd.notna(r["bars"]) else None, add_on, rem_on, now))
    _cache["t"] = 0.0
    return {"stocks": int(len(st)), "free": len(keep), "added": added, "removed": removed, "min_turnover_cr": round(float(ok.loc[list(keep), "turnover_cr"].min()), 2) if keep else None}


def free_symbols(cfg: Config) -> set[str] | None:
    """Symbols that get heavy work, or None when no restriction applies (setting off, or the universe was never built)."""
    if not cfg.restrict_universe:
        return None
    if _cache["v"] is not None and time.time() - _cache["t"] < 60:
        return _cache["v"]
    from . import db
    con = db.connect(cfg)
    try:
        rows = [r[0] for r in con.execute("SELECT symbol FROM universe WHERE free_backtest=1")]
    except Exception:  # noqa: BLE001 - table missing on an old database
        rows = []
    finally:
        con.close()
    _cache["v"], _cache["t"] = (set(rows) if rows else None), time.time()
    return _cache["v"]


def restrict(cfg: Config, frame: pd.DataFrame, col: str = "symbol") -> pd.DataFrame:
    free = free_symbols(cfg)
    return frame if free is None else frame[frame[col].isin(free)].reset_index(drop=True)


def restrict_asof(cfg: Config, sg: pd.DataFrame, tf: str = "1D") -> pd.DataFrame:
    """Keep a signal only if liq_rank <= cfg.free_universe_size on the signal date."""
    if not cfg.restrict_universe or not len(sg):
        return sg
    xp = store.xsec_path(cfg, tf)
    if not xp.exists():
        return restrict(cfg, sg)
    con = store.connect(cfg)
    try:
        con.register("signals_in", sg)
        res = con.execute(f"""
            SELECT s.*
            FROM signals_in s
            JOIN read_parquet('{xp}') x
              ON s.symbol = x.symbol AND CAST(s.date AS TIMESTAMP) = x.date
            WHERE x.liq_rank IS NOT NULL AND x.liq_rank <= {int(cfg.free_universe_size)}
            ORDER BY s.date, s.symbol
        """).df()
        return res.reset_index(drop=True)
    finally:
        con.close()


def label(cfg: Config) -> dict:
    free = free_symbols(cfg)
    n = len(free) if free is not None else None
    txt = f"the {cfg.free_universe_size} most traded stocks on each day" if free is not None else "all stocks"
    return {"universe": "free" if free is not None else "all", "universe_n": n, "label": txt}

