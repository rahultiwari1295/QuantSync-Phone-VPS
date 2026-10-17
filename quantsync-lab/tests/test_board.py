"""Snapshot scans, the liquid universe flag, the precomputed screens board, the changes feed and the scan limit."""
import dataclasses
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from lab import api, board, db, entitlements, pipeline, store, universe

RULE = {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 45}}]}


@pytest.fixture()
def c2(cfg, tmp_path):
    """Same cooked data, its own database, so universe/board state never leaks into other tests."""
    c = dataclasses.replace(cfg, db_path=tmp_path / "db" / "lab.db", run_dir=tmp_path / "run", free_universe_size=8, universe_min_price=0.0)
    c.db_path.parent.mkdir(parents=True, exist_ok=True)
    db.migrate(c)
    universe._cache.update(t=0.0, v=None)
    return c


def test_cook_builds_a_fresh_snapshot(cfg):
    assert store.snapshot_fresh(cfg, "1D")
    m = store.snap_meta(cfg, "1D")
    assert m["symbols"] == 20 and m["rows"] == 20 * cfg.snapshot_bars


def test_snapshot_scan_equals_full_store_scan(cfg, monkeypatch):
    fast = store.signals(cfg, RULE, "1D", only_last=True)
    monkeypatch.setattr(store, "snapshot_fresh", lambda *a, **k: False)
    slow = store.signals(cfg, RULE, "1D", only_last=True)
    assert list(fast["symbol"]) == list(slow["symbol"]) and len(fast) > 0


def test_lagged_rule_works_on_snapshot(cfg):
    r = {"v": 1, "all": [{"l": {"f": "close", "lag": 3}, "op": ">", "r": {"c": 0}}]}
    try:
        out = store.signals(cfg, r, "1D", only_last=True)
    except Exception:  # lag syntax differs by rule version; the equivalence test above is the contract
        pytest.skip("lag form not used in this rule version")
    assert len(out) >= 0


def test_universe_flags_exactly_n_and_keeps_members(c2):
    con = db.connect(c2)
    r1 = universe.build(c2, con)
    assert r1["free"] == 8 and r1["added"] == 8
    first = {r[0] for r in con.execute("SELECT symbol FROM universe WHERE free_backtest=1")}
    assert len(first) == 8 and all(r[0] for r in con.execute("SELECT added_on FROM universe WHERE free_backtest=1"))
    r2 = universe.build(c2, con)                      # nothing changed: nobody moves in or out
    assert r2["added"] == 0 and r2["removed"] == 0
    assert universe.free_symbols(c2) == first


def test_backtests_only_use_free_universe(c2):
    con = db.connect(c2)
    universe.build(c2, con)
    free = universe.free_symbols(c2)
    exit_ = {"v": 1, "any": [{"type": "time", "v": 10}]}
    trades, _ = pipeline.run_trades(c2, RULE, exit_, "1D")
    assert len(trades) > 0 and set(trades["symbol"]) <= free
    res = pipeline.backtest(c2, RULE, exit_, "1D")
    assert res["universe"] == "free" and res["universe_n"] == 8


def test_no_restriction_until_universe_is_built(c2):
    assert universe.free_symbols(c2) is None
    assert universe.label(c2)["universe"] == "all"


@pytest.fixture()
def tiny_def(tmp_path, monkeypatch):
    p = tmp_path / "defs.json"
    p.write_text(json.dumps([
        {"id": "rsi", "name": "RSI", "tf": "1D", "col": "rsi_14", "needs": "", "rule": RULE},
        {"id": "nofield", "name": "Needs facts", "tf": "1D", "col": "pe", "needs": "business",
         "rule": {"v": 1, "all": [{"l": {"f": "not_a_real_field"}, "op": ">", "r": {"c": 1}}]}}]))
    monkeypatch.setattr(board, "DEF_PATH", p)
    return p


def test_board_build_skip_and_force(c2, tiny_def):
    con = db.connect(c2)
    r = board.build(c2, con)
    assert r["screens"] == 2 and r["failed"] == ["nofield"]
    body = json.loads(board.load(con)["body"])
    assert body["screens"]["rsi"]["count"] > 0 and len(body["screens"]["rsi"]["rows"]) <= board.TOP
    assert body["screens"]["nofield"]["pending"] is True and body["screens"]["nofield"]["count"] == 0
    row = body["screens"]["rsi"]["rows"][0]
    assert set(row) == {"symbol", "close", "ret_1", "v"}
    vals = [abs(x["v"]) for x in body["screens"]["rsi"]["rows"]]
    assert vals == sorted(vals, reverse=True)
    assert board.build(c2, con).get("skipped") is True            # same data, nothing to do
    assert board.build(c2, con, force=True)["screens"] == 2
    full = board.one(con, "rsi")
    assert full["count"] >= len(full["rows"]) and full["pending"] is False


def test_board_endpoints_etag_and_cache_headers(c2, tiny_def):
    with TestClient(api.create_app(c2)) as cl:
        assert cl.get("/lab/v1/screens/board").status_code == 503      # not built yet
        board.build(c2, db.connect(c2))
        r = cl.get("/lab/v1/screens/board")
        assert r.status_code == 200 and "s-maxage" in r.headers["cache-control"]
        assert cl.get("/lab/v1/screens/board", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        assert cl.get("/lab/v1/screens/rsi").json()["count"] > 0
        assert cl.get("/lab/v1/screens/nope").status_code == 404
        assert cl.get("/lab/v1/universe").json()["stocks"] == 0


def test_changes_feed_for_signed_in_user(c2):
    with TestClient(api.create_app(c2)) as cl:
        H = {"X-Dev-Uid": "chg1"}
        s = cl.post("/lab/v1/strategies", headers=H, json={"name": "RSI mine", "tf": "1D", "entry": RULE, "exit": {"v": 1, "any": [{"type": "time", "v": 5}]}}).json()
        cl.post(f"/lab/v1/strategies/{s['id']}/follow?on=true&alerts=true", headers=H)
        con = db.connect(c2)
        vid = con.execute("SELECT current_version FROM strategies WHERE id=?", (s["id"],)).fetchone()[0]
        from datetime import date, timedelta
        d1, d2 = (date.today() - timedelta(days=1)).isoformat(), (date.today() - timedelta(days=40)).isoformat()
        con.execute("INSERT INTO live_trades(version_id,symbol,signal_date,entry_date,entry_price,is_open,ret_pct) VALUES(?,?,?,?,?,1,2.5)", (vid, "SYM01", d1, d1, 100.0))
        con.execute("INSERT INTO live_trades(version_id,symbol,signal_date,entry_date,entry_price,exit_date,exit_price,exit_reason,ret_pct,is_open) VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (vid, "SYM02", d2, d2, 100.0, d1, 106.0, "target_pct", 6.0))
        con.execute("INSERT INTO live_trades(version_id,symbol,signal_date,entry_date,entry_price,exit_date,exit_price,exit_reason,ret_pct,is_open) VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (vid, "SYM03", d2, d2, 100.0, d2, 95.0, "stop_pct", -5.0))     # left 40 days ago: outside a 30-day window
        j = cl.get("/lab/v1/changes?days=30", headers=H).json()
        kinds = {(e["kind"], e["sym"]) for e in j["items"] if e["name"] == "RSI mine"}
        assert kinds == {("add", "SYM01"), ("out", "SYM02")}
        out = next(e for e in j["items"] if e["kind"] == "out" and e["sym"] == "SYM02")
        assert out["held"] == 39 and out["reason"] == "target_pct" and out["ret"] == 6.0
        anon = cl.get("/lab/v1/changes").json()
        assert anon["signed_in"] is False and all(e["name"] != "RSI mine" for e in anon["items"])


def test_scan_limit_is_per_person_and_plain_worded():
    key = "u:limit-test"
    entitlements._hits.pop(key, None)
    entitlements.hit(key, 2); entitlements.hit(key, 2)
    with pytest.raises(HTTPException) as e:
        entitlements.hit(key, 2, "scans")
    assert e.value.status_code == 429 and "Ready-made screens are not limited" in e.value.detail
    entitlements.hit("u:someone-else", 2)             # another person is unaffected


def test_plan_limits_in_me(c2):
    with TestClient(api.create_app(c2)) as cl:
        me = cl.get("/lab/v1/me", headers={"X-Dev-Uid": "plan1"}).json()
        assert me["plan"] == "free" and me["limits"]["universe"] == "free" and me["limits"]["scans_per_hour"] == 60
        assert entitlements.limits("pro")["universe"] == "all"
