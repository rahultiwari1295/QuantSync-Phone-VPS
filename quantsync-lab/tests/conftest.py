import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lab.config import Config  # noqa: E402
from lab.store import cook  # noqa: E402
from lab.synth import write_universe  # noqa: E402


@pytest.fixture(scope="session")
def cfg(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab")
    c = Config(data_dir=root / "data", db_path=root / "db" / "lab.db", models_dir=root / "models",
               candles_dir=root / "candles", run_dir=root / "run", auth_mode="dev", firestore_mirror=False)
    for p in (c.db_path.parent, c.models_dir, c.run_dir):
        p.mkdir(parents=True, exist_ok=True)
    write_universe(c.candles_dir, n=20)
    cook(c, "1D"); cook(c, "1W")
    return c
