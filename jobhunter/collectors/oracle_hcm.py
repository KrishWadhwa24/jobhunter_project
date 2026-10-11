from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import urlparse

from ..models import Job
from ..util import PoliteClient, html_to_text
from . import search
from .base import Collector, CollectorError, FetchResult

LIMIT = 25
MAX_PAGES = 12
TERMS = ["software engineer", "graduate", "intern"]


def parse(endpoint: str) -> tuple[str, str]:
    u = urlparse(endpoint.strip())
    m = re.search(r"siteNumber=([A-Za-z0-9_\-]+)", endpoint) or re.search(r"/sites/([A-Za-z0-9_\-]+)", endpoint)
    if not u.netloc or not m:
        raise CollectorError("Oracle HCM endpoint needs the *.oraclecloud.com host and a site number")
    return f"{u.scheme}://{u.netloc}", m.group(1)


class OracleHCM(Collector):
    name = "oracle_hcm"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        host, site = parse(company["slug_or_endpoint"])
        if "oraclecloud.com" not in host:
            raise CollectorError("host is not an oraclecloud.com host; find it in the browser network tab")
        api = f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        seen: dict[str, Job] = {}
        for term in search.terms(TERMS):
            for page in range(MAX_PAGES):
                finder = f"findReqs;siteNumber={site},keyword={term},limit={LIMIT},offset={page * LIMIT},sortBy=POSTING_DATES_DESC"
                data = client.get_json(api, params={"onlyData": "true", "expand": "requisitionList", "finder": finder})
                items = data.get("items") or [{}]
                reqs = items[0].get("requisitionList") or []
                for r in reqs:
                    rid = str(r.get("Id"))
                    if rid in seen:
                        continue
                    seen[rid] = Job(
                        external_id=rid,
                        title=r.get("Title", ""),
                        url=f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                        location=r.get("PrimaryLocation", ""),
                        description=html_to_text(r.get("ShortDescriptionStr", "")),
                        posted_at=r.get("PostedDate"),
                    )
                if len(reqs) < LIMIT:
                    break
        return FetchResult(list(seen.values()), complete=False, note="Oracle HCM: keyword search; closure detection off")
