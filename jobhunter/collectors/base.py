from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping

from ..models import Job
from ..util import PoliteClient


class CollectorError(Exception):
    pass


class Unsupported(CollectorError):
    """Source needs a browser or is not implemented yet."""


@dataclass
class FetchResult:
    jobs: List[Job] = field(default_factory=list)
    complete: bool = True      # False when pagination was cut short; do not mark missing jobs as closed
    note: str = ""


class Collector:
    """One adapter per hiring platform. `company` is a mapping with
    name, source_type, slug_or_endpoint, careers_url."""

    name = "base"

    def list_jobs(self, company: Mapping[str, Any], client: PoliteClient) -> FetchResult:
        raise NotImplementedError

    def fetch_detail(self, job: Job, company: Mapping[str, Any], client: PoliteClient) -> str:
        """Return plain-text description for a job whose list entry had none. Default: nothing."""
        return ""
