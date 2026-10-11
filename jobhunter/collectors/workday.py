from __future__ import annotations

import re
from typing import Any, Mapping, Tuple
from urllib.parse import urlparse

from ..models import Job
from ..util import PoliteClient, html_to_text
from . import search
from .base import Collector, CollectorError, FetchResult

PAGE = 20          # Workday rejects larger pages
MAX_PAGES = 8      # per search term
SEARCH_TERMS = ["software engineer", "graduate", "intern", "university", "developer"]
LOCALE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")


def parse_base(url: str) -> Tuple[str, str, str]:
    """https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced -> (host, tenant, site)"""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    u = urlparse(url)
    if not u.netloc or "myworkdayjobs" not in u.netloc:
        raise CollectorError(f"not a Workday URL: {url}")
    parts = [p for p in u.path.split("/") if p]
    while parts and LOCALE.match(parts[0]):
        parts.pop(0)
    if not parts:
        raise CollectorError(f"cannot find Workday site name in {url}")
    return u.netloc, u.netloc.split(".")[0], parts[0]


class Workday(Collector):
    name = "workday"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        host, tenant, site = parse_base(company["slug_or_endpoint"])
        api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        seen: dict[str, Job] = {}
        complete = True
        failures = []
        for term in search.terms(SEARCH_TERMS):
            offset = 0
            for _ in range(MAX_PAGES):
                try:
                    data = client.post_json(api, json={"appliedFacets": {}, "limit": PAGE, "offset": offset,
                                                       "searchText": term})
                except Exception as e:       # one rejected search term should not lose the others
                    failures.append(e)
                    break
                posts = data.get("jobPostings", [])
                for p in posts:
                    path = p.get("externalPath", "")
                    if not path or path in seen:
                        continue
                    bullets = p.get("bulletFields") or []
                    seen[path] = Job(
                        external_id=(bullets[0] if bullets else path),
                        title=p.get("title", ""),
                        url=f"https://{host}/{site}{path}",
                        location=p.get("locationsText", ""),
                        posted_at=p.get("postedOn"),
                        extra={"path": path},
                    )
                offset += PAGE
                if offset >= data.get("total", 0) or not posts:
                    break
            else:
                complete = False
        if failures and not seen:
            raise failures[0]
        # Searching by term only returns a subset, so a job missing from one run is not proof it closed.
        return FetchResult(list(seen.values()), complete=False,
                           note="Workday: keyword search; closure detection disabled")

    def fetch_detail(self, job: Job, company: Mapping[str, Any], client: PoliteClient) -> str:
        host, tenant, site = parse_base(company["slug_or_endpoint"])
        data = client.get_json(f"https://{host}/wday/cxs/{tenant}/{site}{job.extra['path']}")
        info = data.get("jobPostingInfo") or {}
        extra_loc = info.get("additionalLocations") or []
        if info.get("location"):
            job.location = ", ".join([info["location"]] + list(extra_loc)) if extra_loc else info["location"]
        return html_to_text(info.get("jobDescription", ""))
