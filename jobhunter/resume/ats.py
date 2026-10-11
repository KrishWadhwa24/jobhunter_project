"""An ATS-style score for a resume against a job: a transparent proxy, not any vendor's real number.

  80 points  weighted keyword coverage (job terms found in the resume; required terms count 3x preferred ones)
  12 points  format and parseability (standard headings, contact details, one page, readable text order, dates)
   8 points  job-title alignment (the role words from the job title appear in the headline, summary or titles)

Terms the facts bank cannot truthfully support are reported as gaps; they are never invented to raise the score.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from . import terms as T
from .jd import JD

# A job asking for the broad term is satisfied when the resume truthfully contains a specific instance.
IMPLIED_BY: Dict[str, List[str]] = {
    "Cloud": ["AWS", "Azure", "GCP"],
    "Databases": ["PostgreSQL", "MySQL", "MongoDB", "Redis", "SQL"],
    "NoSQL": ["MongoDB", "Firebase"],
    "Caching": ["Redis"],
    "Unit Testing": ["Jest"],
    "Full-stack": ["Angular", "React", "Next.js"],
    "Event-Driven Architecture": ["Apache Kafka"],
    "Pub/Sub messaging": ["Apache Kafka"],
    "Networking": ["Computer Networks"],
    "Security": [],
    "Git": [],
    "REST APIs": [],
}

REQUIRED_SECTIONS = ["SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION"]   # "SKILLS" also matches "TECHNICAL SKILLS"


@dataclass
class ATSResult:
    score: float = 0.0
    keyword_points: float = 0.0
    format_points: float = 0.0
    title_points: float = 0.0
    coverage: float = 0.0
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"score": round(self.score, 1), "keyword_points": round(self.keyword_points, 1),
                "format_points": round(self.format_points, 1), "title_points": round(self.title_points, 1),
                "coverage": round(self.coverage, 3), "matched": self.matched, "missing": self.missing,
                "notes": self.notes}


def present_terms(text: str) -> Set[str]:
    have = T.term_set(text)
    for broad, specifics in IMPLIED_BY.items():
        if broad not in have and any(s in have for s in specifics):
            have.add(broad)
    return have


def _literal_present(lit: str, text_lower: str) -> bool:
    return lit.lower() in text_lower


def keyword_coverage(resume_text: str, jd: JD) -> ATSResult:
    res = ATSResult()
    have = present_terms(resume_text)
    low = resume_text.lower()
    total = got = 0.0
    for t, w in jd.weights.items():
        total += w
        if t in have:
            got += w
            res.matched.append(t)
        else:
            res.missing.append(t)
    for lit, w in jd.literals.items():
        total += w
        if _literal_present(lit, low):
            got += w
            res.matched.append(lit)
        else:
            res.missing.append(lit)
    res.coverage = (got / total) if total else 1.0
    res.keyword_points = 80.0 * res.coverage
    return res


def format_points(pdf_text: str, pages: int, required: Optional[List[str]] = None) -> (float, List[str]):
    pts, notes = 0.0, []
    up = pdf_text.upper()
    req = required or REQUIRED_SECTIONS
    found = [s for s in req if s in up]
    pts += 4.0 * len(found) / len(req)
    if len(found) < len(req):
        notes.append("missing sections: " + ", ".join(s for s in req if s not in found))
    email = re.search(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+", pdf_text)
    phone = re.search(r"\+?\d[\d \-]{8,}", pdf_text)
    pts += 1.0 * bool(email) + 1.0 * bool(phone)
    if not (email and phone):
        notes.append("contact details not machine-readable")
    order = [up.find(s) for s in [r for r in req if r in ("SUMMARY", "EXPERIENCE", "EDUCATION")]]
    if all(o >= 0 for o in order) and order == sorted(order):
        pts += 2.0
    else:
        notes.append("section order is not read top-to-bottom")
    if pages == 1:
        pts += 2.0
    else:
        notes.append(f"resume is {pages} pages")
    years = re.findall(r"(?:19|20)\d{2}", pdf_text)
    if len(years) >= 4:
        pts += 1.0
    if all(ord(c) < 0x2500 for c in pdf_text):
        pts += 1.0
    else:
        notes.append("contains symbols some ATS cannot read")
    return min(pts, 12.0), notes


def title_points(jd: JD, header_text: str) -> float:
    toks = jd.title_tokens
    if not toks:
        return 8.0
    low = header_text.lower()
    hit = sum(1 for t in toks if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low))
    # "developer"/"engineer"/"sde" are interchangeable for matching purposes
    if hit < len(toks):
        syn = {"developer": "engineer", "engineer": "developer", "sde": "engineer", "swe": "engineer",
               "programmer": "engineer"}
        hit = sum(1 for t in toks if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low)
                  or (t in syn and syn[t] in low))
    return 8.0 * hit / len(toks)


def score(resume_text: str, jd: JD, *, pdf_text: str = "", pages: int = 1, header_text: str = "",
          required: Optional[List[str]] = None) -> ATSResult:
    res = keyword_coverage(resume_text, jd)
    res.format_points, res.notes = format_points(pdf_text or resume_text, pages, required)
    res.title_points = title_points(jd, header_text or resume_text[:600])
    res.score = res.keyword_points + res.format_points + res.title_points
    return res
