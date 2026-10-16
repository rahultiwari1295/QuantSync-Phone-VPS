"""Cooked feature store: one Parquet file per symbol per timeframe, queried through DuckDB."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import FSV
from .config import Config
from .features import compute, to_weekly
from .rules import Compiled, compile_rule, lag_select

REQUIRED = ["date", "open", "high", "low", "close", "volume"]


def read_candles(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    df = df.rename(columns=str.lower)
    missing = set(REQUIRED) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name}: missing {sorted(missing)}")
    df = df[REQUIRED].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).drop_duplicates("date").sort_values("date")
    bad = (df.high < df[["open", "close", "low"]].max(axis=1) - 1e-6) | (df.low > df[["open", "close", "high"]].min(axis=1) + 1e-6)
    return df[~bad & (df.close > 0)].reset_index(drop=True)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[REQUIRED].copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["close"]).drop_duplicates("date").sort_values("date")
    bad = (df.high < df[["open", "close", "low"]].max(axis=1) - 1e-6) | (df.low > df[["open", "close", "high"]].min(axis=1) + 1e-6)
    return df[~bad & (df.close > 0)].reset_index(drop=True)


def iter_candles(cfg: Config, symbols: list[str] | None = None):
    """Yield (symbol, candles). Source: live engine's StockHistory SQLite (rawCandles JSON, opened read-only)
    when `candles_db` is set, otherwise one parquet/csv file per symbol in `candles_dir`."""
    if cfg.candles_db:
        import sqlite3
        con = sqlite3.connect(f"file:{cfg.candles_db}?mode=ro", uri=True, timeout=30)
        try:
            for sym, raw in con.execute("SELECT symbol, rawCandles FROM StockHistory WHERE rawCandles IS NOT NULL ORDER BY symbol"):
                if symbols and sym not in symbols:
                    continue
                try:
                    rows = json.loads(raw)
                    df = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
                    yield sym, _clean(df)
                except Exception as e:  # noqa: BLE001
                    yield sym, e
        finally:
            con.close()
        return
    for p in sorted(cfg.candles_dir.glob("*")):
        if p.suffix in (".parquet", ".csv") and (not symbols or p.stem in symbols):
            try:
                yield p.stem, read_candles(p)
            except Exception as e:  # noqa: BLE001
                yield p.stem, e


def cook(cfg: Config, tf: str = "1D", symbols: list[str] | None = None) -> dict:
    """Rebuild a timeframe into a temp dir, validate, then swap atomically. Returns a summary."""
    t0, target = time.time(), cfg.tf_dir(tf)
    tmp = target.with_name(target.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    done, skipped, rows, last = 0, [], 0, None
    for sym, d in iter_candles(cfg, symbols):
        try:
            if isinstance(d, Exception):
                raise d
            if len(d) < 30:
                skipped.append(sym)
                continue
            f = compute(to_weekly(d) if tf == "1W" else d, tf)
            f.insert(0, "symbol", sym)
            pq.write_table(pa.Table.from_pandas(f, preserve_index=False), tmp / f"{sym}.parquet", compression="zstd")
            rows += len(f); done += 1
            last = max(last, f["date"].max()) if last is not None else f["date"].max()
        except Exception as e:  # one bad symbol must never block the cook
            skipped.append(f"{sym}:{type(e).__name__}")
    if done == 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("cook produced no symbols; keeping previous data")
    prev = target.with_name(target.name + ".old")
    shutil.rmtree(prev, ignore_errors=True)
    if target.exists():
        target.rename(prev)
    tmp.rename(target)
    shutil.rmtree(prev, ignore_errors=True)
    meta = {"fsv": FSV, "tf": tf, "symbols": done, "rows": rows, "skipped": skipped,
            "as_of": str(pd.Timestamp(last).date()), "seconds": round(time.time() - t0, 1)}
    (target / "_meta.json").write_text(json.dumps(meta))
    return meta


def meta(cfg: Config, tf: str) -> dict | None:
    p = cfg.tf_dir(tf) / "_meta.json"
    return json.loads(p.read_text()) if p.exists() else None


def connect(cfg: Config) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"SET threads={int(cfg.duckdb_threads)}; SET memory_limit='{cfg.duckdb_memory_limit}'")
    return con


def _glob(cfg: Config, tf: str) -> str:
    return str(cfg.tf_dir(tf) / "*.parquet")


def signals(cfg: Config, rule: dict, tf: str, start: str | None = None, end: str | None = None,
            only_last: bool = False, columns: str = '"symbol","date","close"') -> pd.DataFrame:
    """Rows where the rule is true. `only_last` restricts to the latest bar of the store (the live scan)."""
    c: Compiled = compile_rule(rule)
    con = connect(cfg)
    try:
        src = f"read_parquet('{_glob(cfg, tf)}')"
        lo = ""
        m = meta(cfg, tf) if only_last else None
        as_of = m.get("as_of") if m else None
        if only_last:  # lags need a little history, never the full file
            if as_of:
                lo = f"WHERE \"date\" >= DATE '{as_of}' - INTERVAL 120 DAY"
            else:
                lo = f"WHERE \"date\" >= (SELECT max(\"date\") FROM {src}) - INTERVAL 120 DAY"
        elif start:
            s_date = pd.Timestamp(start).strftime("%Y-%m-%d")
            lo = f"WHERE \"date\" >= CAST('{s_date}' AS DATE) - INTERVAL 120 DAY"
        sql = f'SELECT * FROM (SELECT *{lag_select(c)} FROM {src} {lo})'
        conds, params = [c.where], list(c.params)
        if only_last:
            if as_of:
                conds.append(f'"date" = DATE \'{as_of}\'')
            else:
                conds.append(f'"date" = (SELECT max("date") FROM {src})')
        if start:
            conds.append('"date" >= CAST(? AS DATE)'); params.append(start)
        if end:
            conds.append('"date" <= CAST(? AS DATE)'); params.append(end)
        q = f'SELECT {columns} FROM ({sql}) WHERE {" AND ".join(conds)} ORDER BY "date","symbol"'
        return con.execute(q, params).df()
    finally:
        con.close()


def bars(cfg: Config, tf: str, symbols: list[str] | None = None, start: str | None = None,
         columns: list[str] | None = None) -> dict[str, pd.DataFrame]:
    """Per-symbol feature frames. Pass `columns` to keep memory small on the 4 GB box."""
    out = {}
    cols = None if columns is None else sorted(set(columns) | {"date"})
    for p in sorted(cfg.tf_dir(tf).glob("*.parquet")):
        if symbols is not None and p.stem not in symbols:
            continue
        df = pq.read_table(p, columns=cols).to_pandas()
        if start:
            df = df[df["date"] >= pd.Timestamp(start)]
        out[p.stem] = df.reset_index(drop=True)
    return out
