from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("/opt/quantsync-lab/data")
    db_path: Path = Path("/opt/quantsync-lab/db/lab.db")
    models_dir: Path = Path("/opt/quantsync-lab/models")
    candles_dir: Path = Path("/opt/quantsync-lab/data/candles")
    candles_db: Path | None = None  # live engine StockHistory SQLite (read-only); takes priority over candles_dir
    run_dir: Path = Path("/opt/quantsync-lab/run")
    auth_mode: str = "firebase"
    firebase_project_id: str = ""
    firestore_mirror: bool = True
    train_start: str = "2018-01-01"
    report_start: str = "2023-01-01"
    select_end: str = "2022-12-31"
    duckdb_threads: int = 1
    duckdb_memory_limit: str = "700MB"
    pricing_enabled: bool = False
    free_ai_jobs_per_day: int = 5
    free_backtests_per_hour: int = 10
    queue_cap: int = 50
    engine_api: str = "http://127.0.0.1:8000"  # live QuantSync engine (flagship trades); "" disables
    extra: dict = field(default_factory=dict)

    @property
    def features_dir(self) -> Path:
        return self.data_dir / "features"

    def tf_dir(self, tf: str) -> Path:
        return self.features_dir / f"tf={tf}"


_PATH_KEYS = {"data_dir", "db_path", "models_dir", "candles_dir", "candles_db", "run_dir"}
_DATE_KEYS = {"train_start", "report_start", "select_end"}


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = path or os.environ.get("QSLAB_CONFIG", "/opt/quantsync-lab/config.yaml")
    raw: dict = {}
    if path and Path(path).exists():
        raw = yaml.safe_load(Path(path).read_text()) or {}
    known = Config.__dataclass_fields__.keys()
    kw = {k: (Path(v) if k in _PATH_KEYS else str(v) if k in _DATE_KEYS else v) for k, v in raw.items() if k in known and k != "extra"}
    extra = dict(raw.get("extra") or {})
    for k in ("cors_origins", "intel_db", "hist_db"):  # allowed at top level too
        if k in raw:
            extra[k] = raw[k]
    return Config(**kw, extra=extra)
