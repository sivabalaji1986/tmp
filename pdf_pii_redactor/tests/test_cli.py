import json

import samples as S
from conftest import all_text
from redactor.cli import main


def test_cli_replace_arguments(tmp_path):
    src = S.make_native(tmp_path / "original.pdf")
    code = main(["--input", str(src), "--output", str(tmp_path / "sanitized.pdf"),
                 "--replace", "Jilaba", "SiBa", "--replace", "98765432", "12345678",
                 "--replace", "jilaba@email.com", "siba@example.com", "--verify-ocr", "ocr-pages"])
    assert code == 0
    assert "Jilaba" not in all_text(tmp_path / "sanitized.pdf")
    assert (tmp_path / "sanitized.report.json").exists() and (tmp_path / "sanitized.report.txt").exists()


def test_cli_config_file_paths_relative_to_config(tmp_path):
    S.make_native(tmp_path / "original.pdf")
    cfg = {"input_file": "original.pdf", "output_file": "sanitized.pdf", "case_sensitive": False,
           "replacements": [{"source": "Jilaba", "replacement": "SiBa"},
                            {"source": "98765432", "replacement": "12345678"}],
           "options": {"verify_ocr": "ocr-pages"}}
    (tmp_path / "mappings.json").write_text(json.dumps(cfg))
    code = main(["--config", str(tmp_path / "mappings.json")])
    assert code == 0
    data = json.loads((tmp_path / "sanitized.report.json").read_text())
    assert data["status"] == "SUCCESS" and data["input_unchanged"]


def test_cli_usage_errors(tmp_path, capsys):
    assert main(["--input", "x.pdf"]) == 3
    (tmp_path / "bad.json").write_text("{not json")
    assert main(["--config", str(tmp_path / "bad.json")]) == 3
    (tmp_path / "c.json").write_text(json.dumps({"input_file": "a.pdf", "output_file": "b.pdf",
                                                  "replacements": [{"source": "Jilaba", "replacement": "A"},
                                                                   {"source": "jilaba", "replacement": "B"}]}))
    assert main(["--config", str(tmp_path / "c.json")]) == 3
    assert "Jilaba" not in capsys.readouterr().err


def test_cli_missing_input_fails(tmp_path):
    code = main(["--input", str(tmp_path / "nope.pdf"), "--output", str(tmp_path / "o.pdf"),
                 "--replace", "Jilaba", "SiBa"])
    assert code == 1
