"""Turn a job description into weighted keywords."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import terms as T
from .llm import LLM, LLMError

REQ_HDR = re.compile(r"(?i)(requirements?|qualifications?|what you('|’)?ll need|what we('|’)?re looking for|must[- ]have|"
                     r"you have|you bring|who you are|minimum|basic qualifications|skills required|required skills|"
                     r"about you|key skills|eligibility)")
PREF_HDR = re.compile(r"(?i)(preferred|nice[- ]to[- ]have|bonus|good to have|desirable|plus|added advantage|"
                      r"great to have|extra credit)")
RESP_HDR = re.compile(r"(?i)(responsibilities|what you('|’)?ll do|what you will do|the role|your role|day[- ]to[- ]day|"
                      r"job description|about the (?:role|job|position))")
OTHER_HDR = re.compile(r"(?i)(about (?:us|the company)|benefits|perks|why join|who we are|our (?:culture|values)|equal opportunity)")

STOP_TITLE = {"i", "ii", "iii", "iv", "1", "2", "3", "of", "and", "the", "a", "an", "for", "in", "at", "to", "new",
              "grad", "graduate", "2026", "2027", "2025", "associate", "senior", "sr", "junior", "jr", "staff",
              "principal", "lead", "level", "bangalore", "bengaluru", "india", "remote", "hyderabad", "pune", "noida",
              "gurugram", "mumbai", "chennai", "delhi"}


@dataclass
class JD:
    title: str = ""
    weights: Dict[str, float] = field(default_factory=dict)      # canonical term -> weight (1..3)
    literals: Dict[str, float] = field(default_factory=dict)     # extra free-text keywords from the LLM
    title_tokens: List[str] = field(default_factory=list)
    source: str = "rules"

    def all_terms(self) -> Dict[str, float]:
        return {**self.weights, **self.literals}


def title_tokens(title: str) -> List[str]:
    toks = [t for t in re.findall(r"[a-z][a-z0-9+#.]*", (title or "").lower()) if t not in STOP_TITLE and len(t) > 1]
    return toks


def analyze_rules(text: str, title: str = "") -> JD:
    jd = JD(title=title, title_tokens=title_tokens(title), source="rules")
    section = "other"
    best: Dict[str, float] = {}
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text or "") if ln.strip()]
    total_counts = T.extract_terms((title or "") + "\n" + (text or ""))
    for ln in lines:
        short_header = len(ln) < 70 and (ln.endswith(":") or ln.isupper() or len(ln.split()) <= 6 or ":" in ln[:40])
        if short_header:
            new_section = ("pref" if PREF_HDR.search(ln) else "req" if REQ_HDR.search(ln)
                           else "resp" if RESP_HDR.search(ln) else "other" if OTHER_HDR.search(ln) else None)
            if new_section:
                section = new_section
                # "Requirements: Java, Kafka" carries content after the colon: keep reading it
                ln = ln.split(":", 1)[1].strip() if ":" in ln else ""
                if not ln:
                    continue
        base = {"req": 3.0, "resp": 2.0, "pref": 1.0, "other": 1.5}[section]
        if re.search(r"(?i)\b(must|required|mandatory|strong|proficien\w+|hands-on|expertise in)\b", ln):
            base = max(base, 3.0)
        if re.search(r"(?i)\b(nice to have|preferred|bonus|plus|familiarity|exposure|good to have)\b", ln):
            base = min(base, 1.0)
        for t in T.extract_terms(ln):
            best[t] = max(best.get(t, 0), base)
    for t, w in list(best.items()):
        if total_counts.get(t, 0) >= 3:
            w = max(w, 3.0)
        if t in T.SOFT:
            w = min(w, 1.0)
        best[t] = w
    for t in T.extract_terms(title or ""):
        best[t] = 3.0
    jd.weights = best
    return jd


_SYSTEM = ("You extract hiring keywords from job descriptions. Respond with JSON only. "
           "Use the exact canonical names from the vocabulary when a term matches one.")


def analyze_llm(text: str, title: str, llm: LLM) -> JD:
    vocab = ", ".join(T.CANONICAL)
    user = (f"Vocabulary of canonical terms: {vocab}\n\n"
            f"Job title: {title}\n\nJob description:\n{text[:7000]}\n\n"
            'Return JSON: {"must_have": [..], "nice_to_have": [..], "other_keywords": [..]}. '
            "must_have = technologies/skills the posting clearly requires. nice_to_have = preferred or bonus. "
            "Use canonical names where possible; other_keywords are important skills not in the vocabulary "
            "(max 8, hard skills only, no soft skills).")
    data = llm.chat_json(_SYSTEM, user, max_tokens=700)
    canon = {c.lower(): c for c in T.CANONICAL}
    jd = JD(title=title, title_tokens=title_tokens(title), source=f"llm:{llm.name}")
    for key, w in (("must_have", 3.0), ("nice_to_have", 1.0)):
        for t in data.get(key, []) or []:
            c = canon.get(str(t).strip().lower())
            if c:
                jd.weights[c] = max(jd.weights.get(c, 0), min(w, 1.0) if c in T.SOFT else w)
            elif key == "must_have" and 2 < len(str(t)) < 40:
                jd.literals[str(t).strip()] = 2.0
    for t in (data.get("other_keywords", []) or [])[:8]:
        t = str(t).strip()
        if 2 < len(t) < 40 and t.lower() not in canon:
            jd.literals.setdefault(t, 1.0)
    return jd


def analyze(text: str, title: str = "", llm: Optional[LLM] = None) -> JD:
    """Rules always run; the LLM, when available, refines which terms are required. Results are merged."""
    rules = analyze_rules(text, title)
    if llm is None:
        return rules
    try:
        ai = analyze_llm(text, title, llm)
    except (LLMError, Exception):
        return rules
    merged = JD(title=title, title_tokens=rules.title_tokens, source=ai.source)
    for t in set(rules.weights) | set(ai.weights):
        a, r = ai.weights.get(t), rules.weights.get(t)
        if a is not None and r is not None:
            merged.weights[t] = max(a, r) if abs(a - r) < 1.5 else a
        else:
            merged.weights[t] = a if a is not None else min(r or 1.0, 2.0)
    merged.literals = dict(ai.literals)
    return merged
