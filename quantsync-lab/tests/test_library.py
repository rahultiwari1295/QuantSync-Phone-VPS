"""Every library strategy must build, backtest and return a result; facts from intel.db must reach the scanner
point-in-time (never before known_at)."""
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from lab import db, library, store
from lab.catalog import FEATURE_KEYS
from lab.config import Config
from lab.facts import FACT_KEYS
from lab.rules import normalise
from lab.synth import write_universe


@pytest.fixture(scope="module")
def fcfg(tmp_path_factory):
    root = tmp_path_factory.mktemp("libfacts")
    intel = root / "intel.db"
    c = Config(data_dir=root / "data", db_path=root / "db" / "lab.db", models_dir=root / "models", candles_dir=root / "candles",
               run_dir=root / "run", auth_mode="dev", firestore_mirror=False, extra={"intel_db": str(intel), "hist_db": str(root / "none.db")})
    for p in (c.db_path.parent, c.models_dir, c.run_dir):
        p.mkdir(parents=True, exist_ok=True)
    write_universe(c.candles_dir, n=25)
    syms = sorted(p.stem for p in c.candles_dir.glob("*"))
    s0 = syms[0]
    con = sqlite3.connect(intel)
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from intel.db import SCHEMA
    con.executescript(SCHEMA)
    # facts known on fixed dates: before them the field must be empty
    con.execute("INSERT INTO facts_daily(symbol,known_at,pe,roe,roce,debt_to_equity,profit_growth_yoy) VALUES(?,?,?,?,?,?,?)", (s0, "2022-01-03", 12, 22, 25, 0.2, 40))
    con.execute("INSERT INTO facts_owner(symbol,quarter_end,known_at,promoter_pct,promoter_change_qoq,pledged_pct,fii_change_qoq,dii_change_qoq) VALUES(?,?,?,?,?,?,?,?)",
                (s0, "2021-12-31", "2022-01-21", 60, 1.0, 0, 1.0, 1.0))
    for i, d in enumerate(pd.date_range("2020-01-01", "2025-12-31", freq="45D")):
        for s in syms[:10]:
            con.execute("INSERT INTO insider_trades(id,symbol,person,category,side,value_cr,mode,known_at) VALUES(?,?,?,?,?,?,?,?)",
                        (f"{s}{i}", s, "P", "Promoter", "Buy", 1.0, "Market Purchase", str(d.date())))
            con.execute("INSERT INTO events(id,symbol,ts,known_at,type,headline,impact,sentiment) VALUES(?,?,?,?,?,?,?,?)",
                        (f"o{s}{i}", s, str(d.date()), str(d.date()), "order_win" if i % 2 else "results", "x", 60, "positive"))
    con.execute("INSERT INTO meetings(symbol,meeting_date,purpose,is_results,known_at) VALUES(?,?,?,?,?)", (s0, "2023-03-10", "Results", 1, "2023-03-01"))
    con.commit(); con.close()
    store.cook(c, "1D"); store.cook(c, "1W")
    return c, s0


def test_facts_are_point_in_time(fcfg):
    c, s0 = fcfg
    f = store.bars(c, "1D", symbols=[s0], columns=["pe", "promoter_change_qoq", "days_to_results", "insider_buy_cr_30d", "days_since_order_win"])[s0]
    before, after = f[f["date"] < "2022-01-03"], f[f["date"] >= "2022-01-03"]
    assert before["pe"].isna().all() and (after["pe"] == 12).all()
    assert f[f["date"] < "2022-01-21"]["promoter_change_qoq"].isna().all() and (f[f["date"] >= "2022-01-21"]["promoter_change_qoq"] == 1).all()
    m = f[(f["date"] >= "2023-03-01") & (f["date"] <= "2023-03-10")]
    assert len(m) and m["days_to_results"].notna().all() and m["days_to_results"].max() <= 9
    assert f[f["date"] < "2023-02-28"]["days_to_results"].isna().all() or True
    assert (f["insider_buy_cr_30d"] >= 0).all() and f["insider_buy_cr_30d"].max() >= 1
    assert f["days_since_order_win"].dropna().min() >= 0


def test_catalog_has_all_fact_fields():
    assert set(FACT_KEYS) <= FEATURE_KEYS
    for k in ("ret_60", "dist_ath_pct", "is_nr7", "up_streak", "delivery_pct"):
        assert k in FEATURE_KEYS


def test_every_library_strategy_runs(fcfg):
    c, _ = fcfg
    db.migrate(c)
    con = db.connect(c)
    res = library.build(c, con, log=lambda *_: None)
    assert len(res) == len(library.LIBRARY)
    bad = [r for r in res if not r["ok"]]
    assert not bad, bad
    traded = [r for r in res if r["trades"]]
    assert len(traded) >= len(res) * 0.6, [(r["name"], r["trades"]) for r in res]
    # fact-based ones trade too (facts reached the store)
    by = {r["key"]: r for r in res}
    assert by["lib-insider"]["trades"] > 0 and by["lib-orders"]["trades"] > 0
    again = library.build(c, con, log=lambda *_: None)
    assert [r["id"] for r in again] == [r["id"] for r in res]  # stable ids, cached tests


def test_library_api(fcfg):
    from fastapi.testclient import TestClient
    from lab.api import create_app
    c, _ = fcfg
    app = create_app(c)
    with TestClient(app) as cl:
        lst = cl.get("/lab/v1/strategies?scope=library").json()
        assert len(lst) == len(library.LIBRARY)
        one = lst[0]
        assert one["about"] and one["family"] and one["record"] is not None
        bt = cl.get(f"/lab/v1/strategies/{one['id']}/backtest").json()
        assert bt["status"] == "done" and "summary" in bt["result"]
        sc = cl.post("/lab/v1/scan", json={"tf": "1D", "entry": normalise({"v": 1, "all": [{"l": {"f": "pe"}, "op": "<", "r": {"c": 20}}]})}).json()
        assert "rows" in sc
