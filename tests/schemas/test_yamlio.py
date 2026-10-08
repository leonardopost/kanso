"""Reading and writing workspace files."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import Exit, ValidationError
from kanso.schemas import Envelope, InstrumentsFile, dump_yaml, load_yaml, parse_yaml, write_yaml
from kanso.schemas.base import KansoModel, NonEmpty
from tests.schemas.strategies import envelopes

ENVELOPE = envelopes().example()

YAML_TRUE = ("on", "yes", "true")
YAML_FALSE = ("off", "no", "false")
"""The words PyYAML's safe loader reads as booleans when bare, in any of the three cases
`CASES` spells them in (6.0.3, measured)."""

CASES: tuple[Callable[[str], str], ...] = (str.lower, str.title, str.upper)


class Universe(KansoModel):
    """Any model with a string field: the refusal is the schema layer's, not one spec's."""

    instruments: list[NonEmpty]


def tickers() -> st.SearchStrategy[str]:
    """Bare tickers YAML reads back as themselves; `NULL`, say, it reads as nothing."""
    return st.from_regex(r"[A-Z]{1,5}", fullmatch=True).filter(lambda t: yaml.safe_load(t) == t)


def test_schema_key_comes_first() -> None:
    assert dump_yaml(ENVELOPE).startswith("schema: 1\n")


def test_missing_schema_is_refused() -> None:
    text = dump_yaml(ENVELOPE).split("\n", 1)[1]
    with pytest.raises(ValidationError) as caught:
        parse_yaml(Envelope, text, "envelope.yaml")
    assert "schema" in caught.value.message
    assert "envelope.yaml" in caught.value.message
    assert caught.value.code is Exit.VALIDATION


def test_unknown_schema_version_is_refused() -> None:
    text = dump_yaml(ENVELOPE).replace("schema: 1", "schema: 2", 1)
    with pytest.raises(ValidationError, match="schema"):
        parse_yaml(Envelope, text, "envelope.yaml")


def test_non_mapping_is_refused() -> None:
    with pytest.raises(ValidationError, match="mapping"):
        parse_yaml(Envelope, "- one\n- two", "envelope.yaml")


def test_malformed_yaml_is_refused() -> None:
    with pytest.raises(ValidationError, match="not valid YAML"):
        parse_yaml(Envelope, "a: [1,\n", "envelope.yaml")


def test_empty_document_is_a_missing_schema() -> None:
    with pytest.raises(ValidationError, match="schema"):
        parse_yaml(Envelope, "", "envelope.yaml")


def test_an_id_keyed_file_needs_no_schema() -> None:
    assert parse_yaml(InstrumentsFile, "", "instruments.yaml").root == {}
    assert dump_yaml(InstrumentsFile({})) == "{}\n"


def test_field_errors_name_the_field_and_the_file() -> None:
    text = dump_yaml(ENVELOPE).replace("lanes:", "lane:")
    with pytest.raises(ValidationError) as caught:
        parse_yaml(Envelope, text, "envelope.yaml")
    assert "envelope.yaml" in caught.value.message
    assert "plan" in caught.value.message


def test_load_and_write(tmp_path: Path) -> None:
    path = tmp_path / "envelope.yaml"
    assert write_yaml(ENVELOPE, path) == path
    assert load_yaml(Envelope, path) == ENVELOPE
    assert not list(tmp_path.glob(".*.tmp"))


def test_load_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot be read"):
        load_yaml(Envelope, tmp_path / "absent.yaml")


def test_a_hand_written_bare_date_is_accepted() -> None:
    from kanso.schemas import DateWindow

    assert parse_yaml(DateWindow, "start: 2024-01-02\nend: 2024-12-31\n").end.year == 2024


@given(
    word=st.sampled_from(YAML_TRUE + YAML_FALSE),
    case=st.sampled_from(CASES),
    around=st.lists(tickers(), max_size=6),
    data=st.data(),
)
def test_a_bare_yaml_boolean_ticker_is_refused_by_place_and_quoting_it_is_the_fix(
    word: str,
    case: Callable[[str], str],
    around: list[str],
    data: st.DataObject,
) -> None:
    bare = case(word)
    place = data.draw(st.integers(min_value=0, max_value=len(around)))
    listed = [*around[:place], bare, *around[place:]]
    value = "true" if word in YAML_TRUE else "false"

    with pytest.raises(ValidationError) as caught:
        parse_yaml(Universe, f"instruments: [{', '.join(listed)}]\n", "spec.yaml")

    assert caught.value.code is Exit.VALIDATION
    assert caught.value.message == (
        f"spec.yaml: instruments.{place}: {value} is a YAML boolean, not a string; YAML reads "
        "a bare ON, OFF, YES, NO, TRUE or FALSE as one"
    )
    assert caught.value.remedy is not None
    assert 'e.g. "ON" rather than ON' in caught.value.remedy
    quoted = [*around[:place], f'"{bare}"', *around[place:]]
    assert parse_yaml(Universe, f"instruments: [{', '.join(quoted)}]\n").instruments == listed


@pytest.mark.parametrize("word", ["Y", "y", "N", "n", "oN"])
def test_a_word_pyyaml_does_not_resolve_is_a_ticker(word: str) -> None:
    """YAML 1.1 lists `y` and `n` as booleans; PyYAML's resolver does not, nor a mixed case."""
    assert parse_yaml(Universe, f"instruments: [{word}]\n").instruments == [word]


def test_a_number_where_a_string_belongs_is_not_called_a_boolean() -> None:
    with pytest.raises(ValidationError) as caught:
        parse_yaml(Universe, "instruments: [1]\n", "spec.yaml")
    assert caught.value.message == "spec.yaml: instruments.0: Input should be a valid string"
    assert caught.value.remedy is None
