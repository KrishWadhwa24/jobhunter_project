"""Always-open opportunities: tutoring platforms, AI-training portals and similar places that take applications all year,
so they never show up as a job posting. The list lives in evergreen.yaml (per profile); your own status per item is
kept in the database."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml

from . import config, db

STATUSES = {"applied": "Applied", "skip": "Not for me", "": ""}


def load() -> List[Dict[str, Any]]:
    p = config.EVERGREEN_PATH
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("items", [])
    out = []
    for it in data:
        if isinstance(it, dict) and it.get("id") and it.get("name"):
            it = dict(it)
            it["id"] = str(it["id"])
            it["kind"] = str(it.get("kind") or "other")
            it["india_ok"] = str(it.get("india_ok") or "unclear").lower()
            it["verified"] = str(it.get("verified") or "no").lower()
            it["requirements"] = [str(r) for r in (it.get("requirements") or [])]
            out.append(it)
    return out


def status(item_id: str) -> str:
    return db.kv_get(f"eg:{item_id}", "") or ""


def set_status(item_id: str, value: str) -> None:
    if value not in STATUSES:
        return
    db.kv_set(f"eg:{item_id}", value)


def listing(kind: str = "", india: str = "", show: str = "open") -> List[Dict[str, Any]]:
    rows = []
    for it in load():
        it["status"] = status(it["id"])
        if kind and it["kind"] != kind:
            continue
        if india == "ok" and it["india_ok"] == "no":
            continue
        if show == "open" and it["status"] in ("skip", "applied"):
            continue
        if show == "applied" and it["status"] != "applied":
            continue
        rows.append(it)
    order = {"yes": 0, "unclear": 1, "no": 2}
    rows.sort(key=lambda r: (r["status"] == "skip", r["status"] == "applied", order.get(r["india_ok"], 1), r["name"].lower()))
    return rows


def kinds() -> List[str]:
    return sorted({it["kind"] for it in load()})
