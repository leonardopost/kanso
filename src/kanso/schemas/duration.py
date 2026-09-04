"""The duration grammar and its two helpers.

A duration is a whole number of one unit: seconds, minutes, hours, days or weeks,
written `<n>(s|m|h|d|w)`. It is the only way a span of time is written in a workspace
file, so a holding period, a bar size and a gate parameter all read the same.

`render_duration` is the inverse up to unit choice: it emits the largest unit that
divides the value exactly, so `parse_duration(render_duration(td)) == td` always and
`render_duration("60s")` is `"1m"`.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Final

from pydantic import StringConstraints

from kanso.errors import ValidationError

DURATION_PATTERN: Final = r"^[0-9]+(s|m|h|d|w)$"
RESOLUTION_PATTERN: Final = r"^([0-9]+(s|m|h|d|w)|tick|quote|trade)$"

Duration = Annotated[str, StringConstraints(pattern=DURATION_PATTERN)]
"""`<n>(s|m|h|d|w)`."""

Resolution = Annotated[str, StringConstraints(pattern=RESOLUTION_PATTERN)]
"""A bar duration, or one of the three unaggregated grains."""

TICK_RESOLUTIONS: Final = ("tick", "quote", "trade")

_RE: Final = re.compile(DURATION_PATTERN)
_SECONDS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_UNITS: Final = (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1))


def parse_duration(text: str, field: str = "duration") -> timedelta:
    """`"30m"` to `timedelta(minutes=30)`; anything else is a validation failure."""
    match = _RE.match(text)
    if match is None:
        raise ValidationError(
            f"{field}: {text!r} is not a duration; expected <n> followed by s, m, h, d or w"
        )
    return timedelta(seconds=int(text[:-1]) * _SECONDS[match.group(1)])


def render_duration(value: timedelta, field: str = "duration") -> str:
    """The shortest exact spelling of a non-negative whole-second duration."""
    seconds = value.total_seconds()
    if seconds < 0 or seconds != int(seconds):
        raise ValidationError(
            f"{field}: {value!r} is not a whole number of seconds at or above zero"
        )
    total = int(seconds)
    if total == 0:
        return "0s"
    for unit, size in _UNITS[:-1]:
        if total % size == 0:
            return f"{total // size}{unit}"
    return f"{total}s"


def is_duration(resolution: str) -> bool:
    """True when a resolution names a bar size rather than an unaggregated grain."""
    return _RE.match(resolution) is not None
