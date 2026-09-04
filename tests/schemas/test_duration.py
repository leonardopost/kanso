"""The duration grammar."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import Exit, ValidationError
from kanso.schemas import is_duration, parse_duration, render_duration
from tests.schemas.strategies import durations


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1s", timedelta(seconds=1)),
        ("90s", timedelta(seconds=90)),
        ("30m", timedelta(minutes=30)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        ("2w", timedelta(weeks=2)),
        ("0d", timedelta(0)),
    ],
)
def test_parse(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "1", "m", "1M", "1.5h", "-1d", "1y", "1 d", "1dd", "PT1H"])
def test_parse_refuses(text: str) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_duration(text, "horizon")
    assert "horizon" in caught.value.message
    assert caught.value.code is Exit.VALIDATION


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (timedelta(0), "0s"),
        (timedelta(seconds=90), "90s"),
        (timedelta(seconds=60), "1m"),
        (timedelta(hours=1), "1h"),
        (timedelta(days=1), "1d"),
        (timedelta(days=14), "2w"),
        (timedelta(days=10), "10d"),
    ],
)
def test_render(value: timedelta, expected: str) -> None:
    assert render_duration(value) == expected


@pytest.mark.parametrize("value", [timedelta(seconds=-1), timedelta(microseconds=1)])
def test_render_refuses(value: timedelta) -> None:
    with pytest.raises(ValidationError, match="length"):
        render_duration(value, "length")


@given(durations(max_units=10_000))
def test_render_inverts_parse(text: str) -> None:
    parsed = parse_duration(text)
    assert parse_duration(render_duration(parsed)) == parsed
    assert render_duration(parse_duration(render_duration(parsed))) == render_duration(parsed)


@given(st.integers(min_value=0, max_value=10**9))
def test_parse_inverts_render(seconds: int) -> None:
    value = timedelta(seconds=seconds)
    assert parse_duration(render_duration(value)) == value


def test_is_duration() -> None:
    assert is_duration("15m")
    assert not is_duration("tick")
    assert not is_duration("quote")
