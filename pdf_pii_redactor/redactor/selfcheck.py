"""Runtime self-check of the installed PDF engine.

PyMuPDF 2.0 (MuPDF 1.28.5) has a redaction regression: when a word inside a
TJ text array is removed, the text after it on the same line shifts left by the
width of the first removed glyph. Security is unaffected (glyphs are removed),
but layout is. Versions pinned in requirements.txt do not have this problem.
"""
from __future__ import annotations

from functools import lru_cache

import pymupdf as fitz


@lru_cache(maxsize=1)
def redaction_layout_ok() -> bool:
    doc = fitz.open()
    try:
        page = doc.new_page(width=300, height=100)
        font = doc.get_new_xref()
        doc.update_object(font, "<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/WinAnsiEncoding>>")
        doc.xref_set_key(page.xref, "Resources", f"<</Font<</F1 {font} 0 R>>>>")
        content = doc.get_new_xref()
        doc.update_object(content, "<<>>")
        doc.update_stream(content, b"BT /F1 11 Tf 10 50 Td [(AAA WWWW BBB)] TJ ET")
        doc.xref_set_key(page.xref, "Contents", f"{content} 0 R")
        page = doc.reload_page(page)
        before = page.search_for("BBB")[0]
        target = page.search_for("WWWW")[0]
        page.add_redact_annot(fitz.Rect(target.x0 + 0.5, target.y1 - 8, target.x1 - 0.5, target.y1 - 3))
        page.apply_redactions(images=0, graphics=0, text=0)
        after = page.search_for("BBB")
        return bool(after) and abs(after[0].x0 - before.x0) < 0.5
    except Exception:  # noqa: BLE001
        return False
    finally:
        doc.close()
