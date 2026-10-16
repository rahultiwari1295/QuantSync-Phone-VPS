"""Endpoints added for the redesigned app: chart analytics, chart batch, benchmark, symbol strategies, holdings."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lab import api, db, metrics, service, store, worker

H = {"X-Dev-Uid": "app1"}
RULE = {"tf": "1D", "entry": {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 55}}]},
        "exit": {"v": 1, "any": [{"type": "stop_pct", "v": 5}, {"type": "target_pct", "v": 10}, {"type": "time", "v": 15}]}}


@pytest.fixture()
def client(cfg):
    with TestClient(api.create_app(cfg)) as c:
        yield c


def test_analytics_shape_and_math():
    t = pd.DataFrame({"symbol": list("ABCD"), "entry_date": pd.to_datetime(["2024-01-02"] * 4),
                      "exit_date": pd.to_datetime(["2024-01-10", "2024-01-10", "2024-02-05", "2024-02-06"]),
                      "ret_pct": [10.0, -4.0, 5.0, -2.0], "exit_reason": ["target_pct", "stop_pct", "time", "stop_pct"], "is_open": [False] * 4})
    a = metrics.analytics(t)
    assert a["equity"][0]["v"] == 100.0
    # day 1: mean(10,-4)=3% -> 103; then +5% -> 108.15; then -2% -> 105.99
    assert a["equity"][-1]["v"] == pytest.approx(105.99, abs=0.01)
    assert [m["month"] for m in a["monthly"]] == ["2024-01", "2024-02"]
    assert a["monthly"][0]["ret"] == pytest.approx(3.0) and a["monthly"][1]["ret"] == pytest.approx(2.9, abs=0.01)
    assert sum(b["n"] for b in a["dist"]) == 4
    ex = {e["label"]: e["n"] for e in a["exits"]}
    assert ex == {"Stop hit": 2, "Target hit": 1, "Time limit": 1}
    assert a["max_losing_streak"] == 1 and a["avg_win"] == 7.5 and a["avg_loss"] == -3.0
    assert metrics.analytics(t.iloc[0:0])["equity"] == []


def test_backtest_carries_analytics_and_card_spark(client, cfg):
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Spark test"}, headers=H).json()["id"]
    con = db.connect(cfg)
    while worker.run_one(cfg, con, "fast"):
        pass
    bt = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=H).json()
    assert bt["status"] == "done"
    an = bt["result"]["analytics"]
    assert an["equity"] and an["monthly"] and an["dist"] and an["exits"]
    card = client.get(f"/lab/v1/strategies/{sid}", headers=H).json()
    assert len(card["spark"]) >= 2


def test_chart_batch_and_benchmark(client, cfg):
    syms = sorted(p.stem for p in cfg.tf_dir("1D").glob("*.parquet"))[:3]
    r = client.get(f"/lab/v1/chart-batch?symbols={','.join(syms)},NOPE&n=30").json()
    assert set(r["bars"]) == set(syms) and len(r["bars"][syms[0]]) == 30 and "close" in r["bars"][syms[0]][0]
    assert client.get("/lab/v1/chart-batch?symbols=").status_code == 400
    b = client.get("/lab/v1/benchmark").json()
    assert b["points"] == [] and b["symbol"] is None  # synthetic universe has no index


def test_symbol_strategies(client, cfg):
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Sym test"}, headers=H).json()["id"]
    con = db.connect(cfg)
    con.execute("UPDATE strategy_versions SET live_from='2026-01-01' WHERE strategy_id=?", (sid,))
    service.update_live(cfg, con)
    row = con.execute("""SELECT l.symbol FROM live_trades l JOIN strategies s ON s.current_version=l.version_id
                         WHERE s.id=? AND l.is_open=1 LIMIT 1""", (sid,)).fetchone()
    if row is None:
        pytest.skip("no open live trade in the synthetic data")
    mine = client.get(f"/lab/v1/symbol/{row[0]}/strategies", headers=H).json()["rows"]
    assert any(r["sid"] == sid and r["cat"] == "y" for r in mine)
    other = client.get(f"/lab/v1/symbol/{row[0]}/strategies", headers={"X-Dev-Uid": "someone"}).json()["rows"]
    assert all(r["sid"] != sid for r in other)  # private strategies stay private


def test_holdings_roundtrip(client):
    assert client.get("/lab/v1/me/holdings").status_code == 401
    body = {"items": [{"symbol": "trent", "qty": 10, "avg_price": 5000}, {"symbol": "BEL", "qty": 100, "avg_price": 210.5}]}
    assert client.put("/lab/v1/me/holdings", json=body, headers=H).json()["count"] == 2
    got = client.get("/lab/v1/me/holdings", headers=H).json()["items"]
    assert [g["symbol"] for g in got] == ["BEL", "TRENT"]
    assert client.put("/lab/v1/me/holdings", json={"items": [{"symbol": "x y", "qty": 1, "avg_price": 1}]}, headers=H).status_code == 422
    client.put("/lab/v1/me/holdings", json={"items": []}, headers=H)
    assert client.get("/lab/v1/me/holdings", headers=H).json()["items"] == []
