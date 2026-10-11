"""Company-specific JSON endpoints (source_type custom_json), chosen by hostname."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse

from ..models import Job
from ..util import PoliteClient, html_to_text
from .base import Collector, FetchResult, Unsupported

AMAZON_QUERIES = [
    {"category[]": "software-development"},
    {"business_category[]": "student-programs"},
]


class Amazon(Collector):
    name = "amazon"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        seen: dict[str, Job] = {}
        for extra in AMAZON_QUERIES:
            offset = 0
            while offset < 1000:
                params = {"country": "IND", "result_limit": 100, "offset": offset, "sort": "recent", **extra}
                data = client.get_json("https://www.amazon.jobs/en/search.json", params=params)
                items = data.get("jobs", [])
                for j in items:
                    jid = str(j.get("id_icims") or j.get("id"))
                    if jid in seen:
                        continue
                    desc = "\n\n".join(html_to_text(j.get(k, "")) for k in
                                       ("description", "basic_qualifications", "preferred_qualifications"))
                    seen[jid] = Job(
                        external_id=jid,
                        title=j.get("title", ""),
                        url="https://www.amazon.jobs" + (j.get("job_path") or f"/en/jobs/{jid}"),
                        location=j.get("normalized_location") or j.get("location", ""),
                        description=desc,
                        posted_at=j.get("posted_date"),
                    )
                if len(items) < 100:
                    break
                offset += 100
        return FetchResult(list(seen.values()), complete=False, note="Amazon: two queries; closure detection off")


class Atlassian(Collector):
    name = "atlassian"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        data = client.get_json("https://www.atlassian.com/endpoint/careers/listings")
        items = data if isinstance(data, list) else data.get("jobs", [])
        jobs = []
        for j in items:
            locs = j.get("locations") or [j.get("location", "")]
            jobs.append(Job(
                external_id=str(j.get("id")),
                title=j.get("title", ""),
                url=j.get("applyUrl") or f"https://www.atlassian.com/company/careers/details/{j.get('id')}",
                location=" | ".join(str(x) for x in locs if x),
                description=html_to_text(j.get("overview", "")) + "\n" + html_to_text(j.get("qualifications", "")),
                posted_at=(j.get("portalJobPost") or {}).get("updatedDate"),
            ))
        return FetchResult(jobs)


class Rippling(Collector):
    name = "rippling"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        data = client.get_json(company["slug_or_endpoint"])
        items = data if isinstance(data, list) else data.get("items", data.get("jobs", []))
        jobs = []
        for j in items:
            wl = j.get("workLocation") or {}
            loc = wl.get("label") if isinstance(wl, dict) else str(wl or "")
            jobs.append(Job(
                external_id=str(j.get("uuid") or j.get("id")),
                title=j.get("name", ""),
                url=j.get("url", ""),
                location=loc or "",
            ))
        return FetchResult(jobs)


def for_host(endpoint: str) -> Collector:
    host = urlparse(endpoint).netloc.lower()
    if "amazon.jobs" in host:
        return Amazon()
    if "atlassian.com" in host:
        return Atlassian()
    if "rippling.com" in host:
        return Rippling()
    raise Unsupported(f"no adapter yet for custom JSON endpoint on {host or endpoint!r}")
