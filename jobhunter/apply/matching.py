"""Pure logic: given a form question, decide what to answer. No browser here, so it is easy to test."""
from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .profile import Profile


@dataclass
class Question:
    id: str                          # selector token assigned by the page script
    kind: str                        # text | textarea | select | radio | checkbox | file | combobox
    label: str
    options: List[Dict[str, str]] = field(default_factory=list)     # [{label, id}]
    required: bool = False
    input_type: str = ""             # email, tel, number, date, url ...
    name: str = ""
    placeholder: str = ""
    value: str = ""
    frame: int = 0
    maxlength: int = 0


@dataclass
class Answer:
    action: str                      # fill | choose | check | upload | skip | ask
    value: str = ""
    key: str = ""
    note: str = ""
    option: Optional[int] = None     # index into question.options when choosing


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[\*✱∗]", " ", s)
    s = re.sub(r"\((required|optional)\)", " ", s)
    s = re.sub(r"[^a-z0-9+@./ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# (key, regex) in priority order: first match wins. Regexes run on the normalised label.
FIELD_PATTERNS: List[Tuple[str, str]] = [
    ("first_name", r"\b(first|given) name\b|\bforename\b"),
    ("last_name", r"\b(last|family) name\b|\bsurname\b"),
    ("full_name", r"^(your |full |legal |candidate |applicant )*name$|\b(full|legal) name\b|^your name\b"),
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile|contact number|telephone|whatsapp"),
    ("linkedin", r"linked ?in"),
    ("github", r"git ?hub"),
    ("portfolio", r"portfolio|personal (web)?site|^website$|\bwebsite\b|\bblog\b|other (link|url)|\burl\b"),
    ("pincode", r"pin ?code|postal code|zip"),
    ("city", r"^city\b|\bcity\b|\btown\b"),
    ("state", r"^state\b|\bstate\b|province"),
    ("country", r"\bcountry\b|nationality"),
    ("location", r"current location|where are you (currently )?(located|based)|\blocation\b|\bresiden"),
    ("current_company", r"current (company|employer|organi[sz]ation)|present (company|employer)|\bemployer\b"),
    ("current_title", r"current (title|role|position|designation)|job title|\bdesignation\b"),
    ("school", r"\b(school|college|university|institution|institute)\b"),
    ("degree", r"\bdegree\b|qualification"),
    ("field", r"\bmajor\b|field of study|discipline|\bbranch\b|\bstream\b|specialisation|specialization"),
    ("grad_month", r"graduation month|month of (graduation|passing)|passing month"),
    ("grad_year", r"graduation (year|date)|year of (graduation|passing)|passing (out )?year|\bbatch\b|expected graduation"),
    ("cgpa", r"\bcgpa\b|\bgpa\b|percentage|\bgrade\b|aggregate"),
    ("current_ctc", r"current (ctc|salary|compensation|pay|package)|present (ctc|salary)"),
    ("expected_ctc", r"expected (ctc|salary|compensation|pay|package)|salary expectation|desired (salary|compensation)|\bctc\b"),
    ("years_experience", r"years? of (relevant |total |work |professional |overall )?experience|total experience|experience in years|how many years"),
    ("start_date", r"start date|earliest (start|joining) date|date of joining|joining date|date available|available from|available to start"),
    ("notice_period", r"notice period|when can you (start|join)|earliest (start|joining|date)|available (to start|from|to join)|joining (date|time)|start date|how soon"),
    ("relocate", r"relocat"),
    ("sponsorship", r"sponsor|\bvisa\b|require .*work (permit|authori[sz]ation)"),
    ("work_authorization", r"authori[sz]ed to work|legally (authori[sz]ed|permitted|eligible)|right to work|work authori[sz]ation|eligible to work|permitted to work"),
    ("referral", r"referred by|employee referral|referral|know anyone|friend or family"),
    ("previously_applied", r"previously (worked|employed|applied)|former (employee|intern)|(have you|did you) (ever )?(worked|applied|been employed|interviewed)|worked (for|at|with) .* before"),
    ("over_18", r"at least 18|over 18|18 years|legal age"),
    ("how_heard", r"how did you (hear|find|learn|come)|where did you (hear|find|learn)|\bsource\b|how do you know"),
]

DEMOGRAPHIC = re.compile(
    r"\bgender\b|\bsex\b|\brace\b|ethnic|veteran|disabilit|pronoun|sexual orientation|transgender|lgbt|caste|"
    r"religio|hispanic|latino|marital|date of birth|\bdob\b|self.?identif")
CONSENT = re.compile(
    r"\b(i )?(agree|accept|consent|acknowledge|certify|confirm|authori[sz]e|understand|declare)\b|privacy|terms|"
    r"gdpr|data (processing|protection)|read and|talent (community|pool)|keep me (informed|updated)|"
    r"receive (emails?|updates|communications?)")
DECLINE_OPTION = re.compile(
    r"decline|prefer not|do ?n.?t wish|do not wish|not (to )?(say|answer|disclose|identify|specified)|rather not|"
    r"choose not|no answer|undisclosed|not applicable|n/a")
PLACEHOLDER_OPTION = re.compile(r"^(select|choose|please select|please choose|--+|-|pick|none)\b.*$|^$")
YES_NO_KEYS = {"relocate", "sponsorship", "work_authorization", "referral", "previously_applied", "over_18"}


DATE_HINTS = [
    (r"dd ?/ ?mm ?/ ?yyyy", "%d/%m/%Y"), (r"dd ?- ?mm ?- ?yyyy", "%d-%m-%Y"), (r"mm ?/ ?dd ?/ ?yyyy", "%m/%d/%Y"),
    (r"mm ?- ?dd ?- ?yyyy", "%m-%d-%Y"), (r"yyyy ?- ?mm ?- ?dd", "%Y-%m-%d"), (r"dd ?\. ?mm ?\. ?yyyy", "%d.%m.%Y"),
    (r"mm ?/ ?yyyy", "%m/%Y"), (r"mm ?- ?yyyy", "%m-%Y"), (r"mmm ?,? ?yyyy", "%b %Y"), (r"month ?,? ?yyyy", "%B %Y"),
]
DATE_KEYS = {"start_date", "notice_period", "grad_year"}


def date_format(q: "Question") -> Optional[str]:
    """strftime format a date box expects, from its type or its placeholder/label hint; None if it is not a date box."""
    if q.input_type == "date":
        return "%Y-%m-%d"
    if q.input_type == "month":
        return "%Y-%m"
    hint = f"{q.placeholder} {q.label}".lower()
    for pat, fmt in DATE_HINTS:
        if re.search(pat, hint):
            return fmt
    return None


def _date_value(key: str, fmt: str, prof: Profile) -> str:
    iso = prof.get("start_date_iso") if key in ("start_date", "notice_period") else prof.get("grad_date_iso")
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime(fmt)
    except (ValueError, TypeError):
        return ""


def _custom_rule(label: str, prof: Profile) -> Optional[str]:
    for r in prof.rules:
        try:
            if re.search(r["match"], label, re.I):
                return r["answer"]
        except re.error:
            continue
    return None


def lookup(label: str, prof: Profile) -> Tuple[str, str]:
    """(key, value) for a question label; key '' when unknown."""
    label = norm(label)
    custom = _custom_rule(label, prof)
    if custom is not None:
        return "custom", custom
    for key, pat in FIELD_PATTERNS:
        if re.search(pat, label):
            return key, prof.get(key)
    return "", ""


def choose_option(key: str, value: str, options: List[str]) -> Optional[int]:
    """Index of the option that best matches, or None. Placeholder entries ('Select...') are never chosen."""
    texts = [norm(o) for o in options]
    real = [i for i, t in enumerate(texts) if not PLACEHOLDER_OPTION.match(t)]
    if not real:
        return None
    if key == "decline":
        for i in real:
            if DECLINE_OPTION.search(texts[i]):
                return i
        return None
    v = norm(value)
    if not v:
        return None
    if v in ("yes", "no"):
        for i in real:                       # "Yes", "Yes, I am authorised", "No, I do not need sponsorship"
            if re.match(rf"^{v}\b", texts[i]):
                return i
        return None
    for i in real:
        if texts[i] == v:
            return i
    if key == "country" or key == "phone_country":
        for i in real:
            if "india" in texts[i] or "+91" in texts[i]:
                return i
    if key == "degree":
        for i in real:
            if re.search(r"bachelor|b\.? ?tech|b\.? ?e\b|undergrad", texts[i]):
                return i
    if key == "how_heard":
        for pref in (r"career|company (web)?site|official", r"linkedin", r"job (board|portal)|portal", r"other"):
            for i in real:
                if re.search(pref, texts[i]):
                    return i
        return None
    if key == "notice_period":
        for i in real:
            if re.search(r"immediate|serving|0.?15|less than|fresher|not applicable|na\b", texts[i]):
                return i
        return None
    if key == "years_experience":
        for i in real:
            if re.search(r"^0\b|fresher|none|less than 1|0.?1|entry", texts[i]):
                return i
        return None
    if key in ("grad_year", "cgpa"):
        for i in real:
            if v in texts[i]:
                return i
        return None
    for i in real:                           # containment either way, on whole words
        t = texts[i]
        if len(v) >= 3 and (re.search(rf"\b{re.escape(v)}\b", t) or (len(t) >= 3 and re.search(rf"\b{re.escape(t)}\b", v))):
            return i
    return None


def resolve(q: Question, prof: Profile) -> Answer:
    label = norm(q.label) or norm(q.placeholder) or norm(q.name)
    both = f"{label} {norm(q.name)}"

    if re.search(r"cover letter|covering letter|letter of motivation|motivation letter", both):
        if q.kind == "file":
            return Answer("cover_upload", key="cover_letter")
        if q.kind in ("textarea", "text"):
            return Answer("cover_text", key="cover_letter")
    if q.kind == "file":
        if re.search(r"resume|\bcv\b|curriculum", both):
            return Answer("upload", key="resume")
        return Answer("ask" if q.required else "skip", note="file upload")

    if q.kind == "checkbox":
        if CONSENT.search(label) or not label:
            return Answer("ask" if q.required else "skip", note="consent / terms checkbox left for you")
        key, value = lookup(label, prof)
        if key and norm(value) == "yes":
            return Answer("check", value, key)
        return Answer("ask" if q.required else "skip", note="checkbox")

    if DEMOGRAPHIC.search(label) and q.kind in ("select", "radio", "combobox"):
        idx = choose_option("decline", "", [o["label"] for o in q.options])
        if idx is not None:
            return Answer("choose", q.options[idx]["label"], "decline", option=idx)
        return Answer("ask" if q.required else "skip", note="demographic question, no 'prefer not to say' option")
    if DEMOGRAPHIC.search(label):
        return Answer("ask" if q.required else "skip", note="demographic question")

    key, value = lookup(label, prof)
    if key == "start_date" and not value:
        value = prof.get("earliest_start")
    if key in DATE_KEYS and q.kind in ("text", "combobox"):
        fmt = date_format(q)
        if fmt:
            dv = _date_value(key, fmt, prof)
            if dv:
                return Answer("fill", dv, key, note="date")
            return Answer("ask" if q.required else "skip", key=key, note="date field and no date saved")
    if not key:
        return Answer("ask" if q.required else "skip", note="no saved answer")
    if key == "current_ctc" and not value:
        return Answer("ask" if q.required else "skip", key=key, note="current CTC is not saved; fill it yourself")
    if not value:
        return Answer("ask" if q.required else "skip", key=key, note=f"'{key}' is empty in application_answers.yaml")

    if q.kind in ("select", "radio", "combobox"):
        idx = choose_option(key, value, [o["label"] for o in q.options]) if q.options else None
        if idx is None and q.kind == "combobox":
            return Answer("fill", value, key)            # let the page type it and pick the first suggestion
        if idx is None:
            return Answer("ask" if q.required else "skip", key=key, note=f"no option matches '{value}'")
        return Answer("choose", q.options[idx]["label"], key, option=idx)

    # text-like
    if q.input_type == "number" and not re.fullmatch(r"[\d.]+", value):
        return Answer("ask" if q.required else "skip", key=key, note="numeric field; saved answer is text")
    if q.input_type == "date":
        return Answer("ask" if q.required else "skip", key=key, note="date field")
    return Answer("fill", value, key)
