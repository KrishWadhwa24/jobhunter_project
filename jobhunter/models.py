from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Job:
    external_id: str
    title: str
    url: str
    location: str = ""
    description: str = ""          # plain text
    posted_at: Optional[str] = None
    extra: dict = field(default_factory=dict)  # collector-specific data needed for detail fetch
