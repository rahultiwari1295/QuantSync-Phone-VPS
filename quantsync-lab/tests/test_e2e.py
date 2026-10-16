import pandas as pd
import pytest
from fastapi.testclient import TestClient

from lab import api, db, jobs, service, worker

H = {"X-Dev-Uid": "u1"}
RULE = {"tf": "1D", "entry": {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 55}}, {"l": {"f": "close"}, "op": ">", "r": {"f": "sma_20"}}]},
        "exit": {"v": 1, "any": [{"type": "stop_pct", "v": 5}, {"type": "target_pct", "v": 10}, {"type": "time", "v": 15}]}}


@pytest.fixture()
def client(cfg):
    with TestClient(api.create_app(cfg)) as c:
        yield c


def drain(cfg, lane):
    con = db.connect(cfg)
    while worker.run_one(cfg, con, lane):
        pass


def test_full_flow(client, cfg):
    assert client.get("/lab/v1/health").json()["ok"]
    assert client.get("/lab/v1/catalog").json()["features"]
    s = client.post("/lab/v1/scan", json=RULE)
    assert s.status_code == 200 and "rows" in s.json()
    assert client.post("/lab/v1/scan", json={**RULE, "entry": {"v": 1, "all": [{"l": {"f": "x"}, "op": ">", "r": {"c": 1}}]}}).status_code == 400
    assert client.post("/lab/v1/backtest", json=RULE).status_code == 401
    jid = client.post("/lab/v1/backtest", json=RULE, headers=H).json()["job_id"]
    assert client.get(f"/lab/v1/jobs/{jid}", headers=H).json()["status"] == "queued"
    assert client.get(f"/lab/v1/jobs/{jid}", headers={"X-Dev-Uid": "u2"}).status_code == 404
    drain(cfg, "fast")
    j = client.get(f"/lab/v1/jobs/{jid}", headers=H).json()
    assert j["status"] == "done" and j["result"]["summary"]["All"]["trades"] > 50
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "RSI trend"}, headers=H).json()["id"]
    assert client.get(f"/lab/v1/strategies/{sid}").status_code == 404  # private
    assert client.get(f"/lab/v1/strategies/{sid}", headers=H).json()["info"]["entry"]["v"] == 1
    assert client.post(f"/lab/v1/strategies/{sid}/ai-gate", json={"gate": 70}, headers=H).status_code == 409
    r = client.post(f"/lab/v1/strategies/{sid}/enhance", headers=H).json()
    assert client.post(f"/lab/v1/strategies/{sid}/enhance", headers=H).status_code == 409  # one at a time
    drain(cfg, "ai")
    en = client.get(f"/lab/v1/strategies/{sid}/enhancement", headers=H).json()
    assert en["status"] == "done" and set(en["result"]["gates"]) == {"50", "60", "70", "80", "90"}
    assert en["result"]["leak"]["rounds"] >= 1
    assert en["result"]["plain_by_year"] and all("trades" in v for v in en["result"]["plain_by_year"].values())
    assert client.post(f"/lab/v1/strategies/{sid}/ai-gate", json={"gate": 70}, headers=H).status_code == 200
    card = client.get(f"/lab/v1/strategies/{sid}", headers=H).json()
    assert card["enhanced"] and card["ai_gate"] == 70 and card["version"] == 2
    con = db.connect(cfg)
    assert service.update_live(cfg, con) >= 0


def test_live_tracker_and_min_trades_rule(client, cfg):
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Live one"}, headers={"X-Dev-Uid": "u3"}).json()["id"]
    con = db.connect(cfg)
    con.execute("UPDATE strategy_versions SET live_from='2026-01-01' WHERE strategy_id=?", (sid,))
    assert service.update_live(cfg, con) > 0
    t = client.get(f"/lab/v1/strategies/{sid}/trades?status=closed", headers={"X-Dev-Uid": "u3"}).json()
    assert t and all(x["is_open"] == 0 for x in t)
    pf = client.get("/lab/v1/me/portfolio?status=closed", headers={"X-Dev-Uid": "u3"}).json()
    assert pf["summary"]["closed"] > 0 and pf["trades"][0]["strategy_name"] == "Live one"
    stats = client.get(f"/lab/v1/strategies/{sid}", headers={"X-Dev-Uid": "u3"}).json()["stats"]
    assert stats["shown"] == (stats["trades"] >= 30)
    if not stats["shown"]:
        assert stats["win_rate"] is None


def test_queue_limits(cfg):
    con = db.connect(cfg)
    con.execute("DELETE FROM jobs")
    jobs.enqueue(con, "enhance", "qa", {})
    with pytest.raises(jobs.QueueError) as e:
        jobs.enqueue(con, "enhance", "qa", {})
    assert e.value.code == 409
    j = jobs.claim(con, "ai"); assert j
    con.execute("UPDATE jobs SET started_at=1 WHERE id=?", (j["id"],))
    assert jobs.recover_stuck(con) == 1
    assert jobs.claim(con, "ai")["attempts"] == 2


def test_handle_and_publish(client):
    h = {"X-Dev-Uid": "u9"}
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Shared"}, headers=h).json()["id"]
    assert client.post(f"/lab/v1/strategies/{sid}/visibility?public=true", headers=h).status_code == 400
    assert client.put("/lab/v1/me/handle?handle=Rahul_Q", headers=h).json()["handle"] == "rahul_q"
    assert client.put("/lab/v1/me/handle?handle=rahul_q", headers={"X-Dev-Uid": "u8"}).status_code == 409
    assert client.post(f"/lab/v1/strategies/{sid}/visibility?public=true", headers=h).status_code == 200
    assert client.get(f"/lab/v1/strategies/{sid}").json()["owner"] == "rahul_q"


def test_exit_enhancement(client, cfg):
    h = {"X-Dev-Uid": "u5"}
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Exit test"}, headers=h).json()["id"]
    client.post(f"/lab/v1/strategies/{sid}/enhance", headers=h)
    drain(cfg, "ai")
    assert client.post(f"/lab/v1/strategies/{sid}/enhance-exit", json={"gate": 50}, headers=h).status_code == 200
    drain(cfg, "ai")
    ex = client.get(f"/lab/v1/strategies/{sid}/enhancement", headers=h).json()["exit"]
    assert ex["tested"] >= 20 and "improved" in ex and ex["gate"] == 50


def test_strategy_backtest_cached(client, cfg):
    rule = {**RULE, "entry": {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 53}}]}}  # not shared with other tests' cache
    sid = client.post("/lab/v1/strategies", json={**rule, "name": "BT"}, headers=H).json()["id"]
    first = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=H).json()
    assert first["status"] == "running"  # queued automatically on save
    again = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=H).json()
    assert again["job_id"] == first["job_id"]  # no duplicate jobs
    assert client.post(f"/lab/v1/strategies/{sid}/backtest").status_code == 401
    drain(cfg, "fast")
    r = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=H).json()
    assert r["status"] == "done" and r["result"]["summary"]["All"]["trades"] > 50
    t = r["result"]["trades"]
    assert t and {"symbol", "entry_date", "exit_date", "ret_pct", "is_open"} <= set(t[0])
    assert t[0]["entry_date"] >= t[-1]["entry_date"]
    dd = r["result"]["summary"]["All"]["max_dd"]
    assert dd is None or -100 < dd <= 0


def test_stale_cache_is_recomputed(client, cfg):
    """A past test cached under an older metric definition is never shown; a fresh one is queued."""
    con = db.connect(cfg)
    rule = {**RULE, "entry": {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 57}}]}}
    sid = client.post("/lab/v1/strategies", json={**rule, "name": "Stale"}, headers=H).json()["id"]
    drain(cfg, "fast")
    con.execute("UPDATE backtest_runs SET as_of=replace(as_of,'|m','|old')")
    assert client.get(f"/lab/v1/strategies/{sid}/backtest", headers=H).json()["status"] == "running"
    drain(cfg, "fast")
    assert client.get(f"/lab/v1/strategies/{sid}/backtest", headers=H).json()["status"] == "done"


def test_list_survives_bad_record(client, cfg):
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Good"}, headers=H).json()["id"]
    con = db.connect(cfg)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("INSERT INTO strategies(id,owner_uid,kind,name,tf,visibility,current_version,created_at,updated_at) VALUES('stg_bad','u1','user','Bad','1D','private','ver_missing',0,0)")
    r = client.get("/lab/v1/strategies?scope=mine", headers=H)
    assert r.status_code == 200
    names = {c["name"] for c in r.json()}
    assert {"Good", "Bad"} <= names and sid in {c["id"] for c in r.json()}


def test_build_flagships(cfg):
    from lab import flagship
    con = db.connect(cfg)
    out = [flagship.build(cfg, con, n, ai=True, log=lambda *_: None) for n in flagship.FLAGSHIPS]
    assert [o["name"] for o in out] == ["AI Alpha", "AI Pattern"]
    assert all(o["past_test"] is not None for o in out)
    ids = {o["name"]: o["id"] for o in out}
    # idempotent: a re-run neither duplicates strategies nor restarts the version
    again = flagship.build(cfg, con, "AI Alpha", ai=False, log=lambda *_: None)
    assert again["id"] == ids["AI Alpha"]
    assert con.execute("SELECT count(*) FROM strategies WHERE kind='flagship' AND archived=0").fetchone()[0] == 2
    assert flagship.pick_gate({"edge_found": False}) is None
    assert flagship.pick_gate({"edge_found": True, "gates": {"60": {"All": {"trades": 80}, "lift": 0.05}}}) == 60
    assert flagship.pick_gate({"edge_found": True, "gates": {"60": {"All": {"trades": 12}, "lift": 0.05}, "50": {"All": {"trades": 60}, "lift": 0.02}}}) == 50


def test_unverifiable_token_does_not_break_public_pages(cfg):
    from dataclasses import replace
    c = replace(cfg, auth_mode="firebase", firebase_project_id="x")
    bad = {"Authorization": "Bearer not-a-real-token"}
    with TestClient(api.create_app(c)) as cl:
        for path in ("/lab/v1/today", "/lab/v1/strategies?scope=flagship", "/lab/v1/catalog"):
            assert cl.get(path, headers=bad).status_code == 200, path
        assert cl.post("/lab/v1/scan", json=RULE, headers=bad).status_code == 200
        assert cl.post("/lab/v1/backtest", json=RULE, headers=bad).status_code == 401   # needs a real user
        assert cl.get("/lab/v1/health").json()["auth"]["mode"] == "firebase"


def test_gated_past_test(client, cfg):
    """With an AI score set, the past test shows only the results that score would have let through."""
    rule = {**RULE, "entry": {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 52}}]}}
    h = {"X-Dev-Uid": "g1"}
    sid = client.post("/lab/v1/strategies", json={**rule, "name": "Gated"}, headers=h).json()["id"]
    drain(cfg, "fast")
    plain = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=h).json()["result"]
    client.post(f"/lab/v1/strategies/{sid}/enhance", headers=h)
    drain(cfg, "ai")
    assert client.post(f"/lab/v1/strategies/{sid}/ai-gate", json={"gate": 70}, headers=h).status_code == 200
    assert client.get(f"/lab/v1/strategies/{sid}/backtest", headers=h).json()["status"] == "running"
    drain(cfg, "ai")
    gated = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=h).json()
    assert gated["status"] == "done" and gated["result"]["ai_gate"] == 70
    assert gated["result"]["summary"]["All"]["trades"] < plain["summary"]["All"]["trades"]
    en = client.get(f"/lab/v1/strategies/{sid}/enhancement", headers=h).json()
    assert gated["result"]["summary"]["All"]["trades"] == en["result"]["gates"]["70"]["All"]["trades"]  # same numbers as the table


ENGINE = {"fetched_at": 0, "live_since": "2026-09-08",
          "closed": [{"engine": "v2", "symbol": f"S{i}", "strategy": "Leader", "entry_date": f"2025-0{1 + i % 9}-10",
                      "exit_date": f"2025-0{1 + i % 9}-20", "entry_price": 100, "exit_price": 100 + (5 if i % 3 else -4),
                      "ret_pct": 5.0 if i % 3 else -4.0, "exit_reason": "target" if i % 3 else "stop", "bars": 10,
                      "origin": "tested"} for i in range(40)]
                    + [{"engine": "v2", "symbol": "LIVE1", "strategy": "Breakout", "entry_date": "2026-09-10", "exit_date": "2026-10-01",
                        "entry_price": 50, "exit_price": 56, "ret_pct": 12.0, "exit_reason": "target", "bars": 15, "origin": "live"},
                       {"engine": "v3", "symbol": "PAT1", "strategy": "HTF", "entry_date": "2026-09-01", "exit_date": "2026-09-05",
                        "entry_price": 10, "exit_price": 9.2, "ret_pct": -8.0, "exit_reason": "stop", "bars": 4, "origin": "live"}],
          "active": [{"engine": "v2", "symbol": "ACT1", "strategy": "Leader", "entry_date": "2026-10-01", "entry_price": 20, "price": 21, "ret_pct": 5.0}]}


def test_flagships_use_engine_record(client, cfg, monkeypatch):
    from lab import engine_feed
    monkeypatch.setattr(engine_feed, "_fetch", lambda c: ENGINE)
    engine_feed._cache.update(t=0, data=None)
    con = db.connect(cfg)
    for name in ("AI Alpha", "AI Pattern"):
        if not con.execute("SELECT 1 FROM strategies WHERE kind='flagship' AND name=?", (name,)).fetchone():
            service.create_strategy(con, cfg, None, name, "1D", RULE["entry"], RULE["exit"], kind="flagship", visibility="public")
    cards = {c["name"]: c for c in client.get("/lab/v1/strategies?scope=flagship").json()}
    a = cards["AI Alpha"]
    assert a["source"] == "engine" and a["stats"]["open"] == 1 and a["stats"]["trades"] == 1 and a["stats"]["wins"] == 1
    assert cards["AI Pattern"]["stats"]["losses"] == 1 and cards["AI Pattern"]["stats"]["open"] == 0
    act = client.get(f"/lab/v1/strategies/{a['id']}/trades?status=active").json()
    assert [t["symbol"] for t in act] == ["ACT1"] and act[0]["is_open"] == 1
    closed = client.get(f"/lab/v1/strategies/{a['id']}/trades?status=closed").json()
    assert len(closed) == 41 and closed[0]["symbol"] == "LIVE1" and closed[0]["origin"] == "live"
    bt = client.get(f"/lab/v1/strategies/{a['id']}/backtest").json()["result"]
    assert bt["source"] == "engine" and bt["summary"]["All"]["trades"] == 41 and bt["summary"]["Live"]["trades"] == 1
    assert bt["years"] == [2025, 2026] and len(bt["trades"]) == 41
    today = client.get("/lab/v1/today").json()
    assert any(f["symbol"] == "ACT1" for f in today["fresh"])
    # engine down: the saved copy keeps the pages working
    engine_feed._cache.update(t=0, data=None)
    monkeypatch.setattr(engine_feed, "_fetch", lambda c: (_ for _ in ()).throw(OSError("down")))
    assert client.get(f"/lab/v1/strategies/{a['id']}/backtest").json()["result"]["summary"]["All"]["trades"] == 41
    engine_feed._cache.update(t=0, data=None)


def test_cards_carry_full_record(client, cfg):
    h = {"X-Dev-Uid": "rec1"}
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Rec"}, headers=h).json()["id"]
    drain(cfg, "fast")
    card = client.get(f"/lab/v1/strategies/{sid}", headers=h).json()
    bt = client.get(f"/lab/v1/strategies/{sid}/backtest", headers=h).json()["result"]["summary"]["All"]
    r = card["record"]
    assert r["trades"] == bt["trades"] > 50 and r["wins"] + r["losses"] == r["trades"] and r["win_rate"] == bt["win_rate"]
    assert [c for c in client.get("/lab/v1/strategies?scope=mine", headers=h).json() if c["id"] == sid][0]["record"]["trades"] == r["trades"]


def test_alerts_and_matches(client, cfg, monkeypatch):
    from lab import engine_feed
    today = pd.Timestamp.today().normalize()
    d = lambda n: (today - pd.Timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    eng = {**ENGINE, "active": [{"engine": "v2", "symbol": "NEWQ", "strategy": "Leader", "entry_date": d(1), "entry_price": 20, "price": 21, "ret_pct": 5.0},
                                {"engine": "v2", "symbol": "OLDQ", "strategy": "Leader", "entry_date": d(9), "entry_price": 20, "price": 19, "ret_pct": -5.0}]}
    monkeypatch.setattr(engine_feed, "_fetch", lambda c: eng)
    engine_feed._cache.update(t=0, data=None)
    con = db.connect(cfg)
    if not con.execute("SELECT 1 FROM strategies WHERE kind='flagship' AND name='AI Alpha'").fetchone():
        service.create_strategy(con, cfg, None, "AI Alpha", "1D", RULE["entry"], RULE["exit"], kind="flagship", visibility="public")
    H1, H2 = {"X-Dev-Uid": "al1"}, {"X-Dev-Uid": "al2"}
    flag = [x for x in client.get("/lab/v1/alerts").json()["items"] if x["cat"] == "q"]
    assert flag and all(x["on"] for x in flag)  # signed out: QuantSync listed and on
    m = client.get("/lab/v1/matches").json()
    assert {r["symbol"]: r["latest"] for r in m["rows"] if r["strategy"] == "AI Alpha"} == {"NEWQ": True, "OLDQ": False}
    assert m["counts"]["latest"] >= 1 and m["counts"]["older"] >= 1 and m["rows"][0]["date"] >= m["rows"][-1]["date"]
    # a member's own strategy is switched on when saved; unticking removes it from the list
    sid = client.post("/lab/v1/strategies", json={**RULE, "name": "Mine A"}, headers=H1).json()["id"]
    items = client.get("/lab/v1/alerts", headers=H1).json()["items"]
    assert [x["cat"] for x in items if x["id"] == sid] == ["y"]
    assert client.get(f"/lab/v1/strategies/{sid}", headers=H1).json()["followers"] == 0  # owner is not their own follower
    con.execute("INSERT INTO live_trades(version_id,symbol,signal_date,entry_date,entry_price,exit_price,ret_pct,is_open) "
                "SELECT current_version,'MYSYM',?,?,100,103,3.0,1 FROM strategies WHERE id=?", (d(0), d(0), sid))
    rows = client.get("/lab/v1/matches", headers=H1).json()["rows"]
    assert any(r["symbol"] == "MYSYM" and r["cat"] == "y" and r["latest"] for r in rows)
    assert not any(r["symbol"] == "MYSYM" for r in client.get("/lab/v1/matches", headers=H2).json()["rows"])  # someone else's: not shown
    assert client.post(f"/lab/v1/strategies/{sid}/follow?on=false", headers=H1).status_code == 200
    assert sid not in [x["id"] for x in client.get("/lab/v1/alerts", headers=H1).json()["items"]]
    assert not any(r["symbol"] == "MYSYM" for r in client.get("/lab/v1/matches", headers=H1).json()["rows"])
    # QuantSync stays listed when switched off, and its matches go away until it is switched back on
    qid = flag[0]["id"]
    assert client.post(f"/lab/v1/strategies/{qid}/follow?on=false", headers=H1).status_code == 200
    off = {x["id"]: x for x in client.get("/lab/v1/alerts", headers=H1).json()["items"]}
    assert off[qid]["on"] is False
    assert not any(r["sid"] == qid for r in client.get("/lab/v1/matches", headers=H1).json()["rows"])
    assert client.post(f"/lab/v1/strategies/{qid}/follow?on=true&alerts=true", headers=H1).status_code == 200
    assert {x["id"]: x["on"] for x in client.get("/lab/v1/alerts", headers=H1).json()["items"]}[qid] is True
    # a public strategy followed with alerts shows as Followed, and disappears when removed
    assert client.put("/lab/v1/me/handle?handle=alerter", headers=H1).status_code == 200
    assert client.post(f"/lab/v1/strategies/{sid}/visibility?public=true", headers=H1).status_code == 200
    assert client.post(f"/lab/v1/strategies/{sid}/follow?on=true&alerts=true", headers=H2).status_code == 200
    assert [x["cat"] for x in client.get("/lab/v1/alerts", headers=H2).json()["items"] if x["id"] == sid] == ["f"]
    assert client.get(f"/lab/v1/strategies/{sid}", headers=H1).json()["followers"] == 1
    engine_feed._cache.update(t=0, data=None)
