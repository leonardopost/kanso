"""`response`: whether trading a follower on a trigger clears the hurdle, and how often it could.

A cell is a trigger threshold, a follower and a horizon. In one session, in time order:

1. **Fire.** The trigger is a condition evaluated at each of the trigger leg's points: a move —
   its level changed by at least `move_bp` since its level as of `within` earlier — or a
   z-score — its level at least `z` standard deviations from the mean of its points over the
   trailing `lookback`, the point itself included and nothing after it. The event's sign is the
   move's, or the z-score's. It fires at the `ts_init` of the point that met it.
2. **One position at a time.** An event that fires while the cell's previous event is still
   held — before its exit — is skipped: the simplest strategy that could harvest the cell.
3. **Enter, hold, exit.** Entry is the follower's first point at or after the fire plus
   `latency_ms`; exit its first point at or after the entry plus the horizon. The cell bets the
   way of the trigger under `side: with` and against it under `side: against`. An event whose
   follower has no point at the entry or the exit inside the session is `unfilled`: counted,
   and scored nowhere.
4. **Score.** The **signal** is the follower's signed change of level — mid for a quote or a
   book — in basis points; the **gross** is what a taker would have made: a quote or book
   follower buys at the ask and sells at the bid it shows, a bar or print follower and a derived
   one take their level. The **hurdle** is the round trip the venue model charges
   (`kanso.screen.hurdle`). The **drift-adjusted** signal is the signal less the follower's
   session drift over the same hold, times the bet's sign, so a trending day in a month whose
   triggers lean one way cannot pass for a reaction: the null is tested on it.

A session's value is the sum of its events' drift-adjusted signal — zero for a session the
cell was live in and nothing fired — so the cell's mean is basis points a day at one notional
an event, and its margin sum, gross less hurdle, is what `ceiling_bp_day` averages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from kanso.schemas.screen import Response, Screen, span_ns
from kanso.screen import grid
from kanso.screen.hurdle import TOUCHED, Hurdles
from kanso.screen.sessions import Series

BP: Final = 10_000.0
MIN_SCORED: Final = 3
"""Points a z-score's trailing window needs before its spread is a number."""


@dataclass(frozen=True)
class CellKey:
    """One cell: its name in a result, the trigger threshold, the follower, the horizon."""

    name: str
    threshold: float
    follower: str
    horizon: str
    params: dict[str, str | float] = field(default_factory=dict)


@dataclass
class Tally:
    """One cell's events in one session."""

    events: int = 0
    unfilled: int = 0
    hits: int = 0
    signal: float = 0.0
    gross: float = 0.0
    hurdle: float = 0.0


def cells(screen: Screen, measure: Response) -> tuple[CellKey, ...]:
    """Every cell of the measure, threshold by follower by horizon, in the order declared."""
    trigger = measure.trigger
    unit = "bp" if trigger.move_bp is not None else "z"
    span = trigger.within if trigger.move_bp is not None else trigger.lookback
    found: list[CellKey] = []
    for threshold in trigger.thresholds:
        for follower in screen.members(measure.followers):
            for horizon in measure.horizons:
                name = f"response/{trigger.leg}/{threshold:g}{unit}/{span}/{follower}/{horizon}"
                params: dict[str, str | float] = {
                    "trigger": trigger.leg,
                    "threshold": threshold,
                    "unit": unit,
                    "span": str(span),
                    "follower": follower,
                    "horizon": horizon,
                    "side": measure.side,
                    "latency_ms": measure.latency_ms,
                }
                found.append(CellKey(name, threshold, follower, horizon, params))
    return tuple(found)


def session(
    screen: Screen,
    measure: Response,
    series: Mapping[str, Series],
    span: tuple[int, int],
    overlap: bool,
    betas: Mapping[str, float],
    hurdles: Hurdles,
) -> list[Tally | None]:
    """One session's tally of every cell, `None` where the cell was not live."""
    out: list[Tally | None] = []
    for key in cells(screen, measure):
        legs = tuple(
            dict.fromkeys(screen.legs_of(measure.trigger.leg) + screen.legs_of(key.follower))
        )
        live = grid.live(legs, series, span, overlap)
        if live is None:
            out.append(None)
            continue
        out.append(_tally(screen, measure, key, series, live, betas, hurdles))
    return out


def _tally(
    screen: Screen,
    measure: Response,
    key: CellKey,
    series: Mapping[str, Series],
    live: tuple[int, int],
    betas: Mapping[str, float],
    hurdles: Hurdles,
) -> Tally:
    fired, signs = _fired(screen, measure, key.threshold, series, live, betas)
    times = _within(grid.instants(screen, key.follower, series), live)
    tally = Tally()
    if len(times) < 2 or not len(fired):
        return tally
    levels = grid.level(screen, key.follower, series, times, betas)
    drift = (levels[-1] - levels[0]) / float(times[-1] - times[0])
    latency = int(round(measure.latency_ms * 1_000_000))
    horizon = span_ns(key.horizon)
    side = 1 if measure.side == "with" else -1
    busy = -1
    for at, sign in zip(fired.tolist(), signs.tolist(), strict=True):
        if at <= busy:
            continue
        entry = int(np.searchsorted(times, at + latency, side="left"))
        exit_ = (
            entry
            if entry == len(times)
            else int(np.searchsorted(times, times[entry] + horizon, side="left"))
        )
        if exit_ == len(times):
            tally.unfilled += 1
            break
        direction = side * sign
        signal = direction * (levels[exit_] - levels[entry]) * BP
        gross = _gross(screen, key.follower, series, times[entry], times[exit_], direction, signal)
        hold = float(times[exit_] - times[entry])
        tally.events += 1
        tally.signal += signal - direction * drift * hold * BP
        tally.gross += gross
        tally.hurdle += hurdles.round_trip(
            key.follower, direction, series, int(times[entry]), int(times[exit_]), betas
        )
        tally.hits += int(gross > 0)
        busy = int(times[exit_])
    return tally


def _fired(
    screen: Screen,
    measure: Response,
    threshold: float,
    series: Mapping[str, Series],
    live: tuple[int, int],
    betas: Mapping[str, float],
) -> tuple[np.ndarray, np.ndarray]:
    """The instants the trigger met `threshold` inside the live span, and the sign of each."""
    trigger = measure.trigger
    times = _within(grid.instants(screen, trigger.leg, series), live)
    levels = grid.level(screen, trigger.leg, series, times, betas)
    if trigger.move_bp is not None:
        assert trigger.within is not None
        before = np.searchsorted(times, times - span_ns(trigger.within), side="right") - 1
        known = before >= 0
        score = np.zeros(len(times))
        score[known] = (levels[known] - levels[before[known]]) * BP
    else:
        assert trigger.lookback is not None
        score = _z(times, levels, span_ns(trigger.lookback))
    hit = np.abs(score) >= threshold
    return times[hit], np.sign(score[hit]).astype(np.int64)


def _z(times: np.ndarray, levels: np.ndarray, lookback: int) -> np.ndarray:
    """Each point's z-score against the points of the trailing `lookback`, itself included."""
    first = np.searchsorted(times, times - lookback, side="right")
    sums = np.concatenate(([0.0], np.cumsum(levels)))
    squares = np.concatenate(([0.0], np.cumsum(levels * levels)))
    index = np.arange(len(times))
    count = (index + 1 - first).astype(np.float64)
    mean = (sums[index + 1] - sums[first]) / count
    variance = np.maximum((squares[index + 1] - squares[first]) / count - mean * mean, 0.0)
    deviation = np.sqrt(variance)
    usable = (count >= MIN_SCORED) & (deviation > 0)
    return np.divide(levels - mean, deviation, out=np.zeros(len(times)), where=usable)


def _gross(
    screen: Screen,
    follower: str,
    series: Mapping[str, Series],
    entry_ns: int,
    exit_ns: int,
    direction: int,
    signal: float,
) -> float:
    """What a taker would have made: across the touch it shows, or its level's move."""
    if follower not in screen.legs or screen.legs[follower].type not in TOUCHED:
        return signal
    leg = series[follower]
    assert leg.bid is not None and leg.ask is not None
    at = np.asarray([entry_ns, exit_ns], dtype=np.int64)
    bid = grid.asof(leg, at, leg.bid)
    ask = grid.asof(leg, at, leg.ask)
    if direction > 0:
        return float(np.log(bid[1] / ask[0]) * BP)
    return float(np.log(bid[0] / ask[1]) * BP)


def _within(times: np.ndarray, live: tuple[int, int]) -> np.ndarray:
    return times[(times >= live[0]) & (times <= live[1])]
