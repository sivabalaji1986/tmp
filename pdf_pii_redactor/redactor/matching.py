"""Literal multi-mapping matcher with deterministic overlap handling.

All matching is done against the *original* content before anything is drawn,
so inserted replacement text can never be matched again (no cascading).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from .config import Mapping


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    mapping: Mapping


@lru_cache(maxsize=None)
def pattern_for(mapping: Mapping) -> re.Pattern:
    tokens = mapping.source.split(" ")
    # Whitespace in a source matches any whitespace *or none*: PDF text extraction
    # frequently drops or duplicates spaces and splits lines.
    body = r"\s*".join(re.escape(t) for t in tokens)
    if mapping.whole_word:
        body = rf"(?<![^\W_]){body}(?![^\W_])"
    flags = re.UNICODE | (0 if mapping.case_sensitive else re.IGNORECASE)
    return re.compile(body, flags)


def find_all(text: str, mappings: list[Mapping]) -> list[Span]:
    """All raw candidate matches for every mapping (may overlap)."""
    spans: list[Span] = []
    for m in mappings:
        for mo in pattern_for(m).finditer(text):
            if mo.end() > mo.start():
                spans.append(Span(mo.start(), mo.end(), m))
    return spans


def select(spans: list[Span]) -> tuple[list[Span], list[Span]]:
    """Resolve overlaps: longer source first, then declaration order, then position.

    Returns (accepted, suppressed). Deterministic for identical input.
    """
    ordered = sorted(spans, key=lambda s: (s.mapping.priority, s.start))
    accepted: list[Span] = []
    suppressed: list[Span] = []
    for s in ordered:
        if any(s.start < a.end and a.start < s.end for a in accepted):
            suppressed.append(s)
        else:
            accepted.append(s)
    accepted.sort(key=lambda s: s.start)
    return accepted, suppressed


def match_text(text: str, mappings: list[Mapping]) -> tuple[list[Span], list[Span]]:
    return select(find_all(text, mappings))


def substitute(text: str, mappings: list[Mapping]) -> tuple[str, int]:
    """Non-cascading string substitution (for metadata, form values, bookmarks)."""
    accepted, _ = match_text(text, mappings)
    if not accepted:
        return text, 0
    out, pos = [], 0
    for s in accepted:
        out.append(text[pos:s.start])
        out.append(s.mapping.replacement)
        pos = s.end
    out.append(text[pos:])
    return "".join(out), len(accepted)


def contains_any(text: str, mappings: list[Mapping]) -> list[Mapping]:
    return [m for m in mappings if pattern_for(m).search(text)]


# ---------------------------------------------------------------- fuzzy (OCR)

def normalise_for_fuzzy(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum() or ch in "@.-_+")


def similarity(a: str, b: str) -> float:
    a, b = normalise_for_fuzzy(a), normalise_for_fuzzy(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def replacement_occurrences(mapping: Mapping, replacement_text: str) -> int:
    """How many times `mapping.source` legitimately appears inside an inserted replacement."""
    return sum(1 for _ in pattern_for(mapping).finditer(replacement_text))
