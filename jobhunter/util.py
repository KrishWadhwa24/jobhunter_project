from __future__ import annotations

import html
import re
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

USER_AGENT = "jobhunter/0.1 (personal job tracker; polite, low-frequency)"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class _Stripper(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        self.out.append(data)


def html_to_text(s: Optional[str], limit: int = 20000) -> str:
    """Convert (possibly entity-escaped) HTML to readable plain text."""
    if not s:
        return ""
    # Greenhouse returns HTML escaped once (&lt;p&gt;...); unescape if it looks escaped.
    if "&lt;" in s and "<" not in s:
        s = html.unescape(s)
    p = _Stripper()
    try:
        p.feed(s)
        text = "".join(p.out)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", s)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit]


_NETWORK = re.compile(
    r"ConnectTimeout|ConnectError|ReadTimeout|PoolTimeout|RemoteProtocolError|handshake operation timed out|"
    r"read operation timed out|nodename nor servname|Name or service not known|Temporary failure in name resolution|"
    r"ERR_NAME_NOT_RESOLVED|ERR_CONNECTION|ERR_INTERNET|ERR_TIMED_OUT|ERR_NETWORK|ERR_SSL|Page\.goto: Timeout", re.I)


def is_network_error(err: object) -> bool:
    """True when an error is about reaching the site (DNS, TLS handshake, timeouts), not about the site's content.
    Those say something about your internet connection, and should be retried soon rather than counted as a broken source."""
    return bool(_NETWORK.search(f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err or "")))


class PoliteClient:
    """httpx wrapper: honest UA, retries with backoff, per-host minimum delay."""

    def __init__(self, min_delay: float = 0.6, timeout: float = 30.0, retries: int = 3) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            timeout=httpx.Timeout(timeout, connect=25.0),     # slow TLS handshakes on a poor connection
            follow_redirects=True,
        )
        self.min_delay = min_delay
        self.retries = retries
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            nxt = self._last.get(host, 0.0) + self.min_delay
            wait = max(0.0, nxt - now)
            self._last[host] = now + wait
        if wait:
            time.sleep(wait)

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        host = urlparse(url).netloc
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self._wait(host)
            try:
                r = self._client.request(method, url, **kw)
                if r.status_code in (429, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2 ** attempt * 2)
                    continue
                return r
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < self.retries:
                    time.sleep(min(2 ** (attempt + 1), 10))
        raise last_exc or RuntimeError("request failed")

    def get(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> httpx.Response:
        return self.request("POST", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        r = self.get(url, **kw)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, **kw: Any) -> Any:
        r = self.post(url, **kw)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
