"""Document-level sanitization: annotations, links, form fields, bookmarks,
metadata, attachments, JavaScript, thumbnails, stale objects."""
from __future__ import annotations

from urllib.parse import unquote

import pymupdf as fitz

from .config import Mapping, Settings
from .matching import contains_any, substitute


def _annot_strings(doc: fitz.Document, annot: fitz.Annot) -> list[str]:
    out = [str(v) for v in (annot.info or {}).values() if v]
    try:
        ap = annot.xref and doc.xref_get_key(annot.xref, "AP/N")
        if ap and ap[0] == "xref":
            stream = doc.xref_stream(int(ap[1].split()[0]))
            if stream:
                out.append(stream.decode("latin-1", "ignore"))
    except Exception:  # noqa: BLE001
        pass
    try:
        out.append(doc.xref_object(annot.xref, compressed=True))
    except Exception:  # noqa: BLE001
        pass
    return out


def clean_page_objects(doc: fitz.Document, page: fitz.Page, mappings: list[Mapping],
                       stats: dict) -> None:
    """Called per page *after* redaction. Removes annotations/links carrying PII
    and rewrites form-field values."""
    # Links (URIs such as mailto:, file paths, named destinations)
    for link in list(page.get_links()):
        blob = " ".join(str(link.get(k, "")) for k in ("uri", "file", "name", "nameddest"))
        blob = blob + " " + unquote(blob)  # mailto:jilaba%40example.com
        if contains_any(blob, mappings):
            page.delete_link(link)
            stats["links_removed"] += 1

    # Annotations (comments, free text, stamps, file attachments ...)
    annots = list(page.annots() or [])
    for annot in annots:
        if annot.type[0] in (fitz.PDF_ANNOT_REDACT, fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_LINK):
            continue
        risky = annot.type[0] == fitz.PDF_ANNOT_FILE_ATTACHMENT
        if not risky:
            risky = any(contains_any(s, mappings) for s in _annot_strings(doc, annot))
        if risky:
            page.delete_annot(annot)
            stats["annotations_removed"] += 1

    # Form fields: rewrite values, flag names/labels that contain PII.
    for widget in list(page.widgets() or []):
        changed = False
        value = widget.field_value
        if isinstance(value, str) and value:
            new, n = substitute(value, mappings)
            if n:
                widget.field_value = new
                changed = True
                stats["form_values_replaced"] += n
        for attr in ("field_label", "field_name"):
            v = getattr(widget, attr, None)
            if isinstance(v, str) and contains_any(v, mappings):
                stats["form_names_with_pii"] += 1
        if changed:
            widget.update()


def clean_document(doc: fitz.Document, settings: Settings, stats: dict) -> None:
    mappings = settings.mappings

    # Bookmarks / outline
    toc = doc.get_toc(simple=True)
    if toc:
        new_toc, n_total = [], 0
        for lvl, title, pno in toc:
            new, n = substitute(title, mappings)
            n_total += n
            new_toc.append([lvl, new, pno])
        if n_total:
            doc.set_toc(new_toc)
            stats["bookmarks_replaced"] += n_total

    # Document information dictionary
    meta = dict(doc.metadata or {})
    new_meta, n_meta = {}, 0
    for k, v in meta.items():
        if k in ("format", "encryption"):
            continue
        if settings.strip_metadata:
            new_meta[k] = ""
            continue
        if isinstance(v, str) and v:
            nv, n = substitute(v, mappings)
            n_meta += n
            new_meta[k] = nv
        else:
            new_meta[k] = v or ""
    doc.set_metadata(new_meta)
    stats["metadata_values_replaced"] = n_meta
    stats["metadata_stripped"] = settings.strip_metadata

    # Attachments, JavaScript, thumbnails, XMP history, annotation replies.
    stats["attachments_found"] = doc.embfile_count()
    doc.scrub(
        attached_files=not settings.keep_attachments,
        embedded_files=not settings.keep_attachments,
        clean_pages=True,
        hidden_text=False,       # text was handled explicitly; keeps OCR layers of scans searchable
        javascript=True,
        metadata=False,          # handled above (mapped values replaced instead of wiped)
        redactions=False,        # already applied page by page
        redact_images=0,
        remove_links=False,
        reset_fields=False,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    if not settings.keep_attachments:
        stats["attachments_removed"] = stats["attachments_found"]
    stats["javascript_removed"] = True
    stats["thumbnails_removed"] = True
    stats["xmp_metadata_removed"] = True


def new_stats() -> dict:
    return {
        "links_removed": 0, "annotations_removed": 0, "form_values_replaced": 0,
        "form_names_with_pii": 0, "bookmarks_replaced": 0, "metadata_values_replaced": 0,
        "attachments_found": 0, "attachments_removed": 0,
    }


SAVE_OPTIONS = dict(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                    clean=True, incremental=False, encryption=fitz.PDF_ENCRYPT_NONE,
                    expand=0, linear=False, no_new_id=False)
