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

**A follower.** A spec may state a leader — `leader_seed` and `leader_index` naming the shocks
another spec draws for one of its instruments — with a `coupling` and a `lag_steps`: each of
its own instruments then takes that share of the leader's shock `lag_steps` late. With the
leader's spec's other parameters — its span among them, step for step, because a shock is drawn
a batch of the whole span at a time — its returns repeat the leader's that many bars later, which is
a lead a screen has something to find in, planted without loading the leader twice.

The two models are the two shapes a test needs. `ou` is mean-reverting: `p` is pulled
back towards `theta` by `kappa` of the gap each step, which is what a mean-reversion
hypothesis has something to find in. `gbm` is a random walk with drift, which is what a
momentum or a null hypothesis is tested against.

Sessions are the regular hours of a US equity venue by default — 09:30 to 16:00 in
`America/New_York`, weekdays only, no holiday calendar, since a market calendar is a
regulator fact this loader has no business inventing. The timezone comes from the
`zoneinfo` database on the host; the offsets it supplies for the sessions a workspace
generates have been fixed by statute since 2007, so they are the same on both hosts. A
spec whose `calendar` is `continuous` is a round-the-clock venue instead: every calendar
day from `start` to `end` is a session, the zone is UTC and the session is 00:00 to 24:00,
so at a resolution that divides the day the last bar of a day closes at 00:00Z of the
next. A continuous calendar fixes the zone and the session, and a spec that states one of
them differently is refused naming the field. The default calendar is recorded in no
manifest, so every dataset generated before the field existed carries the request
parameters it always did.

A continuous spec may also generate `funding`, the settlements of a perpetual: one at
00:00, 08:00 and 16:00 UTC, each the closing instant of an eight-hour period of a session,
so a session's last settles at 00:00Z of the next day as its last daily bar closes. Each
rate is drawn from the instrument's own seed — a whole number of hundredths of a basis
point between -1 and +2 — so a card holding a perpetual pays and is paid funding with no
vendor in the loop. Funding is refused on a weekday calendar: a perpetual settles round the
clock, and a weekend without settlements would be a claim about no venue.

Points are `realtime`: a bar is available at its close, a quote or a trade at its instant
and a settlement at its instant, so `ts_init == ts_event`. Publication is declared by the
adapter that produced the data, and a generator has no adapter and nothing to declare.
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
from kanso.data.types import Funding, resolve_type
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty
from kanso.schemas.duration import Duration, parse_duration

TYPES: Final = ("bar", "quote", "trade", "funding")
"""What this loader can generate: the three market types and a perpetual's funding; any
other custom type is somebody else's to produce."""

FUNDING: Final = "funding"
SETTLEMENT: Final = timedelta(hours=8)
"""A perpetual's funding period: settlements at 00:00, 08:00 and 16:00 UTC."""

RATE_UNIT: Final = 1_000_000
"""Funding rates are whole hundredths of a basis point, drawn as integers over this."""

RATE_RANGE: Final = (-100, 200)
"""The half-open range of those integers: a rate from -1 up to +2 basis points."""

SHOCK_TERMS: Final = 12
"""Uniforms per shock. Twelve is the Irwin–Hall count whose variance is exactly one, so
the scaling is a subtraction rather than a multiplication by an irrational constant."""

WEEKDAYS: Final = 5

Calendar = Literal["weekdays", "continuous"]
DEFAULT_CALENDAR: Final[Calendar] = "weekdays"
CONTINUOUS_SESSION: Final[dict[str, str]] = {
    "timezone": "UTC",
    "session_start": "00:00",
    "session_end": "24:00",
}
"""What a `continuous` calendar fixes: one session per calendar day, midnight to midnight
UTC. `24:00` is not a clock time, so the session span is taken as a day rather than parsed."""

LEADER_FIELDS: Final = frozenset({"leader_seed", "leader_index", "lag_steps", "coupling"})
"""What a spec states only to follow a leader, recorded in no manifest of a spec that does not,
so every dataset generated before a leader could be stated keeps the map it always had."""

GeneratedType = Literal["bar", "quote", "trade", "funding"]
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
    calendar: Calendar = DEFAULT_CALENDAR
    timezone: str = "America/New_York"
    session_start: str = "09:30"
    session_end: str = "16:00"
    leader_seed: int | None = Field(default=None, ge=0)
    leader_index: int = Field(default=0, ge=0)
    lag_steps: int = Field(default=1, ge=1)
    coupling: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _continuous_session(cls, data: Any) -> Any:
        """A continuous calendar fixes the zone and the session; a spec that states one of
        them differently contradicts itself and is refused naming the field."""
        if not isinstance(data, Mapping) or data.get("calendar") != "continuous":
            return data
        stated = dict(data)
        for field, fixed in CONTINUOUS_SESSION.items():
            if stated.get(field, fixed) != fixed:
                raise ValueError(
                    f"{field}: {stated[field]!r} conflicts with calendar 'continuous', whose "
                    f"every session is a UTC calendar day, 00:00 to 24:00; drop the field"
                )
            stated[field] = fixed
        return stated

    @model_validator(mode="after")
    def _validate(self) -> SyntheticSpec:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instruments: repeats an id")
        if len(set(self.types)) != len(self.types):
            raise ValueError("types: repeats a type")
        if not self.types:
            raise ValueError("types: name at least one of bar, quote, trade, funding")
        if FUNDING in self.types and self.calendar != "continuous":
            raise ValueError(
                "types: funding is settled round the clock and needs calendar 'continuous'; "
                "a weekday calendar has no settlements to generate"
            )
        zone(self.timezone)
        if self.calendar == "weekdays" and self.session_span <= timedelta(0):
            raise ValueError(
                f"session_end: {self.session_end} is not after session_start {self.session_start}"
            )
        if self.step <= timedelta(0):
            raise ValueError(f"resolution: {self.resolution} must be longer than zero")
        if (self.leader_seed is None) != (self.coupling == 0):
            raise ValueError(
                "leader_seed: a leader and a coupling above zero are stated together, or neither is"
            )
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
    def session_span(self) -> timedelta:
        """How long one session lasts: a whole day on a continuous calendar."""
        if self.calendar == "continuous":
            return timedelta(days=1)
        opening = _clock(self.session_start, "session_start")
        closing = _clock(self.session_end, "session_end")
        return timedelta(hours=closing.hour, minutes=closing.minute) - timedelta(
            hours=opening.hour, minutes=opening.minute
        )

    @property
    def steps_per_session(self) -> int:
        """Whole steps that close inside one session."""
        return int(self.session_span // self.step)

    @property
    def long_run(self) -> float:
        """The level an `ou` path is pulled towards."""
        return self.start_price if self.theta is None else self.theta

    def sessions(self) -> list[date]:
        """The sessions between `start` and `end`, inclusive: the weekdays, or every
        calendar day on a continuous calendar."""
        day = self.start
        found: list[date] = []
        while day <= self.end:
            if self.calendar == "continuous" or day.weekday() < WEEKDAYS:
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
        found: list[DatasetRef] = []
        for symbol in parsed.instruments:
            instrument = str(instrument_id(symbol, parsed.venue))
            for type_id in parsed.types:
                served = _settlements(parsed) if type_id == FUNDING else stamps
                span = (utc_day(served[0]), utc_day(served[-1]))
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
    so the map round-trips through the model's own field types. The default calendar is
    left out, so a dataset generated before the field existed records the map it always
    did and its manifest is byte-identical; a continuous spec records its calendar and the
    zone and session it fixed, which decode to the same spec.
    """
    encoded: dict[str, str] = {}
    for key, value in spec.model_dump(mode="json").items():
        if key == "calendar" and value == DEFAULT_CALENDAR:
            continue
        if key in LEADER_FIELDS and spec.leader_seed is None:
            continue
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
    """The five independent seeds of one instrument: path, bar, quote, trade, funding.

    A spawned child is keyed by its position alone, so the fifth leaves the first four —
    and every series generated before funding existed — exactly as they were.
    """
    per_instrument = np.random.SeedSequence(spec.seed).spawn(len(spec.instruments))
    return list(per_instrument[index].spawn(5))


def _path(spec: SyntheticSpec, index: int, count: int) -> list[int]:
    """The mid path in whole ticks, one value per step, over the whole span."""
    unit = 10**spec.price_precision
    floor = 1.0 / unit
    shocks = _followed(spec, _shocks(_streams(spec, index)[0], count))
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


def _followed(spec: SyntheticSpec, own: list[float]) -> list[float]:
    """The shocks with a leader's mixed in, `lag_steps` late, when the spec states a leader.

    The leader's shocks are the ones a spec seeded `leader_seed` draws for its instrument at
    `leader_index` over this spec's steps: a spawned child is keyed by its position alone, so
    they are the same whatever else that spec lists — but a shock adds twelve uniforms drawn a
    batch of the whole span at a time, so only a spec over the leader spec's span, step for
    step, redraws the leader's own shocks. A follower is stated with its leader's span.

    Step t takes `coupling` of the leader's shock at t - lag and the rest of its own, scaled by
    one constant square root so the variance stays one; before the lag there is nothing to
    follow and the shock is its own.
    """
    if spec.leader_seed is None:
        return own
    leader = np.random.SeedSequence(spec.leader_seed).spawn(spec.leader_index + 1)
    lead = _shocks(leader[spec.leader_index].spawn(5)[0], len(own))
    rest = math.sqrt(1.0 - spec.coupling * spec.coupling)
    lag = spec.lag_steps
    return [
        spec.coupling * lead[step - lag] + rest * shock if step >= lag else shock
        for step, shock in enumerate(own)
    ]


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


def _settlements(spec: SyntheticSpec) -> list[int]:
    """Every funding settlement over the whole spec, as UTC nanoseconds: the close of each
    eight-hour period of each session, the last at the next day's 00:00."""
    stamps: list[int] = []
    for session in spec.sessions():
        base = datetime.combine(session, time(0), tzinfo=zone("UTC"))
        stamps.extend(to_ns(base + SETTLEMENT * (index + 1)) for index in range(3))
    return stamps


def _funding(
    spec: SyntheticSpec,
    ref: DatasetRef,
    index: int,
    stamps: Sequence[int],
    path: Sequence[int],
    window: tuple[date, date],
) -> Iterator[object]:
    """One realised rate per settlement, drawn from the instrument's own funding seed over
    the whole span before the window is applied, as every series is."""
    settled = _settlements(spec)
    rng = np.random.default_rng(_streams(spec, index)[4])
    rates = rng.integers(*RATE_RANGE, size=len(settled))
    instrument = _instrument(spec, index)
    for step in _selected(spec, settled, window):
        yield Funding(
            instrument_id=instrument,
            rate=int(rates[step]) / RATE_UNIT,
            ts_event=settled[step],
            ts_init=settled[step],
        )


_EMITTERS: Final = {"bar": _bars, "quote": _quotes, "trade": _trades, FUNDING: _funding}


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
