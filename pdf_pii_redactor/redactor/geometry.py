"""Small geometry and rendering helpers shared by the processing stages."""
from __future__ import annotations

import math
from contextlib import contextmanager

import pymupdf as fitz
from PIL import Image


def upright_rect(page: fitz.Page) -> fitz.Rect:
    """Page area in *unrotated* coordinates (the system used by get_text/redaction/insert_text)."""
    r = page.rect * page.derotation_matrix
    r.normalize()
    return r


def render(page: fitz.Page, dpi: int) -> tuple[Image.Image, float]:
    """Render the page as displayed (rotation applied). Returns (image, zoom)."""
    zoom = dpi / 72.0
    # Keep very large pages within a sane pixel budget (~60 MP).
    area_px = page.rect.width * page.rect.height * zoom * zoom
    if area_px > 60e6:
        zoom *= math.sqrt(60e6 / area_px)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return img, zoom


def pixel_box_to_page(page: fitz.Page, box, zoom: float) -> fitz.Rect:
    """Rendered-pixel box -> unrotated page rectangle."""
    x0, y0, x1, y1 = box
    r = fitz.Rect(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom) * page.derotation_matrix
    r.normalize()
    return r


def page_rect_to_pixels(page: fitz.Page, rect: fitz.Rect, zoom: float) -> tuple[int, int, int, int]:
    r = fitz.Rect(rect) * page.rotation_matrix
    r.normalize()
    return (int(r.x0 * zoom), int(r.y0 * zoom), int(math.ceil(r.x1 * zoom)), int(math.ceil(r.y1 * zoom)))


def overlap_fraction(a: fitz.Rect, b: fitz.Rect) -> float:
    """Fraction of `a`'s area covered by `b`."""
    inter = fitz.Rect(a) & fitz.Rect(b)
    if inter.is_empty or a.is_empty:
        return 0.0
    return inter.get_area() / max(fitz.Rect(a).get_area(), 1e-6)


def unit_vectors(direction_deg: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """(along, up) unit vectors for text running at `direction_deg` (counter-clockwise
    as seen by the reader) in PDF/MuPDF y-down coordinates."""
    t = math.radians(direction_deg)
    along = (math.cos(t), -math.sin(t))
    up = (-math.sin(t), -math.cos(t))
    return along, up


def text_box(origin, direction_deg: float, length: float, fontsize: float,
             ascent: float = 0.95, descent: float = 0.3) -> fitz.Rect:
    """Axis-aligned box enclosing a run of text drawn from `origin`."""
    a, u = unit_vectors(direction_deg)
    ox, oy = origin
    pts = []
    for t in (0.0, length):
        for h in (ascent * fontsize, -descent * fontsize):
            pts.append((ox + a[0] * t + u[0] * h, oy + a[1] * t + u[1] * h))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def distance_to_edge(origin, direction_deg: float, area: fitz.Rect) -> float:
    a, _ = unit_vectors(direction_deg)
    ox, oy = origin
    ts = []
    if abs(a[0]) > 1e-6:
        ts.append(((area.x1 if a[0] > 0 else area.x0) - ox) / a[0])
    if abs(a[1]) > 1e-6:
        ts.append(((area.y1 if a[1] > 0 else area.y0) - oy) / a[1])
    return max(0.0, min(ts)) if ts else 0.0


def sample_colors(img: Image.Image, box) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Estimate (background, foreground) colours for a pixel box on a rendered page."""
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = img.size
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)
    pad = max(2, int((y1 - y0) * 0.15))
    ring = []
    px = img.load()
    for x in range(max(0, x0 - pad), min(w, x1 + pad), 2):
        for y in (max(0, y0 - pad), min(h - 1, y1 + pad - 1)):
            ring.append(px[x, y])
    for y in range(max(0, y0 - pad), min(h, y1 + pad), 2):
        for x in (max(0, x0 - pad), min(w - 1, x1 + pad - 1)):
            ring.append(px[x, y])
    inner = [px[x, y] for x in range(x0, x1, 2) for y in range(y0, y1, 2)]

    def median(seq):
        if not seq:
            return (255, 255, 255)
        return tuple(sorted(c[i] for c in seq)[len(seq) // 2] for i in range(3))

    bg = median(ring)
    lum = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    bg_l = lum(bg)
    # Foreground: the pixels that differ most from the background.
    inner.sort(key=lambda c: -abs(lum(c) - bg_l))
    darkest = inner[: max(1, len(inner) // 20)]
    fg = tuple(sum(c[i] for c in darkest) / len(darkest) for i in range(3))
    if abs(lum(fg) - bg_l) < 60:  # too little contrast: fall back to black/white
        fg = (0, 0, 0) if bg_l > 128 else (255, 255, 255)
    return tuple(v / 255 for v in bg), tuple(v / 255 for v in fg)


@contextmanager
def expanded_page(page: fitz.Page, margin: float = 5000.0):
    """Temporarily enlarge the page box so that text placed outside the visible
    page (which MuPDF's text extraction ignores) can be found. Yields a function
    mapping rectangles from the expanded system back to normal page coordinates."""
    mb, cb, rot = fitz.Rect(page.mediabox), fitz.Rect(page.cropbox), page.rotation
    t_norm = page.transformation_matrix
    try:
        page.set_rotation(0)
        page.set_mediabox(fitz.Rect(mb.x0 - margin, mb.y0 - margin, mb.x1 + margin, mb.y1 + margin))
        to_normal = ~page.transformation_matrix * t_norm
        yield lambda r: (fitz.Rect(r) * to_normal).normalize()
    finally:
        page.set_mediabox(mb)
        page.set_cropbox(cb)
        page.set_rotation(rot)
