"""Tests run on SQLite by default. Set TEST_DATABASE_URL=postgresql://... to run them on Postgres
(the schema in that database is wiped for every test, so use a throwaway database)."""
import os

import pytest

from jobhunter import config, db, redis_store
from jobhunter.apply import worker as apply_worker
from jobhunter.resume import runner

PG = os.environ.get("TEST_DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "AUTH_DB", tmp_path / "auth.db")
    monkeypatch.delenv("REDIS_URL", raising=False)
    redis_store.reset()
    monkeypatch.setattr(runner, "start", lambda *a, **k: None)
    monkeypatch.setattr(apply_worker, "start", lambda *a, **k: 0)
    if PG:
        monkeypatch.setenv("DATABASE_URL", PG)
        with db.connect() as c:
            c.execute("DROP SCHEMA public CASCADE")
            c.execute("CREATE SCHEMA public")
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    db.init_db()
    yield
    if PG:
        db.close_pools()


@pytest.fixture(autouse=True)
def local_test_client(monkeypatch):
    """Test clients behave like a browser on the same computer (http://127.0.0.1) unless a test sets another host."""
    from starlette.testclient import TestClient
    orig = TestClient.__init__

    def init(self, app, base_url="http://127.0.0.1:8787", *a, **k):
        orig(self, app, base_url, *a, **k)
    monkeypatch.setattr(TestClient, "__init__", init)
