import time

import pytest
from fastapi.testclient import TestClient

from jobhunter import auth, config, db
from jobhunter.web import security
from jobhunter.web.app import create_app

PW = "correct-horse-1"
EM = "parnika@example.com"
REMOTE = {"host": "mac.example.ts.net", "x-forwarded-for": "203.0.113.5", "x-forwarded-proto": "https"}


@pytest.fixture
def locked():
    auth.add_user(EM, PW, "", "Parnika")
    return TestClient(create_app(config.load_config()), base_url="https://mac.example.ts.net")


@pytest.fixture
def open_site():
    return create_app(config.load_config())


def H(**extra):
    return {**REMOTE, **extra}


def test_password_hash_roundtrip():
    h = security.hash_password("abc12345")
    assert h.startswith("scrypt:") and "abc12345" not in h
    assert security.verify_password("abc12345", h) and not security.verify_password("abc12346", h)
    assert not security.verify_password("x", "garbage")


def test_random_password_shape():
    pw = security.random_password()
    assert len(pw) == 14 and pw.count("-") == 2


def test_local_needs_no_password(open_site):
    c = TestClient(open_site)                              # Host: testserver is treated like any non-local name...
    r = c.get("/", headers={"host": "127.0.0.1:8788"})
    assert r.status_code == 200 and "Sign out" not in r.text


def test_remote_without_password_is_refused(open_site):
    c = TestClient(open_site)
    r = c.get("/", headers=REMOTE)
    assert r.status_code == 503 and "Sign-in is not set up" in r.text
    assert c.get("/healthz", headers=REMOTE).status_code == 200
    assert c.get("/login", headers=REMOTE).status_code == 503


def test_tunnel_with_local_host_header_still_counts_as_remote(locked):
    r = locked.get("/", headers={"host": "127.0.0.1:8788", "x-forwarded-for": "203.0.113.5"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_remote_requires_login_then_works(locked):
    r = locked.get("/", headers=H(), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    assert locked.get("/companies", headers=H(), follow_redirects=False).status_code == 303
    assert locked.post("/cycle/run", headers=H(), follow_redirects=False).headers["location"] == "/login"
    assert "Welcome back" in locked.get("/login", headers=H()).text
    bad = locked.post("/login", data={"email": EM, "password": "nope", "next": "/"}, headers=H())
    assert bad.status_code == 401 and "do not match" in bad.text
    ok = locked.post("/login", data={"email": EM, "password": PW, "next": "/companies"}, headers=H(), follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/companies"
    sc = ok.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc and "secure" in sc
    page = locked.get("/", headers=H())
    assert page.status_code == 200 and "Sign out" in page.text
    assert "Parnika" not in page.text and "this one" not in page.text      # other profiles are never listed remotely


def test_logout_clears_session(locked):
    locked.post("/login", data={"email": EM, "password": PW}, headers=H())
    assert locked.get("/", headers=H()).status_code == 200
    locked.post("/logout", headers=H())
    assert locked.get("/", headers=H(), follow_redirects=False).status_code == 303


def test_forged_expired_and_stale_sessions_rejected(locked):
    u = auth.get_user(EM)
    assert security.session_user(security.make_session(u))["email"] == EM
    assert security.session_user("garbage") is None
    assert security.session_user("a.9999999999.abc.deadbeef") is None
    assert security.session_user(security.make_session(u, days=1, now=time.time() - 3 * 86400)) is None
    tok = security.make_session(u)
    auth.set_password(EM, "another-pass-9")                                  # a new password signs that user out
    assert security.session_user(tok) is None
    locked.cookies.set(security.cookie_name(), "1.2.3")
    assert locked.get("/", headers=H(), follow_redirects=False).status_code == 303
    tok = security.make_session(auth.get_user(EM))
    auth.add_user(EM, "another-pass-9", "english-hons")                      # a user of another profile is not let in here
    assert security.session_user(tok) is None


def test_wrong_profile_user_cannot_sign_in(locked):
    auth.add_user("other@example.com", PW, "english-hons")
    r = locked.post("/login", data={"email": "other@example.com", "password": PW}, headers=H())
    assert r.status_code == 401
    r = locked.post("/login", data={"email": "nobody@example.com", "password": PW}, headers=H())
    assert r.status_code == 401 and "do not match" in r.text                 # same message as a wrong password


def test_email_is_case_insensitive_and_per_email_lockout(locked):
    ok = locked.post("/login", data={"email": EM.upper(), "password": PW}, headers=H(), follow_redirects=False)
    assert ok.status_code == 303
    for i in range(5):                                                       # guesses from many addresses hit the per-email cap
        locked.post("/login", data={"email": EM, "password": "bad"}, headers=H(**{"x-forwarded-for": f"198.51.100.{i + 1}"}))
    r = locked.post("/login", data={"email": EM, "password": PW}, headers=H(**{"x-forwarded-for": "198.51.100.77"}))
    assert r.status_code == 429


def test_user_store(tmp_path):
    u = auth.add_user("Mixed@Case.com", "12345678", "english-hons", "Mixed")
    assert u["email"] == "mixed@case.com" and u["password_hash"].startswith("scrypt:") and "12345678" not in u["password_hash"]
    assert auth.count_for("english-hons") == 1 and auth.count_for("") == 0
    with pytest.raises(ValueError):
        auth.add_user("bad", "12345678")
    with pytest.raises(ValueError):
        auth.add_user("ok@x.com", "short")
    assert auth.remove_user("MIXED@case.com") and not auth.get_user("mixed@case.com")


def test_users_command(capsys):
    from jobhunter import cli
    assert cli.main(["users", "add", "x@y.com", "--password", "12345678"]) == 0
    assert cli.main(["users"]) == 0 and "x@y.com" in capsys.readouterr().out
    assert cli.main(["users", "add", "z@y.com", "--profile", "no-such", "--password", "12345678"]) == 1
    assert cli.main(["users", "remove", "x@y.com"]) == 0 and auth.get_user("x@y.com") is None


def test_require_login_also_on_this_computer(monkeypatch):
    auth.add_user(EM, PW, "")
    monkeypatch.setattr(config, "REQUIRE_LOGIN", True)
    c = TestClient(create_app(config.load_config()))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_rate_limit(locked):
    for _ in range(5):
        assert locked.post("/login", data={"email": EM, "password": "bad"}, headers=H()).status_code == 401
    r = locked.post("/login", data={"email": EM, "password": PW}, headers=H())          # even the right one waits now
    assert r.status_code == 429 and "Too many" in r.text
    other = locked.post("/login", data={"email": "someone@example.com", "password": "bad"}, headers=H(**{"x-forwarded-for": "198.51.100.9"}))
    assert other.status_code == 401                                         # a different address and email is not locked out


def test_limiter_unit():
    lim = security.LoginLimiter(per_ip=2, window=60, global_max=3, global_window=60)
    lim.fail("a", now=0); lim.fail("a", now=1)
    assert lim.wait_seconds("a", now=2) > 0 and lim.wait_seconds("b", now=2) == 0
    assert lim.wait_seconds("a", now=70) == 0
    lim = security.LoginLimiter(per_ip=2, window=60, global_max=3, global_window=60)
    for ip in ("a", "b", "c"):
        lim.fail(ip, now=3)
    assert lim.wait_seconds("d", now=4) > 0                                  # global cap, even for a new address


def test_cross_site_post_blocked(locked):
    locked.post("/login", data={"email": EM, "password": PW}, headers=H())
    r = locked.post("/cycle/run", headers=H(origin="https://evil.example"), follow_redirects=False)
    assert r.status_code == 403
    r = locked.post("/cycle/run", headers=H(origin="https://mac.example.ts.net"), follow_redirects=False)
    assert r.status_code == 303
    r = locked.post("/cycle/run", headers=H(**{"origin": "https://parnika.vercel.app", "x-forwarded-host": "parnika.vercel.app"}),
                    follow_redirects=False)
    assert r.status_code == 303                                              # through a proxy that forwards the public host


def test_open_redirect_after_login_blocked(locked):
    r = locked.post("/login", data={"email": EM, "password": PW, "next": "//evil.example"}, headers=H(), follow_redirects=False)
    assert r.headers["location"] == "/"
    assert security.safe_next("https://evil.example") == "/" and security.safe_next("/\\evil") == "/"


def test_security_headers_and_no_store(locked):
    r = locked.get("/login", headers=H())
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and "strict-transport-security" in r.headers


def test_static_and_manifest_open(locked):
    assert locked.get("/static/style.css", headers=H()).status_code == 200
    m = locked.get("/manifest.webmanifest", headers=H())
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    assert locked.get("/static/icon-192.png", headers=H()).status_code == 200


def test_remote_apply_only_tailors_resume(locked, monkeypatch):
    from jobhunter.apply import worker
    from jobhunter.resume import runner
    from jobhunter import redis_store
    started, filled = [], []
    monkeypatch.setattr(runner, "start", lambda jid, cfg, notify=True: started.append((jid, notify)))
    monkeypatch.setattr(worker, "start", lambda *a, **k: filled.append(a) or 0)
    from jobhunter import pipeline
    from tests.test_pipeline import FakeCollector, JOBS
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme", "pay_tier": "T1", "active": "yes"})
    monkeypatch.setattr(pipeline, "collector_for", lambda c: FakeCollector(JOBS))
    pipeline.run_cycle(config.load_config(), force=True, client=object())
    rows = db.query_jobs([], [], [])
    assert rows
    jid = rows[0]["id"]
    locked.post("/login", data={"email": EM, "password": PW}, headers=H())
    r = locked.post(f"/job/{jid}/action/apply", headers=H(), follow_redirects=False)
    assert r.status_code == 303 and started == [(jid, False)] and not filled
    locked.post(f"/job/{jid}/autofill", headers=H())
    assert not filled
    assert db.get_job(jid)["state"] == "apply_queued"


def test_public_url_guard():
    assert not security.public_http_url("http://127.0.0.1:8787/x")[0]
    assert not security.public_http_url("http://localhost/x")[0]
    assert not security.public_http_url("http://192.168.1.1/")[0]
    assert not security.public_http_url("http://169.254.169.254/latest/meta-data")[0]
    assert not security.public_http_url("file:///etc/passwd")[0]
    assert not security.public_http_url("https://user:pw@example.com/")[0]
    assert security.public_http_url("https://8.8.8.8/")[0]


def test_detect_and_save_reject_private_targets(open_site):
    c = TestClient(open_site)
    h = {"host": "127.0.0.1:8788"}
    r = c.post("/companies/detect", data={"url": "http://127.0.0.1:8787/"}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and "private" in r.headers["location"].lower() or "private%20network" in r.headers["location"]
    r = c.post("/companies/save", data={"name": "X", "source_type": "browser", "careers_url": "http://10.0.0.5/jobs"},
               headers=h, follow_redirects=False)
    assert "private" in r.headers["location"]
    assert not any(x["name"] == "X" for x in db.list_companies())
    r = c.post("/companies/save", data={"name": "Y", "source_type": "nonsense"}, headers=h, follow_redirects=False)
    assert "not%20valid" in r.headers["location"] and not any(x["name"] == "Y" for x in db.list_companies())


def test_upload_limits(open_site):
    c = TestClient(open_site)
    h = {"host": "127.0.0.1:8788"}
    big = b"x" * (security_limit := 9 * 1024 * 1024)
    r = c.post("/resume/upload", files={"file": ("r.pdf", big, "application/pdf")}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and "larger" in r.headers["location"]
    r = c.post("/companies/import", files={"file": ("c.csv", b"a" * (4 * 1024 * 1024), "text/csv")}, headers=h, follow_redirects=False)
    assert "too%20large" in r.headers["location"]


def test_not_found_and_error_pages(open_site):
    c = TestClient(open_site, raise_server_exceptions=False)
    h = {"host": "127.0.0.1:8788"}
    r = c.get("/nope", headers=h)
    assert r.status_code == 404 and "Page not found" in r.text
    assert c.get("/job/99999", headers=h, follow_redirects=False).status_code == 303


def test_company_check_runs_in_background(open_site, monkeypatch):
    from jobhunter import pipeline
    db.upsert_company({"name": "Acme", "source_type": "greenhouse", "slug_or_endpoint": "acme"})
    cid = db.list_companies()[0]["id"]
    import threading
    gate = threading.Event()

    class Out:
        outcomes = []
    monkeypatch.setattr(pipeline, "run_cycle", lambda cfg, only=None, **k: (gate.wait(5), Out())[1])
    c = TestClient(open_site)
    h = {"host": "127.0.0.1:8788"}
    r = c.post(f"/companies/{cid}/check", headers=h, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/companies?checking={cid}"
    assert "Checking Acme" in c.get(r.headers["location"], headers=h).text
    gate.set()
    for _ in range(50):
        if "Checking Acme" not in c.get(r.headers["location"], headers=h).text:
            break
        time.sleep(0.1)
    assert "nothing to check" in c.get(r.headers["location"], headers=h).text
