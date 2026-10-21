"""Native (selectable) text: locate matches with character geometry and remove them
with true PDF redaction (glyphs are deleted from the content stream)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pymupdf as fitz

from .config import Mapping
from .geometry import distance_to_edge, expanded_page, overlap_fraction, unit_vectors, upright_rect
from .matching import Span, match_text

EXTRACT_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE  # no MEDIABOX_CLIP: also see off-page text


@dataclass
class CharRec:
    c: str
    bbox: fitz.Rect
    origin: fitz.Point
    size: float
    font: str
    flags: int
    color: int
    direction: float     # degrees, counter-clockwise as seen in unrotated space
    line_key: tuple
    invisible: bool = False


@dataclass
class Segment:
    """Part of a match lying on one text line."""
    rect: fitz.Rect          # union of character boxes
    band: fitz.Rect          # tight box used for glyph removal (avoids neighbouring lines)
    origin: fitz.Point       # baseline start of the first character
    direction: float
    size: float
    font: str
    flags: int
    color: int
    available: float         # length available along the line for the replacement
    invisible: bool = False  # e.g. OCR text layer of a scan (render mode 3)


@dataclass
class TextHit:
    mapping: Mapping
    segments: list[Segment]
    overlaps_image: bool = False
    source: str = "text"


def _direction(line: dict) -> float:
    dx, dy = line.get("dir", (1.0, 0.0))
    deg = math.degrees(math.atan2(-dy, dx)) % 360
    snapped = round(deg / 90.0) * 90 % 360
    return float(snapped) if abs(deg - snapped) < 2 or abs(deg - snapped) > 358 else deg


def extract_chars(page: fitz.Page) -> tuple[str, list[CharRec | None]]:
    """Character stream of the page; line/block breaks are inserted as newlines (no record)."""
    data = page.get_text("rawdict", flags=EXTRACT_FLAGS)
    text: list[str] = []
    recs: list[CharRec | None] = []
    for bno, block in enumerate(data.get("blocks", [])):
        if block.get("type", 0) != 0:
            continue
        for lno, line in enumerate(block.get("lines", [])):
            direction = _direction(line)
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    if not c:
                        continue
                    for sub in c:  # expanded ligatures may arrive as multi-char strings
                        text.append(sub)
                        recs.append(CharRec(
                            sub, fitz.Rect(ch["bbox"]), fitz.Point(ch["origin"]), span["size"],
                            span.get("font", ""), span.get("flags", 0), span.get("color", 0),
                            direction, (bno, lno),
                            span.get("alpha", 255) == 0
                            or ("char_flags" in span and not span["char_flags"] & 48),
                        ))
            text.append("\n")
            recs.append(None)
        text.append("\n")
        recs.append(None)
    return "".join(text), recs


def _project(rect: fitz.Rect, origin: fitz.Point, along) -> tuple[float, float]:
    ts = [(x - origin.x) * along[0] + (y - origin.y) * along[1]
          for x, y in ((rect.x0, rect.y0), (rect.x1, rect.y0), (rect.x0, rect.y1), (rect.x1, rect.y1))]
    return min(ts), max(ts)


def _segment(chars: list[CharRec], line_chars: list[CharRec], area: fitz.Rect) -> Segment:
    first = chars[0]
    along, up = unit_vectors(first.direction)
    o = first.origin
    t0, t1 = math.inf, -math.inf
    rect = fitz.Rect(first.bbox)
    for ch in chars:
        a, b = _project(ch.bbox, o, along)
        t0, t1 = min(t0, a), max(t1, b)
        rect |= ch.bbox
    w_first = _project(first.bbox, o, along)
    w_last = _project(chars[-1].bbox, o, along)
    inset0 = 0.08 * (w_first[1] - w_first[0])
    inset1 = 0.08 * (w_last[1] - w_last[0])
    s = first.size
    corners = []
    for t in (t0 + inset0, t1 - inset1):
        for h in (0.0, 0.6 * s):
            corners.append((o.x + along[0] * t + up[0] * h, o.y + along[1] * t + up[1] * h))
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    band = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
    # Space available for a replacement: up to the next visible character on the same
    # line, otherwise up to the page edge (minus a small margin).
    following = [c for c in line_chars if not c.c.isspace()
                 and _project(c.bbox, o, along)[0] >= t1 - 0.5 and c not in chars]
    if following:
        nxt = min(_project(c.bbox, o, along)[0] for c in following)
        available = max(t1, nxt - 0.6 * s * 0.25)
    else:
        margin = fitz.Rect(area.x0 + 6, area.y0 + 6, area.x1 - 6, area.y1 - 6)
        available = max(t1, distance_to_edge((o.x, o.y), first.direction, margin))
    return Segment(rect, band, o, first.direction, s, first.font, first.flags, first.color,
                   available - max(0.0, t0), all(c.invisible for c in chars))


def hits_from_spans(spans: list[Span], recs: list[CharRec | None], area: fitz.Rect,
                    image_rects: list[fitz.Rect]) -> list[TextHit]:
    by_line: dict[tuple, list[CharRec]] = {}
    for r in recs:
        if r is not None:
            by_line.setdefault(r.line_key, []).append(r)
    hits = []
    for sp in spans:
        groups: dict[tuple, list[CharRec]] = {}
        for r in recs[sp.start:sp.end]:
            if r is not None and not r.c.isspace():
                groups.setdefault(r.line_key, []).append(r)
        if not groups:
            continue
        segments = [_segment(chars, by_line[key], area) for key, chars in groups.items()]
        overlaps = any(overlap_fraction(seg.rect, img) > 0.2 for seg in segments for img in image_rects)
        hits.append(TextHit(sp.mapping, segments, overlaps))
    return hits


def find_hits(page: fitz.Page, mappings: list[Mapping], image_rects: list[fitz.Rect]):
    text, recs = extract_chars(page)
    accepted, suppressed = match_text(text, mappings)
    return hits_from_spans(accepted, recs, upright_rect(page), image_rects), suppressed


def remove_text(page: fitz.Page, hits: list[TextHit]) -> int:
    """True redaction of the matched glyphs. Returns the number of characters that
    could not be removed (0 when removal succeeded)."""
    if not hits:
        return 0
    for h in hits:
        for seg in h.segments:
            page.add_redact_annot(seg.band, fill=False, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    leftovers = _residual_chars(page, hits)
    if leftovers:
        # Second, slightly larger sweep limited to each leftover character.
        for ch in leftovers:
            r = fitz.Rect(ch.bbox)
            dx = r.width * 0.2
            page.add_redact_annot(fitz.Rect(r.x0 + dx, r.y0, r.x1 - dx, r.y1), fill=False, cross_out=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        leftovers = _residual_chars(page, hits)
    return len(leftovers)


def _residual_chars(page: fitz.Page, hits: list[TextHit]) -> list[CharRec]:
    _, recs = extract_chars(page)
    found = []
    for r in recs:
        if r is None or r.c.isspace():
            continue
        centre = fitz.Point((r.bbox.x0 + r.bbox.x1) / 2, (r.bbox.y0 + r.bbox.y1) / 2)
        for h in hits:
            for seg in h.segments:
                if abs(r.direction - seg.direction) > 1:
                    continue
                along, up = unit_vectors(seg.direction)
                # same baseline (perpendicular offset) and within the matched run
                perp = (r.origin.x - seg.origin.x) * up[0] + (r.origin.y - seg.origin.y) * up[1]
                if abs(perp) < 0.25 * seg.size and seg.rect.contains(centre):
                    found.append(r)
                    break
            else:
                continue
            break
    return found


def remove_image_pixels(page: fitz.Page, hits: list[TextHit], bg_colors: list) -> None:
    """For text lying on top of images (e.g. a scan with an OCR text layer), also wipe
    the image pixels under the match. Text is not touched in this pass."""
    targets = [(seg, col) for h, col in zip(hits, bg_colors) if h.overlaps_image for seg in h.segments]
    if not targets:
        return
    for seg, col in targets:
        pad = 0.12 * seg.size
        r = fitz.Rect(seg.rect.x0 - pad, seg.rect.y0 - pad, seg.rect.x1 + pad, seg.rect.y1 + pad)
        page.add_redact_annot(r, fill=col, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_NONE)


def sweep_offpage(page: fitz.Page, mappings: list[Mapping]) -> int:
    """Remove matches that lie outside the visible page area (not seen by normal
    extraction). Must run after on-page matches were removed and before any
    replacement text is drawn. Returns the number of matches removed."""
    with expanded_page(page):
        text, recs = extract_chars(page)
        accepted, _ = match_text(text, mappings)
        hits = hits_from_spans(accepted, recs, upright_rect(page), [])
        if hits:
            remove_text(page, hits)
    return len(hits)
