"""`synthetic`: the generated series every test and the demo run on.

This loader exists so that nothing in kanso needs a vendor, a credential or a network to
be exercised end to end. It generates a mid-price path from a seed and derives bars,
quotes and trades from it, over trading sessions on weekdays, for as many instruments as
the spec lists.

**Byte reproducibility is the contract, not a nicety.** The same spec must produce the
same bytes on macOS arm64 and on Linux x86_64, today and next year, because a snapshot's
checksum is what makes a card reproducible and this generator is what most snapshots
contain. Three rules follow, and each of them costs something that was worth paying:

* every random draw comes from `numpy.random.default_rng` seeded from the spec, through
  a `SeedSequence` spawned per instrument and per purpose, and never from global state.
  Spawning means the path of the second instrument does not depend on how many types the
  first one emitted, and loading only quotes gives the same quotes as loading everything;
* only `numpy.random.Generator.random` is used, never `standard_normal` or `poisson`. A
  uniform draw is an integer shuffle and an exact multiply; the normal and Poisson
  samplers reach for `log` and `exp` in their rejection branches, and a libm's last bit
  is not portable. The shocks are therefore Irwin–Hall: twelve uniforms added in a fixed
  order, mean zero and variance one, which is Gaussian enough for a fixture and exact
  everywhere;
* the paths are Euler–Maruyama discretisations, so a step is additions, multiplications
  and one constant square root — all correctly rounded by IEEE-754 on every host. The
  closed-form geometric Brownian step would need `exp`, whose error compounds along a
  multiplicative path, so it is not used. Prices are quantised to whole ticks as they
  are produced, and the whole path is generated over the dataset's span before any
  window is applied, so a window never changes what the points in it are.

The two models are the two shapes a test needs. `ou` is mean-reverting: `p` is pulled
back towards `theta` by `kappa` of the gap each step, which is what a mean-reversion
hypothesis has something to find in. `gbm` is a random walk with drift, which is what a
momentum or a null hypothesis is tested against.

Sessions are the regular hours of a US equity venue by default — 09:30 to 16:00 in
`America/New_York`, weekdays only, no holiday calendar, since a market calendar is a
regulator fact this loader has no business inventing. The timezone comes from the
`zoneinfo` database on the host; the offsets it supplies for the sessions a workspace
generates have been fixed by statute since 2007, so they are the same on both hosts.

Points are `realtime`: a bar is available at its close and a quote or a trade at its
instant, so `ts_init == ts_event`. Publication is declared by the adapter that produced
the data, and a generator has no adapter and nothing to declare.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar, Final, Literal

import numpy as np
from nautilus_trader.model.enums import AggressorSide
from pydantic import Field, model_validator

from kanso.data.loader import (
    DatasetRef,
    arrow_batches,
    checked,
    manifest_for,
    to_ns,
    utc_day,
)
from kanso.data.loaders.points import (
    bar_type,
    instrument_id,
    make_bar,
    make_quote,
    make_trade,
    zone,
)
from kanso.data.manifest import Manifest, dataset_id
from kanso.data.types import resolve_type
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty
from kanso.schemas.duration import Duration, parse_duration

TYPES: Final = ("bar", "quote", "trade")
"""What this loader can generate; a custom type is somebody else's to produce."""

SHOCK_TERMS: Final = 12
"""Uniforms per shock. Twelve is the Irwin–Hall count whose variance is exactly one, so
the scaling is a subtraction rather than a multiplication by an irrational constant."""

WEEKDAYS: Final = 5

GeneratedType = Literal["bar", "quote", "trade"]
DEFAULT_TYPES: Final[tuple[GeneratedType, ...]] = ("bar",)
"""What a spec generates when it names no types: the grain a hypothesis usually asks for."""


class SyntheticSpec(KansoModel):
    """A `synthetic` loader spec: what to generate, for whom, and from which seed."""

    loader: Literal["synthetic"] = "synthetic"
    model: Literal["ou", "gbm"] = "ou"
    seed: int = Field(ge=0)
    instruments: list[NonEmpty] = Field(min_length=1)
    venue: NonEmpty = "SIM"
    resolution: Duration
    types: list[GeneratedType] = Field(default_factory=lambda: list(DEFAULT_TYPES))
    start: date
    end: date
    start_price: float = Field(default=100.0, gt=0)
    sigma_bps: float = Field(default=10.0, gt=0)
    kappa: float = Field(default=0.02, gt=0, le=1)
    theta: float | None = Field(default=None, gt=0)
    mu_bps: float = 0.0
    spread_bps: float = Field(default=2.0, gt=0)
    volume: int = Field(default=5_000, gt=0)
    price_precision: int = Field(default=2, ge=0, le=9)
    size_precision: int = Field(default=0, ge=0, le=9)
    timezone: str = "America/New_York"
    session_start: str = "09:30"
    session_end: str = "16:00"

    @model_validator(mode="after")
    def _validate(self) -> SyntheticSpec:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instruments: repeats an id")
        if len(set(self.types)) != len(self.types):
            raise ValueError("types: repeats a type")
        if not self.types:
            raise ValueError("types: name at least one of bar, quote, trade")
        zone(self.timezone)
        if _clock(self.session_end, "session_end") <= _clock(self.session_start, "session_start"):
            raise ValueError(
                f"session_end: {self.session_end} is not after session_start {self.session_start}"
            )
        if self.step <= timedelta(0):
            raise ValueError(f"resolution: {self.resolution} must be longer than zero")
        if self.steps_per_session == 0:
            raise ValueError(
                f"resolution: {self.resolution} is longer than the "
                f"{self.session_start}-{self.session_end} session, so no bar ever closes"
            )
        return self

    @property
    def step(self) -> timedelta:
        """One generator tick: the bar size, and the spacing of quotes and trades."""
        return parse_duration(self.resolution, "resolution")

    @property
    def steps_per_session(self) -> int:
        """Whole steps that close inside one session."""
        opening = _clock(self.session_start, "session_start")
        closing = _clock(self.session_end, "session_end")
        span = timedelta(hours=closing.hour, minutes=closing.minute) - timedelta(
            hours=opening.hour, minutes=opening.minute
        )
        return int(span // self.step)

    @property
    def long_run(self) -> float:
        """The level an `ou` path is pulled towards."""
        return self.start_price if self.theta is None else self.theta

    def sessions(self) -> list[date]:
        """The weekday sessions between `start` and `end`, inclusive."""
        day = self.start
        found: list[date] = []
        while day <= self.end:
            if day.weekday() < WEEKDAYS:
                found.append(day)
            day += timedelta(days=1)
        return found


@dataclass(frozen=True)
class SyntheticLoader:
    """The reference generator. Stateless: the seed and the spec are the whole input."""

    id: ClassVar[str] = "synthetic"

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per instrument and type, spanning the sessions it will serve."""
        parsed = SyntheticSpec.model_validate(dict(spec))
        stamps = _stamps(parsed)
        if not stamps:
            raise ValidationError(
                f"start/end: no weekday session falls between {parsed.start} and {parsed.end}, "
                "so there is nothing to generate"
            )
        span = (utc_day(stamps[0]), utc_day(stamps[-1]))
        found: list[DatasetRef] = []
        for symbol in parsed.instruments:
            instrument = str(instrument_id(symbol, parsed.venue))
            for type_id in parsed.types:
                resolution = parsed.resolution if type_id == "bar" else None
                found.append(
                    DatasetRef(
                        dataset_id=dataset_id(instrument, type_id, resolution, False, span[1]),
                        instrument=instrument,
                        type=type_id,
                        resolution=resolution,
                        span=span,
                        adjusted=False,
                        publication="realtime",
                        request_params=_request_params(parsed),
                    )
                )
        return found

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The dataset's points whose event day falls in `window`."""
        return checked(self._points(ref, window), f"synthetic dataset {ref.dataset_id}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the dataset served over its whole span."""
        return manifest_for(ref, self.id, self.load(ref, ref.span))

    def _points(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object]:
        spec = _spec_of(ref)
        index = _index_of(spec, ref)
        stamps = _stamps(spec)
        path = _path(spec, index, len(stamps))
        emit = _EMITTERS[ref.type]
        yield from emit(spec, ref, index, stamps, path, window)


def _index_of(spec: SyntheticSpec, ref: DatasetRef) -> int:
    """Which of the spec's instruments this dataset is, by its qualified id.

    The position is what seeds the instrument's own stream, so it is recovered from the
    spec rather than stored: appending an instrument to a spec must not move the ones
    already in it.
    """
    qualified = [str(instrument_id(symbol, spec.venue)) for symbol in spec.instruments]
    if ref.instrument not in qualified:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} names instrument {ref.instrument!r}, which its own "
            f"spec does not generate ({', '.join(qualified)})"
        )
    return qualified.index(ref.instrument)


def _spec_of(ref: DatasetRef) -> SyntheticSpec:
    """The spec a ref carries, so `load` needs nothing but the ref it was given."""
    if ref.request_params is None:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} carries no synthetic spec; refs come from "
            "SyntheticLoader.discover and are not built by hand"
        )
    return SyntheticSpec.model_validate(_decode(ref.request_params))


def _request_params(spec: SyntheticSpec) -> dict[str, str]:
    """The spec as the string map a manifest records, which is also what reproduces it.

    A synthetic dataset's provenance *is* its spec: recording it means a manifest names
    everything needed to regenerate the dataset byte for byte, which is what a
    reproducible snapshot claims. Lists are comma-joined and an absent value is empty,
    so the map round-trips through the model's own field types.
    """
    encoded: dict[str, str] = {}
    for key, value in spec.model_dump(mode="json").items():
        if isinstance(value, list):
            encoded[key] = ",".join(str(item) for item in value)
        else:
            encoded[key] = "" if value is None else str(value)
    return encoded


def _decode(params: Mapping[str, str]) -> dict[str, object]:
    """The inverse of `_request_params`, leaving the coercion to the model."""
    decoded: dict[str, object] = {}
    for key, raw in params.items():
        if key in {"instruments", "types"}:
            decoded[key] = [part for part in raw.split(",") if part]
        else:
            decoded[key] = None if raw == "" else raw
    return decoded


def _stamps(spec: SyntheticSpec) -> list[int]:
    """Every step's closing instant over the whole spec, as UTC nanoseconds."""
    tz = zone(spec.timezone)
    opening = _clock(spec.session_start, "session_start")
    step = spec.step
    stamps: list[int] = []
    for session in spec.sessions():
        base = datetime.combine(session, opening, tzinfo=tz)
        for index in range(spec.steps_per_session):
            stamps.append(to_ns(base + step * (index + 1)))
    return stamps


def _shocks(seed: np.random.SeedSequence, count: int) -> list[float]:
    """`count` unit-variance shocks, from uniforms only, added in a fixed order."""
    rng = np.random.default_rng(seed)
    total = np.zeros(count, dtype=np.float64)
    for _ in range(SHOCK_TERMS):
        total += rng.random(count)
    total -= SHOCK_TERMS / 2.0
    return [float(value) for value in total]


def _streams(spec: SyntheticSpec, index: int) -> list[np.random.SeedSequence]:
    """The four independent seeds of one instrument: path, bar, quote, trade."""
    per_instrument = np.random.SeedSequence(spec.seed).spawn(len(spec.instruments))
    return list(per_instrument[index].spawn(4))


def _path(spec: SyntheticSpec, index: int, count: int) -> list[int]:
    """The mid path in whole ticks, one value per step, over the whole span."""
    unit = 10**spec.price_precision
    floor = 1.0 / unit
    shocks = _shocks(_streams(spec, index)[0], count)
    price = spec.start_price
    sigma = spec.sigma_bps / 10_000.0
    drift = spec.mu_bps / 10_000.0
    theta = spec.long_run
    ticks: list[int] = []
    for shock in shocks:
        if spec.model == "ou":
            price = price + spec.kappa * (theta - price) + theta * sigma * shock
        else:
            price = price * (1.0 + drift + sigma * shock)
        if price < floor:
            price = floor
        ticks.append(math.floor(price * unit + 0.5))
    return ticks


def _selected(
    spec: SyntheticSpec, stamps: Sequence[int], window: tuple[date, date]
) -> Iterator[int]:
    """The indices of the steps whose event day falls inside `window`."""
    for index, stamp in enumerate(stamps):
        day = utc_day(stamp)
        if window[0] <= day <= window[1]:
            yield index


def _bars(
    spec: SyntheticSpec,
    ref: DatasetRef,
    index: int,
    stamps: Sequence[int],
    path: Sequence[int],
    window: tuple[date, date],
) -> Iterator[object]:
    unit = 10**spec.price_precision
    wiggle = max(1, int(spec.sigma_bps / 10_000.0 * spec.start_price * unit))
    opening = math.floor(spec.start_price * unit + 0.5)
    size_unit = 10**spec.size_precision
    base = spec.volume * size_unit
    rng = np.random.default_rng(_streams(spec, index)[1])
    volumes = rng.integers(base // 2, base * 2 + 1, size=len(path))
    bars = bar_type(_instrument(spec, index), spec.resolution)
    for step in _selected(spec, stamps, window):
        close = path[step]
        open_ = path[step - 1] if step else opening
        high = max(open_, close) + wiggle
        low = max(1, min(open_, close) - wiggle)
        yield make_bar(
            bars,
            (open_, high, low, close),
            int(volumes[step]),
            spec.price_precision,
            spec.size_precision,
            stamps[step],
            stamps[step],
        )


def _quotes(
    spec: SyntheticSpec,
    ref: DatasetRef,
    index: int,
    stamps: Sequence[int],
    path: Sequence[int],
    window: tuple[date, date],
) -> Iterator[object]:
    size_unit = 10**spec.size_precision
    base = spec.volume * size_unit
    rng = np.random.default_rng(_streams(spec, index)[2])
    sizes = rng.integers(1, base + 1, size=(2, len(path)))
    instrument = _instrument(spec, index)
    for step in _selected(spec, stamps, window):
        mid = path[step]
        half = _half_spread(mid, spec.spread_bps)
        yield make_quote(
            instrument,
            mid - half,
            mid + half,
            int(sizes[0][step]),
            int(sizes[1][step]),
            spec.price_precision,
            spec.size_precision,
            stamps[step],
            stamps[step],
        )


def _trades(
    spec: SyntheticSpec,
    ref: DatasetRef,
    index: int,
    stamps: Sequence[int],
    path: Sequence[int],
    window: tuple[date, date],
) -> Iterator[object]:
    size_unit = 10**spec.size_precision
    base = spec.volume * size_unit
    rng = np.random.default_rng(_streams(spec, index)[3])
    sizes = rng.integers(1, base + 1, size=len(path))
    buyers = rng.integers(0, 2, size=len(path))
    instrument = _instrument(spec, index)
    for step in _selected(spec, stamps, window):
        mid = path[step]
        half = _half_spread(mid, spec.spread_bps)
        buyer = bool(buyers[step])
        yield make_trade(
            instrument,
            mid + half if buyer else mid - half,
            int(sizes[step]),
            AggressorSide.BUYER if buyer else AggressorSide.SELLER,
            f"{spec.instruments[index]}-{step}",
            spec.price_precision,
            spec.size_precision,
            stamps[step],
            stamps[step],
        )


_EMITTERS: Final = {"bar": _bars, "quote": _quotes, "trade": _trades}


def _instrument(spec: SyntheticSpec, index: int) -> Any:
    """The engine instrument id of the spec's `index`-th instrument."""
    return instrument_id(spec.instruments[index], spec.venue)


def _half_spread(mid_ticks: int, spread_bps: float) -> int:
    """Half the quoted spread in ticks, never below one tick."""
    return max(1, int(mid_ticks * spread_bps / 20_000.0))


def _clock(text: str, field: str) -> time:
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field}: {text!r} is not a time of day; expected HH:MM") from None
