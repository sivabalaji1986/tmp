"""Execution report (JSON + human-readable text). Contains no PII values:
mappings are identified by id only, file names containing a mapped value are withheld."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

SUCCESS = "SUCCESS"
REVIEW = "REVIEW_REQUIRED"
FAILED = "FAILED"

STATUS_EXPLANATION = {
    SUCCESS: "All mapped values were removed and replaced; every verification check passed.",
    REVIEW: "A sanitized file was produced but needs a human check before use (see review_reasons). "
            "It was saved with '.REVIEW_REQUIRED' in its name.",
    FAILED: "Removal could not be assured. No output PDF was kept.",
}

LIMITATIONS = [
    "Verification searches extracted text, PDF objects and OCR of rendered pages and images; "
    "it cannot prove that no trace exists in every possible representation.",
    "OCR can misread or miss text (low resolution, handwriting, stylised fonts, colour on colour, "
    "very small text). Unmatched mappings on scanned pages always require review.",
    "Text split by hyphenation across lines, or drawn as individual vector shapes, may not be matched.",
    "Replacement text uses a standard font and approximate size/position; it is not a pixel-perfect match.",
    "Only literal matches (plus OCR near-matches) are found; other spellings, abbreviations or "
    "derived values (e.g. initials, partially masked numbers) are not detected.",
]


@dataclass
class Report:
    input_name: str
    output_name: str
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finished: str = ""
    status: str = FAILED
    output_file: str | None = None
    fail_reasons: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mappings: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    document: dict = field(default_factory=dict)
    sanitization: dict = field(default_factory=dict)
    verification: dict = field(default_factory=dict)
    environment: dict = field(default_factory=dict)
    input_sha256_before: str = ""
    input_sha256_after: str = ""

    def to_dict(self) -> dict:
        return {
            "tool": "pdf-pii-redactor", "version": __version__,
            "status": self.status, "status_explanation": STATUS_EXPLANATION[self.status],
            "input_file": self.input_name, "requested_output": self.output_name,
            "output_file_written": self.output_file,
            "started_utc": self.started, "finished_utc": self.finished,
            "input_unchanged": bool(self.input_sha256_before) and self.input_sha256_before == self.input_sha256_after,
            "input_sha256": self.input_sha256_before,
            "fail_reasons": self.fail_reasons, "review_reasons": self.review_reasons,
            "warnings": self.warnings, "mappings": self.mappings, "pages": self.pages,
            "document": self.document, "sanitization": self.sanitization,
            "verification": self.verification, "environment": self.environment,
            "limitations": LIMITATIONS,
        }

    def write(self, json_path: Path) -> tuple[Path, Path]:
        self.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data = self.to_dict()
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        txt_path = json_path.with_suffix(".txt")
        txt_path.write_text(render_text(data), encoding="utf-8")
        return json_path, txt_path


def render_text(d: dict) -> str:
    L = []
    L.append("PDF PII REDACTION REPORT")
    L.append("=" * 60)
    L.append(f"Status            : {d['status']}")
    L.append(f"                    {d['status_explanation']}")
    L.append(f"Input             : {d['input_file']}  (unchanged: {'yes' if d['input_unchanged'] else 'NOT VERIFIED'})")
    L.append(f"Output written    : {d['output_file_written'] or '(none)'}")
    L.append(f"Run (UTC)         : {d['started_utc']} -> {d['finished_utc']}")
    for title, key in (("FAILURE REASONS", "fail_reasons"), ("REVIEW REASONS", "review_reasons"),
                       ("WARNINGS", "warnings")):
        if d[key]:
            L.append("")
            L.append(title)
            L.extend(f"  - {x}" for x in d[key])
    L.append("")
    L.append("MAPPINGS (values are never shown; ids follow the order in your config / command line)")
    L.append(f"  {'id':<5}{'text':>6}{'ocr':>6}{'uncert.':>9}{'overlap':>9}{'other':>7}  pages / status")
    for m in d["mappings"]:
        pages = ",".join(str(p) for p in m["pages"]) or "-"
        state = "UNMATCHED" if m["unmatched"] else "matched"
        L.append(f"  {m['id']:<5}{m['text_matches']:>6}{m['ocr_matches']:>6}{m['ocr_uncertain']:>9}"
                 f"{m['overlap_suppressed']:>9}{m['other_replacements']:>7}  {pages} / {state}")
    L.append("")
    L.append("PAGES")
    L.append(f"  {'page':<6}{'type':<9}{'ocr':<5}{'text':>6}{'ocr':>6}{'unc.':>6}{'layout':>8}  verification")
    for p in d["pages"]:
        v = p.get("verification", {})
        vtxt = "text " + ("FAIL" if v.get("text_leaks") else "ok")
        if v.get("ocr_checked"):
            vtxt += ", ocr " + ("FAIL" if v.get("ocr_leaks") else ("review" if v.get("ocr_uncertain") else "ok"))
        L.append(f"  {p['page']:<6}{p['category']:<9}{'yes' if p['ocr_used'] else 'no':<5}"
                 f"{p['text_matches']:>6}{p['ocr_matches']:>6}{p['ocr_uncertain']:>6}{p['layout_overflows']:>8}  {vtxt}")
    L.append("")
    L.append("DOCUMENT CLEAN-UP")
    for k, v in d["sanitization"].items():
        L.append(f"  {k:<28}: {v}")
    L.append("")
    L.append("VERIFICATION COVERAGE")
    for k, v in d["verification"].get("coverage", {}).items():
        L.append(f"  {k:<14}: {v}")
    L.append(f"  leaks found   : {d['verification'].get('leak_count', 0)}")
    L.append(f"  uncertain     : {d['verification'].get('uncertain_count', 0)}")
    L.append("")
    L.append("LIMITATIONS")
    L.extend(f"  - {x}" for x in d["limitations"])
    return "\n".join(L) + "\n"
