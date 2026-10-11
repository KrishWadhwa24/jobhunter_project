"""Kula (careers.kula.ai) career sites. The page itself loads its jobs from a public JSON feed, so no browser is needed:
https://careers.kula.ai/api/internal/ats_job_posts?accountName=<account>&page=1&items=99"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, CollectorError, FetchResult

API = "https://careers.kula.ai/api/internal/ats_job_posts"
PAGE_SIZE = 99
MAX_PAGES = 10


def account_of(slug: str) -> str:
    """Accepts 'slice', 'https://careers.kula.ai/slice?jobs=true' or the API URL."""
    s = (slug or "").strip()
    m = re.search(r"accountName=([\w\-]+)", s) or re.search(r"careers\.kula\.ai/([\w\-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w\-]+", s):
        return s
    raise CollectorError("Kula source needs the account name, e.g. 'slice' (from careers.kula.ai/slice)")


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _expired(end_at: Any) -> bool:
    if not end_at:
        return False
    try:
        return datetime.fromisoformat(str(end_at).replace("Z", "+00:00")) < datetime.now(timezone.utc)
    except ValueError:
        return False


class Kula(Collector):
    name = "kula"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        account = account_of(company["slug_or_endpoint"])
        jobs = {}
        for page in range(1, MAX_PAGES + 1):
            data = client.get_json(API, params={"accountName": account, "page": page,
                                                "type": "ats_job_post.index", "items": PAGE_SIZE})
            for j in data.get("data") or []:
                if j.get("listed") is False or j.get("is_confidential") or _expired(j.get("end_at")):
                    continue
                a = j.get("ats_job") or {}
                title = " ".join(str(j.get("title") or "").split())
                if not title:
                    continue
                if (a.get("employment_type") or "") == "internship" and "intern" not in title.lower():
                    title += " (Internship)"
                locs = []
                for o in a.get("offices") or []:
                    loc = o.get("location") or o.get("name") or ""
                    if loc and loc not in locs:
                        locs.append(loc)
                if (a.get("workplace") or "") == "remote" and "Remote" not in locs:
                    locs.append("Remote")
                jid = str(j.get("id"))
                jobs[jid] = Job(
                    external_id=jid,
                    title=title,
                    url=f"https://careers.kula.ai/{account}/{jid}-{_slug(str(j.get('title') or ''))}?jobs=true",
                    location=" | ".join(locs),
                    description=html_to_text(a.get("job_description") or ""),
                    posted_at=j.get("launch_at"),
                )
            pages = int((data.get("meta") or {}).get("pages") or 1)
            if page >= pages:
                break
        return FetchResult(list(jobs.values()))
