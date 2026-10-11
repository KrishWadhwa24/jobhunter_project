"""The one public door.

Every profile's dashboard listens on its own port on this computer. Exposing several ports to the internet would mean
a tunnel entry, a password screen and a bit of upkeep per person. Instead this small gateway listens on ONE port,
shows the sign-in page, and forwards each signed-in person to their own profile's dashboard (the profile they belong to
in the users table). Point the tunnel (Cloudflare) at this port only; everything else stays on 127.0.0.1.

    browser -> tunnel -> gateway (:8780) -> the signed-in person's dashboard (:8787, :8788, ...)
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Dict

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from . import auth, config
from .web import security

log = logging.getLogger("jobhunter.gateway")
WEB = Path(__file__).parent / "web"
COOKIE = "jh_gateway"
MAX_BODY = 16 * 1024 * 1024
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding",
       "upgrade", "content-length", "host"}
RESP_DROP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "server", "date"}


def ports() -> Dict[str, int]:
    """profile slug -> dashboard port ('' is the default profile)."""
    return {p["slug"]: int(p["port"]) for p in config.list_profiles() if p["port"]}


def _page(title: str, text: str, status: int) -> HTMLResponse:
    from html import escape
    return HTMLResponse(
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body><main>'
        f'<div class="wrap" style="max-width:560px"><h1>{escape(title)}</h1><p class="sub">{escape(text)}</p>'
        f'<p><a class="btn" href="/">Try again</a></p></div></main></body></html>', status_code=status)


def create_gateway(port_map=ports, client: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="Jobhunter gateway", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    limiter = security.LoginLimiter()
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=3.0), follow_redirects=False)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        h, path = request.headers, request.url.path
        secure = security.is_https(h, request.url.scheme)

        def done(resp: Response) -> Response:
            for k, v in security.security_headers(secure).items():
                resp.headers.setdefault(k, v)
            if not path.startswith("/static/"):
                resp.headers["Cache-Control"] = "no-store"
            return resp

        try:
            if int(h.get("content-length") or 0) > MAX_BODY:
                return done(PlainTextResponse("That upload is too large.", status_code=413))
        except ValueError:
            return done(PlainTextResponse("Bad request.", status_code=400))
        if request.method not in ("GET", "HEAD", "OPTIONS") and not security.same_origin(h):
            return done(PlainTextResponse("Blocked: this request did not come from the site itself.", status_code=403))
        return done(await call_next(request))

    def login_page(request: Request, nxt: str = "/", error: str = "", status: int = 200, email: str = "") -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": security.safe_next(nxt), "error": error, "email": email, "profile_name": ""},
            status_code=status)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "gateway": True})

    @app.get("/manifest.webmanifest")
    def manifest() -> JSONResponse:
        return JSONResponse({
            "name": "Jobhunter", "short_name": "Jobhunter", "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": "#ffffff", "theme_color": "#0a0a0a",
            "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]},
            media_type="application/manifest+json")

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request, next: str = "/"):
        if security.session_user(request.cookies.get(COOKIE, ""), any_profile=True):
            return RedirectResponse(security.safe_next(next), status_code=303)
        return login_page(request, next)

    @app.post("/login")
    def login_post(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/")):
        ip = security.client_ip(request.headers, request.client.host if request.client else "")
        em = auth.normalize(email)[:200]
        keys = (ip, "mail:" + em)
        wait = max(limiter.wait_seconds(k) for k in keys)
        if wait:
            mins = max(1, (wait + 59) // 60)
            return login_page(request, next, f"Too many wrong attempts. Try again in {mins} minute{'s' if mins != 1 else ''}.", 429, em)
        user = auth.get_user(em)
        good = bool(user) and security.verify_password(password[:200], user["password_hash"])
        if not user:
            security.burn_time(password[:200])
        if not good:
            for k in keys:
                limiter.fail(k)
            return login_page(request, next, "That email and password do not match.", 401, em)
        for k in keys:
            limiter.ok(k)
        auth.touch_login(em)
        resp = RedirectResponse(security.safe_next(next), status_code=303)
        resp.set_cookie(COOKIE, security.make_session(user), max_age=security.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=security.is_https(request.headers, request.url.scheme), path="/")
        return resp

    @app.post("/logout")
    def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH", "OPTIONS"])
    async def forward(request: Request, path: str):
        user = security.session_user(request.cookies.get(COOKIE, ""), any_profile=True)
        if not user:
            if request.method in ("GET", "HEAD"):
                full = request.url.path + (("?" + request.url.query) if request.url.query else "")
                from urllib.parse import quote
                return RedirectResponse(f"/login?next={quote(full)}", status_code=303)
            return RedirectResponse("/login", status_code=303)
        port = port_map().get(user["profile"])
        if not port:
            return _page("No tracker for this account", "This account is not linked to a tracker on this computer.", 404)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"x-jh-gateway", "x-jh-user"}}
        headers["host"] = request.headers.get("host", f"127.0.0.1:{port}")
        headers["x-jh-gateway"] = security.gateway_secret()
        headers["x-jh-user"] = user["email"]
        url = f"http://127.0.0.1:{port}{request.url.path}" + (("?" + request.url.query) if request.url.query else "")
        try:
            upstream = await http.send(http.build_request(request.method, url, headers=headers, content=await request.body()),
                                       stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return _page("This tracker is not running", "The computer that hosts it is off, asleep, or the app has stopped. "
                         "Start it with ./jh up and try again.", 502)
        except httpx.TimeoutException:
            return _page("That took too long", "The tracker did not answer in time. Try again in a moment.", 504)
        resp = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 background=BackgroundTask(upstream.aclose))
        resp.raw_headers = [(k, v) for k, v in upstream.headers.raw if k.decode().lower() not in RESP_DROP]
        return resp

    return app


def build_app() -> FastAPI:
    return create_gateway()


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    security.gateway_secret()                  # create it before anything starts
    log.info("gateway on http://127.0.0.1:%s -> %s", config.GATEWAY_PORT, ports())
    # proxy_headers off: we read X-Forwarded-For ourselves, and never trust it for anything but the login limiter.
    uvicorn.run(build_app(), host="127.0.0.1", port=config.GATEWAY_PORT, log_level="warning", proxy_headers=False)


if __name__ == "__main__":
    main()
