"""The Apply button: tailor the resume, open the form in Chrome, fill it, then tell you what is left.
Jobs are handled one at a time on a single background thread (one visible window, one resume at a time)."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, Optional, Tuple

from .. import db, notifier
from ..collectors.base import Unsupported
from ..resume import runner
from . import browser
from .profile import load_profile

log = logging.getLogger("jobhunter.apply")
_q: "queue.Queue[Tuple[int, int, Dict[str, Any]]]" = queue.Queue()
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_pending: Dict[int, int] = {}          # job_id -> application id (queued or running)


def is_running(job_id: int) -> bool:
    return job_id in _pending


def start(job_id: int, cfg: Dict[str, Any]) -> int:
    global _thread
    with _lock:
        if job_id in _pending:
            return _pending[job_id]
        app_id = db.application_start(job_id)
        _pending[job_id] = app_id
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="apply-worker", daemon=True)
            _thread.start()
    _q.put((job_id, app_id, cfg))
    return app_id


def _loop() -> None:
    while True:
        job_id, app_id, cfg = _q.get()
        try:
            process(job_id, app_id, cfg)
        except Exception as e:           # never lose the reason
            log.exception("apply failed for job %s", job_id)
            db.application_update(app_id, "failed", error=f"{type(e).__name__}: {e}")
            _tell(job_id, f"Auto-fill crashed: {type(e).__name__}: {e}", failed=True)
        finally:
            _pending.pop(job_id, None)


def process(job_id: int, app_id: int, cfg: Dict[str, Any]) -> None:
    job = db.get_job(job_id)
    if not job:
        db.application_update(app_id, "failed", error="job not found")
        return
    db.application_update(app_id, "tailoring")
    res = runner.ensure_resume(job_id, cfg)
    if not res:
        r = db.get_resume(job_id)
        why = (r or {}).get("error") or "the resume could not be tailored"
        db.application_update(app_id, "failed", error=f"resume: {why}")
        _tell(job_id, f"Could not tailor a resume ({why}). Apply on the posting yourself.", failed=True)
        return
    db.application_update(app_id, "filling")
    ctx = None
    try:
        from .writer import ApplyContext
        company = db.get_company(job["company_id"])
        ctx = ApplyContext(dict(job), company["name"] if company else "", cfg, out_dir=runner.resume_dir(job_id))
    except Exception as e:
        log.warning("could not prepare drafts and the cover letter (%s); continuing without them", e)
    try:
        report = browser.fill_job(job["url"], load_profile(), res["pdf_path"], ctx,
                                  on_step=lambda step: _on_step(job_id, app_id, step))
    except Unsupported as e:
        db.application_update(app_id, "failed", error=str(e))
        _tell(job_id, str(e), failed=True)
        return
    except Exception as e:
        db.application_update(app_id, "failed", error=f"{type(e).__name__}: {e}"[:300])
        _tell(job_id, f"Could not open the form ({type(e).__name__}). Apply on the posting yourself.", failed=True)
        return
    status = report.get("status", "failed")
    db.application_update(app_id, status, report=report, error="" if status != "failed" else "; ".join(report.get("notes", []))[:300])
    db.add_event(job_id, "autofill", f"{status}: {len(report.get('filled', []))} fields filled, "
                                     f"{len(report.get('needs', []))} need you")
    _tell(job_id, _summary(report), failed=(status == "failed"), report=report)


def _on_step(job_id: int, app_id: int, step: Dict[str, Any]) -> None:
    """A later step of a multi-page application (after you logged in or pressed Next) was filled."""
    app = db.get_application(job_id)
    rep = (app or {}).get("report") or {}
    rep.setdefault("steps", []).append({"url": step.get("final_url", ""), "filled": len(step["filled"]),
                                        "needs": [n["label"] for n in step["needs"]], "drafts": len(step["drafts"])})
    rep["filled"] = rep.get("filled", []) + step["filled"]
    rep["drafts"] = rep.get("drafts", []) + step["drafts"]
    rep["needs"] = step["needs"]
    rep["login"] = False
    rep["final_url"] = step.get("final_url") or rep.get("final_url", "")
    rep["resume_uploaded"] = rep.get("resume_uploaded") or step.get("resume_uploaded", False)
    status = "needs_you" if step["needs"] else "ready"
    db.application_update(app_id, status, report=rep)
    db.add_event(job_id, "autofill", f"next step: {len(step['filled'])} fields filled, {len(step['needs'])} need you")
    if step["needs"] or step["drafts"]:
        _tell(job_id, "<b>Next step filled.</b>\n" + _step_text(step), report={"status": status})


def _step_text(report: Dict[str, Any]) -> str:
    esc = notifier.esc
    lines = [f"Filled {len(report.get('filled', []))} fields."]
    if report.get("drafts"):
        lines.append("Drafted from your facts (read before submitting):")
        lines += [f"• {esc(d['label'])}" for d in report["drafts"][:6]]
    if report.get("needs"):
        lines.append("Still to do:")
        lines += [f"• {esc(n['label'])}" + (f" ({esc(n['why'])})" if n.get("why") else "") for n in report["needs"][:8]]
    return "\n".join(lines)


def _summary(report: Dict[str, Any]) -> str:
    esc = notifier.esc
    filled, needs = report.get("filled", []), report.get("needs", [])
    if report.get("status") == "ready":
        head = "<b>Form filled. Your turn.</b>"
        body = (f"Filled {len(filled)} fields" + (" and attached your tailored resume" if report.get("resume_uploaded") else "")
                + ". Review the Chrome window that is open on your Mac, then press Submit yourself.")
    elif report.get("status") == "needs_you":
        head = "<b>Needs you.</b>"
        body = f"Filled {len(filled)} fields. The Chrome window is open on your Mac for the rest."
    else:
        head = "<b>Could not auto-fill.</b>"
        body = "; ".join(report.get("notes", [])) or "Could not find the form."
    lines = [body]
    if report.get("drafts"):
        lines.append("Drafted from your facts (read before submitting):")
        lines += [f"• {esc(d['label'])}" for d in report["drafts"][:6]]
    if needs:
        lines.append("Still to do:")
        lines += [f"• {esc(n['label'])}" + (f" ({esc(n['why'])})" if n.get("why") else "") for n in needs[:8]]
    for n in report.get("notes", []):
        if report.get("status") != "failed" and "drafted" not in n:
            lines.append(esc(n))
    return head + "\n" + "\n".join(lines)


def _tell(job_id: int, text: str, failed: bool = False, report: Optional[Dict[str, Any]] = None) -> None:
    tg = notifier.from_env()
    if not tg.configured:
        return
    job = db.get_job(job_id)
    title = notifier.esc(job["title"]) if job else f"job {job_id}"
    msg = f"{text}\n\n{title}"
    if job and (failed or (report or {}).get("status") != "ready"):
        msg += f"\n{notifier.esc(job['url'])}"
    kb = {"inline_keyboard": [[{"text": "Already applied", "callback_data": f"a:applied:{job_id}"}]]}
    try:
        tg.send(msg, keyboard=kb)
    except Exception as e:
        log.warning("could not send the apply update to Telegram: %s", e)
