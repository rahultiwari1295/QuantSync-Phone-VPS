import datetime as dt
import os
import re
import sqlite3
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ops_system as ops
import ops_track
from collectors.admin import auth
from collectors.core import db as cdb
from tests.test_collectors_admin import PEM, token

IST = ops.IST
ROOT = os.path.dirname(os.path.dirname(__file__))


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=IST)


# ── cron maths ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("expr,after,want", [
    ("30 9-15 * * 1-5", at(2026, 10, 21, 21, 30), at(2026, 10, 22, 9, 30)),          # Wed night -> Thu morning
    ("*/5 8-20 * * 1-5", at(2026, 10, 23, 20, 55), at(2026, 10, 26, 8, 0)),           # Fri evening -> Monday
    ("30 7 1 * *", at(2026, 10, 21, 7, 0), at(2026, 11, 1, 7, 30)),                   # monthly
    ("15 3 * * 0", at(2026, 10, 21, 7, 0), at(2026, 10, 25, 3, 15)),                  # Sunday
    ("0 6 * * 6", at(2026, 10, 24, 6, 0), at(2026, 10, 31, 6, 0)),                    # strictly after
    ("0 0 13 * 5", at(2026, 10, 1, 0, 0), at(2026, 10, 2, 0, 0)),                     # day-of-month OR weekday when both are set
    ("*/15 * * * *", at(2026, 10, 21, 10, 7), at(2026, 10, 21, 10, 15)),
    ("@daily", at(2026, 10, 21, 10, 7), at(2026, 10, 22, 0, 0)),
])
def test_cron_next(expr, after, want):
    assert ops.cron_next(expr, after) == want


def test_reboot_has_no_next_and_bad_cron_is_rejected():
    assert ops.cron_next("@reboot") is None
    with pytest.raises(ValueError):
        ops.cron_parse("* * *")


@pytest.mark.parametrize("expr,text", [
    ("*/5 8-20 * * 1-5", "every 5 min, 8–20h · Mon–Fri"),
    ("30 9-15 * * 1-5", "hourly at :30, 9–15h · Mon–Fri"),
    ("55 15 * * 1-5", "15:55 · Mon–Fri"),
    ("0 6 * * 6", "06:00 · Sat"),
    ("30 7 1 * *", "07:30 · day 1 of the month"),
    ("*/15 * * * *", "every 15 min"),
    ("@reboot", "at every reboot"),
])
def test_humanize(expr, text):
    assert ops.humanize(expr) == text


def test_repo_crontab_parses_and_is_labelled(monkeypatch):
    txt = open(os.path.join(ROOT, "crontab.txt")).read()
    monkeypatch.setattr(ops, "_run", lambda cmd, timeout=10: (0, txt))
    ops._cron_cache.update(t=0.0, v=[])
    jobs = ops.cron_jobs()
    live = [j for j in jobs if not j["paused"]]
    assert len(live) >= 20 and all(j["next"] or j["expr"].startswith("@reboot") for j in live)
    labels = {j["label"] for j in jobs}
    assert "End-of-day pipeline" in labels and "News · filings" in labels and any(l.startswith("Lab · board") for l in labels)
    assert any(j["paused"] for j in jobs)                                   # commented-out job lines are shown as paused
    assert next(j for j in live if j["label"] == "End-of-day pipeline")["run"] == "sched-eod"
    assert next(j for j in live if j["label"] == "News · filings")["run"] == "intel-filings"
    assert not any(j["label"].startswith("Lab · ai-check") and j["run"] is None for j in live)


def test_scheduler_timetable_matches_scheduler_py():
    src = open(os.path.join(ROOT, "scheduler.py")).read()
    for j in ops.SCHED_JOBS:
        assert f'_t("{j["name"]}"' in src, j["name"]                         # every job shown on the page is really registered
    registered = set(re.findall(r'_t\("(job_\w+)"', src))
    assert registered == {j["name"] for j in ops.SCHED_JOBS}                # and nothing registered is missing from the page
    assert len(ops._ltp_slots()) == 75 - 0 or len(ops._ltp_slots()) > 70


def test_trading_jobs_cannot_be_run_from_the_page():
    forbidden = ("decision", "finalize", "ltp", "order", "exit")
    for key, spec in ops.RUNNABLE.items():
        joined = " ".join(spec["cmd"]).lower()
        assert not any(f in joined and "freshness" not in joined for f in ("--now decision", "--now finalize", "--now ltp")), key
        assert not any(f in key for f in forbidden), key


# ── owner-only access ───────────────────────────────────────────────────────────────────────
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cdb, "COLLECTOR_DB", str(tmp_path / "c.db"))
    monkeypatch.setattr(cdb, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "get_certs", lambda force=False: {"k1": PEM})
    monkeypatch.setattr(ops, "METRICS_DB", str(tmp_path / "m.db"))
    monkeypatch.setattr(ops_track, "RUNS_DB", str(tmp_path / "runs.db"))
    monkeypatch.setattr(ops, "LOGS", str(tmp_path / "logs"))
    monkeypatch.setattr(ops, "SCHED_LOGS", str(tmp_path / "Data" / "logs"))
    os.makedirs(ops.LOGS); os.makedirs(ops.SCHED_LOGS)
    monkeypatch.setattr(ops, "_run", lambda cmd, timeout=10: (1, ""))      # no systemctl / crontab in the test box
    auth._fails.clear()
    cdb.init().close()
    app = FastAPI(); app.include_router(ops.router)
    return TestClient(app)


def H(**kw):
    return {"Authorization": "Bearer " + token(**kw)}


@pytest.mark.parametrize("path", ["/me", "/live", "/overview", "/schedule", "/data", "/trading", "/metrics", "/logs", "/logs/read?name=x.log"])
def test_everyone_but_the_owner_gets_404(client, path):
    base = "/api/admin/sys"
    assert client.get(base + path).status_code == 404                                   # no token
    assert client.get(base + path, headers=H(email="someone@else.com")).status_code == 404
    assert client.get(base + path, headers=H(verified=False)).status_code == 404
    assert client.get(base + path, headers={"Authorization": "Bearer junk"}).status_code == 404
    assert client.post(base + "/run/lab-board", headers=H(email="x@y.com")).status_code == 404
    assert client.post(base + "/service/api/restart", headers=H(email="x@y.com")).status_code == 404


def test_owner_gets_in(client):
    r = client.get("/api/admin/sys/me", headers=H())
    assert r.status_code == 200 and r.json()["email"] == "rahultiwari1295@gmail.com"


def test_owner_endpoints_work_on_an_empty_box(client):
    for p in ("/live", "/overview", "/schedule", "/data", "/trading", "/metrics?range=1h", "/logs"):
        r = client.get("/api/admin/sys" + p, headers=H())
        assert r.status_code == 200, (p, r.text[:200])
    d = client.get("/api/admin/sys/data", headers=H()).json()
    assert d["items"] and all(i["status"] in ("ok", "stale", "unknown") for i in d["items"])     # a missing database never crashes the page


# ── services, runs, logs: only what is on the allow-lists ────────────────────────────────────
def test_service_actions_are_allow_listed(client):
    base = "/api/admin/sys/service/"
    assert client.post(base + "api/stop", headers=H()).status_code == 400            # would lock you out
    assert client.post(base + "ssh/restart", headers=H()).status_code == 400
    assert client.post(base + "nginx/stop", headers=H()).status_code == 400
    assert client.post(base + "nothing/restart", headers=H()).status_code == 400
    assert client.post(base + "..%2Fetc/restart", headers=H()).status_code in (400, 404)


def test_scheduler_restart_needs_confirm_while_market_is_open(client, monkeypatch):
    monkeypatch.setattr(ops, "market_open", lambda now=None: True)
    r = client.post("/api/admin/sys/service/scheduler/restart", headers=H()).json()
    assert r["ok"] is False and r["needsConfirm"] is True


def test_run_unknown_job_is_rejected(client):
    assert client.post("/api/admin/sys/run/rm-rf", headers=H()).status_code == 400
    assert client.post("/api/admin/sys/run/sched-decision", headers=H()).status_code == 400


def test_run_refuses_heavy_job_when_ram_is_low(client, monkeypatch):
    monkeypatch.setattr(ops, "_meminfo", lambda: {"MemAvailable": 300.0})
    monkeypatch.setattr(ops, "market_open", lambda now=None: False)
    r = client.post("/api/admin/sys/run/lab-cook", headers=H()).json()
    assert r["ok"] is False and "RAM" in r["error"]


def test_run_starts_a_job_and_records_it(client, monkeypatch):
    monkeypatch.setitem(ops.RUNNABLE, "t-echo", {"label": "echo", "cwd": ROOT, "cmd": [sys.executable, "-c", "print('hello')"], "env": {}})
    r = client.post("/api/admin/sys/run/t-echo", headers=H()).json()
    assert r["ok"] and r["log"] == "ops_manual_t-echo.log"
    for _ in range(50):
        h = ops.runs_history()
        if h and h[0]["end"]:
            break
        time.sleep(0.1)
    assert h[0]["job"] == "t-echo" and h[0]["ok"] is True and h[0]["source"] == "manual"
    log = client.get("/api/admin/sys/logs/read?name=ops_manual_t-echo.log", headers=H()).json()
    assert any("hello" in l for l in log["lines"])


def test_logs_cannot_escape_the_log_folders(client, tmp_path):
    (tmp_path / "secret.log").write_text("nope")
    for name in ("../secret.log", "..%2Fsecret.log", "/etc/passwd", "a/b.log", "x.txt", "journal:sudo", "journal:../x"):
        r = client.get("/api/admin/sys/logs/read", params={"name": name}, headers=H())
        assert r.status_code == 404, name


def test_log_lines_hide_tokens(client):
    open(os.path.join(ops.LOGS, "t.log"), "w").write(
        "ok line\nPOST https://api.telegram.org/bot1234567890:AAH-abcdefghijklmnopqrstuvwxyz0123456/sendMessage failed\n"
        "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.abcdefghijklmnop\napi_key=SUPERSECRETVALUE1\n")
    lines = client.get("/api/admin/sys/logs/read?name=t.log", headers=H()).json()["lines"]
    joined = "\n".join(lines)
    assert "AAH-abc" not in joined and "eyJhbG" not in joined and "SUPERSECRET" not in joined and "ok line" in joined


def test_log_search_and_tail(client):
    open(os.path.join(ops.SCHED_LOGS, "scheduler.log"), "w").write("\n".join(f"line {i} {'ERROR' if i % 50 == 0 else 'info'}" for i in range(1000)))
    r = client.get("/api/admin/sys/logs/read?name=scheduler.log&lines=30", headers=H()).json()
    assert len(r["lines"]) == 30 and r["lines"][-1].startswith("line 999")
    e = client.get("/api/admin/sys/logs/read?name=scheduler.log&q=error&lines=100", headers=H()).json()
    assert len(e["lines"]) == 20 and all("ERROR" in l for l in e["lines"])


# ── metrics ─────────────────────────────────────────────────────────────────────────────────
def test_metrics_series_buckets_and_peaks(client):
    now = int(time.time())
    for i in range(40):
        ops._store_sample({"cpu": 10.0 + i, "mem": 50.0, "mem_mb": 1000.0 + i * 10, "swap_mb": 0.0, "load1": 0.5, "disk": 40.0}, ts=now - i * 30)
    j = client.get("/api/admin/sys/metrics?range=1h", headers=H()).json()
    assert len(j["points"]) == 40 and j["peakMemMb"] == pytest.approx(1390.0) and j["peakCpu"] == pytest.approx(49.0)
    six = client.get("/api/admin/sys/metrics?range=6h", headers=H()).json()
    assert 1 <= len(six["points"]) <= 40 and six["bucket"] == 180
    assert client.get("/api/admin/sys/metrics?range=bogus", headers=H()).json()["range"] == "6h"


# ── scheduler job recorder never changes a job ──────────────────────────────────────────────
def test_track_passes_results_and_exceptions_through(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_track, "RUNS_DB", str(tmp_path / "r.db"))
    assert ops_track.track("job_a", lambda x: x * 2)(21) == 42

    def boom():
        raise RuntimeError("angel down")
    with pytest.raises(RuntimeError):
        ops_track.track("job_b", boom)()
    ops_track.track("job_c", lambda: {"status": "error", "msg": "x"})()
    con = ops_track.connect()
    rows = {r["job"]: r for r in con.execute("SELECT * FROM runs")}
    assert rows["job_a"]["ok"] == 1 and rows["job_b"]["ok"] == 0 and "angel down" in rows["job_b"]["note"] and rows["job_c"]["ok"] == 0


def test_track_survives_a_broken_database(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_track, "RUNS_DB", str(tmp_path / "missing" / "sub" / "x.db"))
    monkeypatch.setattr(ops_track, "connect", lambda path=None: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    assert ops_track.track("job_x", lambda: 7)() == 7


# ── data freshness reads real databases ─────────────────────────────────────────────────────
def test_data_freshness_reads_dna_and_flags_stale(tmp_path, monkeypatch):
    p = tmp_path / "dna.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE stock_dna_reports(symbol TEXT PRIMARY KEY, report_date TEXT, updated_at TEXT)")
    con.execute("CREATE TABLE stock_screener_rows(symbol TEXT, scan_date TEXT, scan_ts TEXT)")
    exp = ops.expected_session()
    con.executemany("INSERT INTO stock_dna_reports VALUES (?,?,?)", [("A", exp.isoformat(), f"{exp} 16:10:00"), ("B", exp.isoformat(), f"{exp} 16:11:00")])
    con.executemany("INSERT INTO stock_screener_rows VALUES (?,?,?)", [("A", "2020-01-01", "2020-01-01 10:00:00")])
    con.commit(); con.close()
    monkeypatch.setattr(ops, "paths", lambda: {**{k: str(tmp_path / "none.db") for k in ("master", "history", "intel", "collectors", "fundamentals", "lab")}, "dna": str(p), "labdata": str(tmp_path)})
    d = {i["key"]: i for i in ops.data_freshness(force=True)["items"]}
    assert d["dna"]["status"] == "ok" and d["dna"]["count"] == 2
    assert d["screener"]["status"] == "stale" and d["screener"]["on"] == "2020-01-01"
    assert d["events"]["status"] == "unknown"                                         # its database is missing: shown, not a crash
