import pytest

from redactor.config import ConfigError, build_mappings
from redactor.matching import match_text, substitute


def mk(pairs, cs=False, ww=False):
    w = []
    return build_mappings(pairs, cs, ww, w), w


def test_multiple_mappings_get_ids_in_order():
    ms, _ = mk([{"source": "Alice", "replacement": "A"}, {"source": "12345", "replacement": "B"}])
    assert [m.id for m in ms] == ["M1", "M2"]


def test_exact_duplicate_is_merged_with_warning():
    ms, w = mk([{"source": "Alice", "replacement": "A"}, {"source": "alice", "replacement": "A"}])
    assert len(ms) == 1 and any("duplicate" in x for x in w)


def test_conflicting_duplicate_is_rejected():
    with pytest.raises(ConfigError) as e:
        mk([{"source": "Alice", "replacement": "A"}, {"source": "ALICE", "replacement": "B"}])
    assert "Alice" not in str(e.value) and "ALICE" not in str(e.value)  # no PII in messages


def test_case_sensitive_variants_are_distinct():
    ms, _ = mk([{"source": "Alice", "replacement": "A"}, {"source": "ALICE", "replacement": "B"}], cs=True)
    assert len(ms) == 2


@pytest.mark.parametrize("bad", ["", " ", "x", None])
def test_invalid_source_rejected(bad):
    with pytest.raises(ConfigError):
        mk([{"source": bad, "replacement": "A"}])


def test_longer_source_wins_overlap_regardless_of_order():
    ms, _ = mk([{"source": "jilaba", "replacement": "S"}, {"source": "jilaba@email.com", "replacement": "E"}])
    acc, sup = match_text("mail jilaba@email.com now", ms)
    assert [a.mapping.id for a in acc] == ["M2"] and [s.mapping.id for s in sup] == ["M1"]


def test_no_cascading_replacement():
    ms, w = mk([{"source": "Alpha", "replacement": "Beta"}, {"source": "Beta", "replacement": "Gamma"}])
    assert substitute("Alpha and Beta", ms)[0] == "Beta and Gamma"
    assert any("no cascading" in x for x in w)


def test_case_insensitive_default_and_case_sensitive_option():
    ms, _ = mk([{"source": "Jilaba", "replacement": "S"}])
    assert substitute("JILABA jilaba", ms)[0] == "S S"
    ms, _ = mk([{"source": "Jilaba", "replacement": "S"}], cs=True)
    assert substitute("JILABA Jilaba", ms)[0] == "JILABA S"


def test_whitespace_in_source_is_flexible():
    ms, _ = mk([{"source": "Jilaba  Rao", "replacement": "X"}])
    assert substitute("Jilaba\nRao and JilabaRao", ms)[0] == "X and X"


def test_whole_word_option():
    ms, _ = mk([{"source": "Sam", "replacement": "X"}], ww=True)
    assert substitute("Sam Samsung", ms)[0] == "X Samsung"
