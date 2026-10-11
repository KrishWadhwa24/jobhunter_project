"""Referral helper: where to find people + ready-to-send message drafts.

We deliberately do NOT scrape LinkedIn (against its terms, gets accounts banned). We give you
search links that open LinkedIn / Google with the right query, and drafts you can paste.
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import quote_plus

NOTE_LIMIT = 200   # LinkedIn free-account connection note limit


def _li_people(q: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(q)}&origin=GLOBAL_SEARCH_HEADER"


def _google(q: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(q)}"


def search_links(company: str, college: str) -> List[Dict[str, str]]:
    return [
        {"label": "Engineers at the company", "url": _li_people(f"{company} software engineer India"),
         "why": "Peers refer most often."},
        {"label": f"{college} alumni at the company", "url": _li_people(f"{company} {college}"),
         "why": "Alumni usually reply."},
        {"label": "Recruiters / talent acquisition", "url": _li_people(f"{company} recruiter OR \"talent acquisition\" India"),
         "why": "Can put you into the pipeline directly."},
        {"label": "Engineering managers", "url": _li_people(f"{company} \"engineering manager\" India"),
         "why": "Best for the team you want."},
        {"label": "Google: people on LinkedIn", "url": _google(f"site:linkedin.com/in \"{company}\" \"engineering manager\" OR \"software engineer\" India"),
         "why": "Fallback if LinkedIn search is limited."},
    ]


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def connect_note(profile: Dict[str, Any], company: str, title: str) -> str:
    name = profile.get("name", "")
    college = profile.get("college", "")
    year = profile.get("grad_year", "")
    full = (f"Hi [Name], I'm {name}, a {college} {year} grad and SDE intern. I'm interested in the "
            f"{_short(title, 38)} role at {company}. Could you refer me or point me to the right person?")
    if len(full) <= NOTE_LIMIT:
        return full
    short = f"Hi [Name], I'm {name} ({college} {year}, SDE intern). Interested in the {_short(title, 30)} role at {_short(company, 20)}. Could you refer me?"
    return _short(short, NOTE_LIMIT)


def drafts(profile: Dict[str, Any], company: str, title: str, url: str) -> List[Dict[str, str]]:
    name = profile.get("name", "")
    college = profile.get("college", "")
    year = profile.get("grad_year", "")
    about = profile.get("about", "")
    ctx = f"I'm {name}, a final-year {profile.get('degree', 'CS')} student at {college} (class of {year})"
    if about:
        ctx += f". {about[0].upper() + about[1:]}"
    note = connect_note(profile, company, title)
    return [
        {
            "label": "Connection note (under 200 characters)",
            "text": note,
            "chars": str(len(note)),
        },
        {
            "label": "Follow-up to an engineer or alumnus (after they accept)",
            "text": (f"Hi [Name], thanks for connecting. {ctx}.\n\n"
                     f"I saw the {title} opening at {company} ({url}) and it matches what I've been working on. "
                     f"Would you be open to referring me, or telling me what the team looks for? "
                     f"Happy to send my resume. Thank you for your time."),
        },
        {
            "label": "Message to a recruiter",
            "text": (f"Hi [Name], {ctx}. I'm interested in the {title} role at {company} ({url}) and would like to be "
                     f"considered for {year} joining. Could you let me know the best way to apply, or add me to the "
                     f"pipeline? I'm happy to share my resume."),
        },
        {
            "label": "Message to a hiring manager",
            "text": (f"Hi [Name], I'm {name} from {college}. I'm keen on the {title} opening on your team at {company} "
                     f"({url}). {about[0].upper() + about[1:] + '. ' if about else ''}"
                     f"Could I send you my resume? I'd value any advice on what your team looks for."),
        },
    ]


def build_pack(job: Any, profile: Dict[str, Any]) -> Dict[str, Any]:
    company = job["company"]
    return {
        "company": company,
        "links": search_links(company, profile.get("college", "")),
        "drafts": drafts(profile, company, job["title"], job["url"]),
    }


def as_text(pack: Dict[str, Any]) -> str:
    """Plain-text version for Telegram."""
    lines = [f"Referral help for {pack['company']}", ""]
    for l in pack["links"]:
        lines.append(f"{l['label']}: {l['url']}")
    lines.append("")
    for d in pack["drafts"][:2]:
        lines += [d["label"] + ":", d["text"], ""]
    lines.append("More drafts (recruiter, hiring manager) are on the dashboard job page.")
    return "\n".join(lines)
