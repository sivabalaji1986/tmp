# PDF PII Redactor

Permanently removes names, phone numbers, e-mail addresses or any other text you list from a PDF, and writes a substitute value in the same place. Works on normal (typed) PDFs, scanned PDFs and mixtures of both. Everything runs on your own computer — nothing is uploaded anywhere.

- The **original PDF is never changed**. A new file is created.
- The original text is **actually deleted** (not covered with a box). On scans, the image pixels are wiped before the new text is written.
- Every result is **checked again afterwards** (text search, PDF object scan, OCR of pages and images) and given a clear status.
- The report never contains the values you asked to remove.

---

## 1. One-time setup

You need **Python 3.11 or newer** and **Tesseract OCR** (the free, offline text-recognition engine used for scans).

### Windows

1. Install Python from <https://www.python.org/downloads/> — tick **"Add python.exe to PATH"** during setup.
2. Install Tesseract: download the Windows installer from the UB Mannheim build (<https://github.com/UB-Mannheim/tesseract/wiki>) and install with default options. The tool finds it automatically in `C:\Program Files\Tesseract-OCR\`.
3. Open **Command Prompt** in the project folder and run:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### macOS

1. Install Homebrew if you do not have it (<https://brew.sh>), then:

```bash
brew install python@3.12 tesseract
```

2. In **Terminal**, inside the project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Other languages:** Tesseract ships with English. For other languages install the language pack (macOS: `brew install tesseract-lang`; Windows: tick the languages in the installer) and add e.g. `--ocr-lang eng+chi_sim`.

Check the install with `python pdf_redactor.py --version`.

---

## 2. Everyday use

Activate the environment first (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on macOS).

### Option A — a mappings file (recommended)

Edit `mappings.json`:

```json
{
  "input_file": "original.pdf",
  "output_file": "sanitized.pdf",
  "case_sensitive": false,
  "replacements": [
    {"source": "Jilaba", "replacement": "SiBa"},
    {"source": "98765432", "replacement": "12345678"},
    {"source": "jilaba@email.com", "replacement": "siba@example.com"}
  ]
}
```

```bash
python pdf_redactor.py --config mappings.json
```

File names in the config are relative to the folder the config file is in. You can still override them: `--config mappings.json --input other.pdf --output other_clean.pdf`.

> The mappings file contains the personal data you are removing. Store it securely and delete it when you are done.

### Option B — everything on the command line

```bash
python pdf_redactor.py --input original.pdf --output sanitized.pdf \
  --replace "Jilaba" "SiBa" \
  --replace "98765432" "12345678" \
  --replace "jilaba@email.com" "siba@example.com"
```

(On Windows Command Prompt, write it on one line or use `^` instead of `\`.) Values typed on the command line may be kept in your shell history — the mappings file avoids that.

A replacement may be empty (`"replacement": ""`) to simply remove the value.

---

## 3. Results: what the status means

| Status | Exit code | What you get | What to do |
|---|---|---|---|
| **SUCCESS** | 0 | `sanitized.pdf` | Every check passed. Spot-check the layout and use it. |
| **REVIEW_REQUIRED** | 2 | `sanitized.REVIEW_REQUIRED.pdf` | Removal is likely but something could not be confirmed. Follow the manual review below. |
| **FAILED** | 1 | *no PDF* | A value may still be present, or the file could not be processed. Read the reasons. |
| (usage error) | 3 | nothing | Fix the command or mappings file. |

A report is always written next to the output: `sanitized.report.txt` (human readable) and `sanitized.report.json` (for systems). Mappings are listed as **M1, M2, …** in the order you gave them — never by value.

Typical reasons for **REVIEW_REQUIRED**: an OCR near-match (e.g. the scan reads `Ji1aba`), a mapping not found in a document that contains scans, a digitally signed document, hidden layers, a value found in a raw PDF object that could not be located on the page, or visual verification switched off.

Typical reasons for **FAILED**: a value still found after processing, scanned pages but Tesseract not installed, encrypted/password-protected input, unreadable PDF, output file already exists (use `--overwrite`).

### Manual review workflow

1. Open `*.report.txt`. Read *REVIEW REASONS* and note the page numbers.
2. Open the `.REVIEW_REQUIRED.pdf` and the original side by side. On each listed page, check that every value is gone and the replacement is readable.
3. In the review copy, press Ctrl/Cmd+F and search for each original value (and obvious variants).
4. For "not found" mappings: check the original — if the value is genuinely absent, the result is fine. If it is present but unreadable to OCR (blurred, handwritten, stylised), redact that page manually or re-run with a higher `--ocr-dpi 400`.
5. When satisfied, rename the file to drop `.REVIEW_REQUIRED`. Keep the report with the document's audit trail.

---

## 4. Options

| Option | Default | Meaning |
|---|---|---|
| `--case-sensitive` / `--ignore-case` | ignore case | Match `Jilaba` only, or also `JILABA`, `jilaba`. Can also be set per mapping (`"case_sensitive": true`). |
| `--whole-word` | off | Match only whole words (`Sam` will not match inside `Samsung`). By default values are matched inside longer words too — safer for PII. |
| `--ocr-mode auto\|always\|never` | auto | `auto` OCRs pages with images or unreadable text. `always` also OCRs plain text pages (catches text drawn as shapes). `never` forces review on scanned pages. |
| `--ocr-lang` | eng | Tesseract language(s), e.g. `eng+fra`. |
| `--ocr-dpi` | 300 | Higher = better for small print, slower. |
| `--ocr-rotations auto\|all\|none` | auto | `auto` detects sideways/upside-down pages. Use `all` for pages that mix orientations. |
| `--tesseract-cmd PATH` | auto-detect | Path to `tesseract.exe` if installed somewhere unusual. Also `TESSERACT_CMD` environment variable. |
| `--fuzzy-threshold` | 0.84 | How similar an OCR reading must be to count as an uncertain match. |
| `--no-redact-uncertain` | redact | Report uncertain OCR matches but do not remove them (result is always review). |
| `--verify-ocr all\|ocr-pages\|none` | all | Pages OCR'd during verification. `ocr-pages` is faster for long typed documents; `none` always yields review. |
| `--min-font-scale` | 0.6 | How far a long replacement may be shrunk to fit. |
| `--strict-layout` | off | Treat "replacement did not fit" as needing review. |
| `--font-file FONT.ttf` | built-in | Font for replacements in non-Latin scripts (a Unicode fallback font is used automatically otherwise). |
| `--keep-attachments` | removed | Keep embedded files (they are not inspected, so the result needs review). |
| `--strip-metadata` | replace | Blank all document properties instead of replacing values in them. |
| `--report PATH` | next to output | Where to write the report. |
| `--overwrite` | off | Allow replacing an existing output file. |

All options can also go in the `"options"` block of the mappings file, using underscores (e.g. `"verify_ocr": "ocr-pages"`).

### How mappings are applied

- All mappings are applied in one run across all pages.
- Spaces in a value match any spacing, including none and line breaks (`Jilaba Rao` matches `JilabaRao` and `Jilaba⏎Rao`).
- When values overlap, the **longer** one wins, then the one listed first. Example: with `jilaba` and `jilaba@email.com`, the e-mail address is replaced as a whole.
- Replacements are never processed again: `A→B` and `B→C` turn `A B` into `B C`, not `C C`.
- Two mappings with the same value but different replacements are rejected as ambiguous; identical ones are merged.

---

## 5. What gets cleaned

| Area | Action |
|---|---|
| Page text (including white, tiny, invisible and off-page text) | Matched characters deleted from the page content; replacement drawn in the same place, font family, size, colour and direction. Invisible OCR layers stay invisible. |
| Scanned / image content | OCR locates the value; image pixels under it are overwritten, the area is filled with the surrounding background colour, and the replacement is drawn on top. |
| Vector shapes fully inside an OCR-matched area | Removed. |
| Comments / annotations / file-attachment annotations | Deleted when they contain a value; attachment annotations always deleted. |
| Links (e.g. `mailto:`) | Deleted when they contain a value. |
| Form fields | Values rewritten with the replacement; field names containing a value flag review. |
| Bookmarks, document properties | Values replaced (`--strip-metadata` blanks them). |
| XMP metadata, embedded files, JavaScript, page thumbnails | Removed. |
| Old revisions / unused objects | Removed by a full (non-incremental) rewrite with garbage collection. |

---

## 6. Verification and its limits

After saving, the tool reopens the new file and checks:

1. **Text layer** — character-level search on every page with two independent extraction methods, plus text outside the visible page area.
2. **PDF strings** — annotations, form fields, links, bookmarks, document properties.
3. **Object scan** — every remaining PDF object and decompressed stream, including hex and UTF-16 strings.
4. **Visual OCR** — each page is rendered and OCR'd (exact and near matches).
5. **Image OCR** — every embedded image is OCR'd at its own resolution, which also finds content hidden by clipping or cropping.

Only the exact spots where a replacement was drawn are exempt (relevant when a replacement contains another value).

**Limitations — please read**

- Verification lowers risk; it cannot *prove* that no trace remains in every representation. Treat SUCCESS as "all checks passed", not as a guarantee.
- OCR can miss or misread text (blur, low resolution, handwriting, decorative fonts, light text on coloured backgrounds, very small print). Unmatched values on scanned pages are therefore always flagged for review.
- Only the literal values you list are found (plus OCR near-matches). Other spellings, initials, partially masked numbers (`9876-5432` vs `98765432`), or text hyphenated across lines are **not** matched — add them as extra mappings.
- Text drawn as individual vector outlines (some designed PDFs) is not text to the PDF engine; use `--ocr-mode always`. If such a shape is only partly covered it stays, and verification will fail the run.
- Replacement text uses a standard font at an estimated size and position. A shorter replacement leaves a gap; a longer one is shrunk (down to `--min-font-scale`) and flagged if it still does not fit.
- When a word is removed from inside an image, the area is filled with one background colour; textured or photographic backgrounds will show a patch.
- Removing a value on a page where text lines overlap very closely can occasionally remove part of a neighbouring character.

**Not supported**

- Encrypted / password-protected PDFs (remove the protection first; the tool refuses them).
- Digital signatures: redaction invalidates them; such files always need review.
- XFA (dynamic) forms: the XFA data is not processed; review required.
- Optional-content layers are processed with all layers switched on (review required).
- Content inside attachments (attachments are removed by default).
- Text inside images that OCR cannot read at all.

---

## 7. For IT / developers

```
pdf_redactor.py          entry point
redactor/
  cli.py                 argument parsing, exit codes
  config.py              settings + mapping validation (duplicates, overlaps, chains)
  matching.py            literal matcher, overlap resolution, non-cascading substitution, fuzzy scoring
  classify.py            page classification (native / scanned / mixed / garbled / vector)
  native.py              text-layer matching with glyph geometry, true redaction, off-page sweep
  ocr.py                 Tesseract wrapper, orientation detection, OCR matching, pixel redaction
  drawing.py             replacement text placement, font matching and fitting
  sanitize.py            annotations, links, forms, bookmarks, metadata, attachments, JS, thumbnails
  verify.py              post-save verification
  report.py              JSON/text report (PII-free)
  engine.py              orchestration, temp-file save, status rules
  selfcheck.py           detects a known PyMuPDF 2.0 layout regression at runtime
tests/                   pytest suite with generated sample documents
```

Run the tests (OCR tests are skipped automatically if Tesseract is missing):

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Processing order per page: classify → find text-layer matches → delete glyphs (tight boxes to protect neighbouring lines) → residual sweep → off-page sweep → OCR the rendered page → wipe pixels / text / covered vector shapes under OCR matches → fallback pixel wipe for text-layer matches on images not confirmed by OCR → draw replacements → clean page objects. Then document clean-up, save to a temporary file in the output folder (garbage-collected, non-incremental, unencrypted), verify, and only then move it to its final name (or delete it on failure). The input's SHA-256 is recorded before and after the run.

Logs and reports never contain mapping values; error messages from libraries are withheld if they contain one. File names that contain a mapped value are withheld from the report.

**Version note:** `requirements.txt` pins PyMuPDF below 2.0 because PyMuPDF 2.0 shifts text that follows a redaction on the same line (it is a layout issue, not a leak). If a different version is installed, the tool tests for this at start-up and warns.
