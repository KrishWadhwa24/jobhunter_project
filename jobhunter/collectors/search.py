"""Search terms for career sites that can only be searched by keyword (Workday, Eightfold, Oracle HCM).
Software profiles keep the built-in terms. Other profiles use the job titles of the role types they chose."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_terms: List[str] = []
MAX_TERMS = 14
GENERIC = ["graduate", "trainee", "intern"]


def set_from_config(cfg: Dict[str, Any]) -> None:
    global _terms
    from .. import catalogue, config
    _terms = []
    if config.scheme_name(cfg) == "software" or (cfg.get("roles") or {}).get("core"):
        return
    roles = cfg.get("roles") or {}
    fams = list(roles.get("families") or []) or [f["id"] for f in catalogue.families(config.scheme_name(cfg))]
    out: List[str] = [str(k).strip().lower() for k in roles.get("include") or [] if str(k).strip()]
    depth = 0
    while len(out) < MAX_TERMS - len(GENERIC) and depth < 6:      # round-robin so every chosen family gets terms
        added = False
        for fid in fams:
            f = catalogue.family(fid) or {}
            titles = f.get("titles") or []
            if depth < len(titles):
                t = str(titles[depth]).lower()
                if t not in out:
                    out.append(t)
                    added = True
            if len(out) >= MAX_TERMS - len(GENERIC):
                break
        depth += 1
        if not added and depth > 6:
            break
    _terms = out[:MAX_TERMS - len(GENERIC)] + GENERIC


def terms(default: List[str]) -> List[str]:
    return list(_terms) or list(default)
