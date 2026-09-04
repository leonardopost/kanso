"""`instruments.yaml`: the resolved-instrument cache."""

from __future__ import annotations

from typing import Any

import pytest

from kanso.errors import ValidationError
from kanso.schemas import InstrumentsFile, parse_yaml

ENTRY: dict[str, Any] = {
    "nautilus_id": "AAPL.XNAS",
    "asset_class": "EQUITY",
    "corporate_actions": "adjust_all",
}


def build(**entries: dict[str, Any]) -> InstrumentsFile:
    return InstrumentsFile.model_validate(entries or {"AAPL": ENTRY})


def test_an_entry_splits_into_symbol_and_venue() -> None:
    entry = build()["AAPL"]
    assert entry.symbol == "AAPL"
    assert entry.venue == "XNAS"
    assert build().venues() == {"AAPL": "XNAS"}
    assert "AAPL" in build()
    assert len(build()) == 1


def test_a_dotted_symbol_keeps_its_venue() -> None:
    entry = build(BRKB={**ENTRY, "nautilus_id": "BRK.B.XNAS"})["BRKB"]
    assert entry.symbol == "BRK.B"
    assert entry.venue == "XNAS"


@pytest.mark.parametrize("bad", ["AAPL", "AAPL.", ".XNAS", "AAPL.xnas", "AAPL XNAS"])
def test_a_nautilus_id_names_a_venue(bad: str) -> None:
    with pytest.raises(ValidationError, match="nautilus_id"):
        build(AAPL={**ENTRY, "nautilus_id": bad})


def test_the_asset_class_is_the_engines() -> None:
    with pytest.raises(ValidationError) as caught:
        build(AAPL={**ENTRY, "asset_class": "STOCK"})
    assert "asset_class" in caught.value.message
    assert "EQUITY" in caught.value.message


def test_corporate_actions_is_declared() -> None:
    with pytest.raises(ValidationError, match="corporate_actions"):
        build(AAPL={"nautilus_id": "AAPL.XNAS", "asset_class": "EQUITY"})
    with pytest.raises(ValidationError, match="corporate_actions"):
        build(AAPL={**ENTRY, "corporate_actions": "split_only"})


def test_a_manual_entry_carries_its_own_fields() -> None:
    with pytest.raises(ValidationError, match="override"):
        build(DEMO={**ENTRY, "nautilus_id": "DEMO.SIM", "manual": True})
    entry = build(
        DEMO={
            **ENTRY,
            "nautilus_id": "DEMO.SIM",
            "manual": True,
            "override": {"price_precision": 2},
        }
    )["DEMO"]
    assert entry.manual
    assert entry.resolved is None


def test_a_manual_entry_is_never_resolved() -> None:
    with pytest.raises(ValidationError, match="resolved"):
        build(
            DEMO={
                **ENTRY,
                "manual": True,
                "override": {"price_precision": 2},
                "resolved": {
                    "adapter": "some_adapter",
                    "as_of": "2024-01-02",
                    "at": "2024-01-02T00:00:00Z",
                    "checksum": "d" * 64,
                },
            }
        )


def test_two_ids_may_not_share_an_instrument() -> None:
    with pytest.raises(ValidationError, match="already used by"):
        build(AAPL=ENTRY, APPLE=ENTRY)


def test_attributes_and_sources_are_free_form() -> None:
    entry = build(
        AAPL={
            **ENTRY,
            "attributes": {"sector": "tech", "adv": 1000000},
            "sources": {"some_vendor": "AAPL.US"},
        }
    )["AAPL"]
    assert entry.attributes["sector"] == "tech"
    assert entry.sources["some_vendor"] == "AAPL.US"


def test_unknown_keys_inside_an_entry_are_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(AAPL={**ENTRY, "exchange": "NASDAQ"})


def test_the_file_carries_no_schema_key() -> None:
    """It is keyed by instrument id all the way down, so `schema` would be an instrument."""
    with pytest.raises(ValidationError, match="schema"):
        parse_yaml(InstrumentsFile, "schema: 1\n", "instruments.yaml")
    assert parse_yaml(
        InstrumentsFile,
        "AAPL: {nautilus_id: AAPL.XNAS, asset_class: EQUITY, corporate_actions: none}\n",
        "instruments.yaml",
    ).venues() == {"AAPL": "XNAS"}


def test_a_file_that_is_not_a_map_is_refused() -> None:
    with pytest.raises(ValidationError, match="valid dictionary"):
        InstrumentsFile.model_validate([{"AAPL": ENTRY}])
