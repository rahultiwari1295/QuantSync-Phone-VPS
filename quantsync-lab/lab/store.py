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
    from .facts import Facts
    facts = Facts(cfg)
    for sym, d in iter_candles(cfg, symbols):
        try:
            if isinstance(d, Exception):
                raise d
            if len(d) < 30:
                skipped.append(sym)
                continue
            f = compute(to_weekly(d) if tf == "1W" else d, tf)
            f = facts.enrich(sym, f).copy()
            fk = [k for k in f.columns if k not in ("date",)]
            f = pd.concat([pd.DataFrame({"symbol": sym}, index=f.index), f[["date"]], f[fk].astype("float32")], axis=1)
            pq.write_table(pa.Table.from_pandas(f, preserve_index=False), tmp / f"{sym}.parquet", compression="zstd")
            rows += len(f); done += 1
            last = max(last, f["date"].max()) if last is not None else f["date"].max()
        except Exception as e:  # one bad symbol must never block the cook
            skipped.append(f"{sym}:{type(e).__name__}")
    sources = facts.available
    facts.close()
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
            "as_of": str(pd.Timestamp(last).date()), "seconds": round(time.time() - t0, 1), "fact_sources": sources}
    (target / "_meta.json").write_text(json.dumps(meta))
    try:
        meta["snapshot"] = build_snapshot(cfg, tf)
    except Exception as e:  # noqa: BLE001 - scans fall back to the full store until the next successful build
        meta["snapshot"] = {"error": f"{type(e).__name__}: {e}"}
    if tf == "1D":
        try:
            meta["xsec"] = build_xsec(cfg, tf)
        except Exception as e:  # noqa: BLE001
            meta["xsec"] = {"error": f"{type(e).__name__}: {e}"}
    return meta


def meta(cfg: Config, tf: str) -> dict | None:
    p = cfg.tf_dir(tf) / "_meta.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---- snapshot: the last few bars of every stock in ONE small file ------------------------------------------------
# A "scan today" only needs the latest bar plus a little look-back for [n] lags. Reading that from one 20 MB file takes
# ~50 ms; globbing 2,700 full-history files took ~13 s. The snapshot is rebuilt at the end of every cook.
def snap_path(cfg: Config, tf: str) -> Path:
    return cfg.features_dir / f"snap_tf={tf}.parquet"


def snap_meta(cfg: Config, tf: str) -> dict | None:
    p = cfg.features_dir / f"snap_tf={tf}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (OSError, ValueError):
        return None


def build_snapshot(cfg: Config, tf: str = "1D") -> dict:
    """Keep the last `snapshot_bars` rows of every symbol of the current cooked store. Atomic (temp file, then rename)."""
    t0, frames = time.time(), []
    for p in sorted(cfg.tf_dir(tf).glob("*.parquet")):
        try:
            t = pq.read_table(p).to_pandas()
            frames.append(t.tail(int(cfg.snapshot_bars)))
        except Exception:  # noqa: BLE001 - one unreadable file must not block the snapshot
            continue
    if not frames:
        raise RuntimeError("no cooked symbols to snapshot")
    df = pd.concat(frames, ignore_index=True)
    out = snap_path(cfg, tf)
    tmp = out.with_suffix(".tmp")
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), tmp, compression="zstd")
    tmp.replace(out)
    m = meta(cfg, tf) or {}
    info = {"tf": tf, "as_of": m.get("as_of"), "symbols": int(df["symbol"].nunique()), "rows": int(len(df)),
            "built_at": time.time(), "seconds": round(time.time() - t0, 1)}
    (cfg.features_dir / f"snap_tf={tf}.json").write_text(json.dumps(info))
    return info


def snapshot_fresh(cfg: Config, tf: str) -> bool:
    """The snapshot exists and was built from the data that is live now."""
    s, m = snap_meta(cfg, tf), meta(cfg, tf)
    return bool(s and m and s.get("as_of") == m.get("as_of") and snap_path(cfg, tf).exists())


def connect(cfg: Config) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(f"SET threads={int(cfg.duckdb_threads)}; SET memory_limit='{cfg.duckdb_memory_limit}'")
    return con


def _glob(cfg: Config, tf: str) -> str:
    return str(cfg.tf_dir(tf) / "*.parquet")


# ---- xsec: cross-sectional metrics (liq_rank, rs60_pct) for all stocks on each date -----------------------------
def xsec_path(cfg: Config, tf: str = "1D") -> Path:
    return cfg.features_dir / f"xsec_{tf}.parquet"


def xsec_meta(cfg: Config, tf: str = "1D") -> dict | None:
    p = cfg.features_dir / f"xsec_{tf}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (OSError, ValueError):
        return None


def build_xsec(cfg: Config, tf: str = "1D") -> dict:
    """Build cross-sectional metrics (liq_rank, rs60_pct) for all symbols on each date. Atomic."""
    t0 = time.time()
    parquet_files = list(cfg.tf_dir(tf).glob("*.parquet"))
    if not parquet_files:
        raise RuntimeError(f"no cooked symbols to build xsec for {tf}")

    from .api import BENCH
    bench_sym = None
    for s in BENCH:
        if (cfg.tf_dir(tf) / f"{s}.parquet").exists():
            bench_sym = s
            break

    if bench_sym:
        bench_p = str(cfg.tf_dir(tf) / f"{bench_sym}.parquet")
        bench_join = f"LEFT JOIN (SELECT \"date\", \"ret_60\" AS bench_ret_60 FROM read_parquet('{bench_p}')) b ON f.\"date\" = b.\"date\""
        rs_diff_expr = "CASE WHEN b.bench_ret_60 IS NOT NULL AND NOT isnan(b.bench_ret_60) THEN f.\"ret_60\" - b.bench_ret_60 ELSE f.\"ret_60\" END"
    else:
        import logging
        logging.getLogger(__name__).warning("build_xsec: no benchmark symbol found in %s; using ret_60 alone", BENCH)
        bench_join = ""
        rs_diff_expr = 'f."ret_60"'

    con = connect(cfg)
    out = xsec_path(cfg, tf)
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)

    sql = f"""
    WITH base AS (
        SELECT
            f."symbol",
            f."date",
            f."turnover_med_20",
            {rs_diff_expr} AS rs_diff
        FROM read_parquet('{_glob(cfg, tf)}') f
        {bench_join}
    )
    SELECT
        "date",
        "symbol",
        CASE WHEN "turnover_med_20" IS NOT NULL AND NOT isnan("turnover_med_20") AND "turnover_med_20" > 0
             THEN CAST(rank() OVER (PARTITION BY "date", ("turnover_med_20" IS NOT NULL AND NOT isnan("turnover_med_20") AND "turnover_med_20" > 0) ORDER BY "turnover_med_20" DESC) AS INT)
             ELSE NULL END AS liq_rank,
        CASE WHEN rs_diff IS NOT NULL AND NOT isnan(rs_diff)
             THEN CAST(percent_rank() OVER (PARTITION BY "date", (rs_diff IS NOT NULL AND NOT isnan(rs_diff)) ORDER BY rs_diff ASC) * 100.0 AS FLOAT)
             ELSE NULL END AS rs60_pct
    FROM base
    ORDER BY "date", "symbol"
    """
    try:
        con.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        tmp.replace(out)
        rows = pq.ParquetFile(out).metadata.num_rows
    finally:
        con.close()
        tmp.unlink(missing_ok=True)

    m = meta(cfg, tf) or {}
    info = {"tf": tf, "as_of": m.get("as_of"), "rows": int(rows), "bench": bench_sym,
            "built_at": time.time(), "seconds": round(time.time() - t0, 1)}
    (cfg.features_dir / f"xsec_{tf}.json").write_text(json.dumps(info))
    return info


def signals(cfg: Config, rule: dict, tf: str, start: str | None = None, end: str | None = None,
            only_last: bool = False, columns: str = '"symbol","date","close"') -> pd.DataFrame:
    """Rows where the rule is true. `only_last` restricts to the latest bar of the store (the live scan)."""
    c: Compiled = compile_rule(rule)
    con = connect(cfg)
    try:
        fast = only_last and not start and not end and snapshot_fresh(cfg, tf)
        raw_src = f"read_parquet('{snap_path(cfg, tf)}')" if fast else f"read_parquet('{_glob(cfg, tf)}')"
        xsec_p = xsec_path(cfg, tf)
        if xsec_p.exists():
            src = f"(SELECT s.*, x.liq_rank, x.rs60_pct FROM {raw_src} s LEFT JOIN read_parquet('{xsec_p}') x ON s.symbol = x.symbol AND s.date = x.date)"
        else:
            src = raw_src
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
