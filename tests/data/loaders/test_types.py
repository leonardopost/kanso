"""The data-type registry, and the built-in `CorporateAction`.

Custom types are defined here with `type(...)` and real annotation objects rather than
with a `class` statement, because the engine's decorator reads `cls.__annotations__`
verbatim and this module — like every other in the suite — postpones its annotations.
The engine's registry is also keyed by bare class name for the whole process, so each
type below is defined once and given a name nothing else claims.
"""

from __future__ import annotations

from itertools import count
from typing import Any

import pytest
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.types import (
    BUILTIN_TYPES,
    CorporateAction,
    custom_types,
    data_types,
    register_custom_type,
    resolve_type,
    type_id_of,
)
from kanso.errors import ValidationError

_names = count()


def build_type(**annotations: Any) -> type:
    """A one-shot custom data class with a name nothing else in the process claims."""
    name = f"KansoTestType{next(_names)}"
    return customdataclass(type(name, (Data,), {"__annotations__": dict(annotations)}))


SPLIT = build_type(instrument_id=InstrumentId, note=str, factor=float)
UNREGISTERED = type("KansoTestUnregistered", (Data,), {})


def test_the_three_market_types_are_built_in() -> None:
    assert dict(BUILTIN_TYPES) == dict(bar=Bar, quote=QuoteTick, trade=TradeTick)


def test_the_shipped_custom_type_is_registered() -> None:
    assert custom_types()["corporate_action"] is CorporateAction
    assert data_types()["corporate_action"] is CorporateAction
    assert resolve_type("corporate_action") is CorporateAction


def test_a_corporate_action_carries_the_two_numbers_an_adjustment_needs() -> None:
    action = CorporateAction(
        instrument_id=InstrumentId.from_str("DEMO.XNAS"),
        kind="split",
        ratio=4.0,
        cash=0.0,
        currency="USD",
        ex_date_ns=3,
        ts_event=2,
        ts_init=2,
    )
    assert action.ratio == 4.0
    assert action.to_dict()["kind"] == "split"


def test_a_registered_type_is_admissible_in_data_requirements() -> None:
    """Registration is what makes an id nameable in a hypothesis."""
    register_custom_type("kanso_test_split", SPLIT)
    assert "kanso_test_split" in data_types()
    assert resolve_type("kanso_test_split") is SPLIT


def test_registering_the_same_class_twice_is_a_no_op() -> None:
    register_custom_type("kanso_test_split", SPLIT)
    register_custom_type("kanso_test_split", SPLIT)
    assert data_types()["kanso_test_split"] is SPLIT


def test_an_explicit_schema_is_handed_to_the_engine() -> None:
    """The path an extension takes when its class is not a `customdataclass`."""
    other = build_type(note=str)
    register_custom_type("kanso_test_schema", other, other._schema)
    assert resolve_type("kanso_test_schema") is other


@pytest.mark.parametrize(
    ("type_id", "message"),
    [("Bar", "is not a type id"), ("x", "is not a type id"), ("1st", "is not a type id")],
)
def test_an_id_that_is_not_a_catalogue_id_is_refused(type_id: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        register_custom_type(type_id, SPLIT)


def test_a_built_in_id_cannot_be_redefined() -> None:
    with pytest.raises(ValidationError, match="is a built-in type"):
        register_custom_type("bar", SPLIT)


def test_a_class_the_engine_cannot_carry_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a subclass of the engine's Data type"):
        register_custom_type("kanso_test_plain", dict)


def test_a_taken_id_is_never_silently_shadowed() -> None:
    register_custom_type("kanso_test_taken", build_type(note=str))
    with pytest.raises(ValidationError, match="is already registered to"):
        register_custom_type("kanso_test_taken", build_type(note=str))


def test_one_class_is_one_id() -> None:
    with pytest.raises(ValidationError, match="one class is one type id"):
        register_custom_type("kanso_test_second_id", SPLIT)


def test_a_class_the_catalog_could_not_persist_is_refused() -> None:
    """No Arrow schema and none supplied means the catalog has no way to write it."""
    with pytest.raises(ValidationError, match="carries no Arrow schema"):
        register_custom_type("kanso_test_no_schema", UNREGISTERED)


def test_a_schema_the_engine_rejects_is_refused() -> None:
    with pytest.raises(ValidationError, match="the engine refused the schema"):
        register_custom_type("kanso_test_bad_schema", UNREGISTERED, "not a schema")


def test_an_unknown_type_names_the_ones_that_exist() -> None:
    with pytest.raises(ValidationError, match="known types are"):
        resolve_type("candles")


def test_a_point_reports_its_own_type_id() -> None:
    action = CorporateAction(
        instrument_id=InstrumentId.from_str("DEMO.XNAS"),
        kind="dividend",
        ratio=1.0,
        cash=0.25,
        currency="USD",
        ex_date_ns=2,
        ts_event=1,
        ts_init=1,
    )
    assert type_id_of(action) == "corporate_action"
    with pytest.raises(ValidationError, match="is not a registered data type"):
        type_id_of(object())
