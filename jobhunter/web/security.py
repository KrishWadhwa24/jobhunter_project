"""Protection for the dashboard when it is reached from outside the laptop (a phone, through a tunnel).

Two kinds of request:
  * local   - from a browser on this computer at http://127.0.0.1:<port>. No password, exactly as before.
  * remote  - anything that came through a tunnel or proxy (the Host name is not localhost, or the request
              carries X-Forwarded-* headers). These must sign in with an email and password from the users table
              (auth.py). If the profile has no users the request is refused, so a tunnel can never expose the
              dashboard by accident.

Passwords are stored only as scrypt hashes. Sessions are signed cookies; changing a password signs that user out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .. import config

SESSION_DAYS = 30
COOKIE_PREFIX = "jh_"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0"}
FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "cf-connecting-ip",
                   "cf-ray", "tailscale-user-login", "x-real-ip", "via")
MIN_LENGTH = 8
PUBLIC_PATHS = ("/login", "/healthz", "/static/", "/manifest.webmanifest", "/sw.js", "/icon-", "/apple-touch-icon.png",
                "/favicon.ico")

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt:{_SCRYPT_N}:{_SCRYPT_R}:{_SCRYPT_P}:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split(":")
        if kind != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(dk)), maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(calc, bytes.fromhex(dk))
    except Exception:
        return False


_dummy: List[str] = []


def burn_time(password: str) -> None:
    """Hash the guess against a throw-away hash so an unknown email takes as long as a wrong password."""
    if not _dummy:
        _dummy.append(hash_password("not-a-real-password"))
    verify_password(password, _dummy[0])


def random_password() -> str:
    """Easy to type on a phone: three groups of four lowercase letters and digits."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"        # no look-alikes (i l o 0 1)
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))



# ---------------------------------------------------------------- sessions
def _secret_key() -> bytes:
    """Random per-install key kept in the data folder, mixed with the password hash."""
    p = config.DATA_DIR / "session.key"
    try:
        key = p.read_bytes()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError:
        pass
    return key


def _sign(payload: str, stored_hash: str) -> str:
    return hmac.new(_secret_key() + stored_hash.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


def make_session(user: Dict[str, Any], days: int = SESSION_DAYS, now: Optional[float] = None) -> str:
    exp = int((now if now is not None else time.time()) + days * 86400)
    payload = f"{_b64(user['email'])}.{exp}.{secrets.token_hex(6)}"
    return f"{payload}.{_sign(payload, user['password_hash'])}"


def session_user(token: str, now: Optional[float] = None, any_profile: bool = False) -> Optional[Dict[str, Any]]:
    """The signed-in user for this cookie, or None. Changing a user's password, moving them to another profile or
    deleting them ends their sessions."""
    from .. import auth
    try:
        em, exp, nonce, sig = token.split(".")
        user = auth.get_user(_unb64(em))
        if not user or (not any_profile and user["profile"] != config.PROFILE):
            return None
        if not hmac.compare_digest(sig, _sign(f"{em}.{exp}.{nonce}", user["password_hash"])):
            return None
        return user if int(exp) > (now if now is not None else time.time()) else None
    except Exception:
        return None


def gateway_secret() -> str:
    """Shared by the gateway and the dashboards so a dashboard can tell a request really came from the gateway."""
    d = config.GATEWAY_DIR
    f = d / "secret"
    try:
        v = f.read_text().strip()
        if len(v) >= 32:
            return v
    except FileNotFoundError:
        pass
    d.mkdir(parents=True, exist_ok=True)
    v = secrets.token_hex(32)
    fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(v)
    return v


def from_gateway(headers) -> Optional[str]:
    """The signed-in email the gateway vouches for, or None when the request did not come from the gateway."""
    given = headers.get("x-jh-gateway") or ""
    if not given or not hmac.compare_digest(given, gateway_secret()):
        return None
    return headers.get("x-jh-user") or None


def cookie_name() -> str:
    return COOKIE_PREFIX + (config.PROFILE or "default")


# ---------------------------------------------------------------- who is calling
def _host_only(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_remote(headers, ) -> bool:
    """True for anything that did not come straight from a browser on this computer."""
    if _host_only(headers.get("host", "")) not in LOCAL_HOSTS:
        return True
    return any(h in headers for h in FORWARD_HEADERS)


def client_ip(headers, peer: str = "") -> str:
    for h in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        v = (headers.get(h) or "").split(",")[0].strip()
        if v:
            try:
                return str(ipaddress.ip_address(v))
            except ValueError:
                continue
    return peer or "unknown"


def is_https(headers, scheme: str = "") -> bool:
    return scheme == "https" or (headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https")


def same_origin(headers) -> bool:
    """CSRF check for state-changing requests: a browser always sends Origin (or Referer) on a form POST."""
    src = headers.get("origin") or headers.get("referer") or ""
    if not src or src == "null":
        return src == ""            # no header at all (curl, tests): fine, cookies are SameSite=Lax anyway
    host = (urlparse(src).netloc or "").lower()
    allowed = {(headers.get("host") or "").lower(), (headers.get("x-forwarded-host") or "").split(",")[0].strip().lower()}
    for extra in os.environ.get("JOBHUNTER_ALLOWED_ORIGINS", "").split(","):
        if extra.strip():
            allowed.add(urlparse(extra.strip()).netloc.lower() or extra.strip().lower())
    allowed.discard("")
    return host in allowed


def safe_next(target: str) -> str:
    t = (target or "/").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or "\n" in t or "\r" in t:
        return "/"
    return t


def is_public_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PATHS)


# ---------------------------------------------------------------- brute-force limiter
class LoginLimiter:
    """After 5 wrong passwords from one address, that address waits 15 minutes. A global cap of 30 wrong passwords
    in 10 minutes stops attackers who rotate addresses."""

    def __init__(self, per_ip: int = 5, window: int = 900, global_max: int = 30, global_window: int = 600) -> None:
        self.per_ip, self.window = per_ip, window
        self.global_max, self.global_window = global_max, global_window
        self._ip: Dict[str, List[float]] = {}
        self._all: List[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        self._all = [t for t in self._all if now - t < self.global_window]
        for k in list(self._ip):
            self._ip[k] = [t for t in self._ip[k] if now - t < self.window]
            if not self._ip[k]:
                del self._ip[k]

    def wait_seconds(self, ip: str, now: Optional[float] = None) -> int:
        now = now if now is not None else time.time()
        with self._lock:
            self._prune(now)
            wait = 0
            hits = self._ip.get(ip, [])
            if len(hits) >= self.per_ip:
                wait = int(hits[-self.per_ip] + self.window - now) + 1
            if len(self._all) >= self.global_max:
                wait = max(wait, int(self._all[-self.global_max] + self.global_window - now) + 1)
            return max(0, wait)

    def fail(self, ip: str, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._ip.setdefault(ip, []).append(now)
            self._all.append(now)

    def ok(self, ip: str) -> None:
        with self._lock:
            self._ip.pop(ip, None)


def security_headers(secure: bool) -> Dict[str, str]:
    h = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                    "font-src https://fonts.gstatic.com; img-src 'self' data:; "
                                    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
    }
    if secure:
        h["Strict-Transport-Security"] = "max-age=31536000"
    return h


# ---------------------------------------------------------------- outbound-request guard
def public_http_url(url: str) -> Tuple[bool, str]:
    """For URLs a visitor types in (Companies > add by link): only http(s) to a public internet address, so the
    server cannot be used to probe this laptop or the home network."""
    import socket
    try:
        u = urlparse(url.strip())
    except Exception:
        return False, "That does not look like a web address."
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "Use a full web address starting with http:// or https://"
    if u.username or u.password:
        return False, "Web addresses with a login in them are not allowed."
    host = u.hostname
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return True, ""             # cannot resolve here: the fetch will fail on its own, nothing to protect
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False, "That address points to a private network, which is not allowed."
    return True, ""
