"""Runs resume tailoring in the background for a job and records the result."""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config, db, notifier

log = logging.getLogger("jobhunter.resume")
_lock = threading.Lock()          # one resume at a time: keeps LLM rate limits happy
_active: Dict[int, bool] = {}


def resume_dir(job_id: int) -> Path:
    d = config.DATA_DIR / "resumes" / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:40] or "Role"


def is_running(job_id: int) -> bool:
    return bool(_active.get(job_id))


def start(job_id: int, cfg: Dict[str, Any], notify: bool = True) -> None:
    if _active.get(job_id):
        return
    _active[job_id] = True
    rid = db.resume_start(job_id)
    t = threading.Thread(target=_work, args=(job_id, rid, cfg, notify), name=f"resume-{job_id}", daemon=True)
    t.start()


def ensure_resume(job_id: int, cfg: Dict[str, Any], wait_seconds: int = 240) -> Optional[Dict[str, Any]]:
    """Tailor a fresh resume for this job's current posting, every time (never reuse an older one).
    If a run for this job is already in progress, wait for that one instead of starting a second."""
    import time
    deadline = time.time() + wait_seconds
    if _active.get(job_id):
        while _active.get(job_id) and time.time() < deadline:
            time.sleep(1)
    else:
        _active[job_id] = True
        _work(job_id, db.resume_start(job_id), cfg, True)           # runs here, clears _active when done
    r = db.get_resume(job_id)
    return r if r and r["status"] == "ready" and r.get("pdf_path") and Path(r["pdf_path"]).exists() else None


def _fill_description(job: Dict[str, Any]) -> str:
    """The posting as it is right now: re-read it from the company's site so the resume matches the current text
    (employers edit requirements). Falls back to the stored text when the site cannot be read."""
    stored = job.get("description") or ""
    try:
        from ..collectors.registry import collector_for
        from ..models import Job
        from ..util import PoliteClient
        comp = db.get_company(job["company_id"])
        client = PoliteClient()
        try:
            extra = {}
            if comp["source_type"] == "workday":
                from ..pipeline import _workday_path
                extra["path"] = _workday_path(job["url"], dict(comp))
            desc = collector_for(dict(comp)).fetch_detail(
                Job(job["external_id"], job["title"], job["url"], job["location"], extra=extra), dict(comp), client)
        finally:
            client.close()
        if desc:
            with db.connect() as c:
                c.execute("UPDATE jobs SET description=?, detail_fetched=1 WHERE id=?", (desc[:20000], job["id"]))
        return desc or stored
    except Exception as e:
        log.info("could not re-read the posting for job %s, using the stored text: %s", job["id"], e)
        return stored


def _work(job_id: int, rid: int, cfg: Dict[str, Any], notify: bool) -> None:
    from . import tailor
    try:
        with _lock:
            job = db.get_job(job_id)
            if not job:
                db.resume_finish(rid, "failed", error="job not found")
                return
            job = dict(job)
            job["description"] = _fill_description(job)
            res = tailor.tailor(job, cfg)
            if not res.ok:
                db.resume_finish(rid, "failed", error=res.error)
                return
            d = resume_dir(job_id)
            company = db.get_company(job["company_id"])
            base = f"Krish_Wadhwa_{_slug(company['name'] if company else 'Company')}"
            pdf_path, docx_path = d / f"{base}.pdf", d / f"{base}.docx"
            pdf_path.write_bytes(res.pdf)
            docx_path.write_bytes(res.docx)
            details = {"ats": res.ats, "notes": res.notes, "gaps": res.gaps_unclaimable, "llm": res.llm,
                       "pages": res.pages, "jd_terms": res.jd_terms, "had_description": bool(job["description"])}
            db.resume_finish(rid, "ready", res.score, res.target_reached, details, str(pdf_path), str(docx_path))
            db.add_event(job_id, "resume", f"tailored resume, score {res.score:.0f}")
        if notify:
            _notify(job_id, job, res, pdf_path)
    except Exception as e:           # never lose the reason
        log.exception("resume tailoring failed for job %s", job_id)
        db.resume_finish(rid, "failed", error=f"{type(e).__name__}: {e}")
    finally:
        _active.pop(job_id, None)


def _notify(job_id: int, job: Dict[str, Any], res: Any, pdf_path: Path) -> None:
    tg = notifier.from_env()
    if not tg.configured:
        return
    badge = "reached the 95 target" if res.target_reached else "below the 95 target"
    caption = (f"<b>Resume ready</b>: {notifier.esc(job['title'])}\nScore {res.score:.0f}/100 ({badge}).")
    if res.gaps_unclaimable:
        caption += "\nNot in your facts: " + notifier.esc(", ".join(res.gaps_unclaimable[:6]))
    try:
        tg.send_document(str(pdf_path), caption)
    except Exception as e:
        log.warning("could not send the resume to Telegram: %s", e)
