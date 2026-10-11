"""Spot jobs that need a language you do not work in (a 'Japanese Content Writer', 'Spanish-speaking agent').

The title check is strict: a language name in the title means the job is in that language. The description
check only fires on clear requirements ("fluent in Japanese", "native Spanish speaker") and is skipped when
the text says the language is a plus or preferred.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

LANGUAGES = [
    "japanese", "chinese", "mandarin", "cantonese", "korean", "spanish", "french", "german", "italian",
    "portuguese", "russian", "arabic", "dutch", "turkish", "thai", "vietnamese", "indonesian", "malay",
    "swedish", "danish", "norwegian", "finnish", "czech", "hungarian", "greek", "hebrew", "persian", "farsi",
    "tagalog", "filipino", "hindi", "tamil", "telugu", "kannada", "malayalam", "marathi", "bengali", "gujarati",
    "punjabi", "urdu", "odia", "assamese", "nepali", "sinhala", "swahili", "english",
]
_ALIAS = {"mandarin": "chinese", "cantonese": "chinese", "farsi": "persian", "filipino": "tagalog"}

_REQUIRES = re.compile(r"fluen|proficien|native|bilingual|multilingual|speak|spoken|written|verbal|command of|"
                       r"knowledge of|language skill|mother tongue|\bmust\b|required|\bin\b", re.I)
_OPTIONAL = re.compile(r"\bplus\b|preferred|prefer|advantage|desirable|bonus|nice to have|an asset|added|good to have|optional", re.I)


def canon(name: str) -> str:
    n = name.strip().lower()
    return _ALIAS.get(n, n)


def parse_known(raw: Iterable[str] | str | None) -> Set[str]:
    if raw is None:
        return {"english"}
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]+", raw)
    return {canon(x) for x in raw if x and x.strip()} or {"english"}


def _rx(langs: Iterable[str]) -> Optional[re.Pattern]:
    names = sorted({n for n in LANGUAGES + list(_ALIAS)}, key=len, reverse=True)
    names = [n for n in names if canon(n) not in set(langs)]
    return re.compile(r"\b(" + "|".join(names) + r")\b", re.I) if names else None


class Checker:
    def __init__(self, known: Iterable[str]) -> None:
        self.known = {canon(k) for k in known} | {"english"}
        self.rx = _rx(self.known)

    def in_title(self, title: str) -> Optional[str]:
        """Return the language the title asks for, if it is one you do not work in."""
        if not self.rx:
            return None
        for m in self.rx.finditer(title or ""):
            return canon(m.group(1)).title()
        return None

    def in_description(self, text: str) -> Optional[str]:
        if not self.rx or not text:
            return None
        for m in self.rx.finditer(text):
            before, after = text[max(0, m.start() - 70):m.start()], text[m.end():m.end() + 50]
            ctx = before + " " + after
            if _OPTIONAL.search(ctx):
                continue
            if _REQUIRES.search(before[-40:]) or re.match(r"\s*[- ]?(speak|speaker|language|fluen|proficien|native|bilingual)", after, re.I):
                return canon(m.group(1)).title()
        return None


def mentioned(text: str) -> List[str]:
    return sorted({canon(m.group(1)).title() for m in re.finditer(r"\b(" + "|".join(LANGUAGES) + r")\b", text or "", re.I)})
