from __future__ import annotations

from typing import Any, Mapping

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult


class Ashby(Collector):
    name = "ashby"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        slug = (company["slug_or_endpoint"] or "").strip()
        data = client.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        jobs = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            locs = [j.get("location", "")]
            for s in j.get("secondaryLocations") or []:
                locs.append(s.get("location", "") if isinstance(s, dict) else str(s))
            if j.get("isRemote"):
                locs.append("Remote")
            jobs.append(Job(
                external_id=str(j.get("id") or j.get("jobUrl")),
                title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                location=" | ".join(x for x in locs if x),
                description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                posted_at=j.get("publishedAt"),
            ))
        return FetchResult(jobs)
