import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from redactor.config import build_mappings, Settings  # noqa: E402
from redactor.engine import run  # noqa: E402
import samples  # noqa: E402


def _tesseract_ok() -> bool:
    try:
        from redactor.ocr import OcrEngine
        OcrEngine()
        return True
    except Exception:  # noqa: BLE001
        return False


HAS_OCR = _tesseract_ok()
needs_ocr = pytest.mark.skipif(not HAS_OCR, reason="Tesseract OCR not installed")

DEFAULT_PAIRS = [
    {"source": samples.NAME, "replacement": samples.NAME_R},
    {"source": samples.PHONE, "replacement": samples.PHONE_R},
    {"source": samples.EMAIL, "replacement": samples.EMAIL_R},
]


@pytest.fixture
def redact(tmp_path):
    """Run the full pipeline: redact(input_path, pairs=None, **settings) -> (report, tmp_path)."""
    def _run(inp: Path, pairs=None, case_sensitive=False, **opts):
        warnings = []
        mappings = build_mappings(pairs or DEFAULT_PAIRS, case_sensitive, False, warnings)
        out = tmp_path / "out.pdf"
        s = Settings(input_path=inp, output_path=out, mappings=mappings, warnings=warnings, overwrite=True)
        for k, v in opts.items():
            setattr(s, k, v)
        return run(s)
    return _run


def all_text(path: Path) -> str:
    import pymupdf as fitz
    with fitz.open(path) as d:
        return " ".join(p.get_text() for p in d)


def raw_dump(path: Path) -> bytes:
    """Every object and decompressed stream of the file, plus the file bytes."""
    import pymupdf as fitz
    parts = [path.read_bytes()]
    with fitz.open(path) as d:
        for x in range(1, d.xref_length()):
            try:
                parts.append(d.xref_object(x).encode("latin-1", "ignore"))
                if d.xref_is_stream(x):
                    parts.append(d.xref_stream(x) or b"")
            except Exception:  # noqa: BLE001
                pass
    return b"\n".join(parts)
