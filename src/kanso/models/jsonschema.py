"""A validator for the answer schemas this package sends, and for nothing else.

The four task classes answer with small, closed objects, and the same document is both
what goes on the wire as a schema and what the reply is checked against here. Keeping the
checker in-package rather than taking a dependency on a general JSON Schema library is a
deliberate trade: the vocabulary below is the whole of what the schemas use, a provider
that ignores the constraint is caught by the same code that would have caught a provider
that has no constraint at all, and the complaints are written for a model to act on
rather than for a human to debug a schema with.

The vocabulary is `type`, `properties`, `required`, `additionalProperties: false`,
`items`, `enum`, `minimum`, `maximum`, `minLength`, `maxLength` and `minItems`. Anything
else in a schema is ignored rather than refused, because the schemas are this package's
own and an unknown keyword there is a mistake to fix at the source.

Validation never stops at the first problem: a model correcting one field at a time would
burn the single retry the ladder allows, so every complaint the answer earns is collected
and sent back together.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["validate"]

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
}


def validate(value: object, schema: Mapping[str, object], where: str = "the answer") -> list[str]:
    """Every way `value` fails `schema`, as sentences a model can act on."""
    complaints: list[str] = []
    _check(value, schema, where, complaints)
    return complaints


def _check(value: object, schema: Mapping[str, object], where: str, out: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _is_type(value, expected):
        out.append(f"{where}: expected {expected}, got {_name(value)}")
        return
    _enum(value, schema, where, out)
    _bounds(value, schema, where, out)
    if isinstance(value, dict):
        _object(value, schema, where, out)
    elif isinstance(value, list):
        _array(value, schema, where, out)


def _is_type(value: object, expected: str) -> bool:
    """`True` when `value` is of the named JSON type.

    Booleans are excluded from the numeric types: JSON tells `true` from `1` even though
    Python does not, and a model answering `true` where a number belongs has made exactly
    the mistake this reports.
    """
    allowed = _TYPES.get(expected)
    if allowed is None:
        return True
    if expected in {"number", "integer"} and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


def _name(value: object) -> str:
    """The JSON type name of a value that came out of a JSON decoder.

    Booleans are named before the table is consulted, so `true` is reported as a boolean
    rather than as the number Python considers it.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    for name, types in _TYPES.items():
        if name not in {"number", "integer"} and isinstance(value, types):
            return name
    return "number"


def _enum(value: object, schema: Mapping[str, object], where: str, out: list[str]) -> None:
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        listed = ", ".join(repr(item) for item in allowed)
        out.append(f"{where}: {value!r} is not one of {listed}")


def _bounds(value: object, schema: Mapping[str, object], where: str, out: list[str]) -> None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        low = schema.get("minimum")
        high = schema.get("maximum")
        if isinstance(low, int | float) and value < low:
            out.append(f"{where}: {value} is below the minimum {low}")
        if isinstance(high, int | float) and value > high:
            out.append(f"{where}: {value} is above the maximum {high}")
    if isinstance(value, str):
        longest = schema.get("maxLength")
        shortest = schema.get("minLength")
        if isinstance(longest, int) and len(value) > longest:
            out.append(f"{where}: {len(value)} characters, and at most {longest} are allowed")
        if isinstance(shortest, int) and len(value) < shortest:
            out.append(f"{where}: {len(value)} characters, and at least {shortest} are needed")
    if isinstance(value, list):
        fewest = schema.get("minItems")
        if isinstance(fewest, int) and len(value) < fewest:
            out.append(f"{where}: {len(value)} entries, and at least {fewest} are needed")


def _object(value: dict[str, object], schema: Mapping[str, object], w: str, out: list[str]) -> None:
    properties = schema.get("properties")
    known = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    if isinstance(required, Sequence) and not isinstance(required, str):
        for name in required:
            if name not in value:
                out.append(f"{w}: '{name}' is required and missing")
    if schema.get("additionalProperties") is False:
        for name in value:
            if name not in known:
                out.append(f"{w}: '{name}' is not a field of this object")
    for name, sub in known.items():
        if name in value and isinstance(sub, Mapping):
            _check(value[name], sub, f"{w}.{name}", out)


def _array(value: list[object], schema: Mapping[str, object], where: str, out: list[str]) -> None:
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return
    for index, item in enumerate(value):
        _check(item, items, f"{where}[{index}]", out)
