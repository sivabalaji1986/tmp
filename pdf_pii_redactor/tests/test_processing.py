"""End-to-end tests: native, scanned, mixed, rotated, OCR misses."""
import json

import pymupdf as fitz

import samples
from conftest import all_text, needs_ocr, raw_dump
from redactor.engine import sha256
from redactor.ocr import OcrEngine, ocr_image_xref, find_ocr_matches
from redactor.config import build_mappings

S = samples


def _out(report):
    return report.output_path_obj


def assert_clean(path, sources=(S.NAME, S.PHONE, S.EMAIL)):
    text = all_text(path).lower()
    raw = raw_dump(path).lower()
    for s in sources:
        assert s.lower() not in text
        assert s.lower().encode() not in raw


# ------------------------------------------------------------------ native

def test_native_multiple_mappings(tmp_path, redact):
    src = S.make_native(tmp_path / "in.pdf")
    before = sha256(src)
    r = redact(src, verify_ocr="none" if not _ocr() else "all")
    assert r.status in ("SUCCESS", "REVIEW_REQUIRED")
    assert sha256(src) == before, "input must never be modified"
    out = _out(r)
    assert_clean(out)
    text = all_text(out)
    assert text.count(S.NAME_R) >= 4 and S.PHONE_R in text and S.EMAIL_R in text
    assert "61234567" in text and "joined in 2020" in text  # unrelated content preserved
    m = {x["id"]: x for x in r.mappings}
    assert m["M1"]["text_matches"] == 4 and m["M2"]["text_matches"] == 2 and m["M3"]["text_matches"] == 1
    assert m["M1"]["overlap_suppressed"] == 1  # 'jilaba' inside the e-mail belongs to M3


def test_native_cleans_metadata_bookmarks_annotations_links(tmp_path, redact):
    out = _out(redact(S.make_native(tmp_path / "in.pdf"), verify_ocr="ocr-pages"))
    with fitz.open(out) as d:
        assert S.NAME not in json.dumps(d.metadata)
        assert all(S.NAME not in t for _, t, _ in d.get_toc())
        for p in d:
            assert all(S.NAME not in str(a.info) for a in p.annots())
            assert all(S.EMAIL not in str(l.get("uri", "")) for l in p.get_links())


def test_native_page_geometry_preserved(tmp_path, redact):
    src = S.make_native(tmp_path / "in.pdf")
    out = _out(redact(src, verify_ocr="ocr-pages"))
    with fitz.open(src) as a, fitz.open(out) as b:
        assert [p.rect for p in a] == [p.rect for p in b]


def test_native_replacement_position(tmp_path, redact):
    out = _out(redact(S.make_native(tmp_path / "in.pdf"), verify_ocr="ocr-pages"))
    with fitz.open(out) as d:
        r = d[0].search_for(S.PHONE_R)[0]
    assert abs(r.y1 - 143) < 4 and 100 < r.x0 < 120  # where the original phone number was


def test_case_sensitive_leaves_other_case(tmp_path, redact):
    src = S.make_native(tmp_path / "in.pdf")
    r = redact(src, pairs=[{"source": S.NAME, "replacement": S.NAME_R}], case_sensitive=True,
               verify_ocr="ocr-pages")
    assert "(preferred: jilaba)" in all_text(_out(r))


def test_unmatched_mapping_reported_without_pii(tmp_path, redact):
    src = S.make_native(tmp_path / "in.pdf")
    r = redact(src, pairs=[{"source": S.NAME, "replacement": "X"}, {"source": "Zebedee", "replacement": "Y"}],
               verify_ocr="ocr-pages")
    m = {x["id"]: x for x in r.mappings}
    assert m["M2"]["unmatched"] and not m["M1"]["unmatched"]
    report_text = (tmp_path / "out.report.json").read_text() + (tmp_path / "out.report.txt").read_text()
    for value in (S.NAME, "Zebedee", S.NAME.lower()):
        assert value not in report_text
    assert "M2" in report_text


def test_longer_replacement_shrinks_or_flags(tmp_path, redact):
    src = S.make_native(tmp_path / "in.pdf")
    long = "A Much Longer Replacement Name Than Before"
    r = redact(src, pairs=[{"source": S.NAME, "replacement": long}], verify_ocr="ocr-pages", strict_layout=True)
    overflow = sum(p["layout_overflows"] for p in r.pages)
    assert overflow > 0 and r.status == "REVIEW_REQUIRED"
    assert any("did not fit" in x for x in r.review_reasons)


def test_empty_replacement_is_pure_redaction(tmp_path, redact):
    r = redact(S.make_native(tmp_path / "in.pdf"), pairs=[{"source": S.PHONE, "replacement": ""}],
               verify_ocr="ocr-pages")
    assert S.PHONE not in all_text(_out(r))


# ------------------------------------------------------------- OCR cases

def _ocr():
    from conftest import HAS_OCR
    return HAS_OCR


def assert_images_clean(path):
    eng = OcrEngine()
    ms = build_mappings([{"source": s, "replacement": "x"} for s in (S.NAME, S.PHONE, S.EMAIL)], False, False, [])
    with fitz.open(path) as d:
        for x in range(1, d.xref_length()):
            if d.xref_get_key(x, "Subtype")[1] == "/Image":
                for words in ocr_image_xref(eng, d, x, "all"):
                    assert not [m for m in find_ocr_matches(words, ms, 0.84) if m[2]], "PII pixels remain in image"


@needs_ocr
def test_scanned_page(tmp_path, redact):
    r = redact(S.make_scanned(tmp_path / "in.pdf"))
    assert r.status == "SUCCESS", r.review_reasons + r.fail_reasons
    out = _out(r)
    assert r.pages[0]["category"] == "scanned" and r.pages[0]["ocr_used"]
    assert all(m["ocr_matches"] == 1 for m in r.mappings)
    assert_images_clean(out)
    text = all_text(out)
    assert S.NAME_R in text and S.PHONE_R in text and S.EMAIL_R in text


@needs_ocr
def test_mixed_page(tmp_path, redact):
    r = redact(S.make_mixed(tmp_path / "in.pdf"))
    assert r.status == "SUCCESS", r.review_reasons + r.fail_reasons
    m = {x["id"]: x for x in r.mappings}
    assert m["M1"]["text_matches"] == 1 and m["M1"]["ocr_matches"] == 1
    out = _out(r)
    assert_clean(out)
    assert_images_clean(out)
    assert "not sensitive" in all_text(out)


@needs_ocr
def test_rotated_pages(tmp_path, redact):
    r = redact(S.make_rotated(tmp_path / "in.pdf"))
    assert r.status == "SUCCESS", r.review_reasons + r.fail_reasons
    out = _out(r)
    assert_clean(out)
    assert_images_clean(out)
    assert r.pages[0]["rotation"] == 90
    assert any(rot != 0 for rot in r.pages[1]["ocr_rotations"])  # sideways scan detected
    # Replacement on the sideways scan reads in the same direction as the original text.
    with fitz.open(out) as d:
        lines = [l for b in d[1].get_text("dict")["blocks"] for l in b.get("lines", [])
                 if S.PHONE_R in "".join(s["text"] for s in l["spans"])]
    assert lines and abs(lines[0]["dir"][0]) < 0.01  # vertical, like the scanned text


@needs_ocr
def test_scan_with_misaligned_ocr_text_layer(tmp_path, redact):
    r = redact(S.make_with_ocr_layer(tmp_path / "in.pdf"))
    assert r.status in ("SUCCESS", "REVIEW_REQUIRED"), r.fail_reasons
    out = _out(r)
    assert_clean(out)
    assert_images_clean(out)
    with fitz.open(out) as d:  # replacement drawn once, not twice
        assert len(d[0].search_for(S.PHONE_R)) == 1


@needs_ocr
def test_unsuccessful_ocr_match_requires_review(tmp_path, redact):
    # Heavy blur: OCR cannot read the values -> mappings unmatched -> never SUCCESS.
    src = S.make_scanned(tmp_path / "in.pdf", blur=6)
    r = redact(src)
    assert r.status in ("REVIEW_REQUIRED", "FAILED")
    assert r.status != "SUCCESS"
    if r.status == "REVIEW_REQUIRED":
        assert ".REVIEW_REQUIRED" in _out(r).name
        assert not (tmp_path / "out.pdf").exists()
        assert any("not found" in x or "uncertain" in x.lower() for x in r.review_reasons)


@needs_ocr
def test_ocr_misread_is_uncertain_and_reviewed(tmp_path, redact):
    # The scan says "Jilaba Raoo"; the mapping asks for "Jilaba Rao0" -> near match only.
    src = S.make_scanned(tmp_path / "in.pdf", lines=["Customer: Jilaba Raoo"])
    r = redact(src, pairs=[{"source": "Jilaba Rao0", "replacement": "X"}])
    assert r.status == "REVIEW_REQUIRED"
    assert r.mappings[0]["ocr_uncertain"] >= 1
