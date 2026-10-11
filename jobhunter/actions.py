"""Shared by the dashboard and the Telegram bot: what each button does."""
from __future__ import annotations

from typing import Any, Dict

from . import db, redis_store, referrers

ACTIONS = {"apply", "referrers", "applied", "skip", "reopen"}


def perform(job_id: int, action: str, cfg: Dict[str, Any], fill: bool = True) -> Dict[str, Any]:
    """Returns {ok, state, message, pack?}. fill=False (the site opened remotely): Apply only tailors the resume, it does
    not drive Chrome on the laptop."""
    job = db.get_job(job_id)
    if not job:
        return {"ok": False, "message": "That job no longer exists."}
    if action not in ACTIONS:
        return {"ok": False, "message": f"Unknown action {action}."}

    if action == "apply":
        db.set_job_state(job_id, "apply_queued", "apply requested")
        if not fill:
            from .resume import runner
            runner.start(job_id, cfg, notify=False)
            return {"ok": True, "state": "apply_queued",
                    "message": "Preparing a resume tailored to this job. Download it, then open the posting to apply."}
        redis_store.enqueue_apply(job_id)
        from .apply import worker
        worker.start(job_id, cfg)
        return {"ok": True, "state": "apply_queued",
                "message": ("Working on it: tailoring your resume, then opening the form in Chrome on your Mac and "
                            "filling it. I will message you when it is ready, or if I need you.")}
    if action == "referrers":
        db.set_job_state(job_id, "referral", "referral help requested")
        pack = referrers.build_pack(job, cfg.get("profile", {}))
        return {"ok": True, "state": "referral", "pack": pack, "message": referrers.as_text(pack)}
    if action == "applied":
        db.set_job_state(job_id, "applied", "marked as already applied")
        return {"ok": True, "state": "applied", "message": "Marked as applied and archived."}
    if action == "skip":
        db.set_job_state(job_id, "hidden", "not interested")
        return {"ok": True, "state": "hidden", "message": "Hidden. You will not be alerted about this job again."}
    if action == "reopen":
        db.set_job_state(job_id, "new", "reopened")
        return {"ok": True, "state": "new", "message": "Moved back to New."}
    return {"ok": False, "message": "Nothing to do."}
