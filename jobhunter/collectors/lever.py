from __future__ import annotations

from typing import Any, Mapping

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult


class Lever(Collector):
    name = "lever"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        raw = (company["slug_or_endpoint"] or "").strip()
        host = "api.lever.co"
        if raw.lower().startswith("eu:"):
            host, raw = "api.eu.lever.co", raw[3:]
        data = client.get_json(f"https://{host}/v0/postings/{raw}", params={"mode": "json"})
        jobs = []
        for j in data if isinstance(data, list) else []:
            cats = j.get("categories") or {}
            locs = cats.get("allLocations") or [cats.get("location", "")]
            desc = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
            for lst in j.get("lists") or []:
                desc += "\n" + (lst.get("text") or "") + "\n" + html_to_text(lst.get("content", ""))
            desc += "\n" + (j.get("additionalPlain") or "")
            created = j.get("createdAt")
            jobs.append(Job(
                external_id=str(j["id"]),
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                location=" | ".join(x for x in locs if x),
                description=desc.strip(),
                posted_at=str(created) if created else None,
            ))
        return FetchResult(jobs)
