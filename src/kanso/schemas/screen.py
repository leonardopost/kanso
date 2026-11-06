"""`screen.yaml`: a measurement of declared relationships between declared series.

A screen asks, before any lane is spent, whether a relationship a hypothesis rests on is in
the data and how large it is against the hurdle a trade must clear. The file holds the
question and nothing about the answer — results live in `state.db` — so its bytes can be
content-addressed, as a hypothesis's are.

Two forms. A **bound** screen names a registered hypothesis in `hyp`: its window is that
hypothesis's research window, its costs are that hypothesis's, and every leg must be an
instrument of its universe. A **free** screen states its own `window` and may name any
instrument the catalog can hold.

The rules enforced here are local to the file:

* every name — a leg, a derived leg, a group, a signal — is declared once, and every reference
  names one that is declared;
* a derived leg is a declared function of legs: a `basket` of weights, a `spread` of two legs
  under a fixed or fitted hedge, or a `gap` between two venues' prices of one asset;
* a lattice is finite and declared: lags are distinct and never zero, because the same
  instant's co-movement is not a lead; thresholds and horizons are distinct, and so are a
  catch-up's entries, closes and stops, each on its own side of the gap it was entered at;
* a grid exists whenever a measure samples on one, and no bar leg it samples is coarser;
* a signal is one numeric field of a custom type, filed under one instrument: never a market
  type, which is a leg, and never a timestamp or the instrument itself, which are no value.
* a condition is a dict of named tests over a declared signal, each a comparison that is the
  favoured state and, under a contrast, a disjoint one or `rest` it is set against; a test's
  followers are catalog legs, its family is its measure, and its null is declared with the one
  parameter that null takes and no other.

What the instrument store holds, what the measure library permits, which custom types this
process has registered and what a bound screen's hypothesis says are checked where those
live (`kanso.screen.files`, `kanso.screen.signals`).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints, model_validator

from kanso.errors import ValidationError
from kanso.schemas.base import CatalogueId, HypId, KansoModel, NonEmpty, Sha256, Versioned
from kanso.schemas.duration import Duration, parse_duration
from kanso.schemas.hypothesis import MARKET_TYPES, DateWindow
from kanso.schemas.venue import CostsOverride, VenueCode

SPAN_PATTERN: Final = r"^[0-9]+(ms|s|m|h|d)$"
LAG_PATTERN: Final = r"^-?[0-9]+(ms|s|m|h)$"

Span = Annotated[str, StringConstraints(pattern=SPAN_PATTERN)]
"""`<n>(ms|s|m|h|d)`: a span of time a screen measures over, finer than a hypothesis's grain."""

Lag = Annotated[str, StringConstraints(pattern=LAG_PATTERN)]
"""A signed span: positive when the first series leads the second."""

Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
"""A leg, derived leg, group, signal or test name."""

FieldName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
"""A field of a custom type, spelled as its class annotates it."""

STAMPS: Final = ("ts_event", "ts_init", "instrument_id")
"""The fields every custom point carries that say when and whose it is, never what."""

LegType = Literal["bar", "trade", "quote", "book"]
Side = Literal["with", "against"]
Estimator = Literal["grid", "hy"]

_SPAN: Final = re.compile(r"^(-?)([0-9]+)(ms|s|m|h|d)$")
_NS: Final = {
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
    "d": 86_400_000_000_000,
}
_HOURS: Final = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-4]):([0-5][0-9])$")


def p_floor(sessions: int) -> float:
    """The smallest p a session sign-flip null can give over this many sessions.

    The observed signs and their negation both reach the observed |t|, so of the 2^S sign
    vectors at least two do: no draw count reads a p below 2^(1 - S).
    """
    return float(2.0 ** (1 - sessions))


def span_ns(text: str, field: str = "span") -> int:
    """`"250ms"` to 250,000,000 nanoseconds; a lag keeps its sign."""
    match = _SPAN.match(text)
    if match is None:
        raise ValidationError(f"{field}: {text!r} is not a span; expected <n> and ms, s, m, h or d")
    sign = -1 if match.group(1) else 1
    return sign * int(match.group(2)) * _NS[match.group(3)]


def hours_minutes(span: str) -> tuple[int, int]:
    """`"09:30-16:00"` as minutes after midnight, opening and closing."""
    match = _HOURS.match(span)
    if match is None:
        raise ValidationError(f"hours.span: {span!r} is not HH:MM-HH:MM")
    opens = int(match.group(1)) * 60 + int(match.group(2))
    closes = int(match.group(3)) * 60 + int(match.group(4))
    if closes > 24 * 60 or closes <= opens:
        raise ValidationError(f"hours.span: {span!r} does not close after it opens on one day")
    return opens, closes


class Leg(KansoModel):
    """One catalog series: an instrument and the type it is read as.

    Bars carry their resolution, because a catalog may hold several grains of one name;
    trades, quotes and books are unaggregated and carry none.
    """

    instrument: NonEmpty
    type: LegType
    resolution: Duration | None = None

    @model_validator(mode="after")
    def _resolution_is_a_bar_s(self) -> Leg:
        if self.type == "bar" and self.resolution is None:
            raise ValueError("resolution: a bar leg names its bar size")
        if self.type != "bar" and self.resolution is not None:
            raise ValueError(f"resolution: a {self.type} leg is unaggregated and has none")
        return self


class Signal(KansoModel):
    """One numeric field of a registered custom type, filed under one instrument: a series.

    `instrument` is the id the series is filed under in the catalog — a file loaded by
    `csv_parquet` is filed under its entry's instrument and venue — and only that
    instrument's points are read. A signal filed under one instrument may condition a
    follower of another. Whether `type` is registered and `field` one of its numbers is
    asked of the registry (`kanso.screen.signals`), which this file cannot see.
    """

    type: CatalogueId
    field: FieldName
    instrument: NonEmpty

    @model_validator(mode="after")
    def _a_custom_value(self) -> Signal:
        if self.type in MARKET_TYPES:
            raise ValueError(
                f"type: {self.type!r} is a market type; a signal is a custom type's field, and "
                "a market series is read as a leg"
            )
        if self.field in STAMPS:
            raise ValueError(
                f"field: {self.field!r} says when or whose a point is, not what it holds"
            )
        return self


class Spread(KansoModel):
    """`long` less `beta` times `short`, in log prices.

    `fixed` states beta; `ols` fits it, on the whole window or on its first fold, and a cell
    judged on the data its beta was fitted on says so in its result.
    """

    long: Name
    short: Name
    hedge: Literal["fixed", "ols"]
    beta: float | None = Field(default=None, allow_inf_nan=False)
    fit: Literal["window", "first_fold"] | None = None

    @model_validator(mode="after")
    def _hedge_is_whole(self) -> Spread:
        if self.long == self.short:
            raise ValueError(f"short: {self.short!r} is the long leg too")
        if self.hedge == "fixed" and (self.beta is None or self.fit is not None):
            raise ValueError("beta: a fixed hedge states beta and fits nothing")
        if self.hedge == "ols" and (self.fit is None or self.beta is not None):
            raise ValueError("fit: an ols hedge states where it is fitted and no beta")
        return self


class Gap(KansoModel):
    """One asset on two venues: (a less b) over b, in basis points."""

    a: Name
    b: Name

    @model_validator(mode="after")
    def _two_venues(self) -> Gap:
        if self.a == self.b:
            raise ValueError(f"b: {self.b!r} is the same leg as a")
        return self


class Derived(KansoModel):
    """A synthetic leg: exactly one of a basket, a spread or a gap, over declared legs."""

    basket: dict[Name, Annotated[float, Field(allow_inf_nan=False)]] | None = None
    spread: Spread | None = None
    gap: Gap | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Derived:
        kinds = [kind for kind in ("basket", "spread", "gap") if getattr(self, kind) is not None]
        if len(kinds) != 1:
            raise ValueError("a derived leg is exactly one of basket, spread or gap")
        if self.basket is not None:
            if len(self.basket) < 2:
                raise ValueError("basket: a basket weighs at least two legs")
            if any(weight == 0 for weight in self.basket.values()):
                raise ValueError("basket: a weight of zero leaves a leg out; drop it instead")
        return self

    @property
    def legs(self) -> tuple[str, ...]:
        """The legs this derived leg is a function of, in the order it names them."""
        if self.basket is not None:
            return tuple(self.basket)
        if self.spread is not None:
            return (self.spread.long, self.spread.short)
        assert self.gap is not None
        return (self.gap.a, self.gap.b)


class Hours(KansoModel):
    """A daily span in a named time zone, so daylight saving moves it as the market does."""

    tz: NonEmpty
    span: NonEmpty

    @model_validator(mode="after")
    def _readable(self) -> Hours:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"tz: {self.tz!r} is not a time zone this host knows") from None
        try:
            hours_minutes(self.span)
        except ValidationError as error:
            raise ValueError(error.message.removeprefix("hours.span: ")) from None
        return self


class Clock(KansoModel):
    """Where a cell's samples come from inside each session.

    `grid` is the sampling step of a `grid` estimator; `hours` is `overlap` — each session,
    from the latest first print to the earliest last print of a cell's legs — or a span.
    """

    grid: Span | None = None
    hours: Literal["overlap"] | Hours


class Trigger(KansoModel):
    """When an event fires: a move of `move_bp` within `within`, or a z-score over `lookback`."""

    leg: Name
    move_bp: list[Annotated[float, Field(gt=0, allow_inf_nan=False)]] | None = None
    within: Span | None = None
    z: list[Annotated[float, Field(gt=0, allow_inf_nan=False)]] | None = None
    lookback: Span | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Trigger:
        move = self.move_bp is not None or self.within is not None
        score = self.z is not None or self.lookback is not None
        if move == score:
            raise ValueError("a trigger is a move (move_bp, within) or a z-score (z, lookback)")
        if move and (not self.move_bp or self.within is None):
            raise ValueError("move_bp: a move trigger states its thresholds and its window")
        if score and (not self.z or self.lookback is None):
            raise ValueError("z: a z-score trigger states its thresholds and its lookback")
        _distinct(self.thresholds, "thresholds")
        return self

    @property
    def thresholds(self) -> list[float]:
        """The thresholds the trigger fires at, whichever form it takes."""
        return list(self.move_bp or self.z or [])


class LeadLag(KansoModel):
    """Whether, which way and at what delay two series move together."""

    id: Literal["lead_lag"]
    from_: Name = Field(alias="from")
    to: Name
    estimator: Estimator
    lags: list[Lag] = Field(min_length=1)

    @model_validator(mode="after")
    def _lags(self) -> LeadLag:
        if any(span_ns(lag, "lags") == 0 for lag in self.lags):
            raise ValueError("lags: zero is refused; the same instant's co-movement is not a lead")
        _distinct([span_ns(lag) for lag in self.lags], "lags")
        return self


class Response(KansoModel):
    """Whether trading a follower on a trigger clears the hurdle, and how often it could.

    The followers are named `followers` and not `on`, because YAML 1.1 reads a bare `on` as
    the boolean true, key or value.
    """

    id: Literal["response"]
    trigger: Trigger
    followers: Name
    side: Side
    horizons: list[Span] = Field(min_length=1)
    latency_ms: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _horizons(self) -> Response:
        if any(span_ns(horizon, "horizons") == 0 for horizon in self.horizons):
            raise ValueError("horizons: a horizon of zero holds nothing")
        _distinct([span_ns(horizon) for horizon in self.horizons], "horizons")
        return self


class CatchUp(KansoModel):
    """Whether trading a follower toward the move its leader made, until it has caught up,
    clears the hurdle, and how often it could.

    Every exit is a condition of the two series, never a clock: the position closes when the
    gap it was entered on has shrunk to `close` of its size, and stops when it has grown to
    `stop` times it. `max_hold` is a safety, counted when it fires. The follower's response to
    its leader, `beta`, is fitted per pair — either sign — on the returns of `beta_over`, over
    the window or its first fold, as a fitted spread is.
    """

    id: Literal["catch_up"]
    leader: Name
    followers: Name
    within: Span
    fit: Literal["window", "first_fold"]
    beta_over: Span
    enter_bp: list[Annotated[float, Field(gt=0, allow_inf_nan=False)]] = Field(min_length=1)
    close: list[Annotated[float, Field(ge=0, lt=1, allow_inf_nan=False)]] = Field(min_length=1)
    stop: list[Annotated[float, Field(gt=1, allow_inf_nan=False)]] = Field(min_length=1)
    max_hold: Span
    latency_ms: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _lattice(self) -> CatchUp:
        for name in ("within", "beta_over", "max_hold"):
            if span_ns(getattr(self, name), name) == 0:
                raise ValueError(f"{name}: a span of zero measures nothing")
        _distinct(self.enter_bp, "enter_bp")
        _distinct(self.close, "close")
        _distinct(self.stop, "stop")
        return self


class TurnOfMonth(KansoModel):
    """The turn of each calendar month: its last `l` trading days and its first `m`, declared as
    `[l, m]`, because a turn fixed here would be an opinion. Not both zero, which holds no day."""

    turn_of_month: tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]]

    @model_validator(mode="after")
    def _holds_a_day(self) -> TurnOfMonth:
        if self.turn_of_month == (0, 0):
            raise ValueError("turn_of_month: [0, 0] holds no day of the month")
        return self


CalendarControl = Literal["weekday", "month", "third_friday_week", "pre_holiday"] | TurnOfMonth
"""A calendar fact a condition's returns are demeaned on: the weekday and the month as dummies,
the week holding the month's third Friday, the session before a closure, or a declared turn of
the month."""


OPERATORS: Final = ("gt", "ge", "lt", "le", "eq", "ne")
_UPPER: Final = frozenset({"gt", "ge"})


class Comparison(KansoModel):
    """A signal's value against one number: exactly one of `gt`, `ge`, `lt`, `le`, `eq`, `ne`,
    each compared exactly."""

    gt: float | None = Field(default=None, allow_inf_nan=False)
    ge: float | None = Field(default=None, allow_inf_nan=False)
    lt: float | None = Field(default=None, allow_inf_nan=False)
    le: float | None = Field(default=None, allow_inf_nan=False)
    eq: float | None = Field(default=None, allow_inf_nan=False)
    ne: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def _one(self) -> Comparison:
        if sum(getattr(self, name) is not None for name in OPERATORS) != 1:
            raise ValueError("a comparison states exactly one of gt, ge, lt, le, eq or ne")
        return self

    @property
    def operator(self) -> tuple[str, float]:
        """The operator stated, and the number it compares with."""
        name = next(name for name in OPERATORS if getattr(self, name) is not None)
        return name, float(getattr(self, name))

    @property
    def text(self) -> str:
        """`gt 0`: the comparison as a cell's parameters spell it."""
        name, value = self.operator
        return f"{name} {value:g}"

    def holds(self, value: float) -> bool:
        """Whether `value` meets the comparison, compared exactly."""
        name, bound = self.operator
        if name == "gt":
            return value > bound
        if name == "ge":
            return value >= bound
        if name == "lt":
            return value < bound
        if name == "le":
            return value <= bound
        if name == "eq":
            return value == bound
        return value != bound


def disjoint(a: Comparison, b: Comparison) -> bool:
    """Whether no number meets both comparisons, decided exactly over the six shapes.

    An upper half-line from `a` and a lower one to `b` are disjoint when `a` lies above `b`, or
    at it unless both are closed; two upper or two lower half-lines always meet; `eq v` misses a
    set exactly when `v` is outside it; `ne v` misses only `eq v`.
    """
    (first, low), (second, high) = a.operator, b.operator
    if first == "eq":
        return not b.holds(low)
    if second == "eq":
        return not a.holds(high)
    if "ne" in (first, second) or (first in _UPPER) == (second in _UPPER):
        return False
    if first not in _UPPER:
        (first, low), (second, high) = (second, high), (first, low)
    return low > high or (low == high and not (first == "ge" and second == "le"))


class FixedHold(KansoModel):
    """A position held a fixed number of sessions from the onset that opened it."""

    sessions: int = Field(ge=1)


class ConditionTest(KansoModel):
    """One pre-registered test: a signal, the state it favours, and how a position follows it.

    `when` is the favoured state and `side` the position it takes; under a contrast, `against`
    — a comparison disjoint from `when`, or `rest` for every value `when` misses — takes the
    other. `entry` and `exit` name a session's first or last point, so a period may run across
    one session boundary; `hold` re-decides at every entry (`while`) or holds a fixed number of
    sessions from an onset.
    """

    signal: Name
    when: Comparison
    against: Comparison | Literal["rest"] | None = None
    entry: Literal["open", "close"]
    exit: Literal["open", "close"]
    hold: Literal["while"] | FixedHold
    side: Literal["long", "short"]

    @model_validator(mode="after")
    def _apart(self) -> ConditionTest:
        if isinstance(self.against, Comparison) and not disjoint(self.when, self.against):
            raise ValueError(
                f"against: {self.against.text} meets when {self.when.text}; a value in both "
                "states belongs to neither"
            )
        return self


class Condition(KansoModel):
    """Whether a follower's session returns differ while a declared signal meets a declared
    comparison, after the calendar is taken out, and against the round trip a position pays.

    One measure is one family: its cells are its tests by its followers, judged together by the
    null it declares — session sign flips summed into `block`s, or every circular shift of the
    positions against the returns — and only the sign flip takes a parameter, its block, which
    the shifts refuse. `demean` and `controls` are the design the returns
    are demeaned on; `[]` is the window's mean alone. `latency_ms` is how long before the entry
    it orders the decision is made, at least a millisecond, so a point stamped at a fill's own
    instant is never read for it. `mde_power` asks for the minimum detectable effect at that
    power beside a verdict, and reports nothing else.
    """

    id: Literal["condition"]
    tests: dict[Name, ConditionTest] = Field(min_length=1)
    followers: Name
    payoff: Literal["contrast", "long_flat"]
    null_by: Literal["sign_flip", "circular"]
    block: int | None = Field(default=None, ge=1)
    demean: list[CalendarControl]
    controls: list[Name]
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    mde_power: float | None = Field(default=None, gt=0, lt=1)

    @model_validator(mode="after")
    def _declared(self) -> Condition:
        for name, test in self.tests.items():
            if self.payoff == "contrast" and test.against is None:
                raise ValueError(
                    f"tests.{name}.against: a contrast states what the favoured state is set "
                    "against, a comparison or rest"
                )
            if self.payoff == "long_flat" and test.against is not None:
                raise ValueError(
                    f"tests.{name}.against: a long-or-flat payoff sets the favoured state "
                    "against nothing"
                )
        if self.null_by == "sign_flip" and self.block is None:
            raise ValueError("block: a sign-flip null states the block its sessions are summed in")
        if self.null_by == "circular" and self.block is not None:
            raise ValueError("block: a circular null takes every shift and sums into no block")
        for name, test in self.tests.items():
            hold = test.hold
            if (
                self.block is not None
                and isinstance(hold, FixedHold)
                and hold.sessions > self.block
            ):
                raise ValueError(
                    f"block: {self.block} session(s) is shorter than tests.{name}'s hold of "
                    f"{hold.sessions}, so one position's sessions would be flipped apart"
                )
        kinds = [item if isinstance(item, str) else "turn_of_month" for item in self.demean]
        _distinct(kinds, "demean")
        _distinct(self.controls, "controls")
        return self


Measure = Annotated[LeadLag | Response | CatchUp | Condition, Field(discriminator="id")]

RangeEnd = float | Span
"""A range end: a number, or a span for a parameter that is one."""


class MeasureItem(KansoModel):
    """One entry of the measure library: what it measures and the ranges it admits.

    A measure carries no default and no threshold; its ranges are structural bounds — a lag
    longer than a day is not a lead, a thousand lags are not a lattice — and nothing more.
    """

    id: Literal["lead_lag", "response", "catch_up", "condition"]
    meaningful_when: NonEmpty
    ranges: dict[NonEmpty, tuple[RangeEnd, RangeEnd]]


def condition_keys(screen: Screen, measure: Condition) -> tuple[str, ...]:
    """A condition measure's cell keys, test by follower: `condition/<test>/<follower>/<null>`."""
    return tuple(
        f"condition/{test}/{follower}/{measure.null_by}"
        for test in measure.tests
        for follower in screen.members(measure.followers)
    )


def _passable(verdict: ScreenVerdict, measure: Condition, index: int) -> None:
    """Refuse a verdict no cell of a condition could pass under its own null."""
    alpha, sessions = verdict.alpha, verdict.min_sessions
    if measure.block is not None:
        blocks = -(-sessions // measure.block)
        floor = p_floor(blocks)
        if alpha < floor:
            raise ValueError(
                f"verdict.alpha: {alpha:g} is below {floor:.3g}, the smallest p {blocks} "
                f"block(s) of measures.{index}'s {measure.block} session(s) can give; raise "
                "min_sessions or alpha, or shorten the block"
            )
        return
    floor = 1.0 / sessions
    if alpha < floor:
        raise ValueError(
            f"verdict.alpha: {alpha:g} is below {floor:.3g}, the smallest p a circular shift "
            f"of measures.{index}'s positions can give over {sessions} session(s), one in "
            f"their {sessions} rotations; raise min_sessions or alpha"
        )


class ScreenVerdict(KansoModel):
    """The floors a cell is judged against, declared before any number is read.

    There is no default for any of them: a screen with no `verdict` measures everything and
    judges nothing. One whose `alpha` is below the smallest p `min_sessions` sessions can give
    (`p_floor`) is refused, because no cell could pass it.
    """

    alpha: float = Field(gt=0, lt=1)
    min_margin_bp: float = Field(allow_inf_nan=False)
    min_events_per_day: float = Field(ge=0, allow_inf_nan=False)
    min_sessions: int = Field(ge=1)

    @model_validator(mode="after")
    def _passable(self) -> ScreenVerdict:
        floor = p_floor(self.min_sessions)
        if self.alpha < floor:
            raise ValueError(
                f"alpha: {self.alpha:g} is below {floor:.3g}, the smallest p {self.min_sessions} "
                f"session(s) can give, so no cell could pass; raise min_sessions or alpha"
            )
        return self


class Screen(Versioned):
    """`screens/<id>/screen.yaml`."""

    id: HypId
    title: NonEmpty
    thesis: NonEmpty
    hyp: HypId | None = None
    window: DateWindow | None = None
    legs: dict[Name, Leg] = Field(min_length=1)
    derived: dict[Name, Derived] = Field(default_factory=dict)
    groups: dict[Name, list[Name]] = Field(default_factory=dict)
    signals: dict[Name, Signal] = Field(default_factory=dict)
    clock: Clock
    measures: list[Measure] = Field(min_length=1)
    costs: dict[VenueCode, CostsOverride] | None = None
    verdict: ScreenVerdict | None = None

    @model_validator(mode="after")
    def _admissible(self) -> Screen:
        if self.hyp is None and self.window is None:
            raise ValueError("window: a free screen states its window")
        if self.hyp is not None and self.window is not None:
            raise ValueError(f"window: a bound screen reads {self.hyp}'s research window")
        if self.hyp is not None and self.costs is not None:
            raise ValueError(f"costs: a bound screen is charged {self.hyp}'s costs")
        self._check_names()
        self._check_references()
        self._check_grid()
        self._check_conditions()
        return self

    def _check_names(self) -> None:
        seen: dict[str, str] = {}
        declared = (
            ("leg", self.legs),
            ("derived", self.derived),
            ("group", self.groups),
            ("signal", self.signals),
        )
        for kind, names in declared:
            for name in names:
                if name in seen:
                    raise ValueError(f"{kind}s.{name}: already declared as a {seen[name]}")
                seen[name] = kind

    def _check_references(self) -> None:
        for name, derived in self.derived.items():
            for leg in derived.legs:
                if leg not in self.legs:
                    raise ValueError(f"derived.{name}: {leg!r} is not a declared leg")
        for name, members in self.groups.items():
            if not members:
                raise ValueError(f"groups.{name}: a group holds at least one series")
            _distinct(members, f"groups.{name}")
            for member in members:
                if member not in self.legs and member not in self.derived:
                    raise ValueError(f"groups.{name}: {member!r} is not a declared leg")
        for index, measure in enumerate(self.measures):
            where = f"measures.{index}"
            if isinstance(measure, LeadLag):
                self._known(measure.from_, f"{where}.from")
                self._known(measure.to, f"{where}.to")
            elif isinstance(measure, CatchUp):
                if measure.leader not in self.legs and measure.leader not in self.derived:
                    raise ValueError(f"{where}.leader: {measure.leader!r} is not a declared leg")
                self._known(measure.followers, f"{where}.followers")
                if self.members(measure.followers) == (measure.leader,):
                    raise ValueError(f"{where}.followers: the leader cannot follow itself")
            elif isinstance(measure, Condition):
                self._check_condition(measure, where)
            else:
                if measure.trigger.leg not in self.legs and measure.trigger.leg not in self.derived:
                    raise ValueError(
                        f"{where}.trigger.leg: {measure.trigger.leg!r} is not a declared leg"
                    )
                self._known(measure.followers, f"{where}.followers")

    def _check_condition(self, measure: Condition, where: str) -> None:
        for name, test in measure.tests.items():
            if test.signal not in self.signals:
                raise ValueError(
                    f"{where}.tests.{name}.signal: {test.signal!r} is not a declared signal"
                )
        for control in measure.controls:
            if control not in self.signals:
                raise ValueError(f"{where}.controls: {control!r} is not a declared signal")
        self._known(measure.followers, f"{where}.followers")
        for member in self.members(measure.followers):
            if member not in self.legs:
                raise ValueError(
                    f"{where}.followers: {member!r} is a derived leg, and a condition's "
                    "follower is a catalog leg"
                )

    def _check_conditions(self) -> None:
        """Every condition cell's key once, and a verdict each condition's null could pass."""
        seen: dict[str, int] = {}
        for index, measure in enumerate(self.measures):
            if not isinstance(measure, Condition):
                continue
            for key in condition_keys(self, measure):
                if key in seen:
                    raise ValueError(
                        f"measures.{index}: {key} is measures.{seen[key]}'s cell too; name the "
                        "test apart, or judge it under the other null"
                    )
                seen[key] = index
            if self.verdict is not None:
                _passable(self.verdict, measure, index)

    def _known(self, name: str, where: str) -> None:
        if name not in self.legs and name not in self.derived and name not in self.groups:
            raise ValueError(f"{where}: {name!r} is not a declared leg or group")

    def _check_grid(self) -> None:
        for index, measure in enumerate(self.measures):
            if not isinstance(measure, LeadLag) or measure.estimator != "grid":
                continue
            if self.clock.grid is None:
                raise ValueError(f"clock.grid: measures.{index} samples on a grid and none is set")
            grid = span_ns(self.clock.grid, "clock.grid")
            for lag in measure.lags:
                if span_ns(lag) % grid:
                    raise ValueError(
                        f"measures.{index}.lags: {lag} is not a whole number of "
                        f"{self.clock.grid} grid steps"
                    )
            for leg in self.legs_of(measure.from_) + self.legs_of(measure.to):
                resolution = self.legs[leg].resolution
                if resolution is not None and _ns(resolution) > grid:
                    raise ValueError(
                        f"clock.grid: {self.clock.grid} is finer than leg {leg}'s "
                        f"{resolution} bars, which cannot move inside one step"
                    )

    @property
    def instruments(self) -> tuple[str, ...]:
        """Every instrument the screen reads, its legs' and its signals', sorted."""
        legs = {leg.instrument for leg in self.legs.values()}
        return tuple(sorted(legs | {signal.instrument for signal in self.signals.values()}))

    def members(self, name: str) -> tuple[str, ...]:
        """The series a name stands for: a group's members, or the leg itself."""
        return tuple(self.groups[name]) if name in self.groups else (name,)

    def legs_of(self, name: str) -> tuple[str, ...]:
        """The catalog legs behind a name, derived legs expanded, in first-seen order."""
        found: list[str] = []
        for member in self.members(name):
            for leg in self.derived[member].legs if member in self.derived else (member,):
                if leg not in found:
                    found.append(leg)
        return tuple(found)


Judged = Literal["pass", "fail", "thin"]


class ResponseStats(KansoModel):
    """What a cell that trades — a `response` or a `catch_up` — came to, across the sessions
    it was live in.

    Per event: `gross_bp` a taker would have made, `hurdle_bp` the venue model's round trip,
    `margin_bp` the two's difference, `drift_adjusted_bp` the signal the null is tested on.
    `ceiling_bp_day` is the mean over live sessions of each session's summed margins: what one
    notional, put on every event and holding nothing else, earned a day — no capacity limit,
    no sizing, so a bound on what a search of the mechanism could find, never an estimate of it.
    """

    events: int = Field(ge=0)
    events_per_day: float = Field(ge=0, allow_inf_nan=False)
    gross_bp: float = Field(allow_inf_nan=False)
    hurdle_bp: float = Field(ge=0, allow_inf_nan=False)
    margin_bp: float = Field(allow_inf_nan=False)
    ceiling_bp_day: float = Field(allow_inf_nan=False)
    hit_rate: float = Field(ge=0, le=1)
    unfilled: int = Field(ge=0)
    drift_adjusted_bp: float = Field(allow_inf_nan=False)


class CatchUpStats(KansoModel):
    """How a `catch_up` cell's positions ended, and the response they were entered on.

    `beta` is the follower's fitted response to its leader, `None` when the leader never moved
    over the fit span, which leaves the pair's cells holding no value. Every scored position
    ended one way: `closed` when its gap shrank to the close, `stopped` when it grew to the
    stop, `capped` when `max_hold` came first. The holds those exits produced are an output,
    and `hold_median_s` and `hold_p90_s` describe them.
    """

    beta: float | None = Field(default=None, allow_inf_nan=False)
    closed: int = Field(ge=0)
    stopped: int = Field(ge=0)
    capped: int = Field(ge=0)
    hold_median_s: float = Field(ge=0, allow_inf_nan=False)
    hold_p90_s: float = Field(ge=0, allow_inf_nan=False)


class ConditionStats(KansoModel):
    """What a `condition` cell came to beyond the response its positions make.

    `sessions_when` and `sessions_against` are the held sessions the position took the favoured
    state's side in and the other side in; `exposure` the share of held sessions with a
    position; `episodes`, `episodes_against` and `longest_run` the runs of each state among the
    decisions; `skipped` the onsets a fixed hold let pass. `blocks` is the blocks a sign-flip
    null judged the cell on, `shifts` the shifts there are of a circular one, N − 1 over the
    measure's N sessions, and `exact` whether every assignment the null has was taken once
    rather than drawn — a statement about the arithmetic, not about how well the null's
    assumption holds. `raw_bp` is the estimator on returns not demeaned, `drift_bp_day` the
    follower's mean return over the held sessions, and under a contrast `long_flat_bp` and
    `long_flat_ceiling_bp_day` are the long-or-flat realisation of the same test, reported and
    never judged. `control_r2` is the R² of the state on the design the returns were demeaned
    on — against `rest` the estimator is exactly the state's own coefficient times one less it,
    long or flat that times f(1 − f), and against a comparison approximately so — and `dropped`
    the design's columns that held nothing. `folds_adjusted` is the estimator inside each fold.
    `critical_t` and `mde_bp` are the |t| a cell needs to pass the verdict's alpha under its own
    null and the effect that reaches it at `mde_power`, a detection threshold and never a
    forecast. The signal's age is how old the point read was at each decision.
    """

    payoff: Literal["contrast", "long_flat"]
    null_by: Literal["sign_flip", "circular"]
    sessions_when: int = Field(ge=0)
    sessions_against: int | None = Field(default=None, ge=0)
    exposure: float = Field(ge=0, le=1)
    episodes: int = Field(ge=0)
    episodes_against: int | None = Field(default=None, ge=0)
    longest_run: int = Field(ge=0)
    skipped: int = Field(ge=0)
    blocks: int | None = Field(default=None, ge=0)
    shifts: int | None = Field(default=None, ge=0)
    exact: bool
    raw_bp: float = Field(allow_inf_nan=False)
    drift_bp_day: float = Field(allow_inf_nan=False)
    long_flat_bp: float | None = Field(default=None, allow_inf_nan=False)
    long_flat_ceiling_bp_day: float | None = Field(default=None, allow_inf_nan=False)
    control_r2: float | None = Field(default=None, ge=0, le=1)
    dropped: list[NonEmpty] = Field(default_factory=list)
    folds_adjusted: list[Annotated[float, Field(allow_inf_nan=False)] | None]
    folds_adjusted_same_sign: int = Field(ge=0)
    critical_t: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    mde_bp: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    signal_age_median_s: float = Field(ge=0, allow_inf_nan=False)
    signal_age_max_s: float = Field(ge=0, allow_inf_nan=False)


class Cell(KansoModel):
    """One cell of a result: what it measured, the evidence, and how it was judged.

    `mean`, `se` and `t` are across the sessions the cell held a value in; `p` is its
    family-wise p over its measure's cells. `folds` are the means of its sessions inside
    each of the workspace's calendar folds of the window, `None` for a fold it held none in.
    `staleness` is, per leg, the share of grid intervals the leg did not print in, averaged
    over the sessions. `judged` is absent when the screen declared no verdict.
    """

    measure: int = Field(ge=0)
    id: Literal["lead_lag", "response", "catch_up", "condition"]
    key: NonEmpty
    params: dict[NonEmpty, str | float]
    mean: float = Field(allow_inf_nan=False)
    se: float = Field(ge=0, allow_inf_nan=False)
    t: float = Field(allow_inf_nan=False)
    p: float = Field(gt=0, le=1)
    sessions: int = Field(ge=0)
    folds: list[Annotated[float, Field(allow_inf_nan=False)] | None]
    folds_same_sign: int = Field(ge=0)
    staleness: dict[NonEmpty, Annotated[float, Field(ge=0, le=1)]] = Field(default_factory=dict)
    in_sample_fit: bool = False
    clock_bound: bool = False
    response: ResponseStats | None = None
    catch_up: CatchUpStats | None = None
    condition: ConditionStats | None = None
    judged: Judged | None = None
    reason: str | None = None


class SeriesRead(KansoModel):
    """One series a result read: what it is, the legs or signals that read it, what its clock
    is. `type` is a leg type, or the id of the custom type a signal reads."""

    instrument: NonEmpty
    type: NonEmpty
    resolution: Duration | None = None
    legs: list[Name] = Field(min_length=1)
    timestamps: NonEmpty


class Summary(KansoModel):
    """What a result says as a whole, when a verdict was declared.

    `worth_a_lane` is whether a cell that trades — a `response` or a `catch_up` — passed: a
    lead with no margin is information, not a trade. `best` names the passing cells, the
    strongest first.
    """

    declared: bool
    worth_a_lane: bool | None = None
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    thin: int = Field(default=0, ge=0)
    best: list[NonEmpty] = Field(default_factory=list)


class ScreenResult(Versioned):
    """One measurement of one screen's bytes on one snapshot under one measure library.

    Immutable: the same three pins again return this record rather than a second one.
    `assumptions` states, measure by measure, what that measure's inference rests on;
    `assumption` is the session measures' one, kept so a result recorded before a measure had
    its own still reads.
    """

    screen: HypId
    sha: Sha256
    snapshot: Sha256
    version: NonEmpty
    window: DateWindow
    hyp: HypId | None = None
    hypothesis_sha: Sha256 | None = None
    draws: int = Field(ge=1)
    folds: int = Field(ge=1)
    assumption: NonEmpty
    assumptions: list[NonEmpty] = Field(default_factory=list)
    created_at: datetime
    wall_s: float = Field(ge=0, allow_inf_nan=False)
    peak_mem_gb: float = Field(ge=0, allow_inf_nan=False)
    series: list[SeriesRead]
    cells: list[Cell]
    summary: Summary


def pairs(screen: Screen, measure: LeadLag) -> tuple[tuple[str, str], ...]:
    """The ordered pairs a lead-lag measure judges.

    Every member of `from` against every member of `to`, a series never against itself —
    unless both sides name the one same series, which is its autocorrelation: a spread's
    increments against their own past is mean reversion measured without a model.
    """
    sources = screen.members(measure.from_)
    targets = screen.members(measure.to)
    if sources == targets and len(sources) == 1:
        return ((sources[0], sources[0]),)
    return tuple((a, b) for a in sources for b in targets if a != b)


def followed(screen: Screen, measure: CatchUp) -> tuple[str, ...]:
    """The followers a catch-up trades: every member of `followers` but its leader."""
    return tuple(name for name in screen.members(measure.followers) if name != measure.leader)


def cells(screen: Screen) -> tuple[int, ...]:
    """How many cells each measure holds: the family its correction is over."""
    counts: list[int] = []
    for measure in screen.measures:
        if isinstance(measure, LeadLag):
            counts.append(len(pairs(screen, measure)) * len(measure.lags))
        elif isinstance(measure, CatchUp):
            counts.append(
                len(followed(screen, measure))
                * len(measure.enter_bp)
                * len(measure.close)
                * len(measure.stop)
            )
        elif isinstance(measure, Condition):
            counts.append(len(measure.tests) * len(screen.members(measure.followers)))
        else:
            counts.append(
                len(measure.trigger.thresholds)
                * len(screen.members(measure.followers))
                * len(measure.horizons)
            )
    return tuple(counts)


def _ns(duration: str) -> int:
    return int(parse_duration(duration, "resolution") / timedelta(microseconds=1)) * 1000


def _distinct[T](values: list[T], field: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{field}: a value appears twice")
