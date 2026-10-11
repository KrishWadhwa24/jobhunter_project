import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from jobhunter import auth, config
from jobhunter.gateway import COOKIE, create_gateway
from jobhunter.web import security
from jobhunter.web.app import create_app

PW = "correct-horse-1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(monkeypatch, tmp_path):
    """A real dashboard on a local port (the default profile) with a gateway in front of it."""
    monkeypatch.setattr(config, "GATEWAY_DIR", tmp_path / "gw")
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(config.load_config()), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(50):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    auth.add_user("me@example.com", PW, "", "Me")
    with TestClient(create_gateway(port_map=lambda: {"": port}), base_url="https://jobs.example.com") as gw:
        yield gw, port
    srv.should_exit = True
    t.join(5)


def test_gateway_login_then_forwards_to_the_users_dashboard(stack):
    gw, _ = stack
    r = gw.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")
    bad = gw.post("/login", data={"email": "me@example.com", "password": "nope"})
    assert bad.status_code == 401 and "do not match" in bad.text
    ok = gw.post("/login", data={"email": "ME@example.com", "password": PW, "next": "/companies"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/companies" and COOKIE in ok.headers["set-cookie"]
    page = gw.get("/companies")
    assert page.status_code == 200 and "Companies" in page.text
    assert "Sign out" in page.text and "this one" not in page.text           # served as a remote visitor, no profile list
    home = gw.get("/")
    assert home.status_code == 200 and "Jobs" in home.text


def test_gateway_forwards_posts_and_redirects(stack):
    gw, _ = stack
    gw.post("/login", data={"email": "me@example.com", "password": PW})
    r = gw.post("/cycle/run", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert gw.get("/static/style.css").status_code == 200
    assert gw.get("/nope").status_code == 404


def test_gateway_logout_and_bad_cookie(stack):
    gw, _ = stack
    gw.post("/login", data={"email": "me@example.com", "password": PW})
    gw.post("/logout")
    assert gw.get("/", follow_redirects=False).status_code == 303
    gw.cookies.set(COOKIE, "1.2.3.4")
    assert gw.get("/", follow_redirects=False).status_code == 303


def test_gateway_blocks_cross_site_posts_and_rate_limits(stack):
    gw, _ = stack
    gw.post("/login", data={"email": "me@example.com", "password": PW})
    assert gw.post("/cycle/run", headers={"origin": "https://evil.example"}, follow_redirects=False).status_code == 403
    for _ in range(5):
        gw.post("/login", data={"email": "me@example.com", "password": "bad"}, headers={"x-forwarded-for": "9.9.9.9"})
    assert gw.post("/login", data={"email": "me@example.com", "password": PW}, headers={"x-forwarded-for": "9.9.9.8"}).status_code == 429


def test_gateway_user_without_a_running_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GATEWAY_DIR", tmp_path / "gw")
    auth.add_user("a@example.com", PW, "english-hons")
    dead = free_port()
    with TestClient(create_gateway(port_map=lambda: {"english-hons": dead}), base_url="https://jobs.example.com") as gw:
        gw.post("/login", data={"email": "a@example.com", "password": PW})
        r = gw.get("/")
        assert r.status_code == 502 and "not running" in r.text
    auth.add_user("b@example.com", PW, "nobody")
    with TestClient(create_gateway(port_map=lambda: {}), base_url="https://jobs.example.com") as gw2:
        gw2.post("/login", data={"email": "b@example.com", "password": PW})
        assert gw2.get("/").status_code == 404


def test_dashboard_trusts_only_the_real_gateway(stack, monkeypatch):
    _, port = stack
    base = f"http://127.0.0.1:{port}"
    secret = security.gateway_secret()
    # forged header: treated as an ordinary (local) request, no special powers, and cannot impersonate
    r = httpx.get(base + "/", headers={"x-jh-gateway": "wrong", "x-jh-user": "me@example.com", "x-forwarded-for": "1.2.3.4"},
                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    ok = httpx.get(base + "/", headers={"x-jh-gateway": secret, "x-jh-user": "me@example.com"})
    assert ok.status_code == 200 and "Sign out" in ok.text
    other = httpx.get(base + "/", headers={"x-jh-gateway": secret, "x-jh-user": "ghost@example.com"})
    assert other.status_code == 403
    auth.add_user("w@example.com", PW, "english-hons")                      # right secret, but a user of another profile
    assert httpx.get(base + "/", headers={"x-jh-gateway": secret, "x-jh-user": "w@example.com"}).status_code == 403


def test_gateway_commands_registered():
    from jobhunter import cli
    assert callable(cli.cmd_gateway_start) and callable(cli.cmd_gateway_stop)
