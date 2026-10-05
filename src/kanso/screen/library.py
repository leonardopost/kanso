"""The measure library: what a screen may measure, and the ranges each admits.

One YAML file per measure under `library/`, in the shape the criteria toolbox uses. A
measure lives in the package and never in an extension, for the reason a gate does: a
verdict means the package's arithmetic or it means nothing, and a result records the
version of the library that produced it.

`screen_version` is that record: the package version and a digest of the library files,
so a result measured under one library is never mistaken for one measured under another.
"""

from __future__ import annotations

from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Final

from kanso import __version__
from kanso.errors import ValidationError
from kanso.schemas import parse_yaml
from kanso.schemas.screen import LeadLag, MeasureItem, Response, Screen, span_ns

LIBRARY: Final = Path(__file__).resolve().parent / "library"


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
    """The package version and a digest of the library, so a result records what measured it."""
    digest = sha256()
    for path in sorted(LIBRARY.glob("*.yaml")):
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


def _values(measure: LeadLag | Response) -> dict[str, list[float]]:
    """Each ranged parameter of a measure, as numbers comparable with its range ends."""
    if isinstance(measure, LeadLag):
        return {
            "lag": [abs(span_ns(lag)) for lag in measure.lags],
            "lags": [len(measure.lags)],
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


def _end(end: float | str) -> float:
    return float(span_ns(end, "ranges")) if isinstance(end, str) else end


def _shown(value: float, like: float | str) -> str:
    """A value as the operator wrote its kind: a span in seconds, a number as it is."""
    return f"{value / 1e9:g}s" if isinstance(like, str) else f"{value:g}"
