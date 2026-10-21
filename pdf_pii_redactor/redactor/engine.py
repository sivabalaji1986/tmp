"""Orchestration: preflight -> classify -> redact/replace per page -> document
clean-up -> safe save to a temporary file -> verification -> status -> finalise.

Output rules
  SUCCESS          -> sanitized PDF written to the requested output path
  REVIEW_REQUIRED  -> sanitized PDF written as '<output>.REVIEW_REQUIRED.pdf' (never the
                      requested path, so it cannot be mistaken for a cleared file)
  FAILED           -> no PDF is kept (temporary file is deleted)
A JSON + text report is always written next to the requested output.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf as fitz

from .classify import classify_page
from .config import Settings
from .drawing import FontPicker, color_from_int, draw_text, origin_in_rect
from .geometry import page_rect_to_pixels, render, sample_colors
from .matching import contains_any
from .native import find_hits, remove_image_pixels, remove_text, sweep_offpage
from .ocr import OcrEngine, OcrUnavailable, ocr_page, redact_ocr_hits
from .report import FAILED, REVIEW, SUCCESS, Report
from .selfcheck import redaction_layout_ok
from .sanitize import SAVE_OPTIONS, clean_document, clean_page_objects, new_stats
from .verify import Inserted, verify_output

log = logging.getLogger("pdf_redactor")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def review_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.REVIEW_REQUIRED{output.suffix or '.pdf'}")


def default_report_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.report.json")


def _safe_name(path: Path, settings: Settings) -> str:
    return "[withheld: file name contains a mapped value]" if contains_any(path.name, settings.mappings) else path.name


def run(settings: Settings) -> Report:
    inp, out = settings.input_path, settings.output_path
    report = Report(_safe_name(inp, settings), _safe_name(out, settings))
    report.warnings.extend(settings.warnings)
    report.environment = {
        "python": platform.python_version(), "platform": platform.system(),
        "pymupdf": fitz.VersionBind, "mupdf": fitz.VersionFitz,
    }
    report_path = settings.report_path or default_report_path(out)
    tmp_path: Path | None = None
    try:
        _run(settings, report)
    except _Abort as exc:
        report.status = FAILED
        report.fail_reasons.append(str(exc))
    except Exception as exc:  # noqa: BLE001 - fail closed on anything unexpected
        report.status = FAILED
        msg = str(exc)
        safe = msg if msg and not contains_any(msg, settings.mappings) else "(details withheld)"
        report.fail_reasons.append(f"Unexpected error: {type(exc).__name__}: {safe}"[:300])
        log.debug("unexpected error", exc_info=True)
    finally:
        tmp_path = getattr(report, "_tmp_path", None)
        if tmp_path and Path(tmp_path).exists():
            try:
                os.remove(tmp_path)
            except OSError:
                report.warnings.append("Temporary file could not be deleted; remove it manually")
        if inp.exists():
            report.input_sha256_after = sha256(inp)
            if report.input_sha256_before and report.input_sha256_after != report.input_sha256_before:
                report.status = FAILED
                report.fail_reasons.append("Input file changed during processing")
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report.write(report_path)
            report.report_paths = (report_path, report_path.with_suffix(".txt"))
        except OSError:
            log.error("Report could not be written")
            report.report_paths = None
    return report


class _Abort(Exception):
    """Fail-closed stop with a PII-free reason."""


def _preflight(settings: Settings, report: Report) -> fitz.Document:
    inp, out = settings.input_path, settings.output_path
    if not inp.is_file():
        raise _Abort("Input file not found")
    if not out.parent.is_dir():
        raise _Abort("Output folder does not exist")
    try:
        if out.exists() and os.path.samefile(inp, out):
            raise _Abort("Output must be a different file from the input")
    except OSError:
        pass
    if out.resolve() == inp.resolve():
        raise _Abort("Output must be a different file from the input")
    for candidate in (out, review_path(out)):
        if candidate.exists() and not settings.overwrite:
            raise _Abort("Output file already exists (use --overwrite to replace it)")
    report.input_sha256_before = sha256(inp)
    try:
        doc = fitz.open(inp)
    except Exception:  # noqa: BLE001
        raise _Abort("Input is not a readable PDF") from None
    if not doc.is_pdf:
        raise _Abort("Input is not a PDF")
    if doc.needs_pass or doc.is_encrypted:
        raise _Abort("Encrypted/password-protected PDFs are not supported; remove the protection first")
    if doc.page_count == 0:
        raise _Abort("PDF has no pages")
    return doc


def _features(doc: fitz.Document, report: Report) -> None:
    feats = {"pages": doc.page_count, "signatures": False, "xfa": False, "optional_content": False,
             "attachments": doc.embfile_count(), "form_fields": 0}
    try:
        feats["signatures"] = bool(doc.get_sigflags() > 0)
    except Exception:  # noqa: BLE001
        pass
    cat = doc.pdf_catalog()
    feats["xfa"] = doc.xref_get_key(cat, "AcroForm/XFA")[0] != "null"
    try:
        ocgs = doc.get_ocgs()
    except Exception:  # noqa: BLE001
        ocgs = {}
    feats["optional_content"] = bool(ocgs)
    for page in doc:
        feats["form_fields"] += len(list(page.widgets() or []))
    report.document = feats
    if feats["signatures"]:
        report.review_reasons.append("Document is digitally signed; signatures are invalidated by redaction")
    if feats["xfa"]:
        report.review_reasons.append("XFA (dynamic) form data is present and is not processed")
    if ocgs:
        # Make every layer visible so that hidden-layer content is found and redacted.
        try:
            doc.set_layer(-1, on=list(ocgs.keys()), off=[])
        except Exception:  # noqa: BLE001
            pass
        report.review_reasons.append("Document has optional content layers; all layers were made visible for processing")


def _run(settings: Settings, report: Report) -> None:
    mappings = settings.mappings
    doc = _preflight(settings, report)
    _features(doc, report)
    if not redaction_layout_ok():
        report.warnings.append(
            f"Installed PyMuPDF {fitz.VersionBind} shifts text that follows a redaction on the same line "
            "(layout only). Install the version from requirements.txt for correct layout.")

    infos = [classify_page(p, settings.ocr_mode) for p in doc]
    needs_ocr = any(i.needs_ocr for i in infos)
    disabled_needed = [i.index + 1 for i in infos if "ocr_disabled_but_needed" in i.reasons]
    if disabled_needed:
        report.review_reasons.append(
            f"OCR disabled but page(s) {_ranges(disabled_needed)} contain images/unreadable text")

    engine = None
    if needs_ocr or settings.verify_ocr != "none":
        try:
            engine = OcrEngine(settings.ocr_lang, settings.ocr_psm, settings.tesseract_cmd)
            report.environment["tesseract"] = engine.version
        except OcrUnavailable as exc:
            if needs_ocr:
                raise _Abort(f"OCR is required for page(s) {_ranges([i.index + 1 for i in infos if i.needs_ocr])} "
                             f"but is unavailable: {exc}") from None
            report.environment["tesseract"] = f"unavailable ({exc})"

    picker = FontPicker(settings.font_file)
    stats = new_stats()
    per_map = {m.id: Counter() for m in mappings}
    map_pages = defaultdict(set)
    inserted: dict[int, list[Inserted]] = defaultdict(list)
    ocr_pages: set[int] = set()
    layout_issues = 0

    for page, info in zip(doc, infos):
        pno = page.number
        log.info("Page %d/%d: %s%s", pno + 1, doc.page_count, info.category, " (OCR)" if info.needs_ocr else "")
        pstat = {"text_matches": 0, "ocr_matches": 0, "ocr_uncertain": 0, "layout_overflows": 0,
                 "residual_chars": 0, "ocr_used": False, "ocr_rotations": []}

        # 1) native text -------------------------------------------------------------
        hits, suppressed = find_hits(page, mappings, info.image_rects)
        for s in suppressed:
            per_map[s.mapping.id]["overlap_suppressed"] += 1
        bg_colors = []
        if any(h.overlaps_image for h in hits):
            snap, zoom = render(page, 150)
            for h in hits:
                bg_colors.append(sample_colors(snap, page_rect_to_pixels(page, h.segments[0].rect, zoom))[0]
                                 if h.overlaps_image else None)
        else:
            bg_colors = [None] * len(hits)
        residual = remove_text(page, hits)
        if residual:
            pstat["residual_chars"] = residual
            report.review_reasons.append(f"Page {pno + 1}: {residual} matched character(s) could not be removed from the text layer")
        offpage = sweep_offpage(page, mappings)
        if offpage:
            pstat["offpage_removed"] = offpage
            report.warnings.append(f"Page {pno + 1}: {offpage} match(es) outside the visible page area removed")
        for h in hits:
            per_map[h.mapping.id]["text"] += 1
            map_pages[h.mapping.id].add(pno + 1)
        pstat["text_matches"] = len(hits)

        # 2) OCR (scans, images, unreadable text) --------------------------------------
        ocr_hits = []
        if info.needs_ocr and engine is not None:
            ocr_pages.add(pno)
            pstat["ocr_used"] = True
            found, ostats = ocr_page(engine, page, mappings, settings.ocr_dpi, settings.ocr_rotations,
                                     settings.fuzzy_threshold, settings.ocr_min_conf)
            pstat["ocr_rotations"] = ostats["rotations"]
            for h in found:
                uncertain = (not h.exact) or h.confidence < settings.ocr_min_conf
                if uncertain:
                    pstat["ocr_uncertain"] += 1
                    per_map[h.mapping.id]["ocr_uncertain"] += 1
                    if not settings.redact_uncertain:
                        continue
                else:
                    per_map[h.mapping.id]["ocr"] += 1
                    pstat["ocr_matches"] += 1
                map_pages[h.mapping.id].add(pno + 1)
                ocr_hits.append(h)
            if pstat["ocr_uncertain"]:
                verb = "redacted" if settings.redact_uncertain else "NOT redacted"
                report.review_reasons.append(
                    f"Page {pno + 1}: {pstat['ocr_uncertain']} uncertain OCR match(es) ({verb}); check visually")
            redact_ocr_hits(page, ocr_hits)

        # Pixels under text-layer matches that sit on images: OCR boxes (measured on the
        # actual pixels) take precedence; text-layer boxes are only a fallback because an
        # invisible OCR layer is often misaligned with the scan.
        fallback, fallback_bg = [], []
        for h, bg in zip(hits, bg_colors):
            if h.overlaps_image and not any(
                    fitz.Rect(seg.rect).intersects(o.rect) for seg in h.segments for o in ocr_hits):
                fallback.append(h)
                fallback_bg.append(bg)
        remove_image_pixels(page, fallback, fallback_bg)
        if fallback and info.needs_ocr and engine is not None:
            report.review_reasons.append(
                f"Page {pno + 1}: {len(fallback)} text-layer match(es) over an image were not confirmed by OCR; "
                "image pixels were wiped using text-layer positions - check visually")

        # 3) replacement text --------------------------------------------------------
        for h in hits:
            if not h.mapping.replacement:
                continue
            if h.overlaps_image and any(fitz.Rect(sg.rect).intersects(o.rect)
                                        for sg in h.segments for o in ocr_hits):
                continue  # drawn once, at the OCR-measured position of the visible text
            seg = h.segments[0]
            r = draw_text(page, picker, h.mapping.replacement, (seg.origin.x, seg.origin.y), seg.direction,
                          seg.size, color_from_int(seg.color), seg.available, settings.min_font_scale,
                          seg.font, seg.flags, invisible=seg.invisible)
            if r:
                inserted[pno].append(Inserted(r.box | seg.rect, h.mapping.replacement, h.mapping.id))
                if r.overflow:
                    pstat["layout_overflows"] += 1
        for h in ocr_hits:
            if not h.mapping.replacement:
                continue
            origin = origin_in_rect(h.rect, h.direction, h.baseline_offset)
            r = draw_text(page, picker, h.mapping.replacement, origin, h.direction, h.fontsize, h.fg,
                          h.available, settings.min_font_scale)
            if r:
                inserted[pno].append(Inserted(r.box | h.rect, h.mapping.replacement, h.mapping.id))
                if r.overflow:
                    pstat["layout_overflows"] += 1
        layout_issues += pstat["layout_overflows"]

        # 4) page objects (links, annotations, form fields) -----------------------------
        clean_page_objects(doc, page, mappings, stats)

        report.pages.append({**info.summary(), **pstat})

    # 5) document-level clean-up and safe save ----------------------------------------
    clean_document(doc, settings, stats)
    report.sanitization = stats
    if stats["form_names_with_pii"]:
        report.review_reasons.append("Form field names/labels contain a mapped value (not renamed)")
    if settings.keep_attachments and stats["attachments_found"]:
        report.review_reasons.append("Attachments were kept (--keep-attachments) and are not inspected")

    fd, tmp = tempfile.mkstemp(prefix=".pii-redactor-", suffix=".partial.pdf", dir=settings.output_path.parent)
    os.close(fd)
    report._tmp_path = tmp
    try:
        if picker._fallback is not None or picker._custom is not None:
            try:
                doc.subset_fonts()
            except Exception:  # noqa: BLE001
                report.warnings.append("Replacement font could not be subset (larger output file)")
        doc.save(tmp, **SAVE_OPTIONS)
    finally:
        doc.close()

    # 6) verification ----------------------------------------------------------------
    log.info("Verifying output")
    ver = verify_output(tmp, settings, inserted, ocr_pages, engine)
    report.verification = {
        "coverage": ver.coverage, "leak_count": len(ver.leaks), "uncertain_count": len(ver.uncertain),
        "leaks": ver.leaks, "uncertain": ver.uncertain,
    }
    for p in report.pages:
        p["verification"] = ver.pages.get(p["page"] - 1, {"text_leaks": 0, "ocr_checked": False})

    # 7) mapping summary + status --------------------------------------------------------
    unmatched = []
    for m in mappings:
        c = per_map[m.id]
        entry = {**m.describe(), "text_matches": c["text"], "ocr_matches": c["ocr"],
                 "ocr_uncertain": c["ocr_uncertain"], "overlap_suppressed": c["overlap_suppressed"],
                 "other_replacements": 0, "pages": sorted(map_pages[m.id])}
        entry["unmatched"] = (c["text"] + c["ocr"] + c["ocr_uncertain"]) == 0
        if entry["unmatched"]:
            unmatched.append(m.id)
        report.mappings.append(entry)
    other = stats["form_values_replaced"] + stats["bookmarks_replaced"] + stats["metadata_values_replaced"]
    if other:
        report.warnings.append(f"{other} value(s) replaced in form fields/bookmarks/metadata")
    if unmatched:
        msg = f"Mapping(s) {', '.join(unmatched)} not found in the document"
        if ocr_pages or disabled_needed:
            report.review_reasons.append(msg + " (document contains scanned/image content; OCR may have missed them)")
        else:
            report.warnings.append(msg)
    if layout_issues:
        msg = f"{layout_issues} replacement(s) did not fit the available space (drawn at minimum size, may overlap)"
        (report.review_reasons if settings.strict_layout else report.warnings).append(msg)

    if ver.leaks:
        kinds = sorted({l["check"] for l in ver.leaks})
        report.fail_reasons.append(f"Verification found {len(ver.leaks)} remaining occurrence(s) ({', '.join(kinds)})")
    if ver.uncertain:
        kinds = sorted({u["check"] for u in ver.uncertain})
        report.review_reasons.append(f"Verification raised {len(ver.uncertain)} item(s) needing review ({', '.join(kinds)})")

    if report.fail_reasons:
        report.status = FAILED
        return
    final = settings.output_path if not report.review_reasons else review_path(settings.output_path)
    report.status = SUCCESS if not report.review_reasons else REVIEW
    os.replace(tmp, final)
    report._tmp_path = None
    report.output_file = _safe_name(final, settings)
    report.output_path_obj = final


def _ranges(nums: list[int]) -> str:
    nums = sorted(nums)
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(out)
