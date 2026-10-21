"""Page classification: decide which pages need OCR."""
from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf as fitz

from .geometry import upright_rect

MIN_IMAGE_AREA_FRACTION = 0.003   # images smaller than 0.3% of the page are ignored ...
MIN_IMAGE_PIXELS = 24             # ... as are images narrower/shorter than this many pixels


@dataclass
class PageInfo:
    index: int
    category: str = "native"          # native | scanned | mixed | garbled | vector | blank
    needs_ocr: bool = False
    reasons: list[str] = field(default_factory=list)
    text_chars: int = 0
    image_count: int = 0
    image_coverage: float = 0.0
    rotation: int = 0
    image_rects: list = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "page": self.index + 1, "category": self.category, "ocr_required": self.needs_ocr,
            "reasons": self.reasons, "text_chars": self.text_chars, "images": self.image_count,
            "image_coverage": round(self.image_coverage, 3), "rotation": self.rotation,
        }


def significant_images(page: fitz.Page) -> list[fitz.Rect]:
    area = upright_rect(page)
    page_area = max(area.get_area(), 1.0)
    rects = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & area
        if r.is_empty:
            continue
        if r.get_area() / page_area < MIN_IMAGE_AREA_FRACTION:
            continue
        if min(info.get("width", 0), info.get("height", 0)) < MIN_IMAGE_PIXELS:
            continue
        rects.append(r)
    return rects


def classify_page(page: fitz.Page, ocr_mode: str) -> PageInfo:
    info = PageInfo(index=page.number, rotation=page.rotation)
    text = page.get_text("text", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    visible = [c for c in text if not c.isspace()]
    info.text_chars = len(visible)
    bad = sum(1 for c in visible if c == "�" or 0xE000 <= ord(c) <= 0xF8FF)
    garbled = info.text_chars >= 10 and bad / info.text_chars > 0.1

    rects = significant_images(page)
    info.image_rects = rects
    info.image_count = len(rects)
    area = max(upright_rect(page).get_area(), 1.0)
    info.image_coverage = min(1.0, sum(r.get_area() for r in rects) / area)

    try:
        drawings = len(page.get_cdrawings())
    except Exception:  # noqa: BLE001
        drawings = 0

    if rects and info.text_chars < 20:
        info.category = "scanned"
    elif rects:
        info.category = "mixed"
    elif garbled:
        info.category = "garbled"
    elif info.text_chars == 0 and drawings > 40:
        info.category = "vector"
    elif info.text_chars == 0:
        info.category = "blank"

    if ocr_mode == "never":
        if rects or garbled or info.category == "vector":
            info.reasons.append("ocr_disabled_but_needed")
        return info
    if ocr_mode == "always":
        info.needs_ocr = True
        info.reasons.append("ocr_mode_always")
    if rects:
        info.needs_ocr = True
        info.reasons.append("contains_images")
    if garbled:
        info.needs_ocr = True
        info.reasons.append("unreadable_text_layer")
    if info.category == "vector":
        info.needs_ocr = True
        info.reasons.append("vector_content_without_text")
    return info
