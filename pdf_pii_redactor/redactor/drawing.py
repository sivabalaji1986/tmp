"""Drawing replacement text in place of removed content."""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf as fitz

from .geometry import text_box, unit_vectors

# (serif, mono, bold, italic) -> PDF base-14 font alias used by PyMuPDF
BASE14 = {
    (False, False, False, False): "helv", (False, False, True, False): "hebo",
    (False, False, False, True): "heit", (False, False, True, True): "hebi",
    (True, False, False, False): "tiro", (True, False, True, False): "tibo",
    (True, False, False, True): "tiit", (True, False, True, True): "tibi",
    (False, True, False, False): "cour", (False, True, True, False): "cobo",
    (False, True, False, True): "coit", (False, True, True, True): "cobi",
    (True, True, False, False): "cour", (True, True, True, False): "cobo",
    (True, True, False, True): "coit", (True, True, True, True): "cobi",
}


@dataclass
class DrawResult:
    box: fitz.Rect
    fontsize: float
    scale: float
    overflow: bool
    font: str


class FontPicker:
    """Chooses a font for replacement text and measures it."""

    UNICODE_ALIAS = "PIIRepl"

    def __init__(self, font_file: str | None = None):
        self.font_file = font_file
        self._custom = fitz.Font(fontfile=font_file) if font_file else None
        self._fallback = None
        self._registered: set[tuple[int, int]] = set()

    @staticmethod
    def style(font_name: str, flags: int) -> tuple[bool, bool, bool, bool]:
        n = (font_name or "").lower()
        serif = bool(flags & 4) or any(k in n for k in ("times", "serif", "roman", "georgia", "garamond", "cambria"))
        if "sans" in n:
            serif = False
        mono = bool(flags & 8) or any(k in n for k in ("courier", "mono", "consol"))
        bold = bool(flags & 16) or any(k in n for k in ("bold", "black", "heavy", "semibold"))
        italic = bool(flags & 2) or any(k in n for k in ("italic", "oblique"))
        return serif, mono, bold, italic

    @staticmethod
    def _base14_ok(text: str) -> bool:
        try:
            text.encode("cp1252")
            return True
        except UnicodeEncodeError:
            return False

    def choose(self, page: fitz.Page, text: str, font_name: str = "", flags: int = 0):
        """Return (fontname_for_insert_text, length_function)."""
        if self._custom is None and self._base14_ok(text):
            alias = BASE14[self.style(font_name, flags)]
            return alias, (lambda s, fs, a=alias: fitz.get_text_length(s, fontname=a, fontsize=fs))
        font = self._custom
        if font is None:
            if self._fallback is None:
                self._fallback = fitz.Font("cjk")  # wide Unicode coverage, bundled with PyMuPDF
            font = self._fallback
        key = (id(page.parent), page.number)
        if key not in self._registered:
            page.insert_font(fontname=self.UNICODE_ALIAS, fontbuffer=font.buffer)
            self._registered.add(key)
        return self.UNICODE_ALIAS, (lambda s, fs, f=font: f.text_length(s, fontsize=fs))


def draw_text(page: fitz.Page, picker: FontPicker, text: str, origin: tuple[float, float],
              direction: float, fontsize: float, color, available: float, min_scale: float,
              font_name: str = "", flags: int = 0, invisible: bool = False) -> DrawResult | None:
    """Draw `text` starting at baseline `origin`. Shrinks the font (down to
    `min_scale`) to fit `available`; flags overflow if it still does not fit."""
    if not text:
        return None
    alias, length = picker.choose(page, text, font_name, flags)
    width = length(text, fontsize)
    scale, overflow = 1.0, False
    if available > 0 and width > available * 1.02:
        scale = available / width
        if scale < min_scale:
            scale, overflow = min_scale, True
    fs = fontsize * scale
    color = tuple(max(0.0, min(1.0, c)) for c in color)
    snapped = round(direction / 90.0) * 90 % 360
    kwargs = dict(fontsize=fs, fontname=alias, color=color, render_mode=3 if invisible else 0)
    if abs(((direction - snapped) + 180) % 360 - 180) < 0.5:
        page.insert_text(origin, text, rotate=int(snapped), **kwargs)
    else:
        page.insert_text(origin, text, morph=(fitz.Point(origin), fitz.Matrix(-direction)), **kwargs)
    return DrawResult(text_box(origin, direction, length(text, fs), fs), fs, scale, overflow, alias)


def origin_in_rect(rect: fitz.Rect, direction: float, baseline_offset: float) -> tuple[float, float]:
    """Baseline start for text written along `direction` inside `rect`, where
    `baseline_offset` is measured from the rect's bottom edge in the text frame."""
    d = round(direction / 90.0) * 90 % 360
    b = baseline_offset
    if d == 0:
        return rect.x0, rect.y1 - b
    if d == 90:
        return rect.x1 - b, rect.y1
    if d == 180:
        return rect.x1, rect.y0 + b
    return rect.x0 + b, rect.y0  # 270


def color_from_int(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255)


__all__ = ["FontPicker", "draw_text", "origin_in_rect", "color_from_int", "unit_vectors"]
