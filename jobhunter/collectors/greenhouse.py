from __future__ import annotations

from typing import Any, Mapping

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult


def _split_slug(raw: str) -> tuple[str, str]:
    """'eu:groww' -> ('boards-api.eu.greenhouse.io', 'groww')."""
    raw = (raw or "").strip()
    if raw.lower().startswith("eu:"):
        return "boards-api.eu.greenhouse.io", raw[3:]
    return "boards-api.greenhouse.io", raw


class Greenhouse(Collector):
    name = "greenhouse"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        host, slug = _split_slug(company["slug_or_endpoint"])
        data = client.get_json(f"https://{host}/v1/boards/{slug}/jobs", params={"content": "true"})
        jobs = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            jobs.append(Job(
                external_id=str(j["id"]),
                title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                location=loc,
                description=html_to_text(j.get("content", "")),
                posted_at=j.get("updated_at"),
            ))
        return FetchResult(jobs)
