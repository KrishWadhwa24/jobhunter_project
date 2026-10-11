"""Telegram Bot API over plain httpx (no extra dependency)."""
from __future__ import annotations

import html
import logging
from typing import Any, Dict, List, Optional

import httpx

from . import config

log = logging.getLogger("jobhunter.telegram")
LEVEL_LABELS = config.LEVEL_LABELS


def esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def job_keyboard(job_id: int) -> Dict[str, Any]:
    return {"inline_keyboard": [
        [{"text": "Apply", "callback_data": f"a:apply:{job_id}"},
         {"text": "Find referrers", "callback_data": f"a:referrers:{job_id}"},
         {"text": "Already applied", "callback_data": f"a:applied:{job_id}"}],
        [{"text": "Not interested", "callback_data": f"a:skip:{job_id}"}],
    ]}


def format_job(job: Any, dashboard_base: str = "") -> str:
    reasons = []
    try:
        import json
        reasons = json.loads(job["reasons"] or "[]")
    except Exception:
        pass
    level = LEVEL_LABELS.get(job["level"], job["level"] or "")
    lines = [
        f"<b>{esc(job['title'])}</b>",
        f"{esc(job['company'])}  ·  {esc(job['location'] or 'location not stated')}",
        f"{esc(level)}  ·  fit {job['score']}/100" + (f"  ·  {esc(job['tier'])}" if job["tier"] else ""),
    ]
    if reasons:
        lines.append("")
        lines += [f"– {esc(r)}" for r in reasons[:4]]
    lines.append("")
    link = f'<a href="{esc(job["url"])}">Open posting</a>'
    if dashboard_base:
        link += f'  ·  <a href="{esc(dashboard_base)}/job/{job["id"]}">Dashboard</a>'
    lines.append(link)
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = httpx.Client(timeout=40)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def has_token(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, **payload: Any) -> Dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        r = self._client.post(url, json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data

    def send(self, text: str, keyboard: Optional[Dict[str, Any]] = None, chat_id: Optional[str] = None,
             html_mode: bool = True) -> int:
        payload: Dict[str, Any] = {"chat_id": chat_id or self.chat_id, "text": text[:4000],
                                   "disable_web_page_preview": True}
        if html_mode:
            payload["parse_mode"] = "HTML"
        if keyboard:
            payload["reply_markup"] = keyboard
        return int(self._call("sendMessage", **payload)["result"]["message_id"])

    def send_document(self, path: str, caption: str = "", chat_id: Optional[str] = None) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendDocument"
        with open(path, "rb") as fh:
            r = self._client.post(url, data={"chat_id": chat_id or self.chat_id, "caption": caption[:1000],
                                             "parse_mode": "HTML"}, files={"document": (path.split("/")[-1], fh)})
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendDocument failed: {data.get('description')}")

    def edit(self, message_id: int, text: str, keyboard: Optional[Dict[str, Any]] = None,
             chat_id: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"chat_id": chat_id or self.chat_id, "message_id": message_id,
                                   "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
        payload["reply_markup"] = keyboard or {"inline_keyboard": []}
        try:
            self._call("editMessageText", **payload)
        except RuntimeError as e:
            log.warning("edit failed: %s", e)

    def answer(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])
        except RuntimeError as e:
            log.warning("answerCallbackQuery failed: %s", e)

    def get_updates(self, offset: Optional[int], timeout: int = 25) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", **payload)["result"]

    def me(self) -> Dict[str, Any]:
        return self._call("getMe")["result"]

    def send_job(self, job: Any, dashboard_base: str = "") -> int:
        return self.send(format_job(job, dashboard_base), job_keyboard(job["id"]))


def from_env() -> Telegram:
    return Telegram(config.secret("TELEGRAM_BOT_TOKEN"), config.secret("TELEGRAM_CHAT_ID"))
