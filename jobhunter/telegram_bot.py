"""Handles incoming Telegram updates: button presses and a few commands."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from . import actions, config, db
from .notifier import Telegram, esc, format_job

log = logging.getLogger("jobhunter.telegram")

HELP = ("Commands:\n/status  what the tracker is doing\n/new  your current top matches\n/help  this message\n\n"
        "Tap the buttons under a job to apply, get referrer help, or mark it applied.")


def _status_text() -> str:
    c = db.job_counts()
    comps = db.list_companies()
    ok = sum(1 for r in comps if r["last_status"] == "ok")
    err = sum(1 for r in comps if r["last_status"] == "error")
    skipped = sum(1 for r in comps if r["last_status"] == "skipped")
    net = sum(1 for r in comps if r["last_status"] == "network")
    last = db.kv_get("last_cycle_at", "never")
    return (f"Last check: {last}\nCompanies: {len(comps)} ({ok} ok, {err} failing, {net} connection problems, {skipped} need the browser step)\n"
            f"Open jobs in scope: {c['in_scope']}  ·  new matches: {c['new_matches']}\n"
            f"In referral: {c['referral']}  ·  applied: {c['applied']}")


def handle_update(update: Dict[str, Any], tg: Telegram, cfg: Dict[str, Any], dashboard_base: str = "") -> None:
    if "callback_query" in update:
        _handle_callback(update["callback_query"], tg, cfg, dashboard_base)
        return
    msg = update.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat_id:
        return
    if not tg.chat_id:
        # Not configured yet: only help the owner discover their chat id.
        if text.startswith("/start"):
            tg.send(f"Your chat id is <code>{esc(chat_id)}</code>. Put it in .env as TELEGRAM_CHAT_ID, "
                    "then restart the service.", chat_id=chat_id)
        return
    if chat_id != str(tg.chat_id):
        return        # ignore everyone else
    cmd = text.split()[0].lower() if text else ""
    if cmd in ("/start", "/help"):
        tg.send(esc(HELP))
    elif cmd == "/status":
        tg.send(esc(_status_text()))
    elif cmd == "/new":
        rows = db.query_jobs(["new"], list(cfg["targets"]["levels"]), ["match"], limit=5)
        if not rows:
            tg.send("No new matches right now.")
        for r in rows:
            tg.send_job(r, dashboard_base)


def _handle_callback(cb: Dict[str, Any], tg: Telegram, cfg: Dict[str, Any], dashboard_base: str) -> None:
    msg = cb.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if not tg.chat_id or chat_id != str(tg.chat_id):
        tg.answer(cb["id"], "Not authorised")
        return
    data = cb.get("data", "")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "a" or not parts[2].isdigit():
        tg.answer(cb["id"], "Unknown button")
        return
    action, job_id = parts[1], int(parts[2])
    result = actions.perform(job_id, action, cfg)
    tg.answer(cb["id"], (result.get("message") or "")[:150] if not result.get("pack") else "Referral help sent")
    job = db.get_job(job_id)
    mid = msg.get("message_id")
    if not result["ok"]:
        return
    if action == "referrers":
        tg.send(esc(result["message"]))
        return
    label = {"apply": "Apply requested", "applied": "Marked applied", "skip": "Hidden"}.get(action, "Updated")
    if mid and job:
        tg.edit(mid, format_job(job, dashboard_base) + f"\n\n<i>{esc(label)}</i>", chat_id=chat_id)
    if action == "apply":
        tg.send(esc(result["message"]))


def poll_forever(tg: Telegram, cfg: Dict[str, Any], stop: threading.Event, dashboard_base: str = "") -> None:
    offset: Optional[int] = None
    saved = db.kv_get("telegram_offset")
    if saved and saved.lstrip("-").isdigit():
        offset = int(saved)
    backoff = 2
    while not stop.is_set():
        try:
            updates = tg.get_updates(offset, timeout=25)
            backoff = 2
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    handle_update(u, tg, cfg, dashboard_base)
                except Exception:
                    log.exception("error handling update")
                db.kv_set("telegram_offset", str(offset))
        except Exception as e:
            log.warning("telegram poll error: %s (retry in %ss)", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 120)
