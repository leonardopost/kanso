"""`hypothesis.yaml`: the unit of research, and the rules that make one admissible.

The file is the operator's statement of a falsifiable idea plus the classification an
agent writes back into it. Status, pins and the hypothesis's best card live in the state
store, never here, so the file's bytes are stable enough to be content-addressed.

Five families of rule are enforced here, all of them local to the file:

* the windows are ordered, do not overlap, and the certification window opens no earlier
  than the embargo after research closes — `max(5 x horizon, 1d)`, rounded up to whole
  days because the windows are dated. The embargo is what keeps the loop from optimising
  against the data that is meant to judge it, so it is arithmetic, not advice;
* the resolution and the cost model must be answerable from the data the hypothesis asks
  for: bar resolution needs bars, bars beside a tick resolution need the bar size they are
  read at (`bar_resolution`), a quote-derived spread needs quotes, a fixed spread needs its
  width;
* a classified hypothesis carries `strategy_integrity` among its constraints;
* `depth` views a book the hypothesis holds, and nothing else feeds `on_quote_tick`;
* a `period_target` is read in New York, opens at a wall time that exists once on every
  date of the windows, and stands beside no monthly reset, no benchmark and no book.

Whether a universe id resolves to an instrument, whether a construct exists in the
catalogue and whether an objective applies are checked where those catalogues live.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Final, Literal

from pydantic import Field, model_validator

from kanso.schemas.base import (
    CatalogueId,
    HypId,
    KansoModel,
    NonEmpty,
    Params,
    RemedyError,
    Versioned,
)
from kanso.schemas.duration import Duration, Resolution, is_duration, parse_duration
from kanso.schemas.venue import (
    CostsOverride,
    DatedSpread,
    check_dated_paired,
    first_new_york_day,
)

Mechanism = Literal[
    "mean_reversion",
    "momentum",
    "microstructure",
    "stat_arb",
    "event",
    "carry",
    "vol",
    "other",
]

MINIMUM_EMBARGO = timedelta(days=1)
PERCENT = 100.0
EMBARGO_HORIZONS = 5
INTEGRITY_GATE = "strategy_integrity"


def embargo(horizon: str) -> timedelta:
    """`max(5 x horizon, 1d)`: the gap the certification window keeps from research."""
    return max(EMBARGO_HORIZONS * parse_duration(horizon, "horizon"), MINIMUM_EMBARGO)


def embargo_days(horizon: str) -> int:
    """The embargo in whole days, rounded up, because the windows are dated."""
    return math.ceil(embargo(horizon).total_seconds() / 86400)


class DateWindow(KansoModel):
    """A closed span of dates."""

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError(f"end: {self.end} is before start {self.start}")
        return self


class OpenWindow(KansoModel):
    """A span with no end: paper and live replay run from here to whatever exists."""

    start: date


class Windows(KansoModel):
    """The three windows of a hypothesis, ordered and disjoint."""

    research: DateWindow
    certification: DateWindow
    forward: OpenWindow

    def check_embargo(self, horizon: str) -> None:
        """Refuse a certification window that opens inside the embargo."""
        earliest = self.research.end + timedelta(days=embargo_days(horizon))
        if self.certification.start < earliest:
            raise ValueError(
                f"windows.certification.start: {self.certification.start} is inside the "
                "embargo; "
                f"research ends {self.research.end} and the embargo for a {horizon} horizon "
                f"is {embargo_days(horizon)}d, so certification may not open before {earliest}"
            )

    @model_validator(mode="after")
    def _ordered_and_disjoint(self) -> Windows:
        if self.certification.start <= self.research.end:
            raise ValueError(
                f"certification.start: {self.certification.start} overlaps the research window "
                f"ending {self.research.end}"
            )
        if self.forward.start <= self.certification.end:
            raise ValueError(
                f"forward.start: {self.forward.start} overlaps the certification window "
                f"ending {self.certification.end}"
            )
        return self


class RiskLimits(KansoModel):
    """Percentages of the hypothesis capital, plus the leverage ceiling."""

    max_position_pct: float = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0, le=100)
    max_leverage: float = Field(gt=0)


FULL_BOOK: Final = "full_book"
"""The one sizing rule: every entry is the whole budget in one instrument."""


class Sizing(KansoModel):
    """How every order is sized when the hypothesis says so and the strategy may not.

    `full_book`: every entry is the whole budget in exactly one instrument — the budget over
    the price, floored to whole lots, with the round-trip cost reserved inside it — at most
    one instrument held at a time, and every exit the whole position. The harness computes
    each quantity: `submit_entry` and `submit_exit` take no size, and an attached overlay
    names a `Clip` and never a `Hedge`. Operator-owned like `required_constraints`, so
    classification neither reads nor writes it; and scope, so a `best` earned under one
    rule is not compared with a card run under another.
    """

    mode: Literal["full_book"]
    budget: float = Field(gt=0)


class Warmup(KansoModel):
    """The sessions a strategy is fed before its window, so it trades from the open.

    A session is a calendar day on which any instrument of the universe printed at the
    sleeve's grain — the host's, for an attached construct consulted on another one. The
    runner resolves the last `sessions` of them before the window,
    delivers them to the strategy exactly as the window's own points are delivered, and
    drops every order until the first point of the window arrives: the indicators warm,
    nothing fills, and the measured run begins at the open with no cold start. Every path —
    a card, a certificate, a replay, a stage node — warms on the same rule. Scope, like
    `sizing`: a run warmed on one prefix is not compared with a card run cold.
    """

    sessions: int = Field(gt=0)


FIRST_LEG: Final = "first_leg"
"""The one benchmark: the universe's first instrument, bought and held."""


class Benchmark(KansoModel):
    """What the objective has to beat, when the operator says it must beat something.

    `hold: first_leg` is the universe's first instrument bought on the first print of the
    window and held to its close: the run a strategy trading that leg is worth nothing
    beside unless it does better. The runner produces it — the same engine, costs, splits,
    sizing and warmup as the strategy it is measured against, from a sleeve kanso ships —
    and the objective is the strategy's fold-wise Sharpe minus the hold's, fold by fold.
    Operator-owned like `sizing`, so classification neither reads nor writes it, and
    scope: a best that beat nothing is not compared with a card that has to beat a hold,
    and the leg held is part of it, so reordering the universe moves it too.
    """

    hold: Literal["first_leg"]


Reset = Literal["monthly", "none"]


class Book(KansoModel):
    """How the book behaves between fills: a reset, a carry on what it borrows, a floor.

    `reset: monthly` returns the book to `capital` at the first period end of each calendar
    month: a surplus moves into a cushion outside the book, a deficit is restored from that
    cushion while it lasts, and nothing is ever borrowed to restore it. `financing_rate_bps`
    is charged per year on the notional the book holds above its equity — gross exposure,
    shorts included, less what the account is worth — once per return period, by the runner.
    `maintenance_pct` is the floor the `maintenance_margin` gate holds the book to: what the
    end-of-period holdings would be worth at the period's adverse extreme, over their gross,
    as a percentage. Operator-owned like `sizing`, and scope as a whole: every key changes
    the equity path, so a `best` earned under one policy is not compared with a card run
    under another.
    """

    reset: Reset = "none"
    financing_rate_bps: float = Field(default=0.0, ge=0)
    maintenance_pct: float | None = Field(default=None, gt=0, le=100)

    @property
    def funded(self) -> bool:
        """Whether the policy moves money: a reset restores cash, a carry charges it."""
        return self.reset != "none" or self.financing_rate_bps > 0


NS_PER_MS: Final = 1_000_000
MAX_DEPTH_LEVELS: Final = 400
"""The most levels of each side a strategy may be shown: the depth of the sampled channel
the key models. Measured 2026-10-02 by subscribing to a crypto exchange's public depth
channel: 400 levels a side, a change every 100 ms. It is not the deepest book there is — the
same exchange's archives serve 5,000 levels a side, at one second — but a view deeper than
the sampled channel serves is depth an account on it does not see."""


class Depth(KansoModel):
    """What a strategy sees of a level-two book, when it may not see every change of it.

    The venue is handed every change the catalog holds, so queues, fills and the matching
    are those of the full book. The author is handed two things only: the top `levels`
    prices of each side as they stood at each multiple of `every_ms` after the epoch, as a
    batch of the changes since the last one it was handed, and level one — the best bid and
    ask with their sizes — on every instant the top moved, as a quote. That is what an
    account subscribed to an exchange's sampled depth channel and to its top-of-book channel
    sees, and no more. The grid is milliseconds because the duration grammar is whole
    seconds, as `costs.latency_ms` is. Operator-owned like `book`, so classification neither
    reads nor writes it; and scope, so a best that read every change is not compared with a
    card that saw the book a tenth of a second at a time.
    """

    every_ms: int = Field(gt=0)
    levels: int = Field(ge=1, le=MAX_DEPTH_LEVELS)

    @property
    def every_ns(self) -> int:
        """The grid in nanoseconds, the unit every instant of a feed is stamped in."""
        return self.every_ms * NS_PER_MS


NEW_YORK_ZONE: Final = "America/New_York"
"""The one zone a period target is read in (`PeriodTarget`)."""

WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
"""The weekdays a period target's `opens` may name, Monday first, as `date.weekday` counts."""

_OPENS: Final = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) ([01][0-9]|2[0-3]):([0-5][0-9])$")

_FORWARD_CHECKED: Final = timedelta(days=371)
"""How far into the open forward window an `opens` reading is held to existing once: a year
and a week, so it meets every change of clocks the zone's rules make in a year."""


class PeriodTarget(KansoModel):
    """A return a sleeve stops for the rest of its week on reaching.

    The week opens at the weekday and New York wall time `opens` names — `"Mon 04:00"` — and
    runs to the same reading a week on, so daylight saving moves it with the market; the
    weeks are absolute, never cut from a window's start. At every point the harness hands a
    sleeve it reads the sleeve's `balance` against the equity the runner struck at the
    previous week's last point; once the week has made `target_bp` of `capital`, or of that
    opening equity under `of: period_open`, it cancels the sleeve's working orders, flattens
    every name it holds at that name's next print, and drops every entry until the next week
    has opened and every name is flat (`kanso.nautilus.strategy.KansoStrategy`). Every key is
    required and none has a default: it is a rule the operator states, not a threshold kanso
    picks. Operator-owned like `sizing`, so classification neither reads nor writes it; and
    scope, so a best that traded every week is not compared with a card stopped in some.
    """

    period: Literal["week"]
    opens: str
    tz: str
    target_bp: float
    of: Literal["capital", "period_open"]

    @model_validator(mode="before")
    @classmethod
    def _stated(cls, data: object) -> object:
        """Every key is the operator's to state, so a missing one is named with its remedy."""
        if isinstance(data, dict):
            missing = [name for name in cls.model_fields if name not in data]
            if missing:
                raise RemedyError(
                    f"states no {', '.join(missing)}; every key of a period "
                    "target is the operator's, and none has a default",
                    remedy="state period: week, opens (a weekday and New York wall time, "
                    '"Mon 04:00"), tz: America/New_York, target_bp, and of: capital or of: '
                    "period_open",
                )
        return data

    @model_validator(mode="after")
    def _readable(self) -> PeriodTarget:
        if self.tz != NEW_YORK_ZONE:
            raise RemedyError(
                f"tz: {self.tz!r} is not {NEW_YORK_ZONE}; a run's return periods "
                "are cut from a UTC midnight, which meets a week's opening only where no point "
                "falls between them, as on US equities in New York",
                remedy=f"set period_target.tz to {NEW_YORK_ZONE}",
            )
        if _OPENS.match(self.opens) is None:
            raise RemedyError(
                f"opens: {self.opens!r} is not a weekday and a 24-hour wall time",
                remedy='write it as the weekday\'s three letters and HH:MM, "Mon 04:00"',
            )
        if not (math.isfinite(self.target_bp) and self.target_bp > 0):
            raise RemedyError(
                f"target_bp: {self.target_bp:g} is not a return above zero",
                remedy="state the week's return the sleeve stops at, in bp: 1000 is +10%",
            )
        return self

    @property
    def weekday(self) -> int:
        """The weekday the week opens on, Monday 0, as `date.weekday` counts."""
        return WEEKDAYS.index(self.opens[:3])

    @property
    def minute(self) -> int:
        """The New York minute of the day the week opens at, as `minute_of` counts: 04:00 is 240."""
        hours, minutes = self.opens[4:].split(":")
        return int(hours) * 60 + int(minutes)

    def moved_on(self, spans: Sequence[tuple[date, date]]) -> date | None:
        """The first date of `spans` whose opening reading a change of clocks skips or
        repeats, or `None`: one whose UTC offset depends on which side of the change it is
        read on."""
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self.tz)
        wall = time(self.minute // 60, self.minute % 60)
        for first, last in spans:
            day = first + timedelta(days=(self.weekday - first.weekday()) % 7)
            while day <= last:
                at = datetime.combine(day, wall, tzinfo=zone)
                if at.utcoffset() != at.replace(fold=1).utcoffset():
                    return day
                day += timedelta(days=7)
        return None


class ConstructRef(KansoModel):
    """What `classify` decided this hypothesis is, in portfolio-construction terms."""

    id: CatalogueId
    host: HypId | None = None
    params: Params | None = None
    rationale: str | None = Field(default=None, max_length=240)


class ObjectiveParams(KansoModel):
    """The keep rule: an improvement counts when it clears both."""

    min_delta: float = Field(ge=0)
    k_se: float = Field(gt=0)


class ObjectiveRef(KansoModel):
    """The one scalar a run optimises, with the keep rule's two parameters."""

    id: CatalogueId
    params: ObjectiveParams


class ConstraintRef(KansoModel):
    """A card-stage gate and the values chosen for it."""

    id: CatalogueId
    params: Params = Field(default_factory=dict)


class SessionScope(KansoModel):
    """Which sessions each instrument's market data is delivered on, read off a required series.

    `series` names a custom type the hypothesis requires, filed per instrument with one point
    per session and stamped before the session's first market point; `flag` is an integer
    field of it. An instrument's bars, quotes and trades of a session are delivered only when
    its point of that session carries the flag above zero, and an instrument named in
    `always` is delivered every session whatever its point says. So a pool of a thousand
    names can be researched with only the few in play each session in memory, chosen by a
    rule written down before the session opened. The runner loads nothing outside the scope,
    and the strategy base drops any point outside it that a node delivers, so a card, a
    certificate, a replay and a stage see the same market. Scope, like `warmup`: changing it
    clears `best`.
    """

    series: CatalogueId
    flag: NonEmpty
    always: list[NonEmpty] = Field(default_factory=list)


MARKET_TYPES: Final = ("bar", "quote", "trade", "book")


class Hypothesis(Versioned):
    """A falsifiable idea, its data requirements, its windows and its classification."""

    id: HypId
    title: NonEmpty
    thesis: NonEmpty
    mechanism: Mechanism
    universe: list[NonEmpty] = Field(min_length=1)
    horizon: Duration
    resolution: Resolution
    bar_resolution: Duration | None = None
    """The bar size a `bar` requirement is read at when `resolution` is not one: a sleeve
    clocked on quotes, prints or a book beside instruments that carry only bars — a future,
    an index, a pair — read at this size. Required there whenever `bar` is, and refused
    wherever `resolution` is itself a bar size, where the bars are read at that. Scope, like
    `data_requirements`: changing it clears `best`."""
    data_requirements: list[CatalogueId] = Field(min_length=1)
    data_by_instrument: dict[NonEmpty, list[CatalogueId]] = Field(default_factory=dict)
    session_scope: SessionScope | None = None
    costs: CostsOverride | None = None
    capital: float | None = Field(default=None, gt=0)
    sizing: Sizing | None = None
    risk_limits: RiskLimits
    windows: Windows
    warmup: Warmup | None = None
    benchmark: Benchmark | None = None
    book: Book | None = None
    period_target: PeriodTarget | None = None
    depth: Depth | None = None
    derived_from: list[HypId] = Field(default_factory=list)
    """The hypotheses whose research chose this one — the parent an `explore` draft was
    written from, or the searched sibling an operator drafted a variation of. A trial is a
    card the selection could have kept, and the cards that chose a draft are among them: the
    search a certificate prices, the trials `edge_per_trade` holds a card to and the
    `n_trials` a certificate is stamped with count every card of the hypothesis and of every
    hypothesis it derives from, transitively. A hypothesis that names none counts its own.
    Operator-owned: classification neither reads nor writes it, and `kanso hyp validate`
    refuses a name the workspace has not registered."""
    required_constraints: list[ConstraintRef] | None = None
    construct_: ConstructRef | None = Field(default=None, alias="construct")
    objective: ObjectiveRef | None = None
    constraints: list[ConstraintRef] | None = None

    @property
    def construct(self) -> ConstructRef | None:  # type: ignore[override]
        """The classification, if `classify` has run. Aliased: `construct` is taken."""
        return self.construct_

    @property
    def card_gates(self) -> tuple[ConstraintRef, ...]:
        """Every card-stage gate this hypothesis is judged by, the operator's first.

        `constraints` is the classifier's, rewritten on every classification.
        `required_constraints` is the operator's, and classification does not read or
        write it: `classify` owns three keys of the file and this is a fourth, so it
        survives a re-classification byte for byte. Where both name a gate the operator's
        entry stands and the classifier's is dropped, which is the whole of the rule —
        a model may add to what an operator requires and may not remove or reprice it.
        """
        required = tuple(self.required_constraints or ())
        named = {ref.id for ref in required}
        return required + tuple(ref for ref in self.constraints or () if ref.id not in named)

    @property
    def embargo(self) -> timedelta:
        """The gap this hypothesis's horizon requires between research and certification."""
        return embargo(self.horizon)

    @model_validator(mode="after")
    def _validate(self) -> Hypothesis:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe: repeats an id")
        if len(set(self.data_requirements)) != len(self.data_requirements):
            raise ValueError("data_requirements: repeats a type")
        if parse_duration(self.horizon, "horizon") <= timedelta(0):
            raise ValueError("horizon: must be longer than zero")
        if len(set(self.derived_from)) != len(self.derived_from):
            raise ValueError("derived_from: repeats an id")
        if self.id in self.derived_from:
            raise ValueError(f"derived_from: {self.id!r} cannot derive from itself")
        self.windows.check_embargo(self.horizon)
        self._check_resolution()
        self._check_by_instrument()
        self._check_scope()
        self._check_costs()
        self._check_book()
        self._check_period_target()
        self._check_depth()
        self._check_constraints()
        return self

    def _check_period_target(self) -> None:
        """A period target the rest of the file lets the harness apply as it is written.

        A monthly reset moves cash at a return period's end, which can fall inside a target
        week and move the balance the target reads with no P&L. The benchmark's hold buys once
        and never again, so under the target it would be flattened at its first stopped week
        and sit in cash for the rest of the window. A book's changes reach no point the
        harness reads the target at. And the week's opening reading exists exactly once on
        every date of the windows — the open forward window's first year standing for the
        rest of it — or a change of clocks would move it.
        """
        target = self.period_target
        if target is None:
            return
        if self.book is not None and self.book.reset == "monthly":
            raise RemedyError(
                "period_target: declared beside book.reset: monthly, whose transfer at a "
                "return period's end can fall inside a target week and move the balance the "
                "target reads with no P&L",
                remedy="set book.reset to none, or drop period_target",
            )
        if self.benchmark is not None:
            raise RemedyError(
                "period_target: declared beside benchmark, whose hold buys once and never "
                "again, so the target would flatten it at its first stopped week and leave it "
                "in cash for the rest of the window",
                remedy="drop benchmark, or drop period_target",
            )
        if "book" in self.data_requirements:
            raise RemedyError(
                "period_target: declared on a hypothesis that requires 'book', whose changes "
                "reach no point the harness reads the target at",
                remedy="drop book from data_requirements, or drop period_target",
            )
        forward = self.windows.forward.start
        moved = target.moved_on(
            (
                (self.windows.research.start, self.windows.research.end),
                (self.windows.certification.start, self.windows.certification.end),
                (forward, forward + _FORWARD_CHECKED),
            )
        )
        if moved is not None:
            raise RemedyError(
                f"period_target.opens: {target.opens} {target.tz} does not exist, or exists "
                f"twice, on {moved}, when the clocks change",
                remedy="open the week at a wall time no change of clocks skips or repeats: "
                '"Mon 04:00" never is',
            )

    def _check_scope(self) -> None:
        """A scope reads a series the hypothesis requires, and exempts names it holds."""
        scope = self.session_scope
        if scope is None:
            return
        if scope.series in MARKET_TYPES or scope.series not in self.data_requirements:
            raise ValueError(
                f"session_scope.series: {scope.series!r} must be a custom type named in "
                "data_requirements, so its points are loaded and delivered"
            )
        if len(set(scope.always)) != len(scope.always):
            raise ValueError("session_scope.always: repeats an id")
        outside = [name for name in scope.always if name not in self.universe]
        if outside:
            raise ValueError(f"session_scope.always: {', '.join(outside)} is not in the universe")

    def _check_book(self) -> None:
        """A maintenance floor the leverage ceiling can stand on.

        A book fully levered to `max_leverage` holds equity of `100 / max_leverage` percent
        of its gross before any price moves, so a floor above that would be breached by the
        first entry the limits admit.
        """
        if self.book is None or self.book.maintenance_pct is None:
            return
        at_entry = PERCENT / self.risk_limits.max_leverage
        if at_entry < self.book.maintenance_pct:
            raise ValueError(
                f"book.maintenance_pct: {self.book.maintenance_pct:g}% is above the "
                f"{at_entry:g}% a book levered to max_leverage {self.risk_limits.max_leverage:g} "
                "holds at entry, so the first entry the limits admit would breach it"
            )

    def _check_depth(self) -> None:
        """Depth is a view of a book the hypothesis holds, and the only source of its quotes.

        Level one reaches `on_quote_tick`, so a quote series beside it would be a second
        source of the same handler that neither the author nor a replay could tell apart.
        """
        if self.depth is None:
            return
        if "book" not in self.data_requirements:
            raise ValueError(
                "depth: needs 'book' in data_requirements; depth is what the strategy sees of "
                "a level-two book the hypothesis holds"
            )
        if "quote" in self.data_requirements:
            raise ValueError(
                "depth: refuses 'quote' in data_requirements; under depth level one reaches "
                "on_quote_tick from the book, and a quote series would be a second source of it"
            )

    def _check_by_instrument(self) -> None:
        """`data_by_instrument` narrows what a listed instrument must carry; it adds nothing.

        A universe that mixes sources may hold a type for some instruments and not others —
        a crypto exchange's prints beside the quotes of the equities that follow it, where
        the exchange serves no quotes. A listed instrument is asked for its own list, which
        must be a subset of `data_requirements`; an unlisted one for all of them; and a
        required type nobody is asked for is a type the hypothesis does not need.
        """
        unknown = sorted(set(self.data_by_instrument) - set(self.universe))
        if unknown:
            raise ValueError(f"data_by_instrument: {', '.join(unknown)} is not in the universe")
        for instrument, kinds in self.data_by_instrument.items():
            if not kinds:
                raise ValueError(f"data_by_instrument.{instrument}: names no type")
            if len(set(kinds)) != len(kinds):
                raise ValueError(f"data_by_instrument.{instrument}: repeats a type")
            extra = sorted(set(kinds) - set(self.data_requirements))
            if extra:
                raise ValueError(
                    f"data_by_instrument.{instrument}: {', '.join(extra)} is not in "
                    "data_requirements; list it there, or drop it here"
                )
        asked = {kind for instrument in self.universe for kind in self.required_of(instrument)}
        unused = sorted(set(self.data_requirements) - asked)
        if unused:
            raise ValueError(
                f"data_requirements: no instrument is asked for {', '.join(unused)} once "
                "data_by_instrument is read; drop it, or ask an instrument for it"
            )

    def unpriced(self, rows: Sequence[DatedSpread], day: date) -> list[str]:
        """The names this hypothesis trades — every name asked for `bar`, `quote` or `trade` —
        for which no row of a dated spread table is in force on `day`."""
        return [
            name
            for name in self.universe
            if {"bar", "quote", "trade"} & set(self.required_of(name))
            and not any(row.instrument == name and row.from_ <= day for row in rows)
        ]

    def required_of(self, instrument: str) -> tuple[str, ...]:
        """The types `instrument` must carry: its own list, else every required type."""
        return tuple(self.data_by_instrument.get(instrument, self.data_requirements))

    @property
    def bar_size(self) -> str:
        """The bar size this hypothesis's bars are read at: `bar_resolution` where it states
        one, else `resolution` — a bar size wherever `bar` is required."""
        return self.bar_resolution or self.resolution

    @property
    def grain(self) -> str:
        """The resolution as an operator reads it: beside a tick one, with the size its bars
        are read at — `tick, bars at 1m`."""
        if self.bar_resolution is None:
            return self.resolution
        return f"{self.resolution}, bars at {self.bar_resolution}"

    def _check_resolution(self) -> None:
        required = self.data_requirements
        if is_duration(self.resolution):
            if parse_duration(self.resolution, "resolution") <= timedelta(0):
                raise ValueError("resolution: must be longer than zero")
            if "bar" not in required:
                raise ValueError(
                    f"data_requirements: resolution {self.resolution} is a bar size, so 'bar' "
                    "must be required"
                )
        elif self.resolution == "tick":
            if not ({"trade", "quote", "book"} & set(required)):
                raise ValueError(
                    "data_requirements: resolution 'tick' needs 'trade', 'quote' or 'book' to "
                    "be required"
                )
        elif self.resolution not in required:
            raise ValueError(
                f"data_requirements: resolution {self.resolution!r} must also be required"
            )
        self._check_bar_resolution()

    def _check_bar_resolution(self) -> None:
        """Bars are read at a bar size: the resolution's, or the one `bar_resolution` states.

        A sleeve clocked on quotes, prints or a book has no bar size of its own, so a `bar`
        requirement beside one names the size its bars are read at, or nothing would say what
        size to read, subscribe or snapshot them at. A bar-size resolution is the size its
        bars are read at, and a second would leave the file saying two things.
        """
        stated = self.bar_resolution
        if is_duration(self.resolution):
            if stated is not None:
                raise RemedyError(
                    f"bar_resolution: resolution {self.resolution} is a bar size, and the bars "
                    "are read at it",
                    remedy=f"drop bar_resolution (resolution {self.resolution} is the bar "
                    "size), or keep it and set resolution to tick, quote or trade",
                )
            return
        if "bar" in self.data_requirements and stated is None:
            raise RemedyError(
                f"bar_resolution: resolution {self.resolution!r} is no bar size and 'bar' is "
                "required, so nothing says what size the bars are read at",
                remedy="state bar_resolution, the size the bar instruments carry (1m, say), or "
                "drop bar from data_requirements",
            )
        if stated is None:
            return
        if "bar" not in self.data_requirements:
            raise RemedyError(
                f"bar_resolution: {stated} names a bar size and 'bar' is not required",
                remedy="add bar to data_requirements, and ask the instruments that carry bars "
                "for it under data_by_instrument; or drop bar_resolution",
            )
        if parse_duration(stated, "bar_resolution") <= timedelta(0):
            raise ValueError("bar_resolution: must be longer than zero")

    def _check_costs(self) -> None:
        if self.costs is None:
            return
        if self.costs.spread == "quotes" and "quote" not in self.data_requirements:
            raise ValueError(
                "costs.spread: 'quotes' needs 'quote' in data_requirements to read a spread from"
            )
        if self.costs.spread == "fixed_bps" and self.costs.fixed_bps is None:
            raise ValueError("costs.fixed_bps: required when spread is fixed_bps")
        check_dated_paired(self.costs.spread, self.costs.dated)
        if self.costs.dated is not None:
            self._check_dated(self.costs.dated)

    def _check_dated(self, rows: Sequence[DatedSpread]) -> None:
        """A dated table names the universe's instruments, states no row in force from after
        the research window's last day, and holds a row in force for each name the hypothesis
        trades — every name asked for `bar`, `quote` or `trade` — from the first New York date
        a run of it can read one on, or its fills there would be charged no spread.

        A row from after research is refused because the reserve a sleeve sizes with reads
        every row of every year (`kanso.nautilus.costs.dated_reserve_bps`), and no research
        fill is charged one: a row in force from the embargo or the certification window on —
        the certification year's own, a median of held-out sessions, in force from its first
        day — would hand research a width measured on the held-out window and charge it
        nothing. The last row on or before research's last day stays in force through
        certification instead. What sessions a row was measured on the file does not say, so a
        row in force within research that was measured past it is not refused here. The first
        date read is the New York evening before
        the first UTC day delivered (`first_new_york_day`), and with a warmup of N sessions
        the first day delivered is at least N days before the window, a session being a day
        that printed: that is as far as the file can say, and the sessions the catalog
        resolves are held to the table where they are known — by `hyp validate` when the
        catalog holds them, and where a run is built (`kanso.nautilus.backtest.RunRequest`)."""
        outside = sorted({row.instrument for row in rows} - set(self.universe))
        if outside:
            raise RemedyError(
                f"costs.dated: names {', '.join(outside)}, which is not in the universe",
                remedy="drop the rows of instruments the hypothesis does not trade, or add them "
                "to the universe",
            )
        ends = self.windows.research.end
        late = sorted({row.instrument for row in rows if row.from_ > ends})
        if late:
            raise RemedyError(
                f"costs.dated: {', '.join(late)} has a row from after the research window, "
                f"which ends {ends}; no research fill pays it, and the reserve a sleeve sizes "
                "with reads every row, so research would read a width measured where it may "
                "not look",
                remedy=f"drop the rows from after {ends}: the last row on or before it stays "
                "in force through certification",
            )
        start = self.windows.research.start
        sessions = 0 if self.warmup is None else self.warmup.sessions
        day = first_new_york_day(start - timedelta(days=sessions))
        uncovered = self.unpriced(rows, day)
        if uncovered:
            reached = (
                "the research window's first instant falls on"
                if sessions == 0
                else f"a warmup of {sessions} session(s) before {start} delivers on at the latest"
            )
            raise RemedyError(
                f"costs.dated: {', '.join(uncovered)} has no row in force on {day}, the New "
                f"York date {reached}, so its fills there would be charged no spread",
                remedy=f"state a row from {day} or earlier for {', '.join(uncovered)}",
            )

    def _check_constraints(self) -> None:
        if self.constraints is None:
            return
        ids = [c.id for c in self.constraints]
        if len(set(ids)) != len(ids):
            raise ValueError("constraints: repeats a gate id")
        if INTEGRITY_GATE not in ids:
            raise ValueError(
                f"constraints: {INTEGRITY_GATE} is required of every classified hypothesis"
            )
