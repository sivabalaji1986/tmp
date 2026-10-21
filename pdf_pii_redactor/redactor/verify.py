"""Post-processing validation of the saved output file.

Checks performed (each is recorded in the report's coverage section):
  1. text layer   - character-level search of every page, plus a second plain-text pass
  2. PDF strings  - annotations, form fields, links, bookmarks, document metadata
  3. object scan  - every remaining PDF object/stream (decompressed) for raw occurrences
  4. visual OCR   - rendered pages are OCR'd and searched (exact + fuzzy)
  5. image OCR    - every embedded image is OCR'd in its own pixel space, which also
                    covers clipped/hidden parts of images
Inserted replacement text is excluded only where it legitimately contains a source
(chained mappings) and only at the exact positions where it was drawn.

None of these checks can *prove* irreversible removal; see README "Limitations".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf as fitz

from .config import Mapping, Settings
from .geometry import expanded_page, overlap_fraction, upright_rect
from .matching import contains_any, find_all, pattern_for, replacement_occurrences, similarity
from .native import extract_chars
from .ocr import OcrEngine, find_ocr_matches, ocr_image_xref, ocr_page


@dataclass
class Inserted:
    rect: fitz.Rect
    text: str          # replacement text drawn here (kept in memory only)
    mapping_id: str


@dataclass
class VerifyResult:
    leaks: list[dict] = field(default_factory=list)       # -> FAILED
    uncertain: list[dict] = field(default_factory=list)   # -> REVIEW_REQUIRED
    coverage: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    pages: dict = field(default_factory=dict)


def _allowed(rect: fitz.Rect, mapping: Mapping, inserted: list[Inserted], min_overlap: float) -> bool:
    for ins in inserted:
        if not pattern_for(mapping).search(ins.text):
            continue
        inter = fitz.Rect(rect) & ins.rect
        if inter.is_empty:
            continue
        smaller = min(fitz.Rect(rect).get_area(), ins.rect.get_area()) or 1e-6
        if inter.get_area() / smaller >= min_overlap:
            return True
    return False


_HEX = re.compile(rb"<([0-9A-Fa-f\s]{4,})>")


def _decoded_variants(raw: bytes) -> list[str]:
    out = [raw.decode("latin-1")]
    if b"\x00" in raw:
        for enc in ("utf-16-be", "utf-16-le"):
            out.append(raw.decode(enc, "ignore"))
    for m in _HEX.finditer(raw):
        try:
            b = bytes.fromhex(re.sub(rb"\s", b"", m.group(1)).decode())
        except ValueError:
            continue
        out.append(b[2:].decode("utf-16-be", "ignore") if b.startswith(b"\xfe\xff") else b.decode("latin-1"))
    return out


def verify_output(path, settings: Settings, inserted: dict[int, list[Inserted]],
                  ocr_pages: set[int], engine: OcrEngine | None) -> VerifyResult:
    res = VerifyResult()
    mappings = settings.mappings
    chained = {m.id for m in mappings if any(pattern_for(m).search(o.replacement) for o in mappings)}
    doc = fitz.open(path)
    try:
        _structure(doc, settings, res)
        _text_layer(doc, mappings, inserted, res)
        _pdf_strings(doc, mappings, chained, res)
        _object_scan(doc, mappings, chained, res)
        _visual(doc, settings, inserted, ocr_pages, engine, res)
    finally:
        doc.close()
    return res


def _page_entry(res: VerifyResult, pno: int) -> dict:
    return res.pages.setdefault(pno, {"text_leaks": 0, "ocr_leaks": 0, "ocr_uncertain": 0, "ocr_checked": False})


def _structure(doc, settings, res):
    if doc.is_encrypted:
        res.leaks.append({"check": "structure", "detail": "output is encrypted"})
    if not settings.keep_attachments and doc.embfile_count():
        res.leaks.append({"check": "structure", "detail": "embedded files remain"})
    if doc.xref_xml_metadata():
        res.uncertain.append({"check": "structure", "detail": "XMP metadata stream present"})
    res.coverage["structure"] = "checked encryption, embedded files, XMP metadata"


def _text_layer(doc, mappings, inserted, res):
    total = 0
    for page in doc:
        ins = inserted.get(page.number, [])
        text, recs = extract_chars(page)
        for sp in find_all(text, mappings):
            chars = [r for r in recs[sp.start:sp.end] if r is not None]
            if not chars:
                continue
            rect = fitz.Rect(chars[0].bbox)
            for c in chars[1:]:
                rect |= c.bbox
            if not _allowed(rect, sp.mapping, ins, 0.5):
                res.leaks.append({"check": "text_layer", "page": page.number + 1, "mapping": sp.mapping.id})
                _page_entry(res, page.number)["text_leaks"] += 1
        # Text outside the visible page area (ignored by normal extraction).
        area = upright_rect(page)
        with expanded_page(page) as to_normal:
            etext, erecs = extract_chars(page)
            for sp in find_all(etext, mappings):
                chars = [r for r in erecs[sp.start:sp.end] if r is not None]
                if chars and not to_normal(chars[0].bbox).intersects(area):
                    res.leaks.append({"check": "offpage_text", "page": page.number + 1, "mapping": sp.mapping.id})
                    _page_entry(res, page.number)["text_leaks"] += 1
        # Independent second extraction path (MuPDF plain text, whitespace-normalised).
        plain = " ".join(page.get_text("text").split())
        for m in mappings:
            found = len(pattern_for(m).findall(plain))
            allowed = sum(replacement_occurrences(m, i.text) for i in ins)
            if found > allowed:
                res.leaks.append({"check": "plain_text", "page": page.number + 1, "mapping": m.id})
        total += 1
    res.coverage["text_layer"] = f"{total} page(s) searched with two extraction methods, including off-page text"


def _pdf_strings(doc, mappings, chained, res):
    def report(where, found):
        for m in found:
            entry = {"check": "pdf_strings", "where": where, "mapping": m.id}
            (res.uncertain if m.id in chained else res.leaks).append(entry)

    for page in doc:
        for annot in page.annots() or []:
            blob = " ".join(str(v) for v in (annot.info or {}).values())
            report(f"annotation p{page.number + 1}", contains_any(blob, mappings))
        for w in page.widgets() or []:
            blob = " ".join(str(getattr(w, a, "") or "") for a in ("field_value", "field_label", "field_name"))
            report(f"form field p{page.number + 1}", contains_any(blob, mappings))
        for link in page.get_links():
            blob = " ".join(str(link.get(k, "")) for k in ("uri", "file", "name", "nameddest"))
            report(f"link p{page.number + 1}", contains_any(blob, mappings))
    for _, title, _ in doc.get_toc(simple=True):
        report("bookmark", contains_any(title, mappings))
    for k, v in (doc.metadata or {}).items():
        if isinstance(v, str):
            report(f"metadata:{k}", contains_any(v, mappings))
    res.coverage["pdf_strings"] = "annotations, form fields, links, bookmarks, document info"


def _object_scan(doc, mappings, chained, res):
    content_xrefs = set()
    for page in doc:
        content_xrefs.update(page.get_contents())
    scanned = skipped = 0
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.xref_object(xref, compressed=True)
        except Exception:  # noqa: BLE001
            continue
        subtype = doc.xref_get_key(xref, "Subtype")[1]
        is_font_program = any(doc.xref_get_key(xref, k)[0] != "null" for k in ("Length1", "Length2", "Length3")) \
            or subtype in ("/Type1C", "/CIDFontType0C", "/OpenType")
        blobs = [obj.encode("latin-1", "ignore")]
        if doc.xref_is_stream(xref) and subtype != "/Image" and not is_font_program:
            try:
                blobs.append(doc.xref_stream(xref) or b"")
            except Exception:  # noqa: BLE001
                res.uncertain.append({"check": "object_scan", "detail": f"stream {xref} could not be decoded"})
        elif doc.xref_is_stream(xref):
            skipped += 1
        for blob in blobs:
            for text in _decoded_variants(blob):
                for m in contains_any(text, mappings):
                    if xref in content_xrefs and m.id in chained:
                        continue  # covered by the geometry-aware text-layer check
                    res.uncertain.append({"check": "object_scan", "object": xref, "mapping": m.id})
        scanned += 1
    res.coverage["object_scan"] = (
        f"{scanned} object(s) scanned (decompressed); {skipped} image/font stream(s) "
        "left to OCR / not applicable"
    )


def _visual(doc, settings, inserted, ocr_pages, engine, res):
    if settings.verify_ocr == "none":
        res.coverage["visual_ocr"] = "disabled by user (--verify-ocr none)"
        res.uncertain.append({"check": "visual_ocr", "detail": "visual verification disabled"})
        return
    if engine is None:
        res.coverage["visual_ocr"] = "not performed: OCR engine unavailable"
        res.uncertain.append({"check": "visual_ocr", "detail": "OCR unavailable for verification"})
        return
    mappings = settings.mappings
    checked = 0
    for page in doc:
        if settings.verify_ocr == "ocr-pages" and page.number not in ocr_pages:
            continue
        ins = inserted.get(page.number, [])
        hits, _ = ocr_page(engine, page, mappings, settings.ocr_dpi, settings.ocr_rotations,
                           settings.fuzzy_threshold, settings.ocr_min_conf)
        entry = _page_entry(res, page.number)
        entry["ocr_checked"] = True
        for h in hits:
            if h.exact:
                if _allowed(h.rect, h.mapping, ins, 0.3):
                    continue
                res.leaks.append({"check": "visual_ocr", "page": page.number + 1, "mapping": h.mapping.id})
                entry["ocr_leaks"] += 1
            else:
                own = any(overlap_fraction(h.rect, i.rect) > 0.3
                          and similarity(h.text, i.text) >= similarity(h.text, h.mapping.source) for i in ins)
                if own:
                    continue
                res.uncertain.append({"check": "visual_ocr_fuzzy", "page": page.number + 1,
                                      "mapping": h.mapping.id, "similarity": round(h.similarity, 2)})
                entry["ocr_uncertain"] += 1
        checked += 1
    res.coverage["visual_ocr"] = f"{checked} rendered page(s) OCR'd ({settings.verify_ocr})"

    # Embedded images in their own pixel space (finds hidden/clipped content).
    smasks = set()
    for xref in range(1, doc.xref_length()):
        k = doc.xref_get_key(xref, "SMask")
        if k[0] == "xref":
            smasks.add(int(k[1].split()[0]))
    n_img = 0
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype")[1] != "/Image" or xref in smasks:
            continue
        try:
            word_sets = ocr_image_xref(engine, doc, xref, settings.ocr_rotations)
        except Exception:  # noqa: BLE001
            res.uncertain.append({"check": "image_ocr", "object": xref, "detail": "image could not be decoded"})
            continue
        n_img += 1
        for words in word_sets:
            for m, _box, exact, _conf, score, _t, _n in find_ocr_matches(words, mappings, settings.fuzzy_threshold):
                entry = {"check": "image_ocr", "object": xref, "mapping": m.id}
                if exact:
                    res.leaks.append(entry)
                else:
                    entry["similarity"] = round(score, 2)
                    res.uncertain.append(entry)
    res.coverage["image_ocr"] = f"{n_img} embedded image(s) OCR'd in native resolution"
