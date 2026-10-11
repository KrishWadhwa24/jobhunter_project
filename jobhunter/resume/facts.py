"""Loads resume_facts.yaml: the only material the tailor may use."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .. import config

FACTS_PATH = config.FACTS_PATH


@dataclass
class Bullet:
    id: str
    weight: int
    variants: List[str]
    owner: str = ""            # company or project id, for the claim guard and ordering

    @property
    def text(self) -> str:
        return self.variants[0]


@dataclass
class Facts:
    raw: Dict[str, Any]
    contact: Dict[str, Any]
    education: Dict[str, Any]
    summaries: List[Dict[str, Any]]
    experience: List[Dict[str, Any]]
    projects: List[Dict[str, Any]]
    skills: List[Dict[str, Any]]
    achievements: List[Dict[str, Any]]
    corpus: str = ""
    corpus_lower: str = ""

    def all_skill_items(self) -> List[str]:
        return [i for c in self.skills for i in c["items"]]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def load_facts(path: Optional[Path] = None) -> Facts:
    p = Path(path) if path else config.FACTS_PATH
    if not p.exists():
        raise FileNotFoundError(f"{p} not found. It holds the facts the resume tailor may use.")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    for key in ("contact", "education", "summary", "skills"):
        if not raw.get(key):
            raise ValueError(f"resume_facts.yaml is missing '{key}'")

    def clean_bullets(items: List[Dict[str, Any]], owner: str) -> List[Bullet]:
        out = []
        for b in items:
            variants = [_norm(v) for v in b["variants"] if _norm(v)]
            if not variants:
                continue
            out.append(Bullet(str(b.get("id", "")), int(b.get("weight", 3)), variants, owner))
        return out

    experience = []
    for e in raw.get("experience") or []:
        e = dict(e)
        e.setdefault("title", "")
        e.setdefault("dates", "")
        e["bullets"] = clean_bullets(e.get("bullets") or [], e["company"])
        experience.append(e)
    projects = []
    for n, pr in enumerate(raw.get("projects") or []):
        pr = dict(pr)
        pr.setdefault("id", f"project{n + 1}")
        pr["names"] = [_norm(x) for x in (pr.get("names") or [pr.get("name") or pr["id"]])]
        pr["bullets"] = clean_bullets(pr.get("bullets") or [], pr["id"])
        pr["stack"] = pr.get("stack") or []
        pr["weight"] = int(pr.get("weight", 3))
        projects.append(pr)
    summaries = [{"id": s["id"], "variants": [_norm(v) for v in s["variants"]]} for s in raw["summary"]]
    achievements = [{"weight": int(a.get("weight", 3)), "variants": [_norm(v) for v in a["variants"]]}
                    for a in raw.get("achievements", [])]
    skills = [{"category": c["category"], "items": [str(i) for i in c["items"]]} for c in raw["skills"]]

    parts: List[str] = [yaml.safe_dump(raw["contact"]), yaml.safe_dump(raw["education"])]
    raw.setdefault("experience", [])
    raw.setdefault("projects", [])
    for s in summaries:
        parts += s["variants"]
    for e in experience:
        parts += [e["company"], e["title"], e["dates"]] + [v for b in e["bullets"] for v in b.variants]
    for pr in projects:
        parts += pr["names"] + [str(x) for x in pr["stack"]] + [v for b in pr["bullets"] for v in b.variants]
    for c in skills:
        parts += c["items"]
    for a in achievements:
        parts += a["variants"]
    corpus = "\n".join(parts)
    return Facts(raw, raw["contact"], raw["education"], summaries, experience, projects, skills, achievements,
                 corpus, corpus.lower())
