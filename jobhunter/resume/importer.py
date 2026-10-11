"""Master resume -> resume_facts.yaml.

The person uploads their resume (PDF, DOCX or text) from the dashboard. We read it, split it into sections, and write
resume_facts.yaml: the only material the tailor may draw from. Every line in the facts is copied from the resume;
nothing is invented. An LLM (Groq) may help read messy layouts, but its output is only kept where it can be found in
the resume text. Without an LLM, or if it fails, a rule-based reader does the same job.
"""
from __future__ import annotations

import io
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .. import config
from . import guard

MAX_BYTES = 8 * 1024 * 1024
ALLOWED = (".pdf", ".docx", ".txt", ".md")

HEADER_NOTE = """# Your resume facts: the ONLY source the resume tailor may draw from.
# Generated from your uploaded master resume ({source}) on {when}. Every sentence was copied from it.
# Edit this on the dashboard (Master resume > Review) if something was read wrongly. Upload a new resume any time and
# it is rebuilt (the previous version is kept as resume_facts.previous.yaml).
# Do NOT add things you have not actually done: the tailor will use them.
"""

# ----------------------------------------------------------------------------- text extraction


def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError("Upload a PDF, DOCX or plain text resume.")
    if len(data) > MAX_BYTES:
        raise ValueError("That file is larger than 8 MB.")
    if ext == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            pdfplumber = None
        out: List[str] = []
        if pdfplumber is not None:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages[:6]:
                    out.append(page.extract_text(x_tolerance=1.5, y_tolerance=3) or "")
        else:                                   # pypdf is already installed; pdfplumber reads layouts better
            from pypdf import PdfReader
            for page in PdfReader(io.BytesIO(data)).pages[:6]:
                out.append(page.extract_text() or "")
        text = "\n".join(out)
    elif ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        lines = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                cells = []
                for c in row.cells:
                    if c.text.strip() and c.text.strip() not in cells:
                        cells.append(c.text.strip())
                lines.append("  |  ".join(cells))
        text = "\n".join(lines)
    else:
        text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\r", "\n").replace(" ", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 80:
        raise ValueError("Could not read any text from that file. If it is a scanned image, export a text PDF or DOCX.")
    return text


# ----------------------------------------------------------------------------- rule-based reader
SECTIONS = {
    "summary": r"summary|profile|objective|career objective|about me|professional summary|personal statement",
    "education": r"education|academics?|academic (?:background|qualifications?)|qualifications?",
    "experience": r"(?:work |professional |relevant |internship |teaching )?experience|internships?|employment(?: history)?|"
                  r"work history|volunteer(?:ing)?(?: experience)?|positions? of responsibility|work",
    "projects": r"projects?|academic projects?|writing samples?|publications?|portfolio|research|selected work|work samples?",
    "skills": r"(?:technical |key |core |professional )?skills|core competenc(?:y|ies)|competenc(?:y|ies)|tools|"
              r"languages?|tools (?:&|and) technologies|areas of expertise|strengths",
    "achievements": r"achievements?|awards?(?: (?:&|and) honou?rs)?|honou?rs|accomplishments?|certifications?|"
                    r"courses?|extra-?curriculars?|activities|co-?curricular|sports|chess|interests|leadership",
}
BULLET = re.compile(r"^\s*(?:[-•●▪◦■□★✓✔►➢➤*·–—]|\d+[.)])\s+")
DATE = re.compile(r"(?i)(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*)?(?:19|20)\d{2}"
                  r"(?:\s*[-–—to]+\s*(?:(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*)?(?:19|20)\d{2}"
                  r"|present|current|ongoing|till date|now))?|(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*,?\s*(?:19|20)\d{2})\s*[-–—]\s*(?:present|current)")
EMAIL = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")
PHONE = re.compile(r"(?<![\d/])\+?\d[\d\s()\-]{8,17}\d(?![\d/])")
URL = re.compile(r"(?i)\b(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com|medium\.com|behance\.net|substack\.com|[a-z0-9\-]+\.(?:com|in|io|me|co|org|net|dev))/?[^\s|,;]*")
SCHOOL = re.compile(r"(?i)\b(university|college|institute|school|academy|iit|nit|vidyalaya|polytechnic)\b")
DEGREE = re.compile(r"(?i)\b(b\.?a\.?|b\.?sc\.?|b\.?com|b\.?tech|b\.?e\.?|m\.?a\.?|m\.?sc\.?|mba|bachelor|master|hons\.?|honou?rs|diploma|ph\.?d|12th|xii|class x|intermediate|higher secondary)\b")
TITLE_WORDS = re.compile(r"(?i)\b(intern|internship|trainee|writer|editor|associate|assistant|executive|tutor|teacher|coach|"
                         r"analyst|developer|engineer|manager|volunteer|coordinator|lead|member|captain|head|officer|specialist|"
                         r"consultant|representative|agent|moderator|reviewer|annotator|freelance\w*|fellow|contributor|"
                         r"president|secretary|director|instructor|mentor|researcher|producer|reporter|strategist)\b")


def _slug(s: str, n: int = 0) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:24]
    return s or f"item{n}"


def _split_sections(lines: List[str]) -> Tuple[List[str], Dict[str, List[str]]]:
    head: List[str] = []
    secs: Dict[str, List[str]] = {}
    cur: Optional[str] = None
    for ln in lines:
        raw = ln.strip()
        key = None
        probe = raw.rstrip(":").strip()
        if probe and len(probe) <= 42 and not BULLET.match(raw) and not DATE.search(probe) and len(probe.split()) <= 5:
            for name, pat in SECTIONS.items():
                if re.fullmatch(r"(?i)\s*(?:" + pat + r")\s*(?:&\s*\w+)?\s*", probe):
                    key = name
                    break
        if key:
            cur = key
            secs.setdefault(cur, [])
            # a second heading of the same kind (e.g. 'Awards' after 'Certifications') keeps appending
            continue
        if cur is None:
            head.append(raw)
        else:
            secs[cur].append(raw)
    return head, secs


def _clean_lines(text: str) -> List[str]:
    return [ln.rstrip() for ln in text.split("\n")]


def _contact(head: List[str]) -> Dict[str, Any]:
    joined = "\n".join(head)
    email = (EMAIL.search(joined) or [None])[0] if EMAIL.search(joined) else ""
    phone = ""
    for m in PHONE.finditer(joined.replace(email or "", "")):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 13:
            phone = m.group(0).strip()
            break
    links: List[Dict[str, str]] = []
    for m in URL.finditer(joined.replace(email or "", "")):
        u = m.group(0).rstrip(".,;)")
        if "@" in u:
            continue
        host = re.sub(r"(?i)^https?://(www\.)?", "", u).split("/")[0].lower()
        label = {"linkedin.com": "LinkedIn", "github.com": "GitHub", "medium.com": "Medium",
                 "behance.net": "Behance", "substack.com": "Substack"}.get(host, "Portfolio")
        url = u if u.lower().startswith("http") else "https://" + u
        if not any(l["url"] == url for l in links):
            links.append({"label": label, "url": url})
    name = ""
    for ln in head:
        t = ln.strip()
        if t and not EMAIL.search(t) and not re.search(r"\d{5,}", t) and len(t.split()) <= 6:
            name = re.sub(r"\s+", " ", t.strip("|•-– "))
            break
    location = ""
    for ln in head:
        for piece in re.split(r"\s*[|•·●]\s*|\s{3,}", ln):
            p = piece.strip()
            if not p or p == name or EMAIL.search(p) or URL.search(p) or re.search(r"\d{6,}", p):
                continue
            if re.search(r"(?i),\s*[A-Za-z ]{3,}$|india|delhi|mumbai|bengaluru|bangalore|kolkata|chennai|pune|noida|gurgaon|"
                         r"gurugram|hyderabad|jaipur|chandigarh|lucknow|ahmedabad|usa|uk\b", p) and len(p) < 60:
                location = p
                break
        if location:
            break
    return {"name": name or "Your Name", "phone": phone, "email": email or "", "location": location, "links": links}


def _education(lines: List[str]) -> Dict[str, Any]:
    lines = [l.strip() for l in lines if l.strip()]
    school = degree = dates = detail = ""
    extra: List[str] = []
    for ln in lines:
        d = DATE.search(ln)
        if d and not dates:
            dates = d.group(0).strip()
        body = DATE.sub("", ln).strip(" |,-–—")
        if not school and SCHOOL.search(body) and not re.search(r"(?i)\bbachelor|\bhons\b", body.split("|")[0]) :
            school = body.split("|")[0].strip(" ,-–—")
            rest = [p.strip() for p in body.split("|")[1:] if p.strip()]
            if rest and DEGREE.search(rest[0]) and not degree:
                degree = rest[0]
            continue
        if not degree and DEGREE.search(body):
            degree = body
            continue
        if re.search(r"(?i)cgpa|gpa|percentage|%|grade|first class|distinction", body) and not detail:
            detail = body
            continue
        extra.append(body)
    if not school and lines:
        school = DATE.sub("", lines[0]).strip(" |,-–—")
    if not degree:
        degree = next((e for e in extra if e), "")
    return {"school": school, "degree": degree, "dates": dates, "detail": detail}


def _is_bullet(ln: str) -> bool:
    return bool(BULLET.match(ln))


def _entries(lines: List[str], kind: str) -> List[Dict[str, Any]]:
    """Group lines into entries: header lines (title/company/dates) followed by bullet lines."""
    entries: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    last_bullet = False
    for ln in lines:
        s = ln.strip()
        if not s:
            last_bullet = False
            continue
        if _is_bullet(s):
            if cur is None:
                cur = {"head": [], "bullets": []}
                entries.append(cur)
            cur["bullets"].append(BULLET.sub("", s).strip())
            last_bullet = True
            continue
        # wrapped continuation of the previous bullet: starts lowercase or previous line did not end a sentence
        if last_bullet and cur and cur["bullets"] and (s[:1].islower() or not re.search(r"[.!?:]$", cur["bullets"][-1])) \
                and not DATE.search(s) and not TITLE_WORDS.search(s[:40]):
            cur["bullets"][-1] += " " + s
            continue
        long_sentence = len(s) > 90 and re.search(r"[.!?]$", s)
        if long_sentence and cur is not None:
            cur["bullets"].append(s)
            last_bullet = True
            continue
        if cur is None or cur["bullets"] or len(cur["head"]) >= 3:
            cur = {"head": [], "bullets": []}
            entries.append(cur)
        cur["head"].append(s)
        last_bullet = False
    return [e for e in entries if e["head"] or e["bullets"]]


def _head_parts(head: List[str]) -> Tuple[str, str, str, str]:
    """(company_or_name, title, dates, location) from 1-3 header lines."""
    dates = ""
    pieces: List[str] = []
    for ln in head:
        d = DATE.search(ln)
        if d and not dates:
            dates = d.group(0).strip()
        body = DATE.sub("", ln)
        for p in re.split(r"\s+[|—–]\s+|\s*\|\s*|\s+-\s+|\s{3,}", body):
            p = p.strip(" ,|-–—()")
            if p:
                pieces.append(p)
    location = ""
    for p in list(pieces):
        if re.fullmatch(r"(?i)(?:[A-Z][a-z]+(?:\s[A-Z][a-z]+)?,\s*)?(?:India|Remote|Delhi|Mumbai|Bengaluru|Bangalore|Noida|Pune|"
                        r"Gurugram|Gurgaon|Hyderabad|Chennai|Kolkata|Jaipur|[A-Z][a-z]+,\s*[A-Z]{2})", p) and len(pieces) > 1:
            location = p
            pieces.remove(p)
            break
    title = next((p for p in pieces if TITLE_WORDS.search(p)), "")
    others = [p for p in pieces if p != title]
    company = others[0] if others else (pieces[0] if pieces else "")
    if title and not others:
        company, title = title, ""
    return company, title, dates, location


def _bullets(items: List[str], prefix: str, start_weight: int = 5) -> List[Dict[str, Any]]:
    out = []
    for n, b in enumerate(items):
        b = re.sub(r"\s+", " ", b).strip()
        if len(b) < 12:
            continue
        out.append({"id": f"{prefix}{n + 1}", "weight": max(2, start_weight - n), "variants": [b]})
    return out


def _skills(lines: List[str]) -> List[Dict[str, Any]]:
    cats: List[Dict[str, Any]] = []
    loose: List[str] = []
    for ln in lines:
        s = BULLET.sub("", ln.strip()).strip()
        if not s:
            continue
        if ":" in s and s.index(":") < 45:
            cat, rest = s.split(":", 1)
            items = [i.strip(" .;") for i in re.split(r"[,;|•·]", rest) if i.strip(" .;")]
            if items:
                cats.append({"category": cat.strip(), "items": items})
                continue
        loose += [i.strip(" .;") for i in re.split(r"[,;|•·]", s) if i.strip(" .;")]
    if loose:
        cats.append({"category": "Skills", "items": loose})
    return cats


def parse_rules(text: str) -> Dict[str, Any]:
    head, secs = _split_sections(_clean_lines(text))
    contact = _contact(head[:8])
    education = _education(secs.get("education", []))
    experience = []
    for n, e in enumerate(_entries(secs.get("experience", []), "exp")):
        company, title, dates, loc = _head_parts(e["head"])
        if not company and not e["bullets"]:
            continue
        experience.append({"company": company or "Experience", "title": title, "location": loc, "dates": dates,
                           "bullets": _bullets(e["bullets"], f"{_slug(company, n)}-")})
    projects = []
    for n, e in enumerate(_entries(secs.get("projects", []), "proj")):
        name, sub, dates, _ = _head_parts(e["head"])
        if not name and not e["bullets"]:
            continue
        projects.append({"id": _slug(name, n), "weight": max(2, 5 - n), "names": [name or f"Project {n + 1}"],
                         "stack": [], "links": [], "bullets": _bullets(e["bullets"] or ([sub] if sub else []), "p")})
    skills = _skills(secs.get("skills", []))
    achievements = []
    for ln in secs.get("achievements", []):
        s = BULLET.sub("", ln.strip()).strip()
        if len(s) >= 12:
            achievements.append({"weight": 3, "variants": [re.sub(r"\s+", " ", s)]})
    summary_text = " ".join(l.strip() for l in secs.get("summary", []) if l.strip())
    raw: Dict[str, Any] = {"contact": contact, "education": education}
    raw["summary"] = [{"id": "main", "variants": [summary_text]}] if summary_text else []
    raw["experience"] = experience
    raw["projects"] = projects
    raw["skills"] = skills
    raw["achievements"] = achievements
    return raw


def _auto_summary(raw: Dict[str, Any]) -> str:
    ed = raw.get("education") or {}
    items = [i for c in raw.get("skills", []) for i in c.get("items", [])][:5]
    bits = [f"{ed.get('degree', '').strip()} graduate" if ed.get("degree") else "Graduate"]
    if ed.get("school"):
        bits.append(f"from {ed['school']}")
    s = " ".join(bits)
    if items:
        s += " with skills in " + ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1]
    return s + "."


# ----------------------------------------------------------------------------- LLM reader (optional)
_SYSTEM = ("You convert a resume into structured JSON. Copy wording from the resume exactly: never invent, "
           "rephrase, summarise or add anything. Respond with JSON only.")


def parse_llm(text: str, llm: Any) -> Optional[Dict[str, Any]]:
    user = ("Resume text:\n" + text[:9000] + "\n\nReturn JSON with keys:\n"
            '"contact": {"name","phone","email","location","links":[{"label","url"}]},\n'
            '"education": {"school","degree","dates","detail"},\n'
            '"summary": the resume\'s own summary sentence(s) as a string, or "" if it has none,\n'
            '"experience": [{"company","title","location","dates","bullets":[string]}] (internships, jobs, volunteering, teaching),\n'
            '"projects": [{"name","bullets":[string]}] (projects, publications, writing samples),\n'
            '"skills": [{"category","items":[string]}],\n'
            '"achievements": [string] (awards, certifications, sports, extracurriculars).\n'
            "Each bullet must be a sentence copied from the resume.")
    data = llm.chat_json(_SYSTEM, user, max_tokens=3500)
    if not isinstance(data, dict) or not data.get("contact") or not data.get("education"):
        return None
    raw: Dict[str, Any] = {"contact": data["contact"], "education": data["education"]}
    raw["contact"].setdefault("links", [])
    raw["summary"] = [{"id": "main", "variants": [data["summary"].strip()]}] if isinstance(data.get("summary"), str) and data["summary"].strip() else []
    raw["experience"] = []
    for n, e in enumerate(data.get("experience") or []):
        raw["experience"].append({"company": e.get("company") or "Experience", "title": e.get("title") or "",
                                  "location": e.get("location") or "", "dates": e.get("dates") or "",
                                  "bullets": _bullets([b for b in e.get("bullets") or [] if isinstance(b, str)],
                                                      f"{_slug(e.get('company') or '', n)}-")})
    raw["projects"] = []
    for n, p in enumerate(data.get("projects") or []):
        nm = p.get("name") or f"Project {n + 1}"
        raw["projects"].append({"id": _slug(nm, n), "weight": max(2, 5 - n), "names": [nm], "stack": [], "links": [],
                                "bullets": _bullets([b for b in p.get("bullets") or [] if isinstance(b, str)], "p")})
    raw["skills"] = [{"category": c.get("category") or "Skills", "items": [str(i) for i in c.get("items") or []]}
                     for c in data.get("skills") or [] if isinstance(c, dict)]
    raw["achievements"] = [{"weight": 3, "variants": [a.strip()]} for a in data.get("achievements") or []
                           if isinstance(a, str) and len(a.strip()) >= 12]
    return raw


# ----------------------------------------------------------------------------- grounding
def _norm(s: str) -> str:
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s or "")
    s = re.sub(r"[•●▪◦■□★✓✔►➢➤·]", " ", s)
    return re.sub(r"\s+", " ", s.lower()).strip()


def _grounded(piece: str, source: str, src_words: set) -> bool:
    p = _norm(piece)
    if not p:
        return False
    if p in source:
        return True
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9'+#.%-]*", p) if len(w) > 2]
    if not words:
        return p in source
    hit = sum(1 for w in words if w in src_words)
    return hit / len(words) >= 0.92 and guard.numbers(piece) <= guard.numbers(source)


def ground(raw: Dict[str, Any], text: str) -> Tuple[Dict[str, Any], List[str]]:
    """Drop anything that cannot be found in the resume text. Returns (clean facts, warnings)."""
    source = _norm(text)
    words = set(re.findall(r"[a-z0-9][a-z0-9'+#.%-]*", source))
    warns: List[str] = []

    def keep_bullets(bs: List[Dict[str, Any]], where: str) -> List[Dict[str, Any]]:
        out = []
        for b in bs:
            vs = [v for v in b["variants"] if _grounded(v, source, words)]
            if vs:
                out.append(dict(b, variants=vs))
            else:
                warns.append(f"Dropped a line under {where} that was not in your resume: {b['variants'][0][:70]}")
        return out

    for e in raw.get("experience", []):
        e["bullets"] = keep_bullets(e["bullets"], e["company"])
    for p in raw.get("projects", []):
        p["bullets"] = keep_bullets(p["bullets"], p["names"][0])
    raw["experience"] = [e for e in raw.get("experience", []) if e["bullets"] or _grounded(e["company"], source, words)]
    raw["projects"] = [p for p in raw.get("projects", []) if p["bullets"]]
    for c in raw.get("skills", []):
        c["items"] = [i for i in c["items"] if _grounded(i, source, words)]
    raw["skills"] = [c for c in raw.get("skills", []) if c["items"]]
    raw["achievements"] = [a for a in raw.get("achievements", []) if any(_grounded(v, source, words) for v in a["variants"])]
    for s in raw.get("summary", []):
        s["variants"] = [v for v in s["variants"] if _grounded(v, source, words)]
    raw["summary"] = [s for s in raw.get("summary", []) if s["variants"]]
    return raw, warns


# ----------------------------------------------------------------------------- build + save
def build_facts(text: str, llm: Any = None) -> Tuple[Dict[str, Any], List[str], str]:
    """(facts dict, warnings, method). Uses the LLM reading when it holds up, otherwise the rule-based one."""
    warns: List[str] = []
    method = "rules"
    raw: Optional[Dict[str, Any]] = None
    rules = parse_rules(text)
    if llm is not None:
        try:
            cand = parse_llm(text, llm)
            if cand:
                cand, w = ground(cand, text)
                n_llm = sum(len(e["bullets"]) for e in cand["experience"]) + sum(len(p["bullets"]) for p in cand["projects"])
                n_rule = sum(len(e["bullets"]) for e in rules["experience"]) + sum(len(p["bullets"]) for p in rules["projects"])
                if n_llm >= max(1, int(0.6 * n_rule)) and cand["skills"]:
                    raw, method = cand, f"llm:{getattr(llm, 'name', 'llm')}"
                    warns += w
        except Exception as e:      # network or JSON problems: fall back quietly
            warns.append(f"The AI reader was not available ({type(e).__name__}); used the built-in reader.")
    if raw is None:
        raw, w = ground(rules, text)
        warns += w
    # the contact block and education come from the same place either way; fill gaps from the rule reader
    for k in ("name", "phone", "email", "location"):
        if not raw["contact"].get(k):
            raw["contact"][k] = rules["contact"].get(k, "")
    if not raw["contact"].get("links"):
        raw["contact"]["links"] = rules["contact"].get("links", [])
    for k in ("school", "degree", "dates", "detail"):
        if not raw["education"].get(k):
            raw["education"][k] = rules["education"].get(k, "")
    if not raw["summary"]:
        raw["summary"] = [{"id": "auto", "variants": [_auto_summary(raw)]}]
        warns.append("Your resume has no summary, so a plain one was built from your education and skills. Edit it in Review.")
    if not raw["skills"]:
        warns.append("No skills section was found. Add one in Review: the tailor needs it to match job keywords.")
        raw["skills"] = [{"category": "Skills", "items": ["Communication"]}]
    if not raw["contact"].get("email"):
        warns.append("No email address found: check the contact details in Review.")
    for key in ("contact", "education", "summary", "experience", "projects", "skills", "achievements"):
        raw.setdefault(key, [] if key not in ("contact", "education") else {})
    return raw, warns, method


def to_yaml(raw: Dict[str, Any], source: str) -> str:
    body = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, width=110, default_flow_style=False)
    return HEADER_NOTE.format(source=source, when=datetime.now().strftime("%d %b %Y")) + "\n" + body


def check_yaml(text: str) -> Optional[str]:
    """None when the YAML loads as valid facts, else the error to show."""
    from .facts import load_facts
    try:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.yaml"
            p.write_text(text, encoding="utf-8")
            load_facts(p)
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:300]
    return None


def master_dir() -> Path:
    d = config.PROFILE_DIR / "master_resume"
    d.mkdir(parents=True, exist_ok=True)
    return d


def current_master() -> Optional[Dict[str, Any]]:
    d = config.PROFILE_DIR / "master_resume"
    if not d.exists():
        return None
    files = sorted([f for f in d.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED], key=lambda f: f.stat().st_mtime)
    if not files:
        return None
    f = files[-1]
    return {"name": f.name, "path": f, "bytes": f.stat().st_size, "uploaded": datetime.fromtimestamp(f.stat().st_mtime)}


def save_master(filename: str, data: bytes, llm: Any = None) -> Dict[str, Any]:
    """Store the upload, rebuild resume_facts.yaml from it, and return a report for the page."""
    text = extract_text(filename, data)
    raw, warns, method = build_facts(text, llm)
    out_yaml = to_yaml(raw, Path(filename).name)
    err = check_yaml(out_yaml)
    if err:
        raise ValueError("The resume was read but the result is not usable: " + err)
    d = master_dir()
    for old in d.iterdir():
        if old.is_file() and old.suffix.lower() in ALLOWED:
            old.unlink()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:80] or "master_resume.pdf"
    (d / safe).write_bytes(data)
    (d / "extracted_text.cache").write_text(text, encoding="utf-8")
    if config.FACTS_PATH.exists():
        shutil.copyfile(config.FACTS_PATH, config.FACTS_PATH.with_name("resume_facts.previous.yaml"))
    config.FACTS_PATH.write_text(out_yaml, encoding="utf-8")
    summary = {
        "method": method, "warnings": warns, "file": safe,
        "experience": len(raw["experience"]), "projects": len(raw["projects"]),
        "bullets": sum(len(e["bullets"]) for e in raw["experience"]) + sum(len(p["bullets"]) for p in raw["projects"]),
        "skills": sum(len(c["items"]) for c in raw["skills"]), "achievements": len(raw["achievements"]),
        "name": raw["contact"].get("name", ""),
    }
    return summary


def save_yaml(text: str) -> Optional[str]:
    """Review page: save edited facts. Returns an error string, or None on success."""
    err = check_yaml(text)
    if err:
        return err
    if config.FACTS_PATH.exists():
        shutil.copyfile(config.FACTS_PATH, config.FACTS_PATH.with_name("resume_facts.previous.yaml"))
    config.FACTS_PATH.write_text(text, encoding="utf-8")
    return None
