"""The half the two network protocols share: reading an object out of a reply."""

from __future__ import annotations

import pytest

from kanso.models.wire import CONNECT_TIMEOUT_S, REQUEST_TIMEOUT_S, as_object


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


MEASURED_SHIM_KILL_TIMES_S = (390.0, 240.0)
"""The kill times the two shims running in the operator's workspace were measured at."""


def test_the_request_timeout_leaves_a_shim_room_to_give_up_first() -> None:
    """A shim's own timeout must be lower, so its refusal is the failure that is reported.

    A model reached through a shim around a vendor's CLI takes minutes on a `propose`.
    Whichever side gives up first decides what the operator reads: the shim's status
    reaches them as `the provider answered 504`, and this client's own timeout as
    `the request did not complete (ReadTimeout)`, which names nothing it was doing.
    """
    assert REQUEST_TIMEOUT_S == 420.0
    assert all(kill < REQUEST_TIMEOUT_S for kill in MEASURED_SHIM_KILL_TIMES_S)
    assert CONNECT_TIMEOUT_S < REQUEST_TIMEOUT_S
