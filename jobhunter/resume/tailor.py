"""Builds a tailored resume for one job from resume_facts.yaml, scores it, and renders PDF + DOCX."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import ats, guard, jd as jdmod, llm as llmmod, render
from . import terms as T
from .facts import Bullet, Facts, load_facts
from .render import Entry, ResumeDoc

log = logging.getLogger("jobhunter.resume")

DEFAULT_SUFFIX = "B.Tech CSE '27"      # software profile default; set headline_suffix in resume_facts.yaml to change
TARGET_SCORE = 95.0
MAX_EXP_BULLETS = 6
MAX_PROJECTS = 3
MAX_ACHIEVEMENTS = 3


@dataclass
class Item:
    key: str
    kind: str                    # exp | proj | ach
    group: int                   # index of experience entry / project
    text: str
    prio: float
    forced: bool = False
    dropped: bool = False
    source: Optional[Bullet] = None
    original: str = ""
    rewritten: bool = False


@dataclass
class Plan:
    facts: Facts
    jd: jdmod.JD
    items: List[Item] = field(default_factory=list)
    summary_id: str = ""
    summary_text: str = ""
    summary_original: str = ""
    summary_rewritten: bool = False
    headline: str = ""
    project_order: List[int] = field(default_factory=list)
    project_dropped: Set[int] = field(default_factory=set)
    scale: float = 1.0


@dataclass
class TailorResult:
    ok: bool = False
    error: str = ""
    score: float = 0.0
    target_reached: bool = False
    ats: Dict[str, Any] = field(default_factory=dict)
    pdf: bytes = b""
    docx: bytes = b""
    pages: int = 0
    doc: Optional[ResumeDoc] = None
    notes: List[str] = field(default_factory=list)
    gaps_unclaimable: List[str] = field(default_factory=list)
    llm: Dict[str, Any] = field(default_factory=dict)
    jd_terms: Dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------------- relevance
def rel(text: str, jd: jdmod.JD) -> float:
    have = T.term_set(text)
    low = text.lower()
    r = sum(w for t, w in jd.weights.items() if t in have)
    r += sum(w for lit, w in jd.literals.items() if lit.lower() in low)
    return r


def best_variant(variants: List[str], jd: jdmod.JD) -> Tuple[str, float]:
    best, best_r = variants[0], rel(variants[0], jd)
    for v in variants[1:]:
        r = rel(v, jd)
        if r > best_r + 1e-9:
            best, best_r = v, r
    return best, best_r


_LEVEL_WORDS = {"i", "ii", "iii", "iv", "intern", "internship", "trainee", "junior", "jr", "sr", "senior", "associate", "entry",
                "level", "fresher", "freshers", "graduate", "grad", "new", "remote", "india", "wfh", "work", "from", "home",
                "full", "part", "time", "contract", "freelance", "2025", "2026", "2027"}
_SMALL = {"and", "of", "the", "for", "in", "to", "a", "an", "&"}


def clean_title(title: str) -> str:
    """'Content Writer (Remote) - Bengaluru' -> 'Content Writer'. Keeps the words that name the role."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title or "")
    t = re.split(r"\s[-–—|,:]\s|\s@\s|\sat\s", t)[0]
    words = [w for w in re.split(r"\s+", t.strip()) if w and w.lower() not in _LEVEL_WORDS]
    out = [w if (w.isupper() and len(w) <= 4) else (w.lower() if w.lower() in _SMALL else w[:1].upper() + w[1:]) for w in words]
    if out and out[0].lower() in _SMALL:
        out[0] = out[0].capitalize()
    return " ".join(out[:6])


def pick_headline(jd: jdmod.JD, facts: Optional[Facts] = None) -> str:
    suffix = ((facts.raw.get("headline_suffix") or "").strip() if facts else "")
    if T.ACTIVE == "general":
        label = clean_title(jd.title) or (facts.raw.get("default_headline") if facts else "") or "Content and Communications"
        if not suffix and facts:
            suffix = facts.education.get("degree", "")
        return f"{label} | {suffix}" if suffix else label
    toks = set(jd.title_tokens)
    title = " ".join(jd.title_tokens)
    if {"full", "stack"} & toks or "full-stack" in title or "fullstack" in title:
        label = "Full-Stack Software Engineer"
    elif "backend" in toks or "back-end" in title or "back" in toks:
        label = "Backend Software Engineer"
    elif "sde" in toks:
        label = "Software Development Engineer"
    elif "intern" in toks or "internship" in toks:
        label = "Software Engineering Intern"
    elif "developer" in toks and "engineer" not in toks:
        label = "Software Developer"
    else:
        label = "Software Engineer"
    return f"{label} | {suffix or DEFAULT_SUFFIX}"


# ----------------------------------------------------------------------------- plan
def make_plan(facts: Facts, jd: jdmod.JD) -> Plan:
    plan = Plan(facts, jd)
    plan.headline = pick_headline(jd, facts)
    # summary
    best_s, best_r, best_id = None, -1.0, ""
    for s in facts.summaries:
        text, r = best_variant(s["variants"], jd)
        if r > best_r:
            best_s, best_r, best_id = text, r, s["id"]
    plan.summary_id, plan.summary_text, plan.summary_original = best_id, best_s or "", best_s or ""
    # experience
    for gi, e in enumerate(facts.experience):
        for b in e["bullets"]:
            text, r = best_variant(b.variants, jd)
            plan.items.append(Item(f"exp:{gi}:{b.id}", "exp", gi, text, b.weight * 1.0 + 0.6 * r, False, False, b, text))
    # projects
    scored = []
    for pi, pr in enumerate(facts.projects):
        total = pr["weight"] + 0.2 * rel(" ".join(pr["stack"]), jd)
        for b in pr["bullets"]:
            text, r = best_variant(b.variants, jd)
            plan.items.append(Item(f"proj:{pi}:{b.id}", "proj", pi, text, b.weight * 1.0 + 0.6 * r, False, False, b, text))
            total += 0.6 * r + 0.3 * b.weight
        scored.append((total, pi))
    scored.sort(key=lambda t: -t[0])
    plan.project_order = [pi for _, pi in scored]
    plan.project_dropped = set(plan.project_order[MAX_PROJECTS:])
    # achievements
    for ai, a in enumerate(facts.achievements):
        text, r = best_variant(a["variants"], jd)
        plan.items.append(Item(f"ach:{ai}", "ach", ai, text, a["weight"] * 1.0 + 0.3 * r, False, False, None, text))
    # cap experience bullets
    exp_items = sorted([i for i in plan.items if i.kind == "exp"], key=lambda i: -i.prio)
    for it in exp_items[MAX_EXP_BULLETS:]:
        it.dropped = True
    ach_items = sorted([i for i in plan.items if i.kind == "ach"], key=lambda i: -i.prio)
    for it in ach_items[MAX_ACHIEVEMENTS:]:
        it.dropped = True
    return plan


def assemble(plan: Plan) -> ResumeDoc:
    f, jd = plan.facts, plan.jd
    c = f.contact
    contact = [c.get("location", ""), c.get("phone", ""), c.get("email", "")]
    links = [(l["label"], l["url"]) for l in c.get("links", [])]
    exp_entries: List[Entry] = []
    for gi, e in enumerate(f.experience):
        items = sorted([i for i in plan.items if i.kind == "exp" and i.group == gi and not i.dropped],
                       key=lambda i: -i.prio)
        exp_entries.append(Entry(f"{e['company']} — {e['title']}", e["dates"], e.get("location", ""),
                                 bullets=[i.text for i in items]))
    proj_entries: List[Entry] = []
    for pi in plan.project_order:
        if pi in plan.project_dropped:
            continue
        pr = f.projects[pi]
        items = sorted([i for i in plan.items if i.kind == "proj" and i.group == pi and not i.dropped],
                       key=lambda i: -i.prio)
        if not items:
            continue
        name, _ = best_variant(pr["names"], jd)
        stack = sorted(pr["stack"], key=lambda s: -(jd.weights.get(next(iter(T.term_set(s)), ""), 0)
                                                      if T.term_set(s) else 0))
        proj_entries.append(Entry(name, "", ", ".join(stack), [(l["label"], l["url"]) for l in pr.get("links", [])],
                                  [i.text for i in items]))
    skills = []
    for cat in f.skills:
        def item_w(s: str) -> float:
            ts = T.term_set(s)
            return max([jd.weights.get(t, 0) for t in ts] or [0])
        ordered = sorted(cat["items"], key=lambda s: -item_w(s))
        skills.append((sum(item_w(s) for s in cat["items"]), cat["category"], ordered))
    skills.sort(key=lambda t: -t[0])
    skills_out = [(name, items) for _, name, items in skills]
    ed = f.education
    edu = Entry(ed["school"], ed.get("dates", ""), f"{ed['degree']} | {ed['detail']}" if ed.get("detail") else ed["degree"])
    ach = [i.text for i in sorted([i for i in plan.items if i.kind == "ach" and not i.dropped], key=lambda i: -i.prio)]
    raw = f.raw
    return ResumeDoc(c["name"], plan.headline, [x for x in contact if x], links, plan.summary_text, exp_entries,
                     proj_entries, skills_out, edu, ach,
                     skills_title=raw.get("skills_title") or ("SKILLS" if T.ACTIVE == "general" else "TECHNICAL SKILLS"),
                     experience_title=raw.get("experience_title") or "EXPERIENCE",
                     projects_title=raw.get("projects_title") or "PROJECTS",
                     achievements_title=raw.get("achievements_title") or "ACHIEVEMENTS")


def render_fit(plan: Plan) -> Tuple[ResumeDoc, bytes, int]:
    """Render to one page: shrink slightly, then drop the least valuable lines."""
    for _ in range(40):
        for scale in (1.0, 0.97, 0.94, 0.92):
            plan.scale = scale
            doc = assemble(plan)
            pdf, pages = render.build_pdf(doc, scale)
            if pages <= 1:
                return doc, pdf, pages
        if not _drop_one(plan):
            break
    plan.scale = 0.92
    doc = assemble(plan)
    pdf, pages = render.build_pdf(doc, 0.92)
    return doc, pdf, pages


def _drop_one(plan: Plan) -> bool:
    live = [i for i in plan.items if not i.dropped and not i.forced]
    protected: Set[str] = set()
    exp_live = sorted([i for i in plan.items if i.kind == "exp" and not i.dropped], key=lambda i: -i.prio)
    protected |= {i.key for i in exp_live[:3]}
    for pi in plan.project_order:
        if pi in plan.project_dropped:
            continue
        pl = sorted([i for i in plan.items if i.kind == "proj" and i.group == pi and not i.dropped], key=lambda i: -i.prio)
        if pl:
            protected.add(pl[0].key)
        break                      # only the top project is fully protected
    candidates = [i for i in live if i.key not in protected]
    if not candidates:
        return False
    victim = min(candidates, key=lambda i: i.prio)
    victim.dropped = True
    # a project with no lines left disappears
    if victim.kind == "proj" and not any(i.kind == "proj" and i.group == victim.group and not i.dropped
                                         for i in plan.items):
        plan.project_dropped.add(victim.group)
    return True


# ----------------------------------------------------------------------------- scoring
def evaluate(plan: Plan) -> Tuple[ResumeDoc, bytes, int, ats.ATSResult]:
    doc, pdf, pages = render_fit(plan)
    text = doc.to_text()
    required = ["SUMMARY"] + ([doc.experience_title.upper()] if doc.experience else []) + [
        "SKILLS", "EDUCATION"]
    res = ats.score(text, plan.jd, pdf_text=render.pdf_text(pdf) or text, pages=pages, required=required,
                    header_text=doc.headline + "\n" + doc.summary + "\n" + "\n".join(e.heading for e in doc.experience))
    return doc, pdf, pages, res


def _force_claimable(plan: Plan, res: ats.ATSResult, facts_terms: Set[str]) -> bool:
    """Bring back lines that carry a missing term the facts truthfully contain."""
    changed = False
    for term in res.missing:
        if term not in facts_terms:
            continue
        for it in plan.items:
            if it.source is None or it.forced and not it.dropped:
                continue
            for v in it.source.variants:
                if term in T.term_set(v):
                    it.text, it.dropped, it.forced = v, False, True
                    if it.kind == "proj":
                        plan.project_dropped.discard(it.group)
                    changed = True
                    break
            if changed:
                break
        # a summary that carries the term
        if not changed:
            for s in plan.facts.summaries:
                for v in s["variants"]:
                    if term in T.term_set(v) and term not in T.term_set(plan.summary_text):
                        plan.summary_text, plan.summary_id = v, s["id"]
                        plan.summary_original = v
                        changed = True
                        break
                if changed:
                    break
        if changed:
            return True
    return changed


# ----------------------------------------------------------------------------- LLM rewrite
_REWRITE_SYSTEM = (
    "You edit resume bullet points for ONE specific job. You may only reword. Hard rules: never add a technology, "
    "tool, number, employer, metric or achievement that is not in the original text; keep every number; keep the "
    "meaning; same length or shorter; start with a strong past-tense verb (present tense for ongoing work); no "
    "first person; no emojis. Where the job description uses a different word for something the bullet already "
    "describes (for example 'RESTful services' for 'REST APIs'), use the job's wording. If a bullet cannot be "
    "improved, return it unchanged. Respond with JSON only.")


def rewrite_with_llm(plan: Plan, llm: llmmod.LLM, stats: Dict[str, Any]) -> None:
    jd = plan.jd
    live = [i for i in plan.items if not i.dropped and i.kind in ("exp", "proj")]
    payload = {"summary": plan.summary_text, **{i.key: i.text for i in live}}
    terms_line = ", ".join(sorted(jd.all_terms(), key=lambda t: -jd.all_terms()[t])[:25])
    user = (f"Job title: {jd.title}\nKey terms in the job description (use their wording only where it means the "
            f"same thing): {terms_line}\n\nTexts to improve (JSON id -> text):\n"
            + __import__("json").dumps(payload, ensure_ascii=False)
            + "\n\nReturn JSON: an object with the same ids mapping to the improved text.")
    try:
        data = llm.chat_json(_REWRITE_SYSTEM, user, max_tokens=2400, temperature=0.3)
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return
    accepted = rejected = 0
    reasons: List[str] = []

    def check(new: Any, old: str, allowed: str, key: str) -> Optional[str]:
        if not isinstance(new, str):
            return None
        new = re.sub(r"\s+", " ", new).strip()
        if not new or new == old:
            return None
        if not guard.length_ok(new, old):
            reasons.append(f"{key}: length")
            return None
        problems = guard.violations(new, allowed)
        if problems:
            reasons.append(f"{key}: " + "; ".join(problems[:3]))
            return None
        if not guard.numbers(old) <= guard.numbers(new):
            reasons.append(f"{key}: dropped a number")
            return None
        return new

    new = check(data.get("summary"), plan.summary_text, plan.facts.corpus, "summary")
    if new:
        plan.summary_text, plan.summary_rewritten = new, True
        accepted += 1
    elif "summary" in data:
        rejected += 1
    for it in live:
        allowed = " ".join(it.source.variants) if it.source else it.text
        owner_ctx = ""
        if it.kind == "proj":
            pr = plan.facts.projects[it.group]
            owner_ctx = " ".join(pr["stack"]) + " " + " ".join(pr["names"])
        else:
            e = plan.facts.experience[it.group]
            owner_ctx = e["company"] + " " + e["title"]
        new = check(data.get(it.key), it.text, allowed + " " + owner_ctx, it.key)
        if new:
            it.text, it.rewritten = new, True
            accepted += 1
        elif it.key in data:
            rejected += 1
    stats.update(accepted=accepted, rejected=rejected, reject_reasons=reasons[:8])


# ----------------------------------------------------------------------------- main entry
def tailor(job: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None, facts: Optional[Facts] = None,
           analyzer: Optional[llmmod.LLM] = None, rewriter: Optional[llmmod.LLM] = None,
           use_llm: bool = True) -> TailorResult:
    out = TailorResult()
    T.use_for(cfg)
    try:
        facts = facts or load_facts()
    except Exception as e:
        out.error = ("No master resume yet. Upload yours on the Resume page first." if isinstance(e, FileNotFoundError)
                     else f"Could not load your resume facts: {e}")
        return out
    text = (job.get("description") or "").strip()
    title = job.get("title") or ""
    if use_llm:
        analyzer = analyzer if analyzer is not None else llmmod.groq(cfg)
        rewriter = rewriter if rewriter is not None else (llmmod.github(cfg) or llmmod.groq(cfg))
    else:
        analyzer = rewriter = None
    jd = jdmod.analyze(text, title, analyzer)
    out.jd_terms = dict(jd.all_terms())
    if not jd.weights and not jd.literals:
        out.notes.append("No recognisable keywords in the job description; scored on format and title only.")
    if len(text) < 200:
        out.notes.append("The job description was very short or missing, so keyword matching is weak.")
    out.llm = {"analyzer": jd.source, "rewriter": rewriter.name if rewriter else "none"}

    plan = make_plan(facts, jd)
    facts_terms = ats.present_terms(facts.corpus)
    doc, pdf, pages, res = evaluate(plan)
    base_score = res.score

    # round 2..: bring in true lines that cover missing terms
    for _ in range(3):
        if res.score >= TARGET_SCORE or not _force_claimable(plan, res, facts_terms):
            break
        doc, pdf, pages, res = evaluate(plan)

    # optional LLM rewording, kept only if it does not lower the score
    if rewriter is not None and res.score < 99.5:
        snapshot = (plan.summary_text, [(i.key, i.text, i.rewritten) for i in plan.items])
        stats: Dict[str, Any] = {}
        rewrite_with_llm(plan, rewriter, stats)
        out.llm["rewrite"] = stats
        if stats.get("accepted"):
            d2, p2, pg2, r2 = evaluate(plan)
            if r2.score >= res.score and pg2 <= 1:
                doc, pdf, pages, res = d2, p2, pg2, r2
            else:
                plan.summary_text = snapshot[0]
                for it, (_, t, rw) in zip(plan.items, snapshot[1]):
                    it.text, it.rewritten = t, rw
                out.llm["rewrite"]["reverted"] = "rewording did not improve the score"

    out.doc, out.pdf, out.pages = doc, pdf, pages
    out.docx = render.build_docx(doc, plan.scale)
    out.ats = res.as_dict()
    out.score = res.score
    out.target_reached = res.score >= TARGET_SCORE
    out.gaps_unclaimable = [t for t in res.missing if t not in facts_terms]
    claimable_missing = [t for t in res.missing if t in facts_terms]
    if claimable_missing:
        out.notes.append("Your facts contain " + ", ".join(claimable_missing) + " but there was no room for it.")
    if out.gaps_unclaimable:
        out.notes.append("Not in your resume facts (so not claimed): " + ", ".join(out.gaps_unclaimable[:10]) +
                         ". Add one to resume_facts.yaml only if you have genuinely used it.")
    if not out.target_reached:
        out.notes.append(f"Score {out.score:.1f} is below the {TARGET_SCORE:.0f} target. It is the honest best from "
                         "your facts, not padded with keywords you do not have.")
    out.ok = True
    return out
