"""Loads application_answers.yaml (with contact fallbacks from resume_facts.yaml)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .. import config

ANSWERS_PATH = config.ANSWERS_PATH


class Profile:
    def __init__(self, values: Dict[str, str], rules: List[Dict[str, str]]) -> None:
        self.values = values
        self.rules = rules

    def get(self, key: str) -> str:
        return (self.values.get(key) or "").strip()


def _flat(raw: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for section in ("personal", "education", "work"):
        for k, v in (raw.get(section) or {}).items():
            out[k] = "" if v is None else str(v)
    return out


def load_profile(path: Optional[Path] = None) -> Profile:
    p = Path(path) if path else config.ANSWERS_PATH
    raw: Dict[str, Any] = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    values = _flat(raw)
    # fall back to the resume facts for anything missing or left empty
    def fill(key: str, value: str) -> None:
        if value and not (values.get(key) or "").strip():
            values[key] = value

    try:
        from ..resume.facts import load_facts
        facts = load_facts()
        contact, edu = facts.contact, facts.education
        name = str(contact.get("name", "")).strip()
        fill("full_name", name)
        if name and not values.get("first_name"):
            values["first_name"], _, values["last_name"] = name.partition(" ")
        fill("email", str(contact.get("email", "")))
        fill("phone_full", str(contact.get("phone", "")))
        fill("location", str(contact.get("location", "")))
        if not values.get("city") and contact.get("location"):
            values["city"] = str(contact["location"]).split(",")[0].strip()
        for link in contact.get("links", []) or []:
            label = str(link.get("label", "")).lower()
            if label in ("linkedin", "github"):
                fill(label, str(link.get("url", "")))
        fill("school", str(edu.get("school", "")))
        fill("cgpa", "".join(ch for ch in str(edu.get("detail", "")).split("/")[0] if ch.isdigit() or ch == ".") if "cgpa" in str(edu.get("detail", "")).lower() else "")
        years = re.findall(r"(?:19|20)\d{2}", str(edu.get("dates", "")))
        if years:
            fill("grad_year", years[-1])
    except Exception:
        pass
    if not values.get("phone") and values.get("phone_full"):
        digits = "".join(ch for ch in values["phone_full"] if ch.isdigit())
        values["phone"] = digits[-10:]
    rules = [r for r in (raw.get("rules") or []) if r.get("match") and "answer" in r]
    return Profile(values, [{"match": str(r["match"]), "answer": str(r["answer"])} for r in rules])
