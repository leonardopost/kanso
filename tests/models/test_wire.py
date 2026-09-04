"""The half the two network protocols share: reading an object out of a reply."""

from __future__ import annotations

import pytest

from kanso.models.wire import as_object


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('  {"a": 1}  ', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('```json\n{"a": 1}', {"a": 1}),
    ],
)
def test_an_object_is_found_however_the_model_wrapped_it(
    text: str, expected: dict[str, int]
) -> None:
    assert as_object(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "I cannot answer that.", "[1, 2]", "null", "42", "```", "```json", "```\n[1]\n```"],
)
def test_anything_that_is_not_an_object_is_an_empty_answer(text: str) -> None:
    """Which fails every task class's schema, so the ladder handles it."""
    assert as_object(text) == {}
