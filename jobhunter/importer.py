from __future__ import annotations

import csv
import io
from typing import Dict, List, Tuple

from . import db

REQUIRED = ["company"]


def import_csv_text(text: str) -> Tuple[int, List[str]]:
    """Import companies from CSV text. Returns (count, warnings)."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "company" not in [f.strip().lower() for f in reader.fieldnames]:
        raise ValueError("CSV needs a 'company' column. Expected columns: company, category, pay_tier, "
                         "source_type, slug_or_endpoint, careers_url, verified, active, notes")
    n, warnings = 0, []
    for i, raw in enumerate(reader, start=2):
        row: Dict[str, str] = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name = row.get("company", "")
        if not name:
            warnings.append(f"line {i}: empty company name, skipped")
            continue
        db.upsert_company({
            "name": name,
            "category": row.get("category", ""),
            "pay_tier": row.get("pay_tier", ""),
            "source_type": row.get("source_type", "") or "browser",
            "slug_or_endpoint": row.get("slug_or_endpoint", ""),
            "careers_url": row.get("careers_url", ""),
            "verified": row.get("verified", "no"),
            "active": row.get("active", "yes") or "yes",
            "notes": row.get("notes", ""),
        })
        n += 1
    return n, warnings


def import_csv_file(path: str) -> Tuple[int, List[str]]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return import_csv_text(f.read())
