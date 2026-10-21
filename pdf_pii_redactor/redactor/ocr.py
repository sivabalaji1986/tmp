"""Local OCR (Tesseract) and mapping of OCR matches back to page coordinates.

Everything runs on the local machine. No network calls are made.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass

import pymupdf as fitz
from PIL import Image

from .config import Mapping
from .geometry import pixel_box_to_page, render, sample_colors
from .matching import match_text, similarity

DESCENDER_CHARS = set("gjpqy@(),;[]{}|_Q$")

COMMON_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/usr/bin/tesseract",
]


class OcrUnavailable(Exception):
    pass


@dataclass
class OcrWord:
    text: str
    conf: float
    box: tuple[float, float, float, float]   # pixels in the *rotated* (upright-text) image
    line_key: tuple


@dataclass
class OcrHit:
    mapping: Mapping
    rect: fitz.Rect          # unrotated page coordinates
    direction: float         # text direction (deg, CCW) in unrotated page space
    exact: bool
    confidence: float
    fontsize: float
    baseline_offset: float   # distance from the rect's bottom edge (text frame) to baseline
    available: float         # length available along the text direction
    bg: tuple
    fg: tuple
    matched_text_len: int
    similarity: float = 1.0
    rotation: int = 0
    source: str = "ocr"
    text: str = ""           # recognised text; kept in memory only, never logged or reported


class OcrEngine:
    def __init__(self, lang: str = "eng", psm: int = 11, tesseract_cmd: str | None = None):
        import pytesseract  # imported lazily so the tool runs (fail-closed) without it

        self._pt = pytesseract
        cmd = tesseract_cmd or os.environ.get("TESSERACT_CMD")
        if not cmd:
            cmd = shutil.which("tesseract")
        if not cmd:
            cmd = next((p for p in COMMON_TESSERACT_PATHS if os.path.isfile(p)), None)
        if not cmd:
            raise OcrUnavailable("Tesseract OCR executable not found")
        pytesseract.pytesseract.tesseract_cmd = cmd
        try:
            self.version = str(pytesseract.get_tesseract_version())
            langs = set(pytesseract.get_languages(config=""))
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable(f"Tesseract could not be started ({type(exc).__name__})") from None
        missing = [lng for lng in lang.split("+") if lng not in langs]
        if missing:
            raise OcrUnavailable(f"Tesseract language data missing: {', '.join(missing)}")
        self.has_osd = "osd" in langs
        self.lang = lang
        self.psm = psm

    # ------------------------------------------------------------------ raw OCR
    def words(self, img: Image.Image) -> list[OcrWord]:
        d = self._pt.image_to_data(img, lang=self.lang, config=f"--psm {self.psm}",
                                   output_type=self._pt.Output.DICT)
        out = []
        for i, txt in enumerate(d["text"]):
            if not txt or not txt.strip():
                continue
            try:
                conf = float(d["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            out.append(OcrWord(txt.strip(), conf, (x, y, x + w, y + h),
                               (d["block_num"][i], d["par_num"][i], d["line_num"][i])))
        return out

    def detect_rotation(self, img: Image.Image) -> int | None:
        """Counter-clockwise rotation (0/90/180/270) that makes the text upright, or None."""
        if not self.has_osd:
            return None
        try:
            osd = self._pt.image_to_osd(img, config="--psm 0 -c min_characters_to_try=10")
        except Exception:  # noqa: BLE001 - too little text etc.
            return None
        m = re.search(r"Rotate:\s*(\d+)", osd)
        if not m:
            return None
        clockwise = int(m.group(1)) % 360
        return (360 - clockwise) % 360

    def rotations_for(self, img: Image.Image, mode: str) -> list[int]:
        if mode == "none":
            return [0]
        if mode == "all":
            return [0, 90, 180, 270]
        detected = self.detect_rotation(img)
        if detected is None:
            # Orientation unknown -> be thorough rather than fast.
            return [0, 90, 180, 270] if self._has_little_text(img) else [0]
        return [0] if detected == 0 else [0, detected]

    def _has_little_text(self, img: Image.Image) -> bool:
        small = img.copy()
        small.thumbnail((1200, 1200))
        return len(self.words(small)) < 15


# ---------------------------------------------------------------- coordinates

def rotate_image(img: Image.Image, angle: int) -> Image.Image:
    return img if angle == 0 else img.rotate(angle, expand=True)


def unrotate_box(box, angle: int, size: tuple[int, int]):
    """Map a box from an image rotated CCW by `angle` (expand=True) back to the
    original image of `size` = (W, H)."""
    W, H = size
    x0, y0, x1, y1 = box
    if angle == 0:
        pts = [(x0, y0), (x1, y1)]
    elif angle == 90:
        pts = [(W - y, x) for x, y in ((x0, y0), (x1, y1))]
    elif angle == 180:
        pts = [(W - x, H - y) for x, y in ((x0, y0), (x1, y1))]
    elif angle == 270:
        pts = [(y, H - x) for x, y in ((x0, y0), (x1, y1))]
    else:
        raise ValueError(angle)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ------------------------------------------------------------------ matching

def _lines(words: list[OcrWord]) -> list[list[OcrWord]]:
    """Group words into visual lines geometrically (Tesseract's sparse-text mode
    often puts neighbouring words of one line into different blocks)."""
    rows: list[list[OcrWord]] = []
    for w in sorted(words, key=lambda w: (w.box[1] + w.box[3]) / 2):
        yc, h = (w.box[1] + w.box[3]) / 2, w.box[3] - w.box[1]
        for row in rows:
            r_yc = sum((x.box[1] + x.box[3]) / 2 for x in row) / len(row)
            r_h = sum(x.box[3] - x.box[1] for x in row) / len(row)
            if abs(yc - r_yc) < 0.45 * max(h, r_h):
                row.append(w)
                break
        else:
            rows.append([w])
    lines: list[list[OcrWord]] = []
    for row in rows:
        row.sort(key=lambda w: w.box[0])
        current = [row[0]]
        for w in row[1:]:
            prev = current[-1]
            height = max(prev.box[3] - prev.box[1], w.box[3] - w.box[1], 1)
            if w.box[0] - prev.box[2] > 2.5 * height:
                lines.append(current)
                current = [w]
            else:
                current.append(w)
        lines.append(current)
    return lines


def _span_box(line: list[OcrWord], offsets: list[int], start: int, end: int):
    """Pixel box covering characters [start, end) of the joined line text,
    interpolating inside partially-matched words."""
    xs0, xs1, ys0, ys1, confs = [], [], [], [], []
    for w, off in zip(line, offsets):
        w_start, w_end = off, off + len(w.text)
        if w_end <= start or w_start >= end:
            continue
        a, b = max(start, w_start) - w_start, min(end, w_end) - w_start
        bx0, by0, bx1, by1 = w.box
        cw = (bx1 - bx0) / max(1, len(w.text))
        xs0.append(bx0 + a * cw)
        xs1.append(bx0 + b * cw)
        ys0.append(by0)
        ys1.append(by1)
        confs.append(w.conf)
    if not xs0:
        return None, 0.0
    return (min(xs0), min(ys0), max(xs1), max(ys1)), min(confs)


def find_ocr_matches(words: list[OcrWord], mappings: list[Mapping], fuzzy_threshold: float):
    """Return list of (mapping, pixel_box, exact, confidence, similarity, matched_text, following_x)."""
    results = []
    for line in _lines(words):
        offsets, pos = [], 0
        for w in line:
            offsets.append(pos)
            pos += len(w.text) + 1
        text = " ".join(w.text for w in line)
        accepted, _ = match_text(text, mappings)
        taken: list[tuple[int, int]] = []
        for sp in accepted:
            box, conf = _span_box(line, offsets, sp.start, sp.end)
            if box:
                results.append((sp.mapping, box, True, conf, 1.0, text[sp.start:sp.end],
                                _next_x(line, offsets, sp.end)))
                taken.append((sp.start, sp.end))
        # Fuzzy candidates over word windows (OCR misreads such as 1/l, 0/O).
        for m in sorted(mappings, key=lambda m: m.priority):
            if len(m.source.replace(" ", "")) < 5:
                continue  # too short for reliable fuzzy matching
            best = None
            for i in range(len(line)):
                for j in range(i, len(line)):
                    s0, s1 = offsets[i], offsets[j] + len(line[j].text)
                    window = text[s0:s1]
                    if len(window) > 1.6 * len(m.source) + 2:
                        break
                    if any(s0 < b and a < s1 for a, b in taken):
                        continue
                    score = similarity(window, m.source)
                    if score >= fuzzy_threshold and (best is None or score > best[0]):
                        best = (score, s0, s1)
            if best:
                score, s0, s1 = best
                box, conf = _span_box(line, offsets, s0, s1)
                if box:
                    results.append((m, box, False, conf, score, text[s0:s1], _next_x(line, offsets, s1)))
                    taken.append((s0, s1))
    return results


def _next_x(line, offsets, end):
    for w, off in zip(line, offsets):
        if off >= end:
            return w.box[0]
    return None


# --------------------------------------------------------------- page level

def ocr_page(engine: OcrEngine, page: fitz.Page, mappings: list[Mapping], dpi: int,
             rotation_mode: str, fuzzy_threshold: float, min_conf: float,
             image=None) -> tuple[list[OcrHit], dict]:
    """OCR a rendered page and return hits in unrotated page coordinates."""
    if image is None:
        image, zoom = render(page, dpi)
    else:
        image, zoom = image
    rotations = engine.rotations_for(image, rotation_mode)
    hits: list[OcrHit] = []
    stats = {"rotations": rotations, "words": 0}
    for angle in rotations:
        rimg = rotate_image(image, angle)
        words = engine.words(rimg)
        stats["words"] += len(words)
        for mapping, box, exact, conf, score, mtext, next_x in find_ocr_matches(words, mappings, fuzzy_threshold):
            h_px = box[3] - box[1]
            pad = max(2.0, 0.12 * h_px)
            pad_x = max(3.0, 0.22 * h_px)  # anti-aliased glyph edges extend past the OCR box
            padded = (box[0] - pad_x, box[1] - pad, box[2] + pad_x, box[3] + pad)
            orig_box = unrotate_box(padded, angle, image.size)
            rect = pixel_box_to_page(page, orig_box, zoom)
            bg, fg = sample_colors(image, unrotate_box(box, angle, image.size))
            # Typography estimate in the upright text frame.
            has_desc = any(c in DESCENDER_CHARS for c in mtext)
            h_pt = h_px / zoom
            fontsize = max(4.0, min(72.0, h_pt / (0.95 if has_desc else 0.78)))
            baseline_offset = (0.21 * fontsize if has_desc else 0.0) + pad / zoom
            limit = (next_x - 0.15 * h_px) if next_x is not None else rimg.size[0] - 0.02 * rimg.size[0]
            available = max(box[2], limit) - box[0]
            # Direction in displayed space, then compensate for /Rotate.
            displayed_dir = (360 - angle) % 360
            direction = float((displayed_dir + page.rotation) % 360)
            hits.append(OcrHit(
                mapping=mapping, rect=rect, direction=direction, exact=exact,
                confidence=conf, fontsize=fontsize, baseline_offset=baseline_offset,
                available=available / zoom, bg=bg, fg=fg, matched_text_len=len(mtext),
                similarity=score, rotation=angle, text=mtext,
            ))
    return _dedupe(hits), stats


def _dedupe(hits: list[OcrHit]) -> list[OcrHit]:
    """Same region found under several rotations/mappings: keep the strongest."""
    hits = sorted(hits, key=lambda h: (not h.exact, h.mapping.priority, h.rotation, -h.similarity))
    kept: list[OcrHit] = []
    for h in hits:
        if any(_iou(h.rect, k.rect) > 0.3 for k in kept):
            continue
        kept.append(h)
    return kept


def _iou(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = fitz.Rect(a) & fitz.Rect(b)
    if inter.is_empty:
        return 0.0
    ia = inter.get_area()
    return ia / (a.get_area() + b.get_area() - ia)


def redact_ocr_hits(page: fitz.Page, hits: list[OcrHit]) -> None:
    """Remove the pixels (and any text/line-art) under OCR matches."""
    if not hits:
        return
    for h in hits:
        page.add_redact_annot(h.rect, fill=h.bg, cross_out=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)


def ocr_image_xref(engine: OcrEngine, doc: fitz.Document, xref: int, rotation_mode: str):
    """OCR an embedded image in its own pixel space (catches content that is clipped
    or hidden when the page is rendered). Returns list of OcrWord lists per rotation."""
    pix = fitz.Pixmap(doc, xref)
    if pix.width < 32 or pix.height < 16:
        return []
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n not in (1, 3):
        pix = fitz.Pixmap(fitz.csRGB, pix)
    mode = "L" if pix.n == 1 else "RGB"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    if max(img.size) > 6000:
        img.thumbnail((6000, 6000))
    if mode == "L":
        img = img.convert("RGB")
    out = []
    for angle in engine.rotations_for(img, rotation_mode):
        out.append(engine.words(rotate_image(img, angle)))
    return out
