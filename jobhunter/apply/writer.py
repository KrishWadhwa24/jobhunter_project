"""Drafts for the long-answer questions on application forms, and the cover letter.

Everything is built only from resume_facts.yaml and application_answers.yaml. An LLM (Groq, else GitHub Models)
may phrase it, but its text is rejected by the same truth guard the resume uses if it adds a number, technology
or name that is not in your facts; then a plain template built from your facts is used instead.
Nothing here is ever submitted: drafts are typed into the form for you to read and edit."""
from __future__ import annotations

import io
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..resume import ats, guard, jd as jdmod, llm as llmmod, render
from ..resume.facts import Facts, load_facts
from .profile import Profile, load_profile

log = logging.getLogger("jobhunter.apply")

WHY = re.compile(r"why (do you|are you|would you|us\b|this|should|did you)|what (interests|excites|attracts|motivates|draws)|"
                 r"interest in|motivat|why.*(company|role|position|job|join|apply)|reason for (applying|interest)")
ABOUT = re.compile(r"about yourself|introduce yourself|brief (intro|bio)|professional summary|summary of your|describe yourself|"
                   r"tell us (a bit )?about you|who are you")
PROJECT = re.compile(r"project|proud|achievement|accomplish|significant|relevant experience|technical (skills|background|experience)|"
                     r"experience with|describe .*experience|your background")
HIRE = re.compile(r"strengths?|what (can|do) you (bring|contribute|offer)|how (will|would|can) you contribute|"
                  r"why (should|shall) we (hire|select|choose)|fit for")
SKIP_ALWAYS = re.compile(r"additional information|anything else|any comments|other comments|notes?\b|"
                         r"weakness|disability|accommodation|salary|ctc|compensation|visa|legal|criminal|convict")


def kind_of(label: str) -> str:
    """why | about | project | hire | '' (not a question we draft)."""
    t = (label or "").lower()
    if not t or SKIP_ALWAYS.search(t):
        return ""
    for name, rx in (("why", WHY), ("about", ABOUT), ("hire", HIRE), ("project", PROJECT)):
        if rx.search(t):
            return name
    return ""


def wants_draft(label: str, kind: str, required: bool) -> bool:
    return kind in ("textarea", "text") and bool(kind_of(label))


def _sentences_lower_start(text: str) -> str:
    """Sentence-initial words are capitalised in prose; the guard treats capitals as possible new names."""
    return re.sub(r"(^|[.!?]\s+|\n)([A-Z])(?=[a-z])", lambda m: m.group(1) + m.group(2).lower(), text or "")


def fit(text: str, maxlen: int) -> Optional[str]:
    text = (text or "").strip()
    if not maxlen or len(text) <= maxlen:
        return text or None
    cut = text[:maxlen]
    end = max(cut.rfind(". "), cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if end >= 60:
        return cut[:end + 1].strip()
    return None


class ApplyContext:
    """Per-job helper the form filler calls for drafts and the cover letter. Caches what it produced."""

    def __init__(self, job: Dict[str, Any], company: str, cfg: Optional[Dict[str, Any]] = None, out_dir: Optional[Path] = None,
                 facts: Optional[Facts] = None, prof: Optional[Profile] = None, use_llm: bool = True,
                 llm: Optional[llmmod.LLM] = None) -> None:
        self.job, self.company, self.cfg = job, company or "your company", cfg or {}
        self.title = job.get("title") or "this role"
        self.out_dir = Path(out_dir) if out_dir else None
        self.facts = facts or load_facts()
        self.prof = prof or load_profile()
        self.jd = jdmod.analyze_rules(job.get("description") or "", self.title)
        self._llm = llm if llm is not None else (llmmod.groq(cfg) or llmmod.github(cfg)) if use_llm else None
        self.facts_terms = ats.present_terms(self.facts.corpus)
        self._answers: Dict[str, Tuple[str, str]] = {}
        self._cover: Optional[Tuple[str, str, str]] = None
        profile = (self.cfg.get("profile") or {})
        self.allowed = "\n".join([self.facts.corpus, str(profile.get("about", "")), " ".join(self.prof.values.values()),
                                  self.company, self.title, str(date.today().year)])

    # ---- material taken from the facts ----
    def _bullets(self, n: int = 5) -> List[str]:
        from ..resume import tailor
        cands: List[Tuple[float, str]] = []
        for e in self.facts.experience:
            for b in e["bullets"]:
                txt, sc = tailor.best_variant(b.variants, self.jd)
                cands.append((sc + 0.01 * b.weight, txt))
        for p in self.facts.projects:
            for b in p["bullets"]:
                txt, sc = tailor.best_variant(b.variants, self.jd)
                cands.append((sc + 0.01 * b.weight - 0.2, txt))
        cands.sort(key=lambda x: -x[0])
        return [t for _, t in cands[:n]]

    def _shared_terms(self, n: int = 4) -> List[str]:
        ranked = sorted(self.jd.weights.items(), key=lambda kv: -kv[1])
        return [t for t, _ in ranked if t in self.facts_terms][:n]

    def _digest(self) -> str:
        f = self.facts
        lines = [f"Name: {f.contact.get('name', '')}", f"Education: {f.education.get('degree', '')}, {f.education.get('school', '')}, "
                 f"{f.education.get('dates', '')}, {f.education.get('detail', '')}"]
        lines.append("Summary: " + f.summaries[0]["variants"][0])
        lines.append("Most relevant true experience:")
        lines += ["- " + b for b in self._bullets(5)]
        lines.append("Skills: " + ", ".join(f.all_skill_items()[:30]))
        about = (self.cfg.get("profile") or {}).get("about")
        if about:
            lines.append("Also: " + str(about))
        lines.append(f"Available for full-time from: {self.prof.get('earliest_start') or 'June 2027'}")
        return "\n".join(lines)

    # ---- LLM with guard, else template ----
    def _ask_llm(self, system: str, user: str, max_tokens: int = 700) -> Optional[str]:
        if self._llm is None or not self._llm.model:
            return None
        try:
            return self._llm.chat(system, user, max_tokens=max_tokens, temperature=0.4).strip()
        except Exception as e:
            log.info("draft LLM call failed: %s", e)
            return None

    SYSTEM = ("You write application answers for a student applicant, in the first person, in plain professional English. "
              "Use ONLY the facts provided. Never invent employers, projects, technologies, numbers, dates or achievements, "
              "and do not claim anything about the company beyond what the job text says. No flattery, no clichés such as "
              "'passionate' or 'dynamic', no em dashes, no bullet points. Output only the answer text.")

    def _guarded(self, system: str, user: str, maxlen: int, max_tokens: int = 700) -> Optional[Tuple[str, str]]:
        problems: List[str] = []
        for attempt in range(2):
            prompt = user if not problems else (user + "\n\nYour previous answer was rejected because it contained facts that "
                                                 "are not in the material above: " + "; ".join(problems[:5]) +
                                                 ". Rewrite it without them.")
            out = self._ask_llm(system, prompt, max_tokens)
            if not out:
                return None
            out = re.sub(r"^[\"'`]+|[\"'`]+$", "", out).replace("—", ",").replace("–", "-").strip()
            problems = guard.violations(_sentences_lower_start(out), self.allowed)
            if not problems:
                fitted = fit(out, maxlen)
                if fitted:
                    return fitted, self._llm.name if self._llm else "llm"
                problems = ["too long for the field"]
        return None

    # ---- template fallbacks (always safe: built only from facts) ----
    def _template(self, kind: str) -> str:
        f = self.facts
        summary = f.summaries[0]["variants"][0]
        bullets = self._bullets(3)
        shared = self._shared_terms()
        start = self.prof.get("earliest_start") or "June 2027"
        school = f.education.get("school", "").split("(")[0].strip()
        edu = f"final-year {f.education.get('degree', 'B.Tech')} student at {school}".replace("B.Tech, ", "B.Tech ")
        if kind == "about":
            return summary
        if kind == "project":
            return " ".join(bullets[:2])
        stack = (" Its focus on " + ", ".join(shared[:3]) + " matches the work I have been doing.") if shared else ""
        if kind == "hire":
            return (f"I am a {edu}. {bullets[0]} {bullets[1] if len(bullets) > 1 else ''}".strip() +
                    f" I can join full-time from {start}.")
        return (f"I am a {edu}, and my recent work is hands-on backend engineering. {bullets[0]} "
                f"The {self.title} role at {self.company} is the kind of work I want to do full-time.{stack} "
                f"I am available to join from {start}.")

    # ---- public: a draft for one question ----
    def draft(self, label: str, maxlen: int = 0) -> Optional[Tuple[str, str]]:
        kind = kind_of(label)
        if not kind:
            return None
        key = f"{kind}:{maxlen}"
        if key in self._answers:
            return self._answers[key]
        limit = maxlen or 1100
        words = max(40, min(150, limit // 7))
        user = (f"Question on the application form: {label}\n"
                f"Job: {self.title} at {self.company}\n"
                f"Job description (excerpt):\n{(self.job.get('description') or '')[:1800]}\n\n"
                f"Facts about the applicant:\n{self._digest()}\n\n"
                f"Write the answer in at most {words} words.")
        res = self._guarded(self.SYSTEM, user, limit)
        if not res:
            text = fit(self._template(kind), limit)
            if not text:
                return None
            res = (text, "template")
        self._answers[key] = res
        return res

    # ---- public: cover letter ----
    def cover_letter(self) -> Optional[Tuple[str, str, str]]:
        """(text, pdf_path, how). The PDF is written next to the resume."""
        if self._cover:
            return self._cover
        f = self.facts
        name = f.contact.get("name", "")
        user = (f"Write a cover letter body for the {self.title} role at {self.company}. Three short paragraphs, at most 190 words "
                f"in total, no greeting and no sign-off.\nJob description (excerpt):\n{(self.job.get('description') or '')[:2200]}\n\n"
                f"Facts about the applicant:\n{self._digest()}")
        res = self._guarded(self.SYSTEM, user, 1800, max_tokens=900)
        if res:
            body, how = res
        else:
            how = "template"
            bullets = self._bullets(3)
            shared = self._shared_terms()
            start = self.prof.get("earliest_start") or "June 2027"
            school = f.education.get("school", "").split("(")[0].strip()
            p1 = (f"I am applying for the {self.title} role at {self.company}. I am a final-year {f.education.get('degree', 'B.Tech')} "
                  f"student at {school}, graduating in 2027, and I can join full-time from {start}.")
            p2 = " ".join(bullets[:2])
            p3 = ("The role's focus on " + ", ".join(shared[:3]) + " matches the work above. " if shared else "") + \
                 f"I would welcome the chance to discuss how I can contribute to {self.company}."
            body = "\n\n".join([p1, p2, p3])
        contact = " | ".join(x for x in [self.prof.get("phone_full"), self.prof.get("email")] if x)
        text = f"Dear Hiring Team at {self.company},\n\n{body}\n\nSincerely,\n{name}\n{contact}"
        pdf_path = ""
        if self.out_dir:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^A-Za-z0-9]+", "_", self.company).strip("_")[:40] or "Company"
            p = self.out_dir / f"Krish_Wadhwa_Cover_Letter_{slug}.pdf"
            p.write_bytes(build_cover_pdf(text, name))
            pdf_path = str(p)
        self._cover = (text, pdf_path, how)
        return self._cover


def build_cover_pdf(text: str, name: str = "") -> bytes:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    st = ParagraphStyle("c", fontName=render.FONT, fontSize=10.5, leading=14.5, alignment=TA_LEFT, spaceAfter=8)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=1 * inch, rightMargin=1 * inch, topMargin=1 * inch,
                            bottomMargin=1 * inch, title=f"{name} - Cover letter", author=name)
    flow: List[Any] = []
    for para in text.split("\n\n"):
        flow.append(Paragraph(render._esc(para).replace("\n", "<br/>"), st))
    doc.build(flow)
    return buf.getvalue()
