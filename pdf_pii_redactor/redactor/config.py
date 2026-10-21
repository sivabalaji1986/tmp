"""Settings, mapping loading and mapping validation.

Nothing in this module ever prints or logs a source/replacement value.
Mappings are referred to by their id ("M1", "M2", ...) in all messages.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MIN_SOURCE_LENGTH = 2


class ConfigError(Exception):
    """Raised for invalid configuration. Messages never contain PII values."""


@dataclass(frozen=True)
class Mapping:
    id: str                # stable, PII-free identifier, e.g. "M1"
    source: str            # literal text to remove
    replacement: str       # literal text to draw in its place ("" = pure redaction)
    case_sensitive: bool
    whole_word: bool
    order: int             # position in the merged mapping list

    @property
    def priority(self) -> tuple[int, int]:
        """Longer sources win overlaps; ties are broken by declaration order."""
        return (-len(self.source), self.order)

    def describe(self) -> dict:
        """PII-free description used in reports."""
        return {
            "id": self.id,
            "source_length": len(self.source),
            "replacement_length": len(self.replacement),
            "case_sensitive": self.case_sensitive,
            "whole_word": self.whole_word,
        }


@dataclass
class Settings:
    input_path: Path
    output_path: Path
    mappings: list[Mapping]
    report_path: Path | None = None
    overwrite: bool = False
    # OCR
    ocr_mode: str = "auto"            # auto | always | never
    ocr_lang: str = "eng"
    ocr_dpi: int = 300
    ocr_psm: int = 11
    ocr_rotations: str = "auto"       # auto | all | none
    ocr_min_conf: float = 55.0
    tesseract_cmd: str | None = None
    fuzzy_threshold: float = 0.84
    redact_uncertain: bool = True
    # Verification
    verify_ocr: str = "all"           # all | ocr-pages | none
    # Layout
    min_font_scale: float = 0.6
    strict_layout: bool = False
    font_file: str | None = None
    # Sanitization
    keep_attachments: bool = False
    strip_metadata: bool = False
    warnings: list[str] = field(default_factory=list)


def _as_bool(value, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"'{name}' must be true or false")


def build_mappings(
    raw_pairs: list[dict],
    default_case_sensitive: bool,
    default_whole_word: bool,
    warnings: list[str],
) -> list[Mapping]:
    """Validate raw {source, replacement, ...} dicts and return Mapping objects.

    Rules (deterministic):
      * source must be a non-empty string of at least MIN_SOURCE_LENGTH visible chars
      * leading/trailing whitespace in a source is stripped (warning)
      * internal whitespace runs are treated as "any whitespace or none" when matching
      * exact duplicates (same source under the effective case rule, same replacement)
        are merged with a warning
      * duplicates with different replacements are rejected (ambiguous)
      * overlapping sources are allowed: longer source wins, then declaration order
      * a replacement containing another (or the same) source is allowed; replacements
        are never re-scanned (no cascading), and validation excludes the inserted text
    """
    if not raw_pairs:
        raise ConfigError("No replacement mappings were supplied")

    mappings: list[Mapping] = []
    for idx, item in enumerate(raw_pairs, start=1):
        mid = f"M{idx}"
        if not isinstance(item, dict):
            raise ConfigError(f"Mapping {mid}: each entry must be an object with 'source' and 'replacement'")
        source = item.get("source")
        replacement = item.get("replacement", "")
        if not isinstance(source, str) or not isinstance(replacement, str):
            raise ConfigError(f"Mapping {mid}: 'source' and 'replacement' must be text")
        stripped = source.strip()
        if stripped != source:
            warnings.append(f"Mapping {mid}: leading/trailing whitespace in source was ignored")
        source = " ".join(stripped.split())  # collapse internal whitespace runs
        if len(source.replace(" ", "")) < MIN_SOURCE_LENGTH:
            raise ConfigError(
                f"Mapping {mid}: source must contain at least {MIN_SOURCE_LENGTH} non-space characters"
            )
        if any(ord(c) < 32 for c in replacement):
            raise ConfigError(f"Mapping {mid}: replacement contains control characters")
        cs = _as_bool(item.get("case_sensitive", default_case_sensitive), f"{mid}.case_sensitive")
        ww = _as_bool(item.get("whole_word", default_whole_word), f"{mid}.whole_word")
        # Two entries are duplicates when they would match the same text: identical
        # sources, or sources equal ignoring case unless both are case-sensitive.
        dup = next(
            (
                p for p in mappings
                if p.source == source
                or (p.source.casefold() == source.casefold() and not (cs and p.case_sensitive))
            ),
            None,
        )
        if dup is not None:
            if dup.replacement == replacement:
                warnings.append(f"Mapping {mid}: duplicate of {dup.id}; merged")
                continue
            raise ConfigError(
                f"Mapping {mid}: same source as {dup.id} but a different replacement (ambiguous)"
            )
        if source.casefold() == replacement.casefold() and replacement:
            warnings.append(f"Mapping {mid}: replacement equals source; text will be re-drawn unchanged")
        mappings.append(Mapping(mid, source, replacement, cs, ww, len(mappings)))

    # Informational: flag overlaps and chains (no values disclosed).
    for a in mappings:
        for b in mappings:
            if a is b:
                continue
            if b.source.casefold() in a.source.casefold() and len(b.source) < len(a.source):
                warnings.append(
                    f"Mappings {a.id}/{b.id}: {b.id} is contained in {a.id}; {a.id} takes precedence where both match"
                )
            if b.source.casefold() in a.replacement.casefold():
                warnings.append(
                    f"Mapping {a.id}: its replacement contains the source of {b.id}; "
                    "inserted text is never re-processed (no cascading)"
                )
    return mappings


def _resolve(path_value, base: Path | None, name: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise ConfigError(f"'{name}' must be a file path")
    p = Path(os.path.expanduser(path_value))
    if not p.is_absolute() and base is not None:
        p = base / p
    return p


OPTION_KEYS = {
    "ocr_mode": str, "ocr_lang": str, "ocr_dpi": int, "ocr_psm": int, "ocr_rotations": str,
    "ocr_min_conf": (int, float), "tesseract_cmd": str, "fuzzy_threshold": (int, float),
    "redact_uncertain": bool, "verify_ocr": str, "min_font_scale": (int, float),
    "strict_layout": bool, "font_file": str, "keep_attachments": bool, "strip_metadata": bool,
    "overwrite": bool,
}


def load_config_file(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("Config file not found") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON (line {exc.lineno}, column {exc.colno})") from None
    if not isinstance(data, dict):
        raise ConfigError("Config file must contain a JSON object")
    return data


def build_settings(args) -> Settings:
    """Merge config file (if any) and CLI arguments. CLI values win."""
    warnings: list[str] = []
    cfg: dict = {}
    base: Path | None = None
    if args.config:
        cfg_path = Path(args.config)
        cfg = load_config_file(cfg_path)
        base = cfg_path.resolve().parent

    input_value = args.input or cfg.get("input_file")
    output_value = args.output or cfg.get("output_file")
    if not input_value:
        raise ConfigError("An input PDF is required (--input or 'input_file' in config)")
    if not output_value:
        raise ConfigError("An output PDF is required (--output or 'output_file' in config)")
    # CLI paths are relative to the current folder; config paths to the config's folder.
    input_path = _resolve(input_value, None if args.input else base, "input")
    output_path = _resolve(output_value, None if args.output else base, "output")

    case_sensitive = cfg.get("case_sensitive", False)
    if args.case_sensitive is not None:
        case_sensitive = args.case_sensitive
    case_sensitive = _as_bool(case_sensitive, "case_sensitive")
    whole_word = _as_bool(args.whole_word or cfg.get("whole_word", False), "whole_word")

    raw_pairs = list(cfg.get("replacements", []) or [])
    if not isinstance(raw_pairs, list):
        raise ConfigError("'replacements' must be a list")
    for src, rep in args.replace or []:
        raw_pairs.append({"source": src, "replacement": rep})
    mappings = build_mappings(raw_pairs, case_sensitive, whole_word, warnings)

    options = dict(cfg.get("options", {}) or {})
    for key, value in options.items():
        expected = OPTION_KEYS.get(key)
        if expected is None:
            raise ConfigError(f"Unknown option '{key}' in config")
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ConfigError(f"Option '{key}' has the wrong type")

    s = Settings(input_path=input_path, output_path=output_path, mappings=mappings, warnings=warnings)
    for key, value in options.items():
        setattr(s, key, value)
    # CLI overrides (only when explicitly given)
    for key in OPTION_KEYS:
        cli_value = getattr(args, key, None)
        if cli_value is not None:
            setattr(s, key, cli_value)
    if getattr(args, "report", None):
        s.report_path = Path(args.report)
    elif cfg.get("report_file"):
        s.report_path = _resolve(cfg["report_file"], base, "report_file")
    validate_settings(s)
    return s


def validate_settings(s: Settings) -> None:
    if s.ocr_mode not in ("auto", "always", "never"):
        raise ConfigError("ocr_mode must be auto, always or never")
    if s.ocr_rotations not in ("auto", "all", "none"):
        raise ConfigError("ocr_rotations must be auto, all or none")
    if s.verify_ocr not in ("all", "ocr-pages", "none"):
        raise ConfigError("verify_ocr must be all, ocr-pages or none")
    if not 72 <= s.ocr_dpi <= 600:
        raise ConfigError("ocr_dpi must be between 72 and 600")
    if not 0.5 <= s.fuzzy_threshold <= 1.0:
        raise ConfigError("fuzzy_threshold must be between 0.5 and 1.0")
    if not 0.3 <= s.min_font_scale <= 1.0:
        raise ConfigError("min_font_scale must be between 0.3 and 1.0")
    if s.ocr_psm not in (3, 4, 6, 11, 12):
        raise ConfigError("ocr_psm must be one of 3, 4, 6, 11, 12")
    if s.font_file and not Path(s.font_file).is_file():
        raise ConfigError("font_file does not exist")
