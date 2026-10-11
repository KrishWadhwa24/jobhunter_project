"""The truth check: a rewritten line may not contain a number, technology or proper noun that is not already
in the source text it was derived from."""
from __future__ import annotations

import re
from typing import List, Set

from . import terms as T

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CAP = re.compile(r"\b[A-Z][A-Za-z0-9\+\#\.]{2,}\b")
_COMMON_CAPS = {
    "Built", "Designed", "Developed", "Implemented", "Architected", "Optimized", "Led", "Created", "Wrote", "Shipped",
    "Delivered", "Migrated", "Automated", "Improved", "Reduced", "Increased", "Engineered", "Integrated", "Owned",
    "Drove", "Diagnosed", "Replaced", "Operated", "Operate", "Maintained", "Modelled", "Modeled", "Added", "Cut",
    "Load", "Instrumented", "Captured", "Applied", "Reprocessed", "Enabled", "Launched", "Streamlined", "Analyzed",
    "Analysed", "Resolved", "Tested", "Validated", "Contributed", "Collaborated", "Managed", "Produced", "Enhanced",
    "Refactored", "Debugged", "Deployed", "Monitored", "Orchestrated", "Spearheaded", "Established", "Executed",
    "The", "This", "That", "With", "For", "And", "From", "Using", "Across", "Over", "Under", "Into", "Through",
    "Production", "Real", "Live", "Full", "Backend", "Software", "Engineer", "Engineering", "Development", "Systems",
    "Platform", "Services", "Service", "API", "APIs", "REST", "Data", "Event", "Distributed", "Scalable", "High",
    "Low", "Based", "Driven", "End", "Time", "Core", "Clean", "Open", "Source", "Test", "Unit", "Integration",
}


def numbers(text: str) -> Set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def violations(new_text: str, allowed_text: str) -> List[str]:
    """Empty list = the new text only uses facts present in allowed_text."""
    problems: List[str] = []
    allowed_nums = numbers(allowed_text)
    for n in numbers(new_text) - allowed_nums:
        problems.append(f"new number '{n}'")
    new_terms = T.term_set(new_text)
    allowed_terms = T.term_set(allowed_text)
    for t in sorted(new_terms - allowed_terms):
        problems.append(f"new technology/skill '{t}'")
    allowed_lower = (allowed_text or "").lower()
    for w in set(_CAP.findall(new_text or "")):
        if w in _COMMON_CAPS or w.lower() in allowed_lower:
            continue
        # a capitalised word at the very start of the text is just a sentence start
        if (new_text or "").strip().startswith(w):
            continue
        problems.append(f"new name '{w}'")
    return problems


def length_ok(new_text: str, old_text: str, low: float = 0.55, high: float = 1.3) -> bool:
    if not old_text:
        return True
    r = len(new_text) / max(1, len(old_text))
    return low <= r <= high
