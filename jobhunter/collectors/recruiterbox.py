from __future__ import annotations

from typing import Any, Mapping

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult


class Recruiterbox(Collector):
    """Trakstar Hire / Recruiterbox: https://jsapi.recruiterbox.com/v1/openings?client_name=<x>"""
    name = "recruiterbox"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        data = client.get_json(company["slug_or_endpoint"])
        items = data if isinstance(data, list) else data.get("objects", data.get("openings", []))
        jobs = []
        for j in items:
            loc = j.get("location") or {}
            if isinstance(loc, dict):
                loc = ", ".join(x for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x)
            jobs.append(Job(
                external_id=str(j.get("id")),
                title=j.get("title", ""),
                url=j.get("hosted_url") or j.get("apply_url") or "",
                location=str(loc or ""),
                description=html_to_text(j.get("description", "")),
                posted_at=j.get("created_date"),
            ))
        return FetchResult(jobs)
