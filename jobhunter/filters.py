"""Decide whether a job is relevant, what level it is, and how well it fits.

Design: *level* is computed for every India software job and stored. Which levels you
actually want is a setting (config.yaml targets.levels), so adding SDE-2 / SDE-3 later is a
config change plus `make reevaluate`, not a rewrite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import catalogue, config

INDIA_CITIES = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "navi mumbai", "thane", "delhi",
    "new delhi", "gurgaon", "gurugram", "noida", "greater noida", "ghaziabad", "faridabad", "chennai",
    "kolkata", "ahmedabad", "gandhinagar", "gift city", "jaipur", "chandigarh", "mohali", "panchkula",
    "kochi", "cochin", "trivandrum", "thiruvananthapuram", "coimbatore", "indore", "lucknow", "vadodara",
    "bhubaneswar", "nagpur", "mysuru", "mysore", "visakhapatnam", "vizag", "surat", "kanpur", "dehradun",
    "karnataka", "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh", "gujarat", "kerala",
]
# Short tokens that need word-boundary matching so "pune" doesn't match inside other words.
_INDIA_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(c) for c in INDIA_CITIES) + r")(?![a-z])", re.I)
_NOT_INDIA_RE = re.compile(r"\b(indiana|indianapolis|new york|united states|usa|\bus\b|uk|london|singapore|"
                           r"dublin|toronto|berlin|sydney)\b", re.I)
_MULTI_LOC_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
_REMOTE_RE = re.compile(r"\bremote\b", re.I)

# ---- level detection from the title ----
_T_INTERN = re.compile(r"\b(intern|interns|internship|trainee|apprentice|co-?op)\b", re.I)
_T_STAFF = re.compile(r"\b(staff|principal|distinguished|fellow|architect)\b", re.I)
_T_SENIOR = re.compile(r"\b(senior|sr\.?|lead)\b", re.I)
_T_ENTRY_WORDS = re.compile(
    r"\b(new\s*grad(uate)?s?|graduate|university|campus|fresher|junior|jr\.?|entry[- ]level|early[- ]career|"
    r"associate (software|engineer|developer|member of technical)|college|uhr)\b", re.I)
_ROLE_WORD = r"(?:sde|swe|software(?:\s+development)?\s+engineer|engineer|developer|mts|member\s+of\s+technical\s+staff|technical\s+staff|ase)"
_T_LEVEL_NUM = re.compile(rf"\b{_ROLE_WORD}[\s\-,:(]*(?:level\s*)?(i{{1,3}}|iv|v|1|2|3|4|5)\b(?!\.\d)", re.I)
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}


# ---- general (non-software) levels: intern, entry, mid, senior, lead ----
_G_ENTRY_PROGRAMME = re.compile(r"\b(management|graduate|executive|associate|officer|operations)\s+trainee\b", re.I)
_G_INTERN = re.compile(r"\b(intern|interns|internship|apprentice|co-?op|trainee|summer analyst)\b", re.I)
_G_LEAD = re.compile(r"\b(head|director|vp|avp|vice president|chief|principal|general manager)\b", re.I)
_G_SENIOR = re.compile(r"\b(senior|sr\.?|team lead|lead|staff)\b", re.I)
_G_SENIOR_ENTRYISH = re.compile(r"\bsenior\s+(executive|associate)\b", re.I)
_G_MID = re.compile(r"\b(manager|specialist ii|level (2|ii)|ii)\b", re.I)
_G_ENTRY = re.compile(r"\b(associate|executive|junior|jr\.?|assistant|fresher|freshers|graduate|entry[- ]level|early[- ]career|new\s*grad|"
                      r"coordinator|campus|officer|agent|representative|writer|editor|tutor|trainer)\b", re.I)


def level_from_title_general(title: str) -> Optional[str]:
    t = title or ""
    if _G_ENTRY_PROGRAMME.search(t):
        return "entry"
    if _G_INTERN.search(t):
        return "intern"
    if _G_LEAD.search(t):
        return "lead"
    if _G_SENIOR_ENTRYISH.search(t):
        return "mid"
    if _G_SENIOR.search(t):
        return "senior"
    if _G_MID.search(t):
        return "mid"
    if _G_ENTRY.search(t):
        return "entry"
    return None


def level_from_title(title: str, scheme: str = "software") -> Optional[str]:
    if scheme == "general":
        return level_from_title_general(title)
    t = title or ""
    if _T_INTERN.search(t):
        return "intern"
    if _T_STAFF.search(t):
        return "staff"
    if _T_SENIOR.search(t):
        return "sde3"
    m = _T_LEVEL_NUM.search(t)
    if m:
        tok = m.group(1).lower()
        n = int(tok) if tok.isdigit() else _ROMAN.get(tok, 0)
        return {1: "sde1", 2: "sde2", 3: "sde3", 4: "staff", 5: "staff"}.get(n)
    if _T_ENTRY_WORDS.search(t):
        return "sde1"
    return None


# ---- experience years from the description ----
_YEARS_RANGE = re.compile(r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_YEARS_PLUS = re.compile(r"(?:minimum of|at least|min\.?)?\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I)
_EXP_WORD = re.compile(r"experience|exp\b|working|professional|industry|developing|building|hands-on|software", re.I)
_NON_INTERN = re.compile(r"non[- ]?internship|excluding internships?|not including internships?", re.I)


def years_required(text: str) -> Tuple[Optional[int], bool]:
    """Return (lower bound of the largest experience requirement found, non_internship_flag)."""
    if not text:
        return None, False
    best: Optional[int] = None
    non_intern = False
    range_spans: List[Tuple[int, int]] = []
    hits: List[Tuple[int, int, int]] = []          # (lo, start, end)
    for m in _YEARS_RANGE.finditer(text):
        range_spans.append((m.start(), m.end()))
        hits.append((int(m.group(1)), m.start(), m.end()))
    for m in _YEARS_PLUS.finditer(text):
        if any(a <= m.start() < b or a < m.end() <= b for a, b in range_spans):
            continue                                  # the "2 years" inside "0-2 years"
        hits.append((int(m.group(1)), m.start(), m.end()))
    for lo, start, end in hits:
        ctx = text[max(0, start - 70): end + 70]
        if not _EXP_WORD.search(ctx) or lo > 20:
            continue
        if best is None or lo > best:
            best = lo
        if _NON_INTERN.search(ctx) and lo >= 1:
            non_intern = True
    return best, non_intern


def level_from_years(years: Optional[int], bands: Dict[str, List[int]]) -> Optional[str]:
    if years is None:
        return None
    for lvl in list(bands.keys()):
        lo, hi = bands.get(lvl, [99, 99])
        if lo <= years <= hi:
            return lvl
    return None


# ---- batch / joining signals ----
_BATCH_CTX = re.compile(r"(batch|graduat|pass(?:ing|ed)?[\s-]*out|class of|grads?\b|year of passing|joining|"
                        r"campus|new grad|final[- ]year|fy\s?\d{2}|pursuing)", re.I)
_YEAR = re.compile(r"\b(20[2-3]\d)\b")
_YEAR_RANGE = re.compile(r"\b(20[2-3]\d)\s*(?:-|–|—|to)\s*(20[2-3]\d)\b")
_FY27 = re.compile(r"\bFY\s?-?27\b", re.I)
_IMMEDIATE = re.compile(r"immediate(?:ly)?[\s-]*(?:joiner|join)|join(?:ing)?\s+immediately|available\s+to\s+join\s+(?:immediately|asap)|"
                        r"notice period\s+(?:of\s+)?(?:up to\s+)?\d+|within\s+(?:15|30|45|60)\s+days", re.I)
_RETURN_TO_DEGREE = re.compile(r"(return|returning)\s+to\s+(?:your\s+)?(?:degree|school|university|studies)|"
                               r"at least (?:1|one) (?:semester|term) (?:left|remaining)", re.I)


def batch_signals(title: str, text: str, grad_year: int) -> Dict[str, Any]:
    """Which graduation years does the posting accept?"""
    blob = f"{title}\n{text}"
    years: Set[int] = set()
    for m in re.finditer(r"[^.\n]{0,120}", blob):
        seg = m.group(0)
        if not seg or not _BATCH_CTX.search(seg):
            continue
        for a, b in _YEAR_RANGE.findall(seg):
            lo, hi = sorted((int(a), int(b)))
            if hi - lo <= 8:
                years.update(range(lo, hi + 1))
        for y in _YEAR.findall(seg):
            years.add(int(y))
    if _FY27.search(blob):
        years.add(2027)
    return {
        "years": sorted(years),
        "accepts_grad_year": (grad_year in years) if years else None,
        "immediate_joiner": bool(_IMMEDIATE.search(blob)),
        "return_to_degree": bool(_RETURN_TO_DEGREE.search(blob)),
    }


# ---- location ----
_WORLDWIDE_RE = re.compile(r"\b(worldwide|anywhere|global(ly)?|apac|asia|work from (home|anywhere)|wfh|fully remote|remote[- ]first)\b", re.I)


def india_status(location: str, include_remote_india: bool = True, mode: str = "india") -> Optional[bool]:
    """True = acceptable location, False = clearly elsewhere, None = unknown / ambiguous.
    mode: india (India only), india_or_remote (India plus worldwide remote roles), anywhere (no location filter)."""
    if mode == "anywhere":
        return True
    loc = (location or "").strip()
    if not loc:
        return None
    if _MULTI_LOC_RE.match(loc):
        return None
    if _INDIA_RE.search(loc):
        return True
    if mode == "india_or_remote" and (_REMOTE_RE.search(loc) or _WORLDWIDE_RE.search(loc)):
        if _WORLDWIDE_RE.search(loc) and not _NOT_INDIA_RE.search(loc):
            return True
        return None if not _NOT_INDIA_RE.search(loc) else False
    if _REMOTE_RE.search(loc) and not _NOT_INDIA_RE.search(loc):
        return None if include_remote_india else False
    return False


# ---- config-driven evaluation ----
@dataclass
class Evaluation:
    in_scope: bool = False
    level: str = ""
    verdict: str = "reject"     # match | maybe | reject
    score: int = 0
    reasons: List[str] = field(default_factory=list)
    flags: Dict[str, Any] = field(default_factory=dict)
    needs_detail: bool = False  # worth fetching the full description


class Evaluator:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.cfg = cfg
        roles = cfg.get("roles", {})
        self.scheme_name = config.scheme_name(cfg)
        self.scheme = config.level_scheme(cfg)
        extra_ex = list(roles.get("exclude_extra") or [])
        if roles.get("core"):                                   # hand-written regexes in config.yaml (the default profile)
            self.core = re.compile(roles["core"], re.I)
            self.exclude = re.compile(roles["exclude"], re.I) if roles.get("exclude") else None
            if roles.get("include"):
                self.core = re.compile(f"(?:{roles['core']})|" + "|".join(re.escape(k) for k in roles["include"]), re.I)
        else:                                                   # built from the role families chosen in Settings
            fams = list(roles.get("families") or [])
            if not fams and not roles.get("include"):
                fams = [f["id"] for f in catalogue.families(self.scheme_name)]
            rx = catalogue.compile_roles(fams, list(roles.get("include") or []), extra_ex)
            self.core = rx["core"] or re.compile(r"$^")
            self.exclude = rx["exclude"]
        if roles.get("core") and extra_ex:
            more = "|".join(re.escape(k) for k in extra_ex)
            self.exclude = re.compile((self.exclude.pattern + "|" if self.exclude else "") + more, re.I)
        self.adjacent = re.compile(roles.get("adjacent", "$^"), re.I) if roles.get("adjacent") else None
        t = cfg["targets"]
        self.targets: Set[str] = set(t["levels"])
        self.batch_rules: bool = bool(t.get("apply_batch_rules", True))
        self.max_entry_years: int = int(t.get("max_years_for_entry", 1))
        self.bands: Dict[str, List[int]] = t.get("years_to_level", {})
        self.grad_year: int = int(cfg.get("profile", {}).get("grad_year", 2027))
        loc = cfg.get("locations", {})
        self.include_remote = bool(loc.get("include_remote_india", True))
        self.loc_mode = loc.get("mode", "india")
        self.only_cities = [c.lower() for c in loc.get("only_cities", [])]
        from . import languages as _lang
        self.langs = _lang.Checker((cfg.get("languages") or {}).get("known") or ["english"])
        self.entry_levels = set(self.scheme["entry"])
        self.main_entry = self.scheme["main_entry"]

    # Stage 1: cheap, from list data only (no description needed)
    def prefilter(self, title: str, location: str) -> Evaluation:
        ev = Evaluation()
        if self.exclude and self.exclude.search(title):
            ev.reasons.append("role excluded")
            return ev
        if not self.core.search(title):
            ev.reasons.append("not one of your role types")
            return ev
        lang = self.langs.in_title(title)
        if lang:
            ev.reasons.append(f"needs {lang}")
            return ev
        ind = india_status(location, self.include_remote, self.loc_mode)
        if ind is False:
            ev.reasons.append("outside your locations")
            return ev
        if ind and self.loc_mode != "anywhere" and self.only_cities and not any(c in location.lower() for c in self.only_cities) \
                and "india" not in location.lower():
            ev.reasons.append("city not in your list")
            return ev
        ev.in_scope = True
        ev.flags["india"] = ind
        ev.level = level_from_title(title, self.scheme_name) or ""
        # Titles already clearly outside targets and without level ambiguity do not need a detail fetch.
        ev.needs_detail = (ev.level == "" or ev.level in self.targets)
        return ev

    # Stage 2: full evaluation, with whatever description we have
    def evaluate(self, title: str, location: str, description: str, tier: str = "") -> Evaluation:
        ev = self.prefilter(title, location)
        if not ev.in_scope:
            return ev
        reasons: List[str] = []
        lang = self.langs.in_description(description)
        if lang:
            ev.in_scope, ev.verdict = True, "reject"
            ev.reasons = [f"asks for {lang}"]
            ev.flags["language"] = lang
            return ev
        years, non_intern = years_required(description)
        level = ev.level or level_from_years(years, self.bands) or "unknown"
        level_source = "title" if ev.level else ("experience" if level != "unknown" else "none")
        assumed = False
        if level == "unknown":
            # Many titles ("Software Engineer", "Chess Coach") never state a level. With no sign of seniority in the
            # title or the experience asked for, they are normally entry roles, so show them rather than hide them.
            level, level_source, assumed = self.main_entry, "assumed", True
        ev.level = level
        ev.flags.update({"years": years, "non_internship_years": non_intern, "level_source": level_source})
        batch = batch_signals(title, description, self.grad_year)
        ev.flags["batch"] = batch

        score = 50
        adjacent = bool(self.adjacent and self.adjacent.search(title)) and not re.search(
            r"software|sde|swe|developer|member of technical staff|\bmts\b", title, re.I)
        if adjacent:
            score -= 10
            reasons.append("adjacent role (not pure software)")
        if ev.flags.get("india") is None:
            reasons.append("location unclear")
            score -= 5

        if level == "unknown":
            verdict = "maybe"
            score -= 10
            reasons.append("level unclear")
        else:
            verdict = "match"
            reasons.append("no level stated: treated as entry level" if assumed
                           else f"{config.LEVEL_LABELS.get(level, level)} from {level_source}")
            if assumed:
                score -= 5
            if level in self.entry_levels:
                score += 15
        ev.flags["target"] = level in self.targets          # alerts and the default view use this; the verdict does not

        if level in self.entry_levels:
            if self.batch_rules:
                if batch["accepts_grad_year"] is True:
                    score += 30
                    reasons.append(f"accepts {self.grad_year} graduates")
                elif batch["accepts_grad_year"] is False:
                    verdict = "reject"
                    reasons.append("batch %s only" % "/".join(str(y) for y in batch["years"]))
                if batch["immediate_joiner"]:
                    score -= 15
                    reasons.append("may want an immediate joiner")
                if level == "intern" and batch["return_to_degree"]:
                    score -= 10
                    reasons.append("must return to degree after (likely a later batch)")
            if years is not None and level == self.main_entry:
                if years > self.max_entry_years or (non_intern and years >= 1):
                    if batch["accepts_grad_year"] is not True:
                        verdict = "maybe" if verdict == "match" else verdict
                        reasons.append(f"asks {years}+ yrs{' non-internship' if non_intern else ''}")
                        score -= 15
                else:
                    score += 5
        elif years is not None:
            reasons.append(f"asks {years}+ yrs")

        tier_bonus = {"T1": 8, "T2": 4}.get((tier or "").upper(), 0)
        score += tier_bonus
        ev.score = max(0, min(100, score))
        ev.verdict = verdict
        ev.reasons = reasons
        return ev
