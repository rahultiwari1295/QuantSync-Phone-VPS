import base64, datetime as dt, json, os, sys, time
import jwt, pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from collectors.core import db
from collectors.admin import auth, api

PROJECT = "scantoknow-a1593"

def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(dt.datetime(2020, 1, 1)).not_valid_after(dt.datetime(2040, 1, 1)).sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()

KEY, PEM = _keypair()
OTHER_KEY, _ = _keypair()

def token(email="rahultiwari1295@gmail.com", verified=True, aud=PROJECT, exp=3600, key=KEY, kid="k1", iss=None):
    now = int(time.time())
    claims = {"iss": iss or f"https://securetoken.google.com/{PROJECT}", "aud": aud, "sub": "uid1", "iat": now, "exp": now + exp,
              "email": email, "email_verified": verified}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "COLLECTOR_DB", str(tmp_path / "c.db"))
    monkeypatch.setattr(db, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "get_certs", lambda force=False: {"k1": PEM})
    monkeypatch.setenv("QS_RELAY_TOKEN", "x" * 32)
    auth._fails.clear()
    db.init().close()
    app = FastAPI(); app.include_router(api.router); app.include_router(api.relay_router)
    return TestClient(app)

def H(**kw): return {"Authorization": "Bearer " + token(**kw)}

def test_verify_accepts_owner():
    assert auth.verify(token(), certs_provider=lambda: {"k1": PEM})["email"] == "rahultiwari1295@gmail.com"

@pytest.mark.parametrize("kw", [dict(email="someone@else.com"), dict(verified=False), dict(aud="other-project"), dict(exp=-100),
                                dict(key=OTHER_KEY), dict(kid="nope"), dict(iss="https://evil.example")])
def test_verify_rejects(kw):
    with pytest.raises(auth.AuthError):
        auth.verify(token(**kw), certs_provider=lambda: {"k1": PEM})

def test_verify_rejects_unsigned_and_garbage():
    for t in ("not.a.jwt", jwt.encode({"email": "rahultiwari1295@gmail.com"}, None, algorithm="none")):
        with pytest.raises(auth.AuthError):
            auth.verify(t, certs_provider=lambda: {"k1": PEM})

def test_endpoints_require_admin(client):
    assert client.get("/api/admin/overview").status_code == 401
    assert client.get("/api/admin/overview", headers=H(email="x@y.com")).status_code == 403
    assert client.post("/api/admin/jobs/news/run", headers=H(verified=False)).status_code == 403
    r = client.get("/api/admin/overview", headers=H())
    assert r.status_code == 200 and "coverage" in r.json() and "alerts" in r.json()

def test_throttle_after_repeated_failures(client):
    for _ in range(20):
        client.get("/api/admin/me")
    assert client.get("/api/admin/me", headers=H()).status_code == 429

def test_run_job_and_refresh_are_queued_and_audited(client):
    assert client.post("/api/admin/jobs/news/run", headers=H()).json()["queued"] is True
    assert client.post("/api/admin/jobs/news/run", headers=H()).json()["queued"] is False        # no duplicates
    assert client.post("/api/admin/jobs/nope/run", headers=H()).status_code == 404
    assert client.post("/api/admin/refresh/results/RELIANCE", headers=H()).json()["args"]["symbols"] == "RELIANCE"
    assert client.post("/api/admin/refresh/results/bad;drop", headers=H()).status_code == 400
    assert client.post("/api/admin/refresh/unknown/SBIN", headers=H()).status_code == 400
    acts = [r["action"] for r in client.get("/api/admin/audit", headers=H()).json()["rows"]]
    assert "run_job" in acts and "refresh" in acts

def test_run_job_filters_args(client):
    client.post("/api/admin/jobs/results_daily/run", headers=H(), json={"symbols": "SBIN", "evil": "x", "limit": 5})
    with db.session() as c:
        args = json.loads(c.execute("SELECT args FROM job_queue").fetchone()[0])
    assert args == {"symbols": "SBIN", "limit": 5}

def test_coverage_and_sources(client):
    with db.session() as c:
        db.set_status(c, "results", "AAA", "FRESH", "2026-06-30"); db.set_status(c, "results", "BBB", "MISSING")
        c.execute("INSERT INTO source_health(route, forced_off) VALUES ('direct:nse', 0)")
    cov = client.get("/api/admin/coverage", headers=H()).json()["datasets"]["results"]
    assert cov["FRESH"] == 1 and cov["MISSING"] == 1 and cov["total"] == 2
    rows = client.get("/api/admin/coverage/results?state=MISSING", headers=H()).json()["rows"]
    assert [r["symbol"] for r in rows] == ["BBB"]
    assert client.get("/api/admin/coverage/nothing", headers=H()).status_code == 404
    assert client.post("/api/admin/sources/direct:nse/off", headers=H()).json()["ok"]
    src = client.get("/api/admin/sources", headers=H()).json()["routes"][0]
    assert src["state"] == "off"
    al = client.get("/api/admin/overview", headers=H()).json()["alerts"]
    assert any("switched off" in a["text"] for a in al)

def test_jobs_listing(client):
    j = client.get("/api/admin/jobs", headers=H()).json()["jobs"]
    names = {x["name"] for x in j}
    assert {"listing", "news", "results_daily", "ratios"} <= names
    assert next(x for x in j if x["name"] == "news")["next"] is not None

def test_relay_requires_token_and_stores(client):
    assert client.get("/api/relay/tasks").status_code == 403
    assert client.get("/api/relay/tasks", headers={"x-relay-token": "wrong" * 8}).status_code == 403
    body = base64.b64encode(b'[{"a":1}]').decode()
    r = client.post("/api/relay/ingest", headers={"x-relay-token": "x" * 32}, json={"source": "s", "key": "k", "url": "u", "status": 200, "body_b64": body})
    assert r.json()["ok"]
    with db.session() as c:
        assert bytes(c.execute("SELECT body FROM relay_inbox").fetchone()[0]) == b'[{"a":1}]'

def test_relay_disabled_without_secret(client, monkeypatch):
    monkeypatch.setenv("QS_RELAY_TOKEN", "")
    assert client.get("/api/relay/tasks", headers={"x-relay-token": ""}).status_code == 403

def test_every_refresh_target_is_a_real_job():
    from collectors import registry
    for ds, (job, _) in api.REFRESH_JOB.items():
        assert job in registry.JOBS, f"{ds} refresh queues unknown job {job}"
    assert set(api.DATASETS) >= set(api.REFRESH_JOB)

def test_daemon_would_accept_a_refresh_from_the_api(client):
    from collectors import registry
    client.post("/api/admin/refresh/results/SBIN", headers=H())
    with db.session() as c:
        job = c.execute("SELECT job FROM job_queue").fetchone()[0]
    assert job in registry.JOBS
