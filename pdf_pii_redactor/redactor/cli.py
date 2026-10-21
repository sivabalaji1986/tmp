"""Command-line interface.

Exit codes: 0 = SUCCESS, 2 = REVIEW_REQUIRED, 1 = FAILED, 3 = invalid usage/configuration.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import ConfigError, build_settings

EXIT = {"SUCCESS": 0, "FAILED": 1, "REVIEW_REQUIRED": 2}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf_redactor",
        description="Permanently remove listed values (names, numbers, e-mails ...) from a PDF and "
                    "write replacement text in their place. Works fully offline.",
        epilog="Exit codes: 0 success, 2 review required, 1 failed, 3 invalid usage.",
    )
    p.add_argument("--input", help="PDF to sanitize (never modified)")
    p.add_argument("--output", help="path of the sanitized PDF to create")
    p.add_argument("--config", help="JSON file with input/output and replacement mappings")
    p.add_argument("--replace", nargs=2, action="append", metavar=("SOURCE", "REPLACEMENT"),
                   help="value to remove and its replacement (repeatable). Tip: prefer --config "
                        "so values do not end up in your shell history")
    p.add_argument("--report", help="report path (default: <output>.report.json, plus a .txt)")
    cs = p.add_mutually_exclusive_group()
    cs.add_argument("--case-sensitive", dest="case_sensitive", action="store_const", const=True, default=None)
    cs.add_argument("--ignore-case", dest="case_sensitive", action="store_const", const=False)
    p.add_argument("--whole-word", action="store_true", default=False,
                   help="only match whole words (default: also inside longer words)")
    p.add_argument("--overwrite", action="store_const", const=True, default=None,
                   help="replace an existing output file")

    o = p.add_argument_group("OCR")
    o.add_argument("--ocr-mode", choices=["auto", "always", "never"], default=None,
                   help="auto (default): OCR pages with images/unreadable text; always: every page")
    o.add_argument("--ocr-lang", default=None, help="Tesseract language(s), e.g. eng or eng+fra")
    o.add_argument("--ocr-dpi", type=int, default=None, help="render resolution for OCR (default 300)")
    o.add_argument("--ocr-psm", type=int, default=None, help="Tesseract page segmentation mode (default 11)")
    o.add_argument("--ocr-rotations", choices=["auto", "all", "none"], default=None,
                   help="auto (default): detect page orientation; all: try 0/90/180/270 on every page")
    o.add_argument("--tesseract-cmd", default=None, help="full path to tesseract(.exe) if not on PATH")
    o.add_argument("--fuzzy-threshold", type=float, default=None,
                   help="similarity (0.5-1.0) above which an OCR near-match is treated as uncertain PII (default 0.84)")
    o.add_argument("--no-redact-uncertain", dest="redact_uncertain", action="store_const", const=False,
                   default=None, help="report uncertain OCR matches without redacting them")

    v = p.add_argument_group("Verification and layout")
    v.add_argument("--verify-ocr", choices=["all", "ocr-pages", "none"], default=None,
                   help="pages to OCR during verification (default all; 'none' forces review)")
    v.add_argument("--min-font-scale", type=float, default=None,
                   help="smallest font scale used to fit longer replacements (default 0.6)")
    v.add_argument("--strict-layout", action="store_const", const=True, default=None,
                   help="treat replacements that do not fit as needing review")
    v.add_argument("--font-file", default=None, help="TTF/OTF font for replacement text (non-Latin scripts)")

    s = p.add_argument_group("Document clean-up")
    s.add_argument("--keep-attachments", action="store_const", const=True, default=None,
                   help="keep embedded files (result will require review)")
    s.add_argument("--strip-metadata", action="store_const", const=True, default=None,
                   help="blank all document properties instead of replacing mapped values")

    p.add_argument("--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")
    # Third-party libraries must not echo document content.
    for noisy in ("PIL", "pytesseract"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not args.config and not (args.input and args.output and args.replace):
        print("error: give --config, or --input, --output and at least one --replace", file=sys.stderr)
        return 3
    try:
        settings = build_settings(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3

    from .engine import run  # heavy imports only after the configuration is valid

    report = run(settings)
    print()
    print(f"Status: {report.status}")
    for r in report.fail_reasons:
        print(f"  FAIL:   {r}")
    for r in report.review_reasons:
        print(f"  REVIEW: {r}")
    for w in report.warnings:
        print(f"  note:   {w}")
    out = getattr(report, "output_path_obj", None)
    print(f"Output: {out if out else '(none written)'}")
    paths = getattr(report, "report_paths", None)
    if paths:
        print(f"Report: {paths[0]}  and  {paths[1].name}")
    return EXIT.get(report.status, 1)


if __name__ == "__main__":
    sys.exit(main())
