from __future__ import annotations

from typing import Any, Mapping

from .ashby import Ashby
from .base import Collector, Unsupported
from .browser import BrowserCollector
from .custom import for_host
from .eightfold import Eightfold
from .greenhouse import Greenhouse
from .jibe import Jibe
from .kula import Kula
from .lever import Lever
from .oracle_hcm import OracleHCM
from .recruiterbox import Recruiterbox
from .smartrecruiters import SmartRecruiters
from .workday import Workday

SIMPLE = {
    "greenhouse": Greenhouse,
    "lever": Lever,
    "ashby": Ashby,
    "smartrecruiters": SmartRecruiters,
    "workday": Workday,
    "jibe": Jibe,
    "kula": Kula,
    "eightfold": Eightfold,
    "oracle_hcm": OracleHCM,
    "recruiterbox": Recruiterbox,
}

# Source types read with the headless-browser collector.
NEEDS_BROWSER = {"browser", "html", "rss", "linkedin_only"}


def collector_for(company: Mapping[str, Any]) -> Collector:
    st = (company["source_type"] or "").lower()
    if st in SIMPLE:
        return SIMPLE[st]()
    if st == "custom_json":
        return for_host(company["slug_or_endpoint"])
    if st in NEEDS_BROWSER:
        if "linkedin.com" in (company["careers_url"] or "").lower():
            raise Unsupported("LinkedIn is not scraped (its terms forbid it); use the referral pack instead")
        return BrowserCollector()
    raise Unsupported(f"unknown source_type '{st}'")
