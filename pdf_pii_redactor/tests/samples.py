"""Synthetic test documents (all PII here is fictitious)."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter, ImageFont

NAME, NAME_R = "Jilaba", "SiBa"
PHONE, PHONE_R = "98765432", "12345678"
EMAIL, EMAIL_R = "jilaba@email.com", "siba@example.com"
NRIC, NRIC_R = "S1234567D", "S0000000X"


def font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "Arial.ttf", "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def text_image(lines: list[str], size=(1700, 900), fs=44, blur=0.0, rotate=0) -> bytes:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    f = font(fs)
    y = 60
    for line in lines:
        d.text((70, y), line, fill=(10, 10, 10), font=f)
        y += int(fs * 1.7)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if rotate:
        img = img.rotate(rotate, expand=True, fillcolor="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


SCAN_LINES = [
    "Customer record",
    f"Name: {NAME} Rao",
    f"Phone: {PHONE}",
    f"Email: {EMAIL}",
    "Account opened in 2019.",
]


def make_native(path: Path) -> Path:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 80), "Employee Profile", fontsize=18, fontname="hebo")
    p.insert_text((72, 120), f"Name: {NAME} Rao (preferred: {NAME.lower()})", fontsize=11)
    p.insert_text((72, 140), f"Mobile: {PHONE}   Office: 61234567", fontsize=11, fontname="tiro")
    p.insert_text((72, 160), f"Email: {EMAIL}", fontsize=11, fontname="cour")
    p.insert_text((72, 180), f"Manager notes: {NAME} joined in 2020.", fontsize=11)
    p.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 150, 250, 163), "uri": f"mailto:{EMAIL}"})
    a = p.add_text_annot((400, 120), f"Check with {NAME} about leave")
    a.update()
    p2 = doc.new_page()
    p2.insert_text((72, 100), f"Page two mentions {NAME} again and {PHONE}.", fontsize=12)
    doc.set_toc([[1, f"Profile of {NAME}", 1], [1, "Second page", 2]])
    doc.set_metadata({"author": f"{NAME} Rao", "title": f"Profile {NAME}", "subject": "HR"})
    doc.save(path)
    return path


def make_scanned(path: Path, blur=0.0, lines=None) -> Path:
    doc = fitz.open()
    p = doc.new_page(width=612, height=324)
    p.insert_image(p.rect, stream=text_image(lines or SCAN_LINES, blur=blur))
    doc.save(path)
    return path


def make_mixed(path: Path) -> Path:
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 72), f"Cover note for {NAME} - phone {PHONE}", fontsize=12)
    p.insert_image(fitz.Rect(72, 100, 540, 347), stream=text_image(SCAN_LINES))
    p.insert_text((72, 380), "Text below the image is not sensitive.", fontsize=11)
    doc.save(path)
    return path


def make_rotated(path: Path) -> Path:
    """Page 1: native text on a page with /Rotate 90.
    Page 2: scanned image whose content is sideways (rotated 90 degrees)."""
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), f"Rotated page for {NAME}, tel {PHONE}", fontsize=12)
    p.set_rotation(90)
    p2 = doc.new_page(width=324, height=612)
    p2.insert_image(p2.rect, stream=text_image(SCAN_LINES, rotate=90))
    doc.save(path)
    return path


def make_with_ocr_layer(path: Path) -> Path:
    """Scanned page that also carries an invisible OCR text layer (common for scanners)."""
    doc = fitz.open()
    p = doc.new_page(width=612, height=324)
    p.insert_image(p.rect, stream=text_image(SCAN_LINES))
    scale = 612 / 1700
    for i, line in enumerate(SCAN_LINES):
        y = (60 + i * int(44 * 1.7) + 36) * scale
        p.insert_text((70 * scale, y), line, fontsize=44 * scale * 0.95, render_mode=3)
    doc.save(path)
    return path
