"""Work out how to fetch a company's jobs from a careers-page URL.

Strategy: recognise the hiring platform from the URL itself; if that fails, download the page and
look for platform links embedded in it; otherwise fall back to 'browser'.
"""
from __future__ import annotations

import re
from typing import Dict, Optional
from urllib.parse import urlparse

from .util import PoliteClient

# (source_type, regex, formatter(match) -> slug_or_endpoint). Order = priority.
PATTERNS = [
    ("greenhouse", re.compile(r"boards-api\.(eu\.)?greenhouse\.io/v1/boards/([\w\-]+)", re.I),
     lambda m: ("eu:" if m.group(1) else "") + m.group(2)),
    ("greenhouse", re.compile(r"(?:job-)?boards\.(eu\.)?greenhouse\.io/(?:embed/job_board\?for=)?([\w\-]+)", re.I),
     lambda m: ("eu:" if m.group(1) else "") + m.group(2)),
    ("lever", re.compile(r"(?:jobs|api)\.(eu\.)?lever\.co/(?:v0/postings/)?([\w\-\.]+)", re.I),
     lambda m: ("eu:" if m.group(1) else "") + m.group(2)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w\-%\.]+)", re.I), lambda m: m.group(1)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([\w\-%\.]+)", re.I), lambda m: m.group(1)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([\w\-]+)", re.I), lambda m: m.group(1)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w\-]+)", re.I), lambda m: m.group(1)),
    ("workday", re.compile(r"([\w\-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}[-_][A-Za-z]{2}/)?([\w\-]+)", re.I),
     lambda m: f"https://{m.group(1)}/{m.group(2)}"),
    ("oracle_hcm", re.compile(r"([\w\-]+\.fa\.[\w\-\.]*oraclecloud\.com)/hcmUI/CandidateExperience/\w+/sites/([\w\-]+)", re.I),
     lambda m: f"https://{m.group(1)}/hcmUI/CandidateExperience/en/sites/{m.group(2)}"),
    ("kula", re.compile(r"careers\.kula\.ai/api/internal/ats_job_posts\?[^\s\"']*accountName=([\w\-]+)", re.I), lambda m: m.group(1)),
    ("kula", re.compile(r"careers\.kula\.ai/(?!api/|_next|static|assets)([a-z0-9][\w\-]*)", re.I), lambda m: m.group(1)),
    ("recruiterbox", re.compile(r"(jsapi\.recruiterbox\.com/v1/openings\?client_name=[\w\-]+)", re.I),
     lambda m: "https://" + m.group(1)),
]

# Platforms we recognise but cannot read without a browser yet.
BROWSER_VENDORS = [
    ("Keka", re.compile(r"[\w\-]+\.keka\.com/careers", re.I)),
    ("Darwinbox", re.compile(r"[\w\-]+\.darwinbox\.(in|com)", re.I)),
    ("TurboHire", re.compile(r"[\w\-]+\.turbohire\.co", re.I)),
    ("TalentRecruit", re.compile(r"[\w\-]+\.talentrecruit\.com", re.I)),
    ("Skillate", re.compile(r"[\w\-]+\.skillate\.com", re.I)),
    ("Recruiterflow", re.compile(r"recruiterflow\.com", re.I)),
    ("Freshteam", re.compile(r"[\w\-]+\.freshteam\.com", re.I)),
    ("Zoho Recruit", re.compile(r"zohorecruit\.[a-z]+", re.I)),
    ("Workable", re.compile(r"apply\.workable\.com|[\w\-]+\.workable\.com", re.I)),
    ("Phenom", re.compile(r"phenompeople\.com", re.I)),
    ("Avature", re.compile(r"[\w\-]+\.avature\.net", re.I)),
]


def _match_patterns(text: str) -> Optional[Dict[str, str]]:
    for st, rx, fmt in PATTERNS:
        m = rx.search(text)
        if m:
            slug = fmt(m)
            if slug and slug.lower() not in {"embed", "v1", "api", "careers", "jobs"}:
                return {"source_type": st, "slug_or_endpoint": slug}
    return None


def detect_from_text(text: str, careers_url: str = "") -> Dict[str, str]:
    hit = _match_patterns(text)
    if hit:
        hit.update(careers_url=careers_url, note=f"Detected {hit['source_type']} from page links")
        return hit
    for vendor, rx in BROWSER_VENDORS:
        if rx.search(text):
            return {"source_type": "browser", "slug_or_endpoint": careers_url, "careers_url": careers_url,
                    "note": f"{vendor} careers site: needs the browser collector (planned)"}
    return {"source_type": "browser", "slug_or_endpoint": careers_url, "careers_url": careers_url,
            "note": "No known hiring platform found: needs the browser collector (planned)"}


def detect_source(url: str, client: Optional[PoliteClient] = None) -> Dict[str, str]:
    url = url.strip()
    if not urlparse(url).netloc:
        raise ValueError("Enter a full URL, e.g. https://example.com/careers")
    hit = _match_patterns(url)
    if hit:
        hit.update(careers_url=url, note=f"Detected {hit['source_type']} from the URL")
        return hit
    own = client is None
    client = client or PoliteClient()
    try:
        r = client.get(url)
        body = r.text if r.status_code < 400 else ""
        final = str(r.url)
    except Exception as e:
        return {"source_type": "browser", "slug_or_endpoint": url, "careers_url": url,
                "note": f"Could not load the page ({type(e).__name__}); saved as browser source"}
    finally:
        if own:
            client.close()
    hit = _match_patterns(final) or _match_patterns(body)
    if hit:
        hit.update(careers_url=url, note=f"Detected {hit['source_type']} from links on the page")
        return hit
    return detect_from_text(final + "\n" + body, url)
