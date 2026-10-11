from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult

LIMIT = 100
MAX_PAGES = 10


class Jibe(Collector):
    """Jibe (iCIMS) career sites: https://careers.<company>.com/api/jobs?location=India&page=1"""
    name = "jibe"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        u = urlparse(company["slug_or_endpoint"])
        base = f"{u.scheme}://{u.netloc}/api/jobs"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        q.setdefault("location", "India")
        q["limit"] = LIMIT
        jobs, complete = [], True
        for page in range(1, MAX_PAGES + 1):
            q["page"] = page
            data = client.get_json(base, params=q)
            items = data.get("jobs", [])
            for it in items:
                d = it.get("data", it)
                loc = d.get("location_name") or ", ".join(x for x in [d.get("city"), d.get("state"), d.get("country")] if x)
                jobs.append(Job(
                    external_id=str(d.get("req_id") or d.get("slug") or d.get("title")),
                    title=d.get("title", ""),
                    url=d.get("apply_url") or d.get("canonical_url") or "",
                    location=loc or "",
                    description=html_to_text(d.get("description", "")) + "\n" + html_to_text(d.get("qualifications", "")),
                    posted_at=d.get("posted_date") or d.get("create_date"),
                ))
            if page * LIMIT >= data.get("totalCount", 0) or not items:
                break
        else:
            complete = False
        return FetchResult(jobs, complete=complete)
