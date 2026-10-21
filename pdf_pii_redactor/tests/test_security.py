"""Secure-removal regression tests and fail-closed behaviour."""
import io

import pymupdf as fitz
import pytest

import samples as S
from conftest import all_text, needs_ocr, raw_dump
from redactor import engine as engine_mod
from redactor.config import Settings, build_mappings
from redactor.ocr import OcrUnavailable
from redactor.verify import verify_output


def _settings(tmp_path, inp, **kw):
    ms = build_mappings([{"source": S.NAME, "replacement": S.NAME_R},
                         {"source": S.PHONE, "replacement": S.PHONE_R}], False, False, [])
    s = Settings(input_path=inp, output_path=tmp_path / "out.pdf", mappings=ms, overwrite=True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_overlay_only_redaction_is_detected(tmp_path):
    """A black box drawn over text (fake redaction) must be caught by verification."""
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), f"Name: {S.NAME}", fontsize=12)
    p.draw_rect(fitz.Rect(100, 85, 160, 105), color=(0, 0, 0), fill=(0, 0, 0))
    path = tmp_path / "fake.pdf"
    doc.save(path)
    res = verify_output(path, _settings(tmp_path, path, verify_ocr="none"), {}, set(), None)
    assert any(l["check"] == "text_layer" for l in res.leaks)


def test_hidden_white_tiny_and_offpage_text_removed(tmp_path):
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Visible line", fontsize=12)
    p.insert_text((72, 140), f"white {S.NAME}", fontsize=12, color=(1, 1, 1))
    p.insert_text((72, 160), f"invisible {S.PHONE}", fontsize=12, render_mode=3)
    p.insert_text((72, 180), f"tiny {S.NAME}", fontsize=1)
    p.insert_text((700, 900), f"off page {S.NAME}", fontsize=12)  # outside the media box
    src = tmp_path / "hidden.pdf"
    doc.save(src)
    r = engine_mod.run(_settings(tmp_path, src, verify_ocr="ocr-pages"))
    assert r.status in ("SUCCESS", "REVIEW_REQUIRED"), r.fail_reasons
    raw = raw_dump(r.output_path_obj)
    assert S.NAME.encode() not in raw and S.PHONE.encode() not in raw
    assert S.NAME not in all_text(r.output_path_obj)


def test_incremental_history_is_removed(tmp_path):
    """Old revisions kept by incremental saves must not survive."""
    src = tmp_path / "hist.pdf"
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), f"Original owner {S.NAME}", fontsize=12)
    doc.save(src)
    doc.close()
    doc = fitz.open(src)
    doc[0].insert_text((72, 140), "Later edit", fontsize=12)
    doc.saveIncr()
    doc.close()
    assert src.read_bytes().count(b"%%EOF") >= 2
    r = engine_mod.run(_settings(tmp_path, src, verify_ocr="ocr-pages"))
    out = r.output_path_obj
    assert out.read_bytes().count(b"%%EOF") == 1
    assert S.NAME.encode() not in raw_dump(out)


def test_attachments_and_xmp_removed(tmp_path):
    src = tmp_path / "att.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), f"Hello {S.NAME}", fontsize=12)
    doc.embfile_add("notes.txt", f"secret {S.NAME}".encode())
    doc.set_xml_metadata(f"<x:xmpmeta xmlns:x='adobe:ns:meta/'><r>{S.NAME}</r></x:xmpmeta>")
    doc.save(src)
    r = engine_mod.run(_settings(tmp_path, src, verify_ocr="ocr-pages"))
    with fitz.open(r.output_path_obj) as d:
        assert d.embfile_count() == 0 and d.xref_xml_metadata() == 0
    assert S.NAME.encode() not in raw_dump(r.output_path_obj)


def test_form_field_values_replaced(tmp_path):
    src = tmp_path / "form.pdf"
    doc = fitz.open()
    p = doc.new_page()
    w = fitz.Widget()
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.field_name = "applicant"
    w.rect = fitz.Rect(72, 80, 300, 100)
    w.field_value = f"{S.NAME} Rao"
    p.add_widget(w)
    doc.save(src)
    r = engine_mod.run(_settings(tmp_path, src, verify_ocr="ocr-pages"))
    with fitz.open(r.output_path_obj) as d:
        vals = [x.field_value for x in d[0].widgets()]
    assert vals == [f"{S.NAME_R} Rao"]
    assert S.NAME.encode() not in raw_dump(r.output_path_obj)


@needs_ocr
def test_pii_in_hidden_part_of_image_fails_closed(tmp_path):
    """PII in an image region that is clipped away on the page cannot be seen by page
    OCR; image-level verification must catch it and refuse a SUCCESS."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1600, 400), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 60), "Visible part only", fill="black", font=S.font(60))
    d.text((40, 250), f"Hidden: {S.NAME} {S.PHONE}", fill="black", font=S.font(60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    src = tmp_path / "clip.pdf"
    doc = fitz.open()
    p = doc.new_page(width=400, height=200)
    xref = p.insert_image(fitz.Rect(0, 0, 400, 100), stream=buf.getvalue())
    # Clip the bottom half of the image away by enlarging the image and cropping the page.
    doc.save(src)
    doc.close()
    doc = fitz.open(src)
    pg = doc[0]
    pg.set_cropbox(fitz.Rect(0, 0, 400, 50))
    doc.save(tmp_path / "clip2.pdf")
    r = engine_mod.run(_settings(tmp_path, tmp_path / "clip2.pdf"))
    assert r.status == "FAILED"
    assert any("image_ocr" in x for x in r.fail_reasons)
    assert not (tmp_path / "out.pdf").exists()
    assert xref


def test_ocr_required_but_unavailable_fails_closed(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OcrUnavailable("Tesseract OCR executable not found")
    monkeypatch.setattr(engine_mod, "OcrEngine", boom)
    src = S.make_scanned(tmp_path / "in.pdf")
    r = engine_mod.run(_settings(tmp_path, src))
    assert r.status == "FAILED"
    assert not (tmp_path / "out.pdf").exists() and not (tmp_path / "out.REVIEW_REQUIRED.pdf").exists()
    assert not list(tmp_path.glob(".pii-redactor-*"))  # temp file cleaned up


def test_ocr_disabled_on_scan_requires_review(tmp_path):
    src = S.make_scanned(tmp_path / "in.pdf")
    r = engine_mod.run(_settings(tmp_path, src, ocr_mode="never", verify_ocr="none"))
    assert r.status == "REVIEW_REQUIRED"


def test_encrypted_input_rejected(tmp_path):
    src = tmp_path / "enc.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), S.NAME)
    doc.save(src, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw2")
    r = engine_mod.run(_settings(tmp_path, src))
    assert r.status == "FAILED" and any("Encrypted" in x for x in r.fail_reasons)


def test_existing_output_not_overwritten(tmp_path):
    src = S.make_native(tmp_path / "in.pdf")
    (tmp_path / "out.pdf").write_bytes(b"keep me")
    s = _settings(tmp_path, src, overwrite=False)
    r = engine_mod.run(s)
    assert r.status == "FAILED" and (tmp_path / "out.pdf").read_bytes() == b"keep me"


def test_output_same_as_input_rejected(tmp_path):
    src = S.make_native(tmp_path / "in.pdf")
    s = _settings(tmp_path, src)
    s.output_path = src
    before = src.read_bytes()
    r = engine_mod.run(s)
    assert r.status == "FAILED" and src.read_bytes() == before


def test_report_and_console_contain_no_pii(tmp_path, capsys):
    from redactor.cli import main
    src = S.make_native(tmp_path / "in.pdf")
    code = main(["--input", str(src), "--output", str(tmp_path / "o.pdf"),
                 "--replace", S.NAME, S.NAME_R, "--replace", S.PHONE, S.PHONE_R, "--verify-ocr", "ocr-pages"])
    assert code in (0, 2)
    captured = capsys.readouterr()
    blob = captured.out + captured.err + (tmp_path / "o.report.json").read_text() + (tmp_path / "o.report.txt").read_text()
    for v in (S.NAME, S.PHONE, S.NAME_R, S.PHONE_R):
        assert v not in blob


def test_pii_filename_withheld_in_report(tmp_path):
    src = S.make_native(tmp_path / f"{S.NAME}_profile.pdf")
    r = engine_mod.run(_settings(tmp_path, src, verify_ocr="ocr-pages"))
    assert S.NAME not in (tmp_path / "out.report.json").read_text()
    assert r.input_name.startswith("[withheld")


def test_verification_flags_offpage_text(tmp_path):
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Nothing here", fontsize=12)
    p.insert_text((900, 1200), f"{S.NAME}", fontsize=12)
    path = tmp_path / "off.pdf"
    doc.save(path)
    res = verify_output(path, _settings(tmp_path, path, verify_ocr="none"), {}, set(), None)
    assert any(l["check"] == "offpage_text" for l in res.leaks)


def test_invisible_text_replacement_stays_invisible(tmp_path):
    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Visible header", fontsize=12)
    p.insert_text((72, 160), f"layer {S.PHONE}", fontsize=12, render_mode=3)
    src = tmp_path / "inv.pdf"
    doc.save(src)
    r = engine_mod.run(_settings(tmp_path, src, verify_ocr="ocr-pages"))
    with fitz.open(r.output_path_obj) as d:
        spans = [s for b in d[0].get_text("dict")["blocks"] for l in b.get("lines", [])
                 for s in l["spans"] if S.PHONE_R in s["text"]]
        pix = d[0].get_pixmap(clip=fitz.Rect(60, 140, 300, 170))
    assert spans and spans[0]["alpha"] == 0
    assert min(pix.samples) > 200  # nothing visible drawn
