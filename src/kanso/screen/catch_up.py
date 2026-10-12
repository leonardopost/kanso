"""`catch_up`: whether trading a follower toward the move its leader made, until it has caught
up, clears the hurdle, and how often it could.

A cell is a leader, a follower, an entry, a close and a stop. Its pair's response `beta` is
fitted before any cell is measured (`moments`, `slope`): the slope of the follower's returns on
the leader's, both taken between consecutive multiples of `beta_over` inside each session of the
fit span — the window under `fit: window`, its first calendar fold under `fit: first_fold`,
whose sessions then hold no value for the pair's cells. Either sign is a response: a follower
that moves against its leader has a negative beta, and the cell trades it that way.

In one session, at every instant either series moves, in time order:

1. **The gap.** Over the trailing `within`, the leader's move times beta is the follower's
   implied move, and the gap is that less the follower's own move — levels as of each end,
   never ahead, in basis points.
2. **Enter** when the leader made the move and the follower has not: the implied move and the
   gap are each at least `enter_bp`, the same way. The position is the follower, the gap's way,
   entered at its first point at or after that instant plus `latency_ms`. An event that fires
   while the previous position is held is skipped: one position at a time.
3. **Exit on a condition.** From the entry the gap is followed from the anchor it was entered
   on — `within` before the instant that entered it — so a leader that keeps moving moves the
   target with it. The position closes at the first instant its gap has shrunk to `close` of
   its size at entry, and stops at the first it has grown to `stop` times it; `max_hold` after
   the entry it is capped, a safety the result counts. It leaves at the follower's first point
   at or after that instant plus `latency_ms`. An event with no follower point to enter or leave
   at inside the session is `unfilled`: counted, scored nowhere, and nothing after it is held.
4. **Score** as a `response` does (`kanso.screen.response`): the signal at the mid, the gross a
   taker makes across the touch, the venue model's round trip, and the drift-adjusted signal the
   null is tested on. Each exit's reason and the hold it produced are recorded, so how long a
   rule holds is something the screen measures rather than something it was told.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from kanso.schemas.screen import CatchUp, CatchUpStats, Screen, followed, span_ns
from kanso.screen import grid
from kanso.screen.fit import Moments, sample
from kanso.screen.hurdle import Hurdles
from kanso.screen.response import BP, taker_gross
from kanso.screen.sessions import Series

Pair = tuple[str, str]
"""A leader and one of its followers."""

FIRST_LOOK: Final = 1024
"""Instants a position's exit is first looked for in; the look doubles until the cap."""


@dataclass(frozen=True)
class CellKey:
    """One cell: its name in a result, the pair, its entry, its close and its stop."""

    name: str
    leader: str
    follower: str
    enter_bp: float
    close: float
    stop: float
    params: dict[str, str | float] = field(default_factory=dict)


@dataclass
class Tally:
    """One cell's positions in one session: a `response.Tally` and how each one ended."""

    events: int = 0
    unfilled: int = 0
    hits: int = 0
    signal: float = 0.0
    gross: float = 0.0
    hurdle: float = 0.0
    closed: int = 0
    stopped: int = 0
    capped: int = 0
    holds: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class Path:
    """One pair's session, sampled at every instant either series moved inside its live span."""

    times: np.ndarray
    lead: np.ndarray
    follow: np.ndarray
    anchor: np.ndarray
    implied: np.ndarray
    gap: np.ndarray
    beta: float
    fills: np.ndarray
    levels: np.ndarray
    drift: float


def pairs(screen: Screen, measure: CatchUp) -> tuple[Pair, ...]:
    """The leader against each follower, in the order the followers are declared."""
    return tuple((measure.leader, follower) for follower in followed(screen, measure))


def cells(screen: Screen, measure: CatchUp) -> tuple[CellKey, ...]:
    """Every cell of the measure, follower by entry by close by stop, in the order declared."""
    found: list[CellKey] = []
    for follower in followed(screen, measure):
        for enter in measure.enter_bp:
            for close in measure.close:
                for stop in measure.stop:
                    name = (
                        f"catch_up/{measure.leader}>{follower}/{enter:g}bp/{measure.within}"
                        f"/close{close:g}/stop{stop:g}"
                    )
                    params: dict[str, str | float] = {
                        "leader": measure.leader,
                        "follower": follower,
                        "enter_bp": enter,
                        "within": measure.within,
                        "close": close,
                        "stop": stop,
                        "max_hold": measure.max_hold,
                        "fit": measure.fit,
                        "beta_over": measure.beta_over,
                        "latency_ms": measure.latency_ms,
                    }
                    found.append(
                        CellKey(name, measure.leader, follower, enter, close, stop, params)
                    )
    return tuple(found)


def moments(
    screen: Screen,
    measure: CatchUp,
    series: Mapping[str, Series],
    span: tuple[int, int],
    overlap: bool,
    betas: Mapping[str, float],
) -> dict[Pair, Moments]:
    """One session's moments of every pair: the follower's returns on the leader's, each taken
    between consecutive multiples of `beta_over` inside the pair's live span."""
    step = span_ns(measure.beta_over)
    found: dict[Pair, Moments] = {}
    for pair in pairs(screen, measure):
        live = grid.live(_legs(screen, pair), series, span, overlap)
        if live is None:
            found[pair] = Moments()
            continue
        times = grid.grid(live, step)
        lead = np.diff(grid.level(screen, pair[0], series, times, betas))
        follow = np.diff(grid.level(screen, pair[1], series, times, betas))
        both = ~(np.isnan(lead) | np.isnan(follow))
        found[pair] = sample(follow[both], lead[both])
    return found


def slope(moments: Moments) -> float | None:
    """The follower's response to its leader, or `None` when the leader never moved."""
    if moments.n < 2 or moments.m2_y <= 0.0:
        return None
    return moments.c_xy / moments.m2_y


def session(
    screen: Screen,
    measure: CatchUp,
    series: Mapping[str, Series],
    span: tuple[int, int],
    overlap: bool,
    betas: Mapping[str, float],
    slopes: Mapping[Pair, float | None],
    hurdles: Hurdles,
) -> list[Tally | None]:
    """One session's tally of every cell, `None` where the cell was not live or has no beta."""
    paths: dict[str, Path | None] = {}
    out: list[Tally | None] = []
    for key in cells(screen, measure):
        if key.follower not in paths:
            beta = slopes.get((key.leader, key.follower))
            paths[key.follower] = (
                None
                if beta is None
                else _path(screen, measure, key, series, span, overlap, betas, beta)
            )
        path = paths[key.follower]
        out.append(
            None if path is None else _tally(screen, measure, key, path, series, betas, hurdles)
        )
    return out


def exits(live: Sequence[Tally], beta: float | None) -> CatchUpStats:
    """How a cell's positions ended, summed over the sessions it was live in."""
    holds = [hold for day in live for hold in day.holds]
    return CatchUpStats(
        beta=beta,
        closed=sum(day.closed for day in live),
        stopped=sum(day.stopped for day in live),
        capped=sum(day.capped for day in live),
        hold_median_s=float(np.median(holds)) if holds else 0.0,
        hold_p90_s=float(np.percentile(holds, 90)) if holds else 0.0,
    )


def _path(
    screen: Screen,
    measure: CatchUp,
    key: CellKey,
    series: Mapping[str, Series],
    span: tuple[int, int],
    overlap: bool,
    betas: Mapping[str, float],
    beta: float,
) -> Path | None:
    live = grid.live(_legs(screen, (key.leader, key.follower)), series, span, overlap)
    if live is None:
        return None
    both = np.union1d(
        grid.instants(screen, key.leader, series), grid.instants(screen, key.follower, series)
    )
    times = _within(both, live)
    fills = _within(grid.instants(screen, key.follower, series), live)
    lead = grid.level(screen, key.leader, series, times, betas)
    follow = grid.level(screen, key.follower, series, times, betas)
    anchor = np.searchsorted(times, times - span_ns(measure.within), side="right") - 1
    known = anchor >= 0
    implied = np.full(len(times), np.nan)
    gap = np.full(len(times), np.nan)
    implied[known] = beta * (lead[known] - lead[anchor[known]]) * BP
    gap[known] = implied[known] - (follow[known] - follow[anchor[known]]) * BP
    levels = grid.level(screen, key.follower, series, fills, betas)
    drift = 0.0
    if len(fills) >= 2:
        drift = float((levels[-1] - levels[0]) / float(fills[-1] - fills[0]))
    return Path(times, lead, follow, anchor, implied, gap, beta, fills, levels, drift)


def _tally(
    screen: Screen,
    measure: CatchUp,
    key: CellKey,
    path: Path,
    series: Mapping[str, Series],
    betas: Mapping[str, float],
    hurdles: Hurdles,
) -> Tally:
    tally = Tally()
    with np.errstate(invalid="ignore"):
        fire = np.flatnonzero(
            (np.abs(path.implied) >= key.enter_bp)
            & (np.abs(path.gap) >= key.enter_bp)
            & (path.implied * path.gap > 0)
        )
    latency = int(round(measure.latency_ms * 1_000_000))
    cap = span_ns(measure.max_hold)
    fired = path.times[fire]
    next_ = 0
    while next_ < len(fire):
        at = int(fire[next_])
        entry = int(np.searchsorted(path.fills, path.times[at] + latency, side="left"))
        if entry == len(path.fills):
            tally.unfilled += 1
            break
        direction = 1 if path.gap[at] > 0 else -1
        leave, reason = _leave(key, path, at, int(path.fills[entry]), cap, direction)
        exit_ = int(np.searchsorted(path.fills, leave + latency, side="left"))
        if exit_ == len(path.fills):
            tally.unfilled += 1
            break
        signal = direction * (path.levels[exit_] - path.levels[entry]) * BP
        entered, left = int(path.fills[entry]), int(path.fills[exit_])
        gross = taker_gross(screen, key.follower, series, entered, left, direction, signal)
        hold = float(left - entered)
        tally.events += 1
        tally.signal += signal - direction * path.drift * hold * BP
        tally.gross += gross
        tally.hurdle += hurdles.round_trip(key.follower, direction, series, entered, left, betas)
        tally.hits += int(gross > 0)
        setattr(tally, reason, getattr(tally, reason) + 1)
        tally.holds.append(hold / 1e9)
        next_ = int(np.searchsorted(fired, left, side="right"))
    return tally


def _leave(
    key: CellKey, path: Path, at: int, entered: int, cap: int, direction: int
) -> tuple[int, str]:
    """The instant the position's exit condition first held, and which one did.

    The gap is followed from the entry's anchor at every instant from the fill to the cap, in
    looks that double, so a position that closes in a second reads a second of the path.
    """
    size = abs(float(path.gap[at]))
    anchor = int(path.anchor[at])
    first = int(np.searchsorted(path.times, entered, side="left"))
    last = int(np.searchsorted(path.times, entered + cap, side="right"))
    look = FIRST_LOOK
    while first < last:
        upto = min(first + look, last)
        lead = path.lead[first:upto] - path.lead[anchor]
        follow = path.follow[first:upto] - path.follow[anchor]
        followed_gap = direction * (path.beta * lead - follow) * BP
        closed = followed_gap <= key.close * size
        stopped = followed_gap >= key.stop * size
        ended = np.flatnonzero(closed | stopped)
        if len(ended):
            index = int(ended[0])
            return int(path.times[first + index]), "closed" if closed[index] else "stopped"
        first, look = upto, look * 2
    return entered + cap, "capped"


def _legs(screen: Screen, pair: Pair) -> tuple[str, ...]:
    return tuple(dict.fromkeys(screen.legs_of(pair[0]) + screen.legs_of(pair[1])))


def _within(times: np.ndarray, live: tuple[int, int]) -> np.ndarray:
    return times[(times >= live[0]) & (times <= live[1])]
