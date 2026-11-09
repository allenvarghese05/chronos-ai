"""Small deny list for spoken/on-screen script text (docs/06 §4, docs/09).

A defence-in-depth check: the prompt already asks for calm, factual copy. Matching is whole-word,
case-insensitive, after NFKC normalisation. A hit makes the script invalid (retry/template).
"""

from __future__ import annotations

import re
import unicodedata

DENIED_TERMS: frozenset[str] = frozenset(
    {
        "fuck", "fucking", "shit", "bullshit", "bitch", "bastard", "asshole", "cunt", "dick",
        "motherfucker", "slut", "whore", "retard", "faggot", "nigger",
    }
)  # fmt: skip

_WORD = re.compile(r"\w+")


def find_denied(text: str) -> list[str]:
    """Denied terms present in *text* (sorted, unique)."""
    words = _WORD.findall(unicodedata.normalize("NFKC", text).casefold())
    return sorted({w for w in words if w in DENIED_TERMS})


def mask_denied(text: str) -> str:
    """Replace each denied word in *text* with an em dash (same token count)."""

    def repl(match: re.Match[str]) -> str:
        word = match.group(0)
        return "—" if unicodedata.normalize("NFKC", word).casefold() in DENIED_TERMS else word

    return _WORD.sub(repl, text)
