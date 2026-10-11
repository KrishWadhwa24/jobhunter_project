from __future__ import annotations

from typing import Any, Mapping

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult

PAGE = 100
MAX_PAGES = 10


class SmartRecruiters(Collector):
    name = "smartrecruiters"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        cid = (company["slug_or_endpoint"] or "").strip()
        base = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
        jobs, offset, complete = [], 0, True
        for _ in range(MAX_PAGES):
            data = client.get_json(base, params={"country": "in", "limit": PAGE, "offset": offset})
            content = data.get("content", [])
            for j in content:
                loc = j.get("location") or {}
                locs = ", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x)
                jobs.append(Job(
                    external_id=str(j["id"]),
                    title=j.get("name", ""),
                    url=f"https://jobs.smartrecruiters.com/{cid}/{j['id']}",
                    location=locs,
                    posted_at=j.get("releasedDate"),
                    extra={"detail": j.get("ref") or f"{base}/{j['id']}"},
                ))
            offset += PAGE
            if offset >= data.get("totalFound", 0) or not content:
                break
        else:
            complete = False
        return FetchResult(jobs, complete=complete)

    def fetch_detail(self, job: Job, company: Mapping[str, Any], client: PoliteClient) -> str:
        data = client.get_json(job.extra["detail"])
        secs = ((data.get("jobAd") or {}).get("sections")) or {}
        parts = []
        for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
            s = secs.get(key) or {}
            parts.append(html_to_text(s.get("text", "")))
        return "\n\n".join(p for p in parts if p)
