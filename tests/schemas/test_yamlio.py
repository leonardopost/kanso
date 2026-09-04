"""Reading and writing workspace files."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.errors import Exit, ValidationError
from kanso.schemas import Envelope, InstrumentsFile, dump_yaml, load_yaml, parse_yaml, write_yaml
from tests.schemas.strategies import envelopes

ENVELOPE = envelopes().example()


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
