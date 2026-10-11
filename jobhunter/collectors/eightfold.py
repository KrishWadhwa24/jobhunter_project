from __future__ import annotations

from typing import Any, List, Mapping
from urllib.parse import parse_qs, urlparse

from ..models import Job
from ..util import PoliteClient, html_to_text
from . import search
from .base import Collector, CollectorError, FetchResult

PAGE = 10
MAX_PAGES = 30
QUERIES = ["software engineer", "graduate", "intern", "university", "developer"]


def _positions(data: Any) -> List[dict]:
    if isinstance(data, dict):
        d = data.get("data", data)
        for key in ("positions", "jobs", "results"):
            if isinstance(d, dict) and isinstance(d.get(key), list):
                return d[key]
    return []


class Eightfold(Collector):
    """Eightfold career sites expose /api/pcsx/search?domain=...&query=...&location=...&start=N"""
    name = "eightfold"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        u = urlparse(company["slug_or_endpoint"])
        if not u.netloc:
            raise CollectorError("Eightfold endpoint must be a full URL")
        base = f"{u.scheme}://{u.netloc}{u.path}"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        q.setdefault("location", "India")
        seen: dict[str, Job] = {}
        for term in search.terms(QUERIES):
            q["query"] = term
            for page in range(MAX_PAGES):
                q["start"] = page * PAGE
                data = client.get_json(base, params=q)
                items = _positions(data)
                for it in items:
                    pid = str(it.get("id") or it.get("display_job_id") or it.get("name"))
                    if pid in seen:
                        continue
                    locs = it.get("locations") or it.get("standardizedLocations") or [it.get("location", "")]
                    path = it.get("positionUrl") or f"/careers/job/{pid}"
                    seen[pid] = Job(
                        external_id=pid,
                        title=it.get("name") or it.get("title", ""),
                        url=path if path.startswith("http") else f"{u.scheme}://{u.netloc}{path}",
                        location=" | ".join(str(x) for x in locs if x),
                        description=html_to_text(it.get("job_description", "")),
                        posted_at=str(it.get("postedTs") or "") or None,
                    )
                if len(items) < PAGE:
                    break
        return FetchResult(list(seen.values()), complete=False, note="Eightfold: keyword search; closure detection off")
