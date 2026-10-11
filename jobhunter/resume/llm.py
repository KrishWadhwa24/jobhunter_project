"""Two OpenAI-compatible chat APIs: Groq (job analysis) and GitHub Models (rewriting).
Both are optional: without keys the tailor still works with its rule-based path."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .. import config

log = logging.getLogger("jobhunter.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GITHUB_URL = "https://models.github.ai/inference/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GITHUB_CATALOG_URL = "https://models.github.ai/catalog/models"
# Model names change often, so the app asks each service what it offers and takes the first preference that exists.
GROQ_PREFER = ["llama-3.3-70b", "llama-3.1-70b", "llama-4-maverick", "llama-4-scout", "kimi", "gpt-oss-120b",
               "qwen", "gpt-oss-20b", "llama-3.1-8b"]
GITHUB_PREFER = ["openai/gpt-4.1-mini", "openai/gpt-4o-mini", "openai/gpt-4.1", "openai/gpt-4o",
                 "meta/llama-3.3-70b-instruct", "meta/meta-llama-3.1-70b-instruct", "microsoft/phi-4",
                 "mistral-ai/mistral-small"]
NOT_CHAT = ("whisper", "tts", "guard", "embed", "moderation", "transcribe", "image", "vision-preview", "orpheus")


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, name: str, url: str, model: str, key: str, extra_headers: Optional[Dict[str, str]] = None,
                 json_mode: bool = True) -> None:
        self.name, self.url, self.model, self.key = name, url, model, key
        self.extra_headers = extra_headers or {}
        self.json_mode = json_mode
        self._client = httpx.Client(timeout=60, follow_redirects=True)

    def chat(self, system: str, user: str, *, json_out: bool = False, max_tokens: int = 1800,
             temperature: float = 0.2) -> str:
        body: Dict[str, Any] = {"model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_out and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", **self.extra_headers}
        last = ""
        for attempt in range(3):
            try:
                r = self._client.post(self.url, json=body, headers=headers)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = min(float(r.headers.get("retry-after", 2 * (attempt + 1)) or 2), 20)
                last = f"HTTP {r.status_code}"
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise LLMError(f"{self.name}: key rejected (HTTP {r.status_code}: {r.text[:160].strip()}). "
                               "Check the key in .env and that it is allowed to use models.")
            if r.status_code >= 400:
                raise LLMError(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}")
            try:
                msg = r.json()["choices"][0]["message"]
                content = msg.get("content") or ""
                if not content.strip() and body["max_tokens"] < 8000:
                    # reasoning models spend tokens thinking before answering: give them room and retry once
                    body["max_tokens"] = min(body["max_tokens"] * 6, 8000)
                    continue
                return content
            except Exception:
                raise LLMError(f"{self.name}: unexpected response (HTTP {r.status_code}, "
                               f"{r.headers.get('content-type', '?')}, server={r.headers.get('server', '?')}, "
                               f"url={r.url}): {r.text[:200]!r}")
        raise LLMError(f"{self.name}: gave up after retries ({last})")

    def chat_json(self, system: str, user: str, **kw: Any) -> Dict[str, Any]:
        txt = self.chat(system, user, json_out=True, **kw)
        return parse_json(txt)


def parse_json(txt: str) -> Dict[str, Any]:
    txt = (txt or "").strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        out = json.loads(txt)
    except ValueError:
        m = re.search(r"\{.*\}", txt, re.S)
        if not m:
            raise LLMError("model did not return JSON")
        try:
            out = json.loads(m.group(0))
        except ValueError:
            raise LLMError("model returned invalid JSON")
    if not isinstance(out, dict):
        raise LLMError("model returned JSON that is not an object")
    return out


def _cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (cfg or {}).get("llm", {}) or {}


_model_cache: Dict[str, str] = {}


def _pick(available: List[str], prefer: List[str]) -> str:
    chat = [m for m in available if not any(x in m.lower() for x in NOT_CHAT)]
    for pref in prefer:
        for m in chat:
            if pref in m.lower():
                return m
    return chat[0] if chat else ""


def discover_model(name: str, url: str, headers: Dict[str, str], prefer: List[str], ids_from: Any) -> str:
    """Ask the service which models this key can use. Cached per process."""
    if name in _model_cache:
        return _model_cache[name]
    try:
        r = httpx.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        model = _pick(ids_from(r.json()), prefer)
    except Exception as e:
        log.warning("could not list %s models: %s", name, e)
        model = ""
    if model:
        _model_cache[name] = model
    return model


def groq(cfg: Optional[Dict[str, Any]] = None) -> Optional[LLM]:
    key = config.secret("GROQ_API_KEY") or config.secret("GROK_API_KEY")
    if not key:
        return None
    model = _cfg(cfg).get("groq_model") or discover_model(
        "Groq", GROQ_MODELS_URL, {"Authorization": f"Bearer {key}"}, GROQ_PREFER,
        lambda j: [m.get("id", "") for m in j.get("data", [])])
    if not model:
        return LLM("Groq", GROQ_URL, "", key)       # ping() will report the real reason
    return LLM("Groq", GROQ_URL, model, key)


def github(cfg: Optional[Dict[str, Any]] = None) -> Optional[LLM]:
    key = config.secret("GITHUB_TOKEN")
    if not key:
        return None
    hdrs = {"Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}
    model = (_cfg(cfg).get("github_model")
             or discover_model("GitHub Models", GITHUB_CATALOG_URL, hdrs, GITHUB_PREFER,
                               lambda j: [m.get("id", "") for m in (j if isinstance(j, list) else j.get("models", []))])
             or GITHUB_PREFER[1])              # catalog not readable: try a long-standing model and report the real error
    return LLM("GitHub Models", GITHUB_URL, model, key, {"Accept": "application/vnd.github+json",
                                                         "X-GitHub-Api-Version": "2022-11-28"})


def ping(llm: Optional[LLM]) -> Tuple[bool, str]:
    if llm is None:
        return False, "no key in .env"
    if not llm.model:
        return False, "key found, but could not get the list of available models (check the key, or set llm.groq_model / llm.github_model in config.yaml)"
    try:
        out = llm.chat("Reply with the single word: ok", "ping", max_tokens=300, temperature=0)
        return True, f"ok ({llm.model})" if out else f"empty reply ({llm.model})"
    except LLMError as e:
        return False, str(e)
