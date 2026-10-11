"""Optional Redis: a cross-process cycle lock and the apply queue.

Everything here degrades gracefully: with no REDIS_URL, or Redis down, the app still works
(in-process lock only, apply requests are recorded in the database).
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from . import config
from .util import utcnow

log = logging.getLogger("jobhunter.redis")
LOCK_KEY = config.redis_prefix() + "cycle_lock"
APPLY_QUEUE = config.redis_prefix() + "apply_queue"

_clients: Dict[str, Any] = {}
_warned: set = set()
_lock = threading.Lock()

_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def client() -> Optional[Any]:
    url = config.redis_url()
    if not url:
        return None
    with _lock:
        if url in _clients:
            return _clients[url]
        try:
            import redis
            r = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=3, decode_responses=True)
            r.ping()
            _clients[url] = r
            return r
        except Exception as e:
            if url not in _warned:
                _warned.add(url)
                log.warning("Redis unavailable (%s); continuing without it", type(e).__name__)
            return None


def reset() -> None:
    with _lock:
        _clients.clear()
        _warned.clear()


def ping() -> str:
    """'ok', 'not configured' or an error description (for `make doctor`)."""
    url = config.redis_url()
    if not url:
        return "not configured (optional)"
    try:
        import redis
        redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3).ping()
        return f"ok ({config.redact(url)})"
    except Exception as e:
        return f"not reachable: {type(e).__name__}: {e}"


@contextmanager
def cycle_lock(ttl_seconds: int = 7200) -> Iterator[bool]:
    """Yields True if this process may run a cycle. Without Redis it always yields True."""
    r = client()
    if r is None:
        yield True
        return
    token = uuid.uuid4().hex
    try:
        got = bool(r.set(LOCK_KEY, token, nx=True, ex=ttl_seconds))
    except Exception:
        yield True
        return
    try:
        yield got
    finally:
        if got:
            try:
                r.eval(_RELEASE, 1, LOCK_KEY, token)
            except Exception:
                pass


def release_lock() -> None:
    """Drop this profile's cycle lock. Only called once its service has been stopped, because a service that is
    killed mid-cycle never releases the lock and would block `check` for up to two hours."""
    r = client()
    if r is not None:
        try:
            r.delete(LOCK_KEY)
        except Exception:
            pass


def enqueue_apply(job_id: int) -> bool:
    """Put a job on the apply queue for the auto-fill worker (built in a later step)."""
    r = client()
    if r is None:
        return False
    try:
        r.lpush(APPLY_QUEUE, json.dumps({"job_id": job_id, "queued_at": utcnow()}))
        return True
    except Exception:
        return False


def apply_queue_depth() -> Optional[int]:
    r = client()
    if r is None:
        return None
    try:
        return int(r.llen(APPLY_QUEUE))
    except Exception:
        return None
