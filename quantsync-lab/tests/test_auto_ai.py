"""Unit tests for Phase 4: Automatic AI Check for QuantSync Strategies."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pandas as pd
import pytest

from lab import ai, cli, db, jobs, service, worker
from lab.config import Config

RULE = {
    "tf": "1D",
    "entry": {"v": 1, "all": [{"l": {"f": "rsi_14"}, "op": ">", "r": {"c": 50}}]},
    "exit": {"v": 1, "any": [{"type": "stop_pct", "v": 5}, {"type": "target_pct", "v": 10}, {"type": "time", "v": 15}]},
}


def drain(cfg, lane="ai"):
    con = db.connect(cfg)
    while worker.run_one(cfg, con, lane):
        pass


def test_db_migration_idempotent(tmp_path):
    """Test that db.migrate adds new columns and safely rebuilds live_trades with variant."""
    db_file = tmp_path / "test.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
    CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE ai_enhancements(
      id TEXT PRIMARY KEY, version_id TEXT NOT NULL, status TEXT NOT NULL,
      candidate TEXT, model_path TEXT, result_json TEXT, exit_result_json TEXT, reason TEXT,
      created_at REAL NOT NULL, finished_at REAL);
    CREATE TABLE live_trades(
      id INTEGER PRIMARY KEY AUTOINCREMENT, version_id TEXT NOT NULL, symbol TEXT NOT NULL,
      signal_date TEXT NOT NULL, entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
      exit_reason TEXT, ret_pct REAL, is_open INTEGER NOT NULL DEFAULT 1, score REAL,
      UNIQUE(version_id, symbol, signal_date));
    """)
    # Insert dummy old rows
    con.execute("INSERT INTO ai_enhancements(id, version_id, status, created_at) VALUES('e1', 'v1', 'done', 1000.0)")
    con.execute("INSERT INTO live_trades(version_id, symbol, signal_date) VALUES('v1', 'SYM', '2025-01-01')")
    con.commit()
    con.close()

    c = Config(db_path=db_file)
    # 1. Run migration
    db.migrate(c)

    con = db.connect(c)
    enh_cols = {r["name"] for r in con.execute("PRAGMA table_info(ai_enhancements)").fetchall()}
    assert {"kind", "protocol", "verdict", "as_of"}.issubset(enh_cols)
    row = con.execute("SELECT kind FROM ai_enhancements WHERE id='e1'").fetchone()
    assert row["kind"] == "user"

    lt_cols = {r["name"] for r in con.execute("PRAGMA table_info(live_trades)").fetchall()}
    assert "variant" in lt_cols
    lt_row = con.execute("SELECT variant FROM live_trades WHERE version_id='v1'").fetchone()
    assert lt_row["variant"] == "plain"

    # Verify UNIQUE(version_id, variant, symbol, signal_date) allows same symbol on same date for different variants
    con.execute("INSERT INTO live_trades(version_id, variant, symbol, signal_date) VALUES('v1', 'ai', 'SYM', '2025-01-01')")

    # 2. Run migration again (must be idempotent)
    db.migrate(c)
    assert con.execute("SELECT count(*) FROM live_trades").fetchone()[0] == 2
    con.close()


def test_cli_ai_check_stale_and_queuing(cfg):
    """Test that ai-check --stale queues once and not again for the same month."""
    db.migrate(cfg)
    con = db.connect(cfg)
    # Create a flagship strategy
    res = service.create_strategy(con, cfg, None, "Flagship Test", "1D", RULE["entry"], RULE["exit"], kind="system", visibility="public")
    sid, vid = res["id"], res["version_id"]

    # First run of ai-check: should queue 1 job for the new strategy
    cli.main(["ai-check", "--stale", "--name", "Flagship Test"], cfg=cfg)
    job_row = con.execute("SELECT id, status FROM jobs WHERE kind='ai_check' AND json_extract(payload_json, '$.version_id')=?", (vid,)).fetchone()
    assert job_row is not None
    assert job_row["status"] == "queued"

    # Second run immediately: should not queue a duplicate while queued/running
    cli.main(["ai-check", "--stale", "--name", "Flagship Test"], cfg=cfg)
    q_count = con.execute("SELECT count(*) FROM jobs WHERE kind='ai_check' AND json_extract(payload_json, '$.version_id')=?", (vid,)).fetchone()[0]
    assert q_count == 1

    # Drain worker so job completes
    drain(cfg, "ai")

    enh = con.execute("SELECT status, kind, protocol FROM ai_enhancements WHERE version_id=? AND kind='system'", (vid,)).fetchone()
    assert enh is not None
    assert enh["status"] in ("done", "insufficient")
    assert enh["kind"] == "system"

    # Third run after completion: not stale, so 0 jobs queued
    cli.main(["ai-check", "--stale", "--name", "Flagship Test"], cfg=cfg)
    assert con.execute("SELECT count(*) FROM jobs WHERE kind='ai_check' AND json_extract(payload_json, '$.version_id')=?", (vid,)).fetchone()[0] == 1


def test_paused_live_condition(cfg):
    """If AI variant has >= 20 live trades and underperforms plain rules over last 6 months, verdict flips to paused_live."""
    db.migrate(cfg)
    con = db.connect(cfg)
    res = service.create_strategy(con, cfg, None, "Live Recheck Test", "1D", RULE["entry"], RULE["exit"], kind="system", visibility="public")
    sid, vid = res["id"], res["version_id"]

    # Insert 20 closed live trades for AI variant with negative return
    # and 20 closed live trades for plain variant with positive return
    recent_date = str((pd.Timestamp.now() - pd.DateOffset(days=10)).date())
    with db.tx(con):
        for i in range(25):
            con.execute("""INSERT INTO live_trades(version_id, variant, symbol, signal_date, entry_date, exit_date, ret_pct, is_open)
                           VALUES(?,?,?,?,?,?,?,0)""",
                        (vid, "ai", f"S_{i}", f"2026-01-{i+1:02d}", f"2026-01-{i+1:02d}", recent_date, -3.5))
            con.execute("""INSERT INTO live_trades(version_id, variant, symbol, signal_date, entry_date, exit_date, ret_pct, is_open)
                           VALUES(?,?,?,?,?,?,?,0)""",
                        (vid, "plain", f"S_{i}", f"2026-01-{i+1:02d}", f"2026-01-{i+1:02d}", recent_date, 2.5))

    # Run _ai_check
    payload = {"strategy_id": sid, "version_id": vid}
    result = service._ai_check(cfg, con, payload, lambda p, s: None)

    enh = con.execute("SELECT verdict, reason FROM ai_enhancements WHERE version_id=? AND kind='system' ORDER BY created_at DESC LIMIT 1", (vid,)).fetchone()
    assert enh["verdict"] == "paused_live"
    assert "stopped beating the plain rules" in enh["reason"]


def test_update_live_plain_and_ai_variants(cfg):
    """Test that update_live records both plain and ai variant trades when AI is improved."""
    db.migrate(cfg)
    con = db.connect(cfg)
    res = service.create_strategy(con, cfg, None, "Dual Live Test", "1D", RULE["entry"], RULE["exit"], kind="system", visibility="public")
    sid, vid = res["id"], res["version_id"]

    # Set live_from to start of 2026
    con.execute("UPDATE strategy_versions SET live_from='2026-01-01' WHERE id=?", (vid,))

    # Create dummy model
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    clf = LogisticRegression()
    clf.fit(np.zeros((10, len(ai.AI_FEATURES_25))), np.array([0, 1] * 5))
    scorer = ai.Scorer(clf, np.linspace(0, 1, 101), cols=ai.AI_FEATURES_25, model_name="logreg")
    model_path = cfg.models_dir / f"test_sys_{vid}.pkl"
    scorer.save(model_path)

    # Insert system enhancement marked 'improved'
    aid = db.new_id("enh")
    con.execute("""INSERT INTO ai_enhancements(id, version_id, status, kind, protocol, verdict, model_path, result_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (aid, vid, "done", "system", "v2", "improved", str(model_path), json.dumps({"gate": 0}), time.time()))

    # Run update_live
    n = service.update_live(cfg, con)
    assert n > 0

    variants = {r["variant"] for r in con.execute("SELECT DISTINCT variant FROM live_trades WHERE version_id=?", (vid,)).fetchall()}
    assert "plain" in variants
    assert "ai" in variants

    s_plain = service.live_stats(con, vid, "plain")
    s_ai = service.live_stats(con, vid, "ai")
    assert isinstance(s_plain, dict) and "trades" in s_plain
    assert isinstance(s_ai, dict) and "trades" in s_ai


def test_phase5_api_endpoints(cfg):
    """Verify Phase 5 API: /enhancement schema, /strategies card AI block, and /backtest?variant=ai|plain."""
    from fastapi.testclient import TestClient
    from lab import api
    client = TestClient(api.create_app(cfg))
    con = db.connect(cfg)
    res = service.create_strategy(con, cfg, None, "API Phase 5 Test", "1D", RULE["entry"], RULE["exit"], kind="system", visibility="public")
    sid, vid = res["id"], res["version_id"]

    res_json = {
        "verdict": "improved",
        "gate": 60,
        "valid": {"plain": {"trades": 50, "win_rate": 52.0, "avg_ret": 1.5}, "ai": {"trades": 35, "win_rate": 62.0, "avg_ret": 2.8}},
        "blind": {"plain": {"trades": 20, "win_rate": 50.0, "avg_ret": 1.2}, "ai": {"trades": 15, "win_rate": 66.0, "avg_ret": 3.1}},
        "checked_at": 1760000000,
    }
    con.execute("""INSERT INTO ai_enhancements(id, version_id, status, kind, protocol, verdict, as_of, result_json, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ("enh_p5", vid, "done", "system", "v2", "improved", "2026-04-01", json.dumps(res_json), time.time()))

    # 1. Test GET /strategies/{sid}/enhancement
    r_enh = client.get(f"/lab/v1/strategies/{sid}/enhancement").json()
    assert r_enh["status"] == "done"
    assert r_enh["kind"] == "system"
    assert r_enh["protocol"] == "v2"
    assert r_enh["verdict"] == "improved"
    assert r_enh["as_of"] == "2026-04-01"
    assert r_enh["result"]["gate"] == 60

    # 2. Test card in GET /strategies/{sid}
    r_strat = client.get(f"/lab/v1/strategies/{sid}").json()
    assert "ai" in r_strat
    ai_b = r_strat["ai"]
    assert ai_b["status"] == "done"
    assert ai_b["verdict"] == "improved"
    assert ai_b["default"] == "ai"
    assert ai_b["gate"] == 60
    assert ai_b["enhanced"]["win_rate"] == 62.0

    # 3. Test backtest with variant=plain and variant=ai
    r_bt_plain = client.get(f"/lab/v1/strategies/{sid}/backtest?variant=plain").json()
    assert r_bt_plain["status"] in ("running", "done")
    r_bt_ai = client.get(f"/lab/v1/strategies/{sid}/backtest?variant=ai").json()
    assert r_bt_ai["status"] in ("running", "done")


