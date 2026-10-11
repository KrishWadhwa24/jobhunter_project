"""Who may sign in. A small shared user table (SQLite, auth.db next to jh) used by every profile's dashboard.

Each user belongs to one profile (a profile is one person's job tracker: its own jobs, companies, resume).
The dashboard of a profile only accepts the users that belong to it. Passwords are stored as scrypt hashes.
Manage users with:  ./jh users | ./jh users add EMAIL --profile NAME | ./jh users passwd EMAIL | ./jh users remove EMAIL
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SCHEMA = """CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_login_at TEXT
)"""


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def _conn() -> sqlite3.Connection:
    path = config.AUTH_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute(SCHEMA)
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_user(email: str, password: str, profile: str = "", name: str = "") -> Dict[str, Any]:
    """Create the user, or change the password/profile of an existing one."""
    from .web import security
    email = normalize(email)
    if not EMAIL_RE.match(email):
        raise ValueError("That does not look like an email address.")
    if len(password) < security.MIN_LENGTH:
        raise ValueError(f"The password must be at least {security.MIN_LENGTH} characters.")
    h = security.hash_password(password)
    with closing(_conn()) as c, c:
        c.execute("""INSERT INTO users(email, password_hash, profile, name, created_at) VALUES(?,?,?,?,?)
                     ON CONFLICT(email) DO UPDATE SET password_hash=excluded.password_hash, profile=excluded.profile,
                     name=CASE WHEN excluded.name != '' THEN excluded.name ELSE users.name END""",
                  (email, h, profile, name, _now()))
    return get_user(email) or {}


def set_password(email: str, password: str) -> bool:
    u = get_user(email)
    if not u:
        return False
    add_user(email, password, u["profile"], u["name"])
    return True


def get_user(email: str) -> Optional[Dict[str, Any]]:
    with closing(_conn()) as c:
        r = c.execute("SELECT * FROM users WHERE email=?", (normalize(email),)).fetchone()
    return dict(r) if r else None


def list_users() -> List[Dict[str, Any]]:
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute("SELECT * FROM users ORDER BY profile, email")]


def remove_user(email: str) -> bool:
    with closing(_conn()) as c, c:
        return c.execute("DELETE FROM users WHERE email=?", (normalize(email),)).rowcount > 0


def count_for(profile: str) -> int:
    with closing(_conn()) as c:
        return int(c.execute("SELECT COUNT(*) FROM users WHERE profile=?", (profile,)).fetchone()[0])


def touch_login(email: str) -> None:
    with closing(_conn()) as c, c:
        c.execute("UPDATE users SET last_login_at=? WHERE email=?", (_now(), normalize(email)))
