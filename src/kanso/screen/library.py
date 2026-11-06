"""The measure library: what a screen may measure, and the ranges each admits.

One YAML file per measure under `library/`, in the shape the criteria toolbox uses. A
measure lives in the package and never in an extension, for the reason a gate does: a
verdict means the package's arithmetic or it means nothing, and a result records the
version of the library that produced it.

`screen_version` is that record: the package version and a digest of the library files and
of this package's own modules, so a result measured under one library, or by one build of the
arithmetic, is never mistaken for one measured under another. The modules are in it because a
result is returned as stored under its key: with the package version alone, a development
build that measures differently, or a fix that ships before the version moves, would be handed
the old build's numbers for the same screen and snapshot. A change to any module of the package
moves it, so a result is measured again even when the change was not to its arithmetic; the
arithmetic a screen borrows from elsewhere — a hurdle's `fill_cost` — moves with the version.
"""

from __future__ import annotations

from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Final

from kanso import __version__
from kanso.errors import ValidationError
from kanso.schemas import parse_yaml
from kanso.schemas.screen import (
    CatchUp,
    Condition,
    FixedHold,
    LeadLag,
    MeasureItem,
    Response,
    Screen,
    TurnOfMonth,
    span_ns,
)

SOURCE: Final = Path(__file__).resolve().parent
LIBRARY: Final = SOURCE / "library"


@cache
def catalogue() -> dict[str, MeasureItem]:
    """Every shipped measure, by id."""
    items: dict[str, MeasureItem] = {}
    for path in sorted(LIBRARY.glob("*.yaml")):
        item = parse_yaml(MeasureItem, path.read_text(encoding="utf-8"), path.name)
        if item.id != path.stem:
            raise ValidationError(f"{path.name}: declares id {item.id!r}, so it is misfiled")
        items[item.id] = item
    return items


@cache
def screen_version() -> str:
    """The package version and a digest of the library and the modules that measure with it."""
    digest = sha256()
    for path in [*sorted(LIBRARY.glob("*.yaml")), *sorted(SOURCE.glob("*.py"))]:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return f"{__version__}+{digest.hexdigest()[:12]}"


def check(screen: Screen) -> None:
    """Refuse a measure whose parameters leave the ranges its library entry declares."""
    items = catalogue()
    for index, measure in enumerate(screen.measures):
        item = items[measure.id]
        for key, values in _values(measure).items():
            low, high = item.ranges[key]
            floor, ceiling = _end(low), _end(high)
            for value in values:
                if not floor <= value <= ceiling:
                    raise ValidationError(
                        f"measures.{index}.{key}: {_shown(value, low)} is outside the "
                        f"{measure.id} range [{low}, {high}]",
                        remedy="choose a value inside the range of "
                        f"kanso/screen/library/{item.id}.yaml",
                    )


def _values(measure: LeadLag | Response | CatchUp | Condition) -> dict[str, list[float]]:
    """Each ranged parameter of a measure, as numbers comparable with its range ends."""
    if isinstance(measure, Condition):
        return _condition_values(measure)
    if isinstance(measure, LeadLag):
        return {
            "lag": [abs(span_ns(lag)) for lag in measure.lags],
            "lags": [len(measure.lags)],
        }
    if isinstance(measure, CatchUp):
        return {
            "within": [span_ns(measure.within)],
            "beta_over": [span_ns(measure.beta_over)],
            "enter_bp": list(measure.enter_bp),
            "close": list(measure.close),
            "stop": list(measure.stop),
            "max_hold": [span_ns(measure.max_hold)],
            "latency_ms": [measure.latency_ms],
        }
    trigger = measure.trigger
    values: dict[str, list[float]] = {
        "horizon": [span_ns(horizon) for horizon in measure.horizons],
        "horizons": [len(measure.horizons)],
        "latency_ms": [measure.latency_ms],
    }
    if trigger.move_bp is not None and trigger.within is not None:
        values["move_bp"] = list(trigger.move_bp)
        values["within"] = [span_ns(trigger.within)]
    if trigger.z is not None and trigger.lookback is not None:
        values["z"] = list(trigger.z)
        values["lookback"] = [span_ns(trigger.lookback)]
    return values


def _condition_values(measure: Condition) -> dict[str, list[float]]:
    """A condition's ranged parameters: how many tests and controls, each fixed hold, a
    sign-flip null's block, each end of a turn of the month, its latency and its power."""
    values: dict[str, list[float]] = {
        "tests": [len(measure.tests)],
        "controls": [len(measure.controls)],
        "hold_sessions": [
            test.hold.sessions
            for test in measure.tests.values()
            if isinstance(test.hold, FixedHold)
        ],
        "turn_of_month": [
            end
            for item in measure.demean
            if isinstance(item, TurnOfMonth)
            for end in item.turn_of_month
        ],
        "latency_ms": [measure.latency_ms],
    }
    if measure.block is not None:
        values["block"] = [measure.block]
    if measure.mde_power is not None:
        values["mde_power"] = [measure.mde_power]
    return values


def _end(end: float | str) -> float:
    return float(span_ns(end, "ranges")) if isinstance(end, str) else end


def _shown(value: float, like: float | str) -> str:
    """A value as the operator wrote its kind: a span in seconds, a number as it is."""
    return f"{value / 1e9:g}s" if isinstance(like, str) else f"{value:g}"
