import os

import pytest

from jobhunter import redis_store

URL = os.environ.get("TEST_REDIS_URL", "").strip()


def test_without_redis_everything_degrades(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    redis_store.reset()
    with redis_store.cycle_lock() as got:
        assert got is True
    assert redis_store.enqueue_apply(1) is False
    assert redis_store.apply_queue_depth() is None
    assert "not configured" in redis_store.ping()


def test_unreachable_redis_degrades(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    redis_store.reset()
    with redis_store.cycle_lock() as got:
        assert got is True


@pytest.mark.skipif(not URL, reason="set TEST_REDIS_URL to test against a real Redis")
def test_lock_is_exclusive_and_queue_works(monkeypatch):
    monkeypatch.setenv("REDIS_URL", URL)
    redis_store.reset()
    r = redis_store.client()
    r.delete(redis_store.LOCK_KEY, redis_store.APPLY_QUEUE)
    with redis_store.cycle_lock() as first:
        assert first is True
        with redis_store.cycle_lock() as second:
            assert second is False            # another holder is refused
    with redis_store.cycle_lock() as again:
        assert again is True                  # released after the first finished
    assert redis_store.enqueue_apply(7) and redis_store.apply_queue_depth() == 1
