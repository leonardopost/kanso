"""The data-type registry: what a hypothesis may ask for and what a loader may yield.

Three market-data types are built in — `bar`, `quote` and `trade`, the engine's `Bar`,
`QuoteTick` and `TradeTick` — and everything else is a **custom type**, registered
under an id by `register_custom_type`. kanso ships one, `corporate_action`; a vendor
adapter or a workspace extension registers its own the same way, and from that moment
the id is admissible in a hypothesis's `data_requirements`, loadable by a loader,
persistable in the catalog and selectable in a backtest by dotted path.

Registration is a process-wide fact, not a workspace one: the engine keys its
serialisable-type registry by the bare class name, so a type is registered once per
process and a second class claiming a taken id is refused rather than silently
shadowing the first. Re-registering the *same* class under the *same* id is a no-op, so
importing a module twice is harmless.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
The catalog can only persist a type the Arrow serializer knows, so registration for
Arrow is not an optimisation but the condition of being storable at all.
`@customdataclass` performs that registration itself; a `Data` subclass built by other
means is registered here through the engine's own `register_arrow`, with the encoder
and decoder built by the engine's `make_dict_serializer` / `make_dict_deserializer`
from the class's `to_dict` / `from_dict`. Field annotations on such a class are
restricted to `InstrumentId`, `str`, `bool`, `float`, `int`, `bytes`, `ndarray` and
`dict`; a schema is a `pyarrow.Schema`, which the engine hands out as `get_schema(cls)`
or as the `_schema` attribute the decorator sets.
"""

from __future__ import annotations

import re
from typing import Final

from nautilus_trader.core.data import Data
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick

from kanso.data.types.corporate_action import KINDS, TYPE_ID, CorporateAction
from kanso.errors import ValidationError

__all__ = [
    "BUILTIN_TYPES",
    "KINDS",
    "CorporateAction",
    "custom_types",
    "data_types",
    "register_custom_type",
    "resolve_type",
    "type_id_of",
]

TYPE_ID_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
"""A type id is spelled like every other catalogue id, so it reads the same in
`data_requirements` as a construct or gate id does in its own list."""

BUILTIN_TYPES: Final[dict[str, type]] = {
    "bar": Bar,
    "quote": QuoteTick,
    "trade": TradeTick,
}
"""The three market-data types every workspace has without registering anything."""

_CUSTOM: dict[str, type] = {}


def register_custom_type(type_id: str, py_class: type, arrow_schema: object = None) -> None:
    """Register `py_class` under `type_id` as a loadable, storable data type.

    `arrow_schema` may be omitted when the class already carries the Arrow registration
    `@customdataclass` performs; otherwise it is the `pyarrow.Schema` the catalog writes
    the type with, and the class must provide `to_dict` and `from_dict`.

    Refuses (validation) an id that is not a catalogue id, an id already meaning a
    built-in type or another class, a class that is not a `Data` subclass, and a class
    with no Arrow schema and none supplied.
    """
    if TYPE_ID_PATTERN.match(type_id) is None:
        raise ValidationError(
            f"type_id: {type_id!r} is not a type id; expected a lower-case word of 2 to 64 "
            "characters starting with a letter"
        )
    if type_id in BUILTIN_TYPES:
        raise ValidationError(
            f"type_id: {type_id!r} is a built-in type ({BUILTIN_TYPES[type_id].__name__}) and "
            "cannot be redefined"
        )
    if not (isinstance(py_class, type) and issubclass(py_class, Data)):
        raise ValidationError(
            f"py_class: {_name(py_class)} is not a subclass of the engine's Data type, so the "
            "catalog and the engine cannot carry it"
        )
    held = _CUSTOM.get(type_id)
    if held is not None:
        if held is py_class:
            return
        raise ValidationError(
            f"type_id: {type_id!r} is already registered to {held.__name__}; a type id names one "
            "class for the life of the process"
        )
    taken = next((i for i, c in _CUSTOM.items() if c is py_class), None)
    if taken is not None:
        raise ValidationError(
            f"py_class: {py_class.__name__} is already registered as {taken!r}; one class is one "
            "type id"
        )
    _register_arrow(py_class, arrow_schema)
    _CUSTOM[type_id] = py_class


def custom_types() -> dict[str, type]:
    """The registered custom types, by id."""
    return dict(_CUSTOM)


def data_types() -> dict[str, type]:
    """Every type a `data_requirements` entry may name, by id."""
    return {**BUILTIN_TYPES, **_CUSTOM}


def resolve_type(type_id: str) -> type:
    """The class a type id names.

    Refuses (validation) an unknown id, naming what is known, because an unknown id in
    `data_requirements` or a loader spec is almost always an extension that failed to
    import rather than a typo.
    """
    known = data_types()
    found = known.get(type_id)
    if found is None:
        raise ValidationError(
            f"type: {type_id!r} is not a known data type; known types are "
            f"{', '.join(sorted(known))}",
            remedy=(
                "register the type with kanso.data.types.register_custom_type, or check that the "
                "extension providing it imported cleanly (`kanso ext show`)"
            ),
        )
    return found


def type_id_of(point: object) -> str:
    """The type id of a data point, for grouping a loader's output by dataset."""
    for type_id, cls in data_types().items():
        if type(point) is cls:
            return type_id
    raise ValidationError(
        f"{_name(point)} is not a registered data type; register it with "
        "kanso.data.types.register_custom_type before loading it"
    )


def _register_arrow(py_class: type, arrow_schema: object) -> None:
    """Give the catalog a way to write the class, or explain why it has none."""
    from nautilus_trader.serialization.arrow.serializer import (
        list_schemas,
        make_dict_deserializer,
        make_dict_serializer,
        register_arrow,
    )

    if arrow_schema is None:
        if py_class not in list_schemas():
            raise ValidationError(
                f"arrow_schema: {py_class.__name__} carries no Arrow schema, so the catalog "
                "cannot persist it and no schema was supplied",
                remedy=(
                    "decorate the class with nautilus_trader.model.custom.customdataclass, which "
                    "registers a schema, or pass one to register_custom_type"
                ),
            )
        return
    try:
        register_arrow(
            py_class,
            arrow_schema,
            make_dict_serializer(arrow_schema),
            make_dict_deserializer(py_class),  # type: ignore[no-untyped-call]
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"arrow_schema: the engine refused the schema for {py_class.__name__}: {exc}"
        ) from None


def _name(value: object) -> str:
    return value.__name__ if isinstance(value, type) else type(value).__name__


register_custom_type(TYPE_ID, CorporateAction)
