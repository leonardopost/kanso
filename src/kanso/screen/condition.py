"""`condition`: whether a follower's session returns differ while a declared signal meets a
declared comparison, against the round trip a position pays.

A cell is a test and a follower, a catalog leg. Its sessions are the window's days in the
clock's zone on which the follower has a point, and in each of them two marks are kept: the
**open**, its value as of its first instant in the session, and the **close**, its last point
(`marks`). For 1m bars stamped at their close, a US regular session is `span: "09:31-16:01"`.

1. **Periods, named by their exit session.** A test enters at one session's open or close and
   exits at one: `open → close` is a session's own, and `close → close`, `open → open` and
   `close → open` run from the follower's previous session into this one — the only returns a
   screen takes across a session boundary. A period is valid when its entry exists, comes
   before its exit, and, across a boundary, no day between the two sessions is one the
   follower's market opened by the calendar on file (`kanso.data.closures`), checked only inside
   the span the calendar holds and not at all with none on file; an invalid one is counted
   `unfilled` and scored nowhere. Its
   return is the change of log price, in basis points.
2. **The decision, as of the latency before the entry.** The signal is read as of the entry's
   instant less `latency_ms` (`kanso.screen.signals`): the newest point published by then, never
   one after, and none before the window opened. The state is `when` where the favoured
   comparison holds, else `against` where the contrast's other state does, else neither; it is
   undefined where no point has been published yet, and such a period holds no value — never a
   zero. A control is read as of the period's exit — the session's open or close, as the test
   exits — because it describes the session the return is scored in: the point read, its
   `ts_init` at or before that instant, must carry its `ts_event` on the session's date in the
   clock's zone and hold exactly 0 or 1. A table stamped at midnight UTC on its date is dated the
   day before west of Greenwich, and is refused there.
3. **One position at a time.** Under `hold: while` the position is re-decided at every entry:
   the test's side while `when` holds, the other side while `against` does, flat otherwise.
   Under `hold: {sessions: k}` an onset — the state beginning, from a defined other one — opens
   a position held k sessions of the follower's calendar, valid or not, and an onset while it
   is held is skipped and counted.
4. **Demean, once a cell.** The held periods' returns are regressed on the declared calendar
   design and controls, with the state not in it (`kanso.screen.demean`). Long or flat, the
   estimate is the mean of position times residual, the excess a session over the calendar's
   mean, and a session's value is its position less the cell's mean position times its
   residual, which sums to the same; under a contrast it is the mean residual long less the
   mean residual short, so a correctly signed effect is positive under either, and a verdict
   passes no cell at or under zero, its p being two-sided (`kanso.screen.run`). Under circular
   shifts each weight is residualised on the design as the returns were, so the values spread
   as the estimate does (`kanso.screen.nulls.values_of`). Under sign flips it is not: a
   month's residuals, and its residualised weights, sum to zero over the month, so their block
   sums share its fitted mean across the blocks it spans, spread less than their total, and
   run the null above its level — measured with weekday and month dummies over 250 sessions in
   blocks of five, contrast positions in runs averaging ten, 6.95% of p fell at or under 0.05,
   against 3.05% on the weights alone, which overstate the standard error by about
   `1 / √(1 − R²)` and err conservative. A contrast's `sessions` are those in either state.
5. **Round trips, by position.** A position is a maximal run of held sessions with one
   direction; across sessions it runs on only when its periods are contiguous — the exit of one
   is the entry of the next — so a state held k sessions close to close is one round trip, and
   open to close it is k. Each is charged `Hurdles.round_trip` struck at its entry and its exit,
   however many sessions apart, and its gross is what a taker made between the two: a quoted
   follower buys the ask and sells the bid it showed. Net is booked by session, the round trip
   on the session the position left in.
6. **Judge** the measure's cells together: their values summed into declared blocks of the
   sessions any of them held and sign-flipped, or their positions shifted round the calendar
   against their residuals (`kanso.screen.nulls`). A shift rotates one index, so a circular
   family holds only the sessions every one of its cells holding any holds, and a cell that
   held more gives the rest up, from its residuals to its round trips; a cell that holds none
   takes none from the others.

NautilusTrader facts relied on (nautilus_trader 1.231.0): a custom point read from the catalog
arrives wrapped in `CustomData`, and a query filters on `ts_init` inclusively at both ends
(`kanso.nautilus.facts`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from kanso.data import closures
from kanso.errors import PreconditionError
from kanso.schemas.screen import (
    Cell,
    Comparison,
    Condition,
    ConditionStats,
    ConditionTest,
    FixedHold,
    ResponseStats,
    Screen,
    TurnOfMonth,
    p_floor,
)
from kanso.screen import demean, nulls, signals
from kanso.screen.hurdle import TOUCHED, Hurdles
from kanso.screen.response import BP, taker_gross
from kanso.screen.sessions import Series, zone_of

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.screen.run import Context

NS_PER_SECOND: Final = 1_000_000_000
WHEN: Final = 1
AGAINST: Final = -1
NEITHER: Final = 0

SIGN_FLIP: Final = "blocks of {block} sessions are roughly independent of each other"
CIRCULAR: Final = (
    "with no relationship, every circular shift of a cell's positions against its demeaned "
    "returns is as likely as the one observed"
)


@dataclass(frozen=True)
class Mark:
    """One instant of a follower: its price, and the touch where it shows one."""

    t: int
    price: float
    bid: float | None = None
    ask: float | None = None


Marks = tuple[Mark, Mark]
"""A follower's open and close in one session."""


@dataclass(frozen=True)
class CellKey:
    """One cell: its name in a result, its test, its follower."""

    name: str
    test: str
    follower: str
    params: dict[str, str | float] = field(default_factory=dict)


def assumption(measure: Condition) -> str:
    """What a condition's inference rests on, stated in its result."""
    if measure.block is not None:
        return SIGN_FLIP.format(block=measure.block)
    return CIRCULAR


def cells(screen: Screen, measure: Condition) -> tuple[CellKey, ...]:
    """Every cell of the measure, test by follower, in the order declared."""
    found: list[CellKey] = []
    for name, test in measure.tests.items():
        for follower in screen.members(measure.followers):
            params: dict[str, str | float] = {
                "test": name,
                "signal": test.signal,
                "when": test.when.text,
                "entry": test.entry,
                "exit": test.exit,
                "hold": "while" if test.hold == "while" else f"{_held(test)} sessions",
                "side": test.side,
                "follower": follower,
                "payoff": measure.payoff,
                "null_by": measure.null_by,
                "latency_ms": measure.latency_ms,
            }
            if isinstance(test.against, Comparison):
                params["against"] = test.against.text
            elif test.against is not None:
                params["against"] = test.against
            key = f"condition/{name}/{follower}/{measure.null_by}"
            found.append(CellKey(key, name, follower, params))
    return tuple(found)


def marks(screen: Screen, measure: Condition, series: Mapping[str, Series]) -> list[Marks | None]:
    """Each follower's open and close in one session, `None` where it has no point."""
    found: list[Marks | None] = []
    for follower in screen.members(measure.followers):
        leg = series[follower]
        if leg.empty:
            found.append(None)
            continue
        first = int(np.searchsorted(leg.ts, leg.ts[0], side="right")) - 1
        found.append((_mark(leg, first), _mark(leg, len(leg.ts) - 1)))
    return found


def refuse_uncalendared(
    screen: Screen, definitions: Mapping[str, Any], window: tuple[date, date]
) -> None:
    """Refuse a turn of the month or a pre-holiday control no calendar on file can read."""
    first = window[0].replace(day=1)
    last = _month_end(window[1])
    for index, measure in enumerate(screen.measures):
        if not isinstance(measure, Condition):
            continue
        needs = [
            "turn_of_month" if isinstance(item, TurnOfMonth) else item
            for item in measure.demean
            if isinstance(item, TurnOfMonth) or item == "pre_holiday"
        ]
        if not needs:
            continue
        for follower in screen.members(measure.followers):
            calendar = _calendar(definitions[screen.legs[follower].instrument])
            if calendar is not None and calendar.first <= first and last <= calendar.last:
                continue
            held = (
                "none is on file"
                if calendar is None
                else f"the one on file holds {calendar.first}..{calendar.last}"
            )
            raise PreconditionError(
                f"measures.{index}.demean: {' and '.join(needs)} read {follower}'s market "
                f"calendar over every month the window touches, {first}..{last}, and {held}",
                remedy="drop them from demean, or supply them as 0/1 controls",
            )


@dataclass(frozen=True)
class _Follower:
    """One follower's sessions: the day index and date of each, its marks there, its calendar."""

    days: list[int]
    dates: list[date]
    marks: list[Marks]
    calendar: closures.Calendar | None


@dataclass(frozen=True)
class _Plan:
    """One cell's periods, one a session of its follower's, before any value is read."""

    key: CellKey
    test: ConditionTest
    follower: _Follower
    entries: list[Mark | None]
    exits: list[Mark]
    valid: list[bool]


@dataclass(frozen=True)
class _States:
    """Each of a cell's sessions' decision, and how old the point it read was, in seconds."""

    state: list[int | None]
    ages: np.ndarray


@dataclass(frozen=True)
class _Realised:
    """One realisation of a cell's positions over its held sessions, and what it came to.

    `held` are positions in the follower's sessions; `days` the window's day indices of them;
    `spanned` the orthonormal vectors the returns were demeaned on beyond the intercept.
    """

    held: list[int]
    days: list[int]
    positions: np.ndarray
    residuals: np.ndarray
    spanned: np.ndarray
    estimate: float
    raw: float
    drift: float
    stats: ResponseStats
    net: np.ndarray
    r2: float | None
    dropped: list[str]
    folds: list[float | None]


@dataclass(frozen=True)
class _Found:
    """One cell before its null: its plan, its decisions and its realisations."""

    plan: _Plan
    states: _States
    skipped: int
    realised: _Realised
    lone: _Realised | None


def measure(
    context: Context,
    index: int,
    measure: Condition,
    days: Sequence[date],
    sessions: Sequence[list[Marks | None]],
    fold_of: np.ndarray,
    catalog: Any,
    hurdles: Hurdles,
) -> list[Cell]:
    """Every cell of a condition measure, judged together by its declared null."""
    screen = context.screen
    followers = {
        name: _follower(context, name, position, days, sessions)
        for position, name in enumerate(screen.members(measure.followers))
    }
    plans = [
        _plan(measure.tests[key.test], key, followers[key.follower])
        for key in cells(screen, measure)
    ]
    latency = int(round(measure.latency_ms * 1_000_000))
    decided: list[tuple[_Plan, _States, np.ndarray, np.ndarray, int]] = []
    for plan, (read, controls) in zip(
        plans, _reads(catalog, context, measure, plans, latency), strict=True
    ):
        states = _states(plan, read, latency)
        gated = _controls(context, measure, plan, controls)
        positions, skipped = _positions(plan.test, states.state, measure.payoff)
        decided.append((plan, states, gated, positions, skipped))
    shared = None
    if measure.block is None:
        shared = _shared(
            [
                {plan.follower.days[at] for at in _holding(plan, positions, gated)}
                for plan, _, gated, positions, _ in decided
            ]
        )
    found: list[_Found] = []
    for plan, states, gated, positions, skipped in decided:
        realised = _realise(
            context, measure, plan, days, positions, gated, hurdles, fold_of, measure.payoff, shared
        )
        lone = None
        if measure.payoff == "contrast":
            flat, _ = _positions(plan.test, states.state, "long_flat")
            lone = _realise(
                context, measure, plan, days, flat, gated, hurdles, fold_of, "long_flat", shared
            )
        found.append(_Found(plan, states, skipped, realised, lone))
    union = sorted({day for item in found for day in item.realised.days})
    column = {day: position for position, day in enumerate(union)}
    positions_by = np.full((len(found), len(union)), np.nan, dtype=np.float64)
    residuals_by = np.full((len(found), len(union)), np.nan, dtype=np.float64)
    shifted = measure.block is None
    width = max((len(item.realised.spanned) for item in found), default=0) if shifted else 0
    spanned_by = np.zeros((len(found), width, len(union)), dtype=np.float64)
    for row, item in enumerate(found):
        at = [column[day] for day in item.realised.days]
        positions_by[row, at] = item.realised.positions
        residuals_by[row, at] = item.realised.residuals
        if shifted:
            vectors = spanned_by[row, : len(item.realised.spanned)]
            vectors[:, at] = item.realised.spanned
    contrast = measure.payoff == "contrast"
    seeded = nulls.seed(context.sha, context.snapshot_id, index)
    values = nulls.values_of(positions_by, residuals_by, contrast, spanned_by)
    carried = ~np.isnan(values)
    if measure.block is not None:
        evidence = nulls.evidence(nulls.blocks(values, measure.block), context.draws, seeded)
    else:
        evidence = nulls.circular(
            positions_by, residuals_by, contrast, context.draws, seeded, spanned_by
        )
    counts = np.add.reduce(carried, axis=1)
    reported = np.add.reduce(carried & (positions_by != 0), axis=1) if contrast else counts
    return [
        _cell(
            context,
            index,
            measure,
            item,
            evidence,
            row,
            (int(counts[row]), int(reported[row])),
            len(union),
            fold_of,
        )
        for row, item in enumerate(found)
    ]


def _follower(
    context: Context,
    name: str,
    position: int,
    dates: Sequence[date],
    sessions: Sequence[list[Marks | None]],
) -> _Follower:
    days: list[int] = []
    found: list[Marks] = []
    for day, session in enumerate(sessions):
        held = session[position]
        if held is not None:
            days.append(day)
            found.append(held)
    instrument = context.screen.legs[name].instrument
    calendar = _calendar(context.definitions[instrument])
    return _Follower(days, [dates[day] for day in days], found, calendar)


def _plan(test: ConditionTest, key: CellKey, follower: _Follower) -> _Plan:
    """Each session's period: its entry, its exit, and whether it can be scored."""
    same = test.entry == "open" and test.exit == "close"
    entries: list[Mark | None] = []
    exits: list[Mark] = []
    valid: list[bool] = []
    for position, (opened, closed) in enumerate(follower.marks):
        exit_ = closed if test.exit == "close" else opened
        entry: Mark | None = opened
        if not same:
            before = follower.marks[position - 1] if position else None
            entry = None if before is None else before[1] if test.entry == "close" else before[0]
        ok = entry is not None and entry.t < exit_.t
        entries.append(entry)
        exits.append(exit_)
        valid.append(ok and (same or not _opened_between(follower, position)))
    return _Plan(key, test, follower, entries, exits, valid)


def _opened_between(follower: _Follower, position: int) -> bool:
    """Whether the market opened on a day strictly between two sessions of the follower, by
    the calendar on file and only inside the span it holds."""
    calendar = follower.calendar
    if calendar is None:
        return False
    day = follower.dates[position - 1] + timedelta(days=1)
    while day < follower.dates[position]:
        if calendar.first <= day <= calendar.last and not calendar.closed(day):
            return True
        day += timedelta(days=1)
    return False


def _reads(
    catalog: Any,
    context: Context,
    measure: Condition,
    plans: Sequence[_Plan],
    latency: int,
) -> list[tuple[signals.Read, dict[str, signals.Read]]]:
    """Every cell's signal as of each decision, and its controls as of each exit, read once."""
    wanted: dict[str, list[np.ndarray]] = {}
    for plan in plans:
        decisions = [-1 if entry is None else entry.t - latency for entry in plan.entries]
        wanted.setdefault(plan.test.signal, []).append(np.asarray(decisions, dtype=np.int64))
        exits = np.asarray([mark.t for mark in plan.exits], dtype=np.int64)
        for control in measure.controls:
            wanted.setdefault(control, []).append(exits)
    joined = {name: np.concatenate(parts) for name, parts in wanted.items()}
    read = signals.asof(catalog, context.screen, joined, context.window)
    offsets = dict.fromkeys(wanted, 0)

    def taken(name: str, size: int) -> signals.Read:
        start, offsets[name] = offsets[name], offsets[name] + size
        whole = read[name]
        end = start + size
        return signals.Read(
            whole.value[start:end], whole.ts_init[start:end], whole.ts_event[start:end]
        )

    found: list[tuple[signals.Read, dict[str, signals.Read]]] = []
    for plan in plans:
        size = len(plan.entries)
        state = taken(plan.test.signal, size)
        found.append((state, {control: taken(control, size) for control in measure.controls}))
    return found


def _states(plan: _Plan, read: signals.Read, latency: int) -> _States:
    """Each session's state, `None` where no period was entered or no point was published."""
    test = plan.test
    state: list[int | None] = []
    ages: list[float] = []
    for entry, value, stamp in zip(
        plan.entries, read.value.tolist(), read.ts_init.tolist(), strict=True
    ):
        if entry is None or value != value:
            state.append(None)
            ages.append(np.nan)
            continue
        against = test.against
        if test.when.holds(value):
            state.append(WHEN)
        elif against == "rest" or (isinstance(against, Comparison) and against.holds(value)):
            state.append(AGAINST)
        else:
            state.append(NEITHER)
        ages.append((entry.t - latency - stamp) / NS_PER_SECOND)
    return _States(state, np.asarray(ages, dtype=np.float64))


def _positions(
    test: ConditionTest, state: Sequence[int | None], payoff: str
) -> tuple[np.ndarray, int]:
    """Each session's position, NaN where undefined, and the onsets a fixed hold skipped.

    Long or flat, `against` is flat: the realisation reported beside a contrast holds the
    favoured state alone.
    """
    side = 1.0 if test.side == "long" else -1.0
    states = [NEITHER if payoff == "long_flat" and one == AGAINST else one for one in state]
    found = np.full(len(states), np.nan, dtype=np.float64)
    if test.hold == "while":
        for position, one in enumerate(states):
            if one is not None:
                found[position] = side * one
        return found, 0
    remaining, held, skipped = 0, 0.0, 0
    for position, one in enumerate(states):
        before = states[position - 1] if position else None
        onset = one is not None and one != NEITHER and before is not None and before != one
        if remaining > 0:
            found[position] = held
            remaining -= 1
            skipped += int(onset)
        elif onset:
            assert one is not None
            held = side * one
            found[position] = held
            remaining = _held(test) - 1
        elif one is not None:
            found[position] = 0.0
    return found, skipped


def _controls(
    context: Context, measure: Condition, plan: _Plan, reads: Mapping[str, signals.Read]
) -> np.ndarray:
    """Each session's controls, sessions by controls, NaN where one has no point yet; a point
    whose `ts_event` falls on another date in the clock's zone, or holding anything but 0 or 1,
    is refused."""
    found = np.full((len(plan.exits), len(measure.controls)), np.nan, dtype=np.float64)
    zone = zone_of(context.screen)
    for column, name in enumerate(measure.controls):
        read = reads[name]
        for position, valid in enumerate(plan.valid):
            value = float(read.value[position])
            if not valid or value != value:
                continue
            day = plan.follower.dates[position]
            stamp = int(read.ts_event[position]) // NS_PER_SECOND
            dated = datetime.fromtimestamp(stamp, zone).date()
            if dated != day:
                raise PreconditionError(
                    f"controls.{name}: the point read for {plan.key.follower}'s session of {day} "
                    f"is dated {dated} in {zone.key}; a control describes the session it is read "
                    "for",
                    remedy=f"give {name} a point every session, its ts_event on the session's "
                    f"date in {zone.key} and its ts_init at or before the period's exit, and "
                    f"{signals.reload(context.screen, name)}",
                )
            if value not in (0.0, 1.0):
                raise PreconditionError(
                    f"controls.{name}: {value:g} on {day}; a control is 0 or 1",
                    remedy=f"set every session's {name} to 0 or 1, and "
                    f"{signals.reload(context.screen, name)}",
                )
            found[position, column] = value
    return found


def _holding(plan: _Plan, positions: np.ndarray, controls: np.ndarray) -> list[int]:
    """The follower's sessions a cell holds: its period valid, its position and every control
    defined."""
    return [
        position
        for position, valid in enumerate(plan.valid)
        if valid and not np.isnan(positions[position]) and not np.isnan(controls[position]).any()
    ]


def _shared(held: Sequence[set[int]]) -> set[int]:
    """The window's sessions every cell holding any holds: the one index a circular family's
    shifts rotate, so that no shift moves a cell's positions off its returns."""
    holding = [days for days in held if days]
    return set.intersection(*holding) if holding else set()


def _realise(
    context: Context,
    measure: Condition,
    plan: _Plan,
    days: Sequence[date],
    positions: np.ndarray,
    controls: np.ndarray,
    hurdles: Hurdles,
    fold_of: np.ndarray,
    payoff: str,
    shared: set[int] | None,
) -> _Realised:
    """A cell's held sessions — under a circular null only those every cell of the family
    holds — its demeaned values, its round trips and its net by session."""
    contrast = payoff == "contrast"
    follower = plan.follower
    held = [
        position
        for position in _holding(plan, positions, controls)
        if shared is None or follower.days[position] in shared
    ]
    on = [follower.days[position] for position in held]
    returns = np.asarray([_return(plan, position) for position in held], dtype=np.float64)
    signed = positions[held]
    matrix, names = demean.design(
        [follower.dates[position] for position in held],
        measure.demean,
        {name: controls[held, column] for column, name in enumerate(measure.controls)},
        follower.calendar,
    )
    residuals, _, dropped = demean.residuals(matrix, names, returns)
    spanned = demean.spanned(matrix, names)
    if contrast:
        moving = signed != 0
        r2 = demean.r2(matrix[moving], names, (signed[moving] > 0).astype(np.float64))
    else:
        r2 = demean.r2(matrix, names, (signed != 0).astype(np.float64))
    stats, net = _traded(context.screen, plan, held, signed, returns, residuals, hurdles)
    by_day = np.full(len(days), np.nan, dtype=np.float64)
    by_day[on] = net
    folds = fold_of[np.asarray(on, dtype=np.int64)]
    adjusted: list[float | None] = []
    for fold in range(context.folds):
        inside = folds == fold
        adjusted.append(_estimate(signed[inside], residuals[inside], contrast))
    return _Realised(
        held=held,
        days=on,
        positions=signed,
        residuals=residuals,
        spanned=spanned,
        estimate=_estimate(signed, residuals, contrast) or 0.0,
        raw=_estimate(signed, returns, contrast) or 0.0,
        drift=float(np.add.reduce(returns) / len(returns)) if len(returns) else 0.0,
        stats=stats,
        net=by_day,
        r2=r2,
        dropped=dropped,
        folds=adjusted,
    )


def _return(plan: _Plan, position: int) -> float:
    entry = plan.entries[position]
    assert entry is not None
    return float(BP * (np.log(plan.exits[position].price) - np.log(entry.price)))


def _estimate(signed: np.ndarray, returns: np.ndarray, contrast: bool) -> float | None:
    """The estimator on these sessions' returns, `None` with no session to read: long or flat,
    the mean of each session's position less these sessions' mean position times its return,
    the excess a session; under a contrast long less short, weighed over these sessions.

    On the window's residuals, which sum to zero over it, that is the mean of position times
    residual. Inside a fold they do not sum to zero, and centring the positions on the fold's
    own mean keeps the fold's drift times its exposure out of it; on returns not demeaned it
    keeps the follower's drift out of it, so the two differ by what the calendar took."""
    return _mean(nulls.values_of(signed[None, :], returns[None, :], contrast)[0])


def _mean(values: np.ndarray) -> float | None:
    held = values[~np.isnan(values)]
    return float(np.add.reduce(held) / len(held)) if len(held) else None


def _traded(
    screen: Screen,
    plan: _Plan,
    held: Sequence[int],
    signed: np.ndarray,
    returns: np.ndarray,
    residuals: np.ndarray,
    hurdles: Hurdles,
) -> tuple[ResponseStats, np.ndarray]:
    """The cell's positions as round trips, and its net booked by held session."""
    joins = plan.test.entry == plan.test.exit
    net = signed * returns
    runs: list[list[int]] = []
    for row, position in enumerate(held):
        if signed[row] == 0:
            continue
        last = runs[-1][-1] if runs else None
        if (
            joins
            and last is not None
            and held[last] == position - 1
            and signed[last] == signed[row]
        ):
            runs[-1].append(row)
        else:
            runs.append([row])
    gross_total = hurdle_total = 0.0
    hits = 0
    for run in runs:
        entry = plan.entries[held[run[0]]]
        assert entry is not None
        direction = int(signed[run[0]])
        gross, hurdle = _round_trip(
            screen, plan.key.follower, entry, plan.exits[held[run[-1]]], direction, hurdles
        )
        net[run[-1]] += gross - float(np.add.reduce(net[run])) - hurdle
        gross_total += gross
        hurdle_total += hurdle
        hits += int(gross > 0)
    sessions = len(held)
    per = max(len(runs), 1)
    stats = ResponseStats(
        events=len(runs),
        events_per_day=round(len(runs) / sessions, 6) if sessions else 0.0,
        gross_bp=gross_total / per,
        hurdle_bp=hurdle_total / per,
        margin_bp=(gross_total - hurdle_total) / per,
        ceiling_bp_day=float(np.add.reduce(net)) / sessions if sessions else 0.0,
        hit_rate=hits / per,
        unfilled=sum(not valid for valid in plan.valid),
        drift_adjusted_bp=float(np.add.reduce(signed * residuals)) / per,
    )
    return stats, net


def _round_trip(
    screen: Screen, follower: str, entry: Mark, exit_: Mark, direction: int, hurdles: Hurdles
) -> tuple[float, float]:
    """A position's gross and its round trip, struck at its entry and its exit."""
    touched = screen.legs[follower].type in TOUCHED
    series = Series(
        ts=np.asarray([entry.t, exit_.t], dtype=np.int64),
        price=np.asarray([entry.price, exit_.price], dtype=np.float64),
        bid=np.asarray([entry.bid, exit_.bid], dtype=np.float64) if touched else None,
        ask=np.asarray([entry.ask, exit_.ask], dtype=np.float64) if touched else None,
    )
    move = direction * BP * float(np.log(exit_.price) - np.log(entry.price))
    gross = taker_gross(screen, follower, {follower: series}, entry.t, exit_.t, direction, move)
    hurdle = hurdles.round_trip(follower, direction, {follower: series}, entry.t, exit_.t, {})
    return gross, hurdle


def _cell(
    context: Context,
    index: int,
    measure: Condition,
    item: _Found,
    evidence: nulls.Evidence,
    row: int,
    held: tuple[int, int],
    union: int,
    fold_of: np.ndarray,
) -> Cell:
    """One cell's record. `held` is the sessions holding a value, which `se` is scaled by, and
    the sessions reported: under a contrast those in either state alone."""
    realised = item.realised
    carried, sessions = held
    blocks = int(evidence.sessions[row]) if measure.block is not None else None
    se = float(evidence.se[row])
    if blocks is not None:
        se = blocks / carried * se if carried else 0.0
    critical = _critical(context.screen, measure, evidence, blocks)
    mde = None
    if critical is not None and measure.mde_power is not None:
        mde = (critical + NormalDist().inv_cdf(measure.mde_power)) * se
    side = 1.0 if item.plan.test.side == "long" else -1.0
    contrast = measure.payoff == "contrast"
    ages = item.states.ages[item.realised.held]
    ages = ages[~np.isnan(ages)]
    episodes, longest = _runs(item.states.state, WHEN)
    folds = nulls.fold_means(realised.net, fold_of, context.folds)
    lone = item.lone
    condition = ConditionStats(
        payoff=measure.payoff,
        null_by=measure.null_by,
        sessions_when=int(np.add.reduce(realised.positions == side)),
        sessions_against=int(np.add.reduce(realised.positions == -side)) if contrast else None,
        exposure=float(np.mean(realised.positions != 0)) if len(realised.held) else 0.0,
        episodes=episodes,
        episodes_against=_runs(item.states.state, AGAINST)[0] if contrast else None,
        longest_run=longest,
        skipped=item.skipped,
        blocks=blocks,
        shifts=None if measure.block is not None else max(union - 1, 0),
        exact=evidence.exact,
        raw_bp=realised.raw,
        drift_bp_day=realised.drift,
        long_flat_bp=None if lone is None else lone.estimate,
        long_flat_ceiling_bp_day=None if lone is None else lone.stats.ceiling_bp_day,
        control_r2=realised.r2,
        dropped=realised.dropped,
        folds_adjusted=realised.folds,
        folds_adjusted_same_sign=nulls.same_sign(realised.folds, realised.estimate),
        critical_t=critical,
        mde_bp=mde,
        signal_age_median_s=float(np.median(ages)) if len(ages) else 0.0,
        signal_age_max_s=float(np.max(ages)) if len(ages) else 0.0,
    )
    return Cell(
        measure=index,
        id="condition",
        key=item.plan.key.name,
        params=item.plan.key.params,
        mean=realised.estimate,
        se=se,
        t=float(evidence.t[row]),
        p=float(evidence.p[row]),
        sessions=sessions,
        folds=folds,
        folds_same_sign=nulls.same_sign(folds, realised.stats.ceiling_bp_day),
        response=realised.stats,
        condition=condition,
    )


def _critical(
    screen: Screen, measure: Condition, evidence: nulls.Evidence, blocks: int | None
) -> float | None:
    """The |t| a cell needs to pass the verdict's alpha under its own null; `None` with no
    verdict or no power asked for, or when no |t| can."""
    verdict = screen.verdict
    if verdict is None or measure.mde_power is None:
        return None
    if blocks is None:
        return nulls.critical(evidence.maxima, verdict.alpha, enumerated_flips=False)
    if verdict.alpha < p_floor(blocks):
        return None
    return nulls.critical(evidence.maxima, verdict.alpha, enumerated_flips=evidence.exact)


def _runs(state: Sequence[int | None], target: int) -> tuple[int, int]:
    """How many runs of `target` the decisions hold, and the longest, in sessions."""
    runs = longest = length = 0
    for one in state:
        length = length + 1 if one == target else 0
        runs += int(length == 1)
        longest = max(longest, length)
    return runs, longest


def _held(test: ConditionTest) -> int:
    assert isinstance(test.hold, FixedHold)
    return test.hold.sessions


def _mark(leg: Series, at: int) -> Mark:
    if leg.bid is None or leg.ask is None:
        return Mark(int(leg.ts[at]), float(leg.price[at]))
    return Mark(int(leg.ts[at]), float(leg.price[at]), float(leg.bid[at]), float(leg.ask[at]))


def _calendar(definition: Any) -> closures.Calendar | None:
    return closures.calendar(definition.asset_class.name, definition.id.venue.value)


def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)
