"""The role catalogue (catalogue.yaml): which families of jobs exist, and how to recognise them by title."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern

import yaml

PATH = Path(__file__).parent / "catalogue.yaml"


@lru_cache(maxsize=1)
def load() -> Dict[str, Any]:
    return yaml.safe_load(PATH.read_text(encoding="utf-8")) or {}


def families(scheme: Optional[str] = None) -> List[Dict[str, Any]]:
    fams = load().get("families", [])
    return [f for f in fams if not scheme or f.get("scheme") == scheme]


def family(fid: str) -> Optional[Dict[str, Any]]:
    return next((f for f in load().get("families", []) if f["id"] == fid), None)


def backgrounds() -> List[Dict[str, str]]:
    return load().get("backgrounds", [])


def _alt(fragments: List[str]) -> str:
    return "|".join(f"(?:{f})" for f in fragments if f)


def compile_roles(selected: List[str], include: List[str], exclude_extra: List[str]) -> Dict[str, Optional[Pattern[str]]]:
    """(core, exclude) title regexes for the chosen families plus the user's own keywords."""
    core: List[str] = []
    excl: List[str] = []
    for fid in selected:
        f = family(fid)
        if f:
            core += f.get("match", [])
            excl += f.get("exclude", [])
    for kw in include:
        kw = kw.strip().lower()
        if kw:
            core.append(re.escape(kw).replace(r"\ ", r"\s+"))
    for kw in exclude_extra:
        kw = kw.strip().lower()
        if kw:
            excl.append(re.escape(kw).replace(r"\ ", r"\s+"))
    wrap = lambda frag: re.compile(r"(?<![a-z0-9])(?:" + frag + r")(?![a-z0-9])", re.I) if frag else None  # noqa: E731
    return {"core": wrap(_alt(core)), "exclude": wrap(_alt(excl))}
