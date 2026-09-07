"""The answer validator: what it accepts, what it complains about, and how it says so."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from kanso.models import validate
from kanso.models.jsonschema import VOCABULARY
from kanso.models.router import CHECK_SCHEMA, CHECK_TASK
from kanso.models.tasks import ANSWER_SCHEMAS

OBJECT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 5},
        "count": {"type": "integer", "minimum": 1, "maximum": 9},
        "flag": {"type": "boolean"},
        "tags": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "stage": {"type": "string", "enum": ["cert", "paper"]},
        "free": {"type": "object"},
    },
    "required": ["name", "count"],
    "additionalProperties": False,
}


def test_a_conforming_object_earns_no_complaint() -> None:
    value = {
        "name": "abc",
        "count": 4,
        "flag": True,
        "tags": ["x"],
        "stage": "cert",
        "free": {"anything": 1},
    }
    assert validate(value, OBJECT) == []


def test_a_missing_required_field_is_named() -> None:
    assert validate({"name": "abc"}, OBJECT) == ["the answer: 'count' is required and missing"]


def test_an_unknown_field_is_refused_when_the_object_is_closed() -> None:
    complaints = validate({"name": "abc", "count": 1, "extra": 1}, OBJECT)
    assert complaints == ["the answer: 'extra' is not a field of this object"]


def test_the_wrong_type_names_both_types_and_stops_descending() -> None:
    assert validate("nope", OBJECT) == ["the answer: expected object, got string"]


def test_every_problem_is_collected_rather_than_the_first() -> None:
    complaints = validate({"name": "a", "count": 99}, OBJECT)
    assert len(complaints) == 2
    assert any("at least 2" in c for c in complaints)
    assert any("above the maximum 9" in c for c in complaints)


def test_bounds_are_checked_on_strings_numbers_and_arrays() -> None:
    value = {"name": "abcdef", "count": 0, "tags": []}
    complaints = validate(value, OBJECT)
    assert any("at most 5 are allowed" in c for c in complaints)
    assert any("below the minimum 1" in c for c in complaints)
    assert any("at least 1 are needed" in c for c in complaints)


def test_an_enum_names_what_was_allowed() -> None:
    complaints = validate({"name": "abc", "count": 1, "stage": "live"}, OBJECT)
    assert complaints == ["the answer.stage: 'live' is not one of 'cert', 'paper'"]


def test_a_boolean_is_not_a_number() -> None:
    assert validate({"name": "abc", "count": True}, OBJECT) == [
        "the answer.count: expected integer, got boolean"
    ]


def test_array_items_are_reported_by_index() -> None:
    complaints = validate({"name": "abc", "count": 1, "tags": ["ok", 3]}, OBJECT)
    assert complaints == ["the answer.tags[1]: expected string, got number"]


def test_every_json_type_is_named_the_way_json_names_it() -> None:
    text: dict[str, object] = {"type": "string"}
    assert validate(None, {"type": "object"}) == ["the answer: expected object, got null"]
    assert validate(1.5, {"type": "integer"}) == ["the answer: expected integer, got number"]
    assert validate([], text) == ["the answer: expected string, got array"]
    assert validate({}, text) == ["the answer: expected string, got object"]
    assert validate(True, text) == ["the answer: expected string, got boolean"]
    assert validate("s", {"type": "object"}) == ["the answer: expected object, got string"]


def test_alternatives_are_satisfied_by_one_branch_and_name_them_all_when_none_fits() -> None:
    """`anyOf` is how a parameter value says scalar without saying which scalar."""
    scalar: dict[str, Any] = {
        "anyOf": [{"type": "number"}, {"type": "string"}, {"type": "boolean"}]
    }
    assert validate(1.5, scalar) == []
    assert validate("two", scalar) == []
    assert validate(True, scalar) == []
    assert validate([1], scalar) == ["the answer: expected number or string or boolean, got array"]
    assert validate({"a": 1}, scalar) == [
        "the answer: expected number or string or boolean, got object"
    ]


def test_an_unknown_type_keyword_constrains_nothing() -> None:
    assert validate("anything", {"type": "null"}) == []


def test_an_array_of_unconstrained_items_is_accepted() -> None:
    assert validate([1, "two", None], {"type": "array"}) == []


def test_a_schema_with_no_type_accepts_any_shape() -> None:
    assert validate(7, {}) == []
    assert validate({"a": 1}, {"required": ["a"]}) == []


def test_a_property_schema_that_is_not_a_mapping_is_ignored() -> None:
    assert validate({"a": 1}, {"type": "object", "properties": {"a": "not a schema"}}) == []


def test_a_required_list_that_is_a_string_is_ignored() -> None:
    assert validate({}, {"type": "object", "required": "name"}) == []


# --- the shipped schemas are the ones a provider will accept ------------------


def nodes(schema: Any, where: str = "the schema") -> Iterator[tuple[str, dict[str, Any]]]:
    """Every schema document inside `schema`, itself first, with the path that reached it."""
    if not isinstance(schema, dict):
        return
    yield where, schema
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, sub in properties.items():
            yield from nodes(sub, f"{where}.{name}")
    yield from nodes(schema.get("items"), f"{where}[]")
    for index, branch in enumerate(schema.get("anyOf") or []):
        yield from nodes(branch, f"{where}|{index}")


ON_THE_WIRE: list[tuple[str, dict[str, Any]]] = [
    *ANSWER_SCHEMAS.items(),
    (CHECK_TASK, CHECK_SCHEMA),
]
"""Every schema this package sends to a provider.

The four task classes and the register check. `check` is not a task class, but it is sent
the same way — `route`'s sibling builds a `Call` around `CHECK_SCHEMA` and both clients put
`call.schema` into their request unchanged — so a guard that skipped it would leave one
document reaching a provider unexamined.
"""

SHIPPED: list[tuple[str, dict[str, Any]]] = [
    node for task, schema in ON_THE_WIRE for node in nodes(schema, task)
]
"""Every document inside every schema this package puts on a wire."""


def test_every_shipped_schema_node_is_one_a_provider_accepts() -> None:
    """The three shapes measured to earn a 400, refused here instead of on the wire.

    `classify` and `certify_plan` shipped in 0.1.1 with a free-form `{"type": "object"}`
    for their parameter maps and were answered 400 by the provider they were sent to,
    every time, for the life of the release: the model layer had only ever been driven
    against the `mock` protocol, and offline nothing could see it. This is what would
    have seen it, and it costs no network to run.
    """
    for where, node in SHIPPED:
        assert node, f"{where}: an empty schema accepts any JSON value and is refused"
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, (
                f"{where}: an object must set additionalProperties to false"
            )
        assert node.get("additionalProperties") is not True, (
            f"{where}: additionalProperties: true is not supported"
        )


def test_every_shipped_schema_keyword_is_one_this_checker_reads() -> None:
    """A keyword outside the vocabulary is a constraint kanso asks for and never checks."""
    for where, node in SHIPPED:
        unknown = sorted(set(node) - VOCABULARY)
        assert unknown == [], f"{where}: {', '.join(unknown)} is outside the vocabulary"


def test_an_arbitrary_object_never_validates_against_a_task_schema() -> None:
    """An empty answer is what an unlisted mock class and an unparseable reply both give."""
    for schema in ANSWER_SCHEMAS.values():
        assert validate({}, schema)


@given(st.text(min_size=0, max_size=300))
def test_a_string_passes_exactly_when_it_is_inside_its_length_bounds(text: str) -> None:
    schema: dict[str, Any] = {"type": "string", "minLength": 3, "maxLength": 200}
    assert (validate(text, schema) == []) == (3 <= len(text) <= 200)


@given(st.integers(min_value=-1000, max_value=1000))
def test_a_number_passes_exactly_when_it_is_inside_its_bounds(value: int) -> None:
    schema: dict[str, Any] = {"type": "integer", "minimum": -10, "maximum": 10}
    assert (validate(value, schema) == []) == (-10 <= value <= 10)
