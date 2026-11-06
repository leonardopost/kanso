"""A measure's evidence across sessions, and its family-wise null: session sign flips, or
circular shifts of a condition's positions.

**The session is the unit of replication.** A cell's evidence is one value per session it
holds — a correlation at a lag, a drift-adjusted response — reported as their mean and the
standard error of their spread, which is how a card's metric gets its noise floor, one
level finer. Volatility clustering, intraday seasonality and a few busy hours carrying most
of the prints all stay inside a session, where they cannot pass for a relationship. What the
sessions are then assumed to be is the null's, and every result states it measure by measure:
under sign flips, that sessions — or a condition's blocks of them — are roughly independent of
each other; under circular shifts, that every rotation of a condition's positions against its
returns is as likely as the one observed.

**Max-T over exactly the declared cells.** Under the null of no relationship a cell's
per-session values are symmetric about zero, so one draw flips the sign of every session's
value with one shared vector of signs, recomputes every cell's t and keeps the largest
absolute t across the measure. A cell's adjusted p is its rank against those maxima,
`(1 + #{draws whose maximum reaches |t|}) / (draws + 1)` — Westfall and Young's step, on
session sign flips. One vector per draw is shared by every cell, so the cells' dependence
is kept: forty lags of one pair are not forty independent chances. A flip leaves each
value's square where it was, so only the signed sums are redrawn.

**Exact when it can be.** S sessions have 2^S vectors of signs. When that is no more than
the draws, every vector is taken once and a cell's p is the share of them whose maximum
reaches its |t| — exact, with no draw involved. The observed signs and their negation both
reach it, as does every vector that agrees with either on the sessions the cell holds, so no
p is below 2^(1 − sessions held); a p estimated from draws is held to that floor too, since
no assignment of signs can give less, however the draws fell.

**Seeds come from the pins, never from global state.** Each measure draws from its own
`numpy.random.Generator(PCG64(seed))`, the seed being the first eight bytes of the sha256
of the screen's bytes, the snapshot and the measure's index. The same pins give the same
signs; another cell in one measure changes that measure's family and no other's.

**Circular shifts, for positions that persist.** A position held while a slow state holds is
the same sign for runs of sessions, so its values are not independent session to session, and
flipping their signs one at a time would read a run as many chances. A `condition` measure may
be judged instead by shifting every cell's positions round the calendar against its returns,
which stay with their dates: one draw moves every position k sessions later, wrapping at the
end, with one k shared by every cell of the measure, so the cells' dependence is kept, and so is
each cell's own — its runs, its holds, how long a state persists. Only the alignment of positions
with returns is redrawn.

**Every shift, so the level is the null's.** The shifts are the whole group of rotations: over
the measure's N sessions every k from 1 to N − 1, which with the identity — the alignment
observed — are all N. When every rotation is as likely as the one observed, the observed is a
uniform draw from those N, its rank among them is uniform, and P(p ≤ α) ≤ α at every α: exact
as a level, to the degree the returns are exchangeable under rotation, which residuals fitted on
a calendar design are only approximately. A set that left out the shifts near the identity would
not be a group, and its p would read too small, the more so the more it left out: on one contrast
cell with runs averaging 40 sessions over 250, leaving out every shift under 60 either way put
2.9% of p at or under 0.01 and 6.9% at or under 0.05. A shift of a session or two nearly
re-aligns a persistent position and nearly reaches its |t|; that costs power, never level. When
the N − 1 shifts are no more than the draws every one is taken once; otherwise `draws` distinct
ones are drawn from the measure's own generator, which keeps p valid by the random-permutation
argument: under the null the identity and the shifts drawn are exchangeable.

A cell's p under either null is its rank against the family's maxima, R of which reach its |t|:

    sign flips, every vector (2^S <= draws)   R / 2^S,              never below 2^(1 - S held)
    sign flips, drawn                         (1 + R) / (draws + 1), never below 2^(1 - S held)
    circular shifts, every one (N-1 <= draws) (1 + R) / N,           so at least 1 / N
    circular shifts, drawn                    (1 + R) / (draws + 1)

The `+ 1` of the shifts is the observed alignment, the identity, which is never drawn. A cell
whose t is zero reads p one under both, as does every cell of a family with fewer than two
sessions. The maxima are kept, so the |t| a cell would need to reach a given p — the null's own
critical value — can be read off them (`critical`).

**No BLAS.** Every sum is numpy's own reduction in a fixed order, so one host gives the
same bytes every time.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Final

import numpy as np

BATCH: Final = 64
"""Draws evaluated at once: bounds the memory of a draw at `BATCH x cells x sessions` values."""


@dataclass(frozen=True)
class Evidence:
    """Per cell: the mean of its sessions, its standard error, t, the sessions it held, p.

    `maxima` are the family's largest |t| under each draw of the null, and `exact` whether the
    draws were every assignment there is rather than a sample of them.
    """

    mean: np.ndarray
    se: np.ndarray
    t: np.ndarray
    sessions: np.ndarray
    p: np.ndarray
    maxima: np.ndarray
    exact: bool


def seed(screen_sha: str, snapshot_id: str, index: int) -> int:
    """The seed of one measure's draws, from the pins and from nothing else."""
    digest = sha256(f"{screen_sha}|{snapshot_id}|{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def evidence(values: np.ndarray, draws: int, seeded: int) -> Evidence:
    """Each cell's mean, standard error and t across its sessions, and its max-T p.

    `values` is cells x sessions, NaN where a cell held no value in a session. A cell with
    fewer than two sessions, or whose sessions all agree, has no spread to judge against:
    its t is zero and its p one.
    """
    held = ~np.isnan(values)
    filled = np.where(held, values, 0.0)
    counts = held.sum(axis=1)
    squares = np.add.reduce(filled * filled, axis=1)
    sessions = values.shape[1]
    observed_t = _t(_sums(np.ones((1, sessions), dtype=np.int64), filled)[0], squares, counts)
    magnitude = np.abs(observed_t)
    exceed = np.zeros(len(values), dtype=np.int64)
    exact = 2**sessions <= draws
    vectors = _exhaustive(sessions) if exact else _drawn(sessions, draws, seeded)
    found: list[np.ndarray] = []
    for signs in vectors if len(values) else ():
        maxima = np.max(np.abs(_t(_sums(signs, filled), squares[None, :], counts[None, :])), axis=1)
        exceed += np.add.reduce(maxima[:, None] >= magnitude[None, :], axis=0)
        found.append(maxima)
    estimated = exceed / 2**sessions if exact else (1 + exceed) / (draws + 1)
    floor = np.power(2.0, 1.0 - np.maximum(counts, 1))
    mean = np.divide(
        np.add.reduce(filled, axis=1), counts, out=np.zeros(len(values)), where=counts > 0
    )
    return Evidence(
        mean=mean,
        se=_se(np.add.reduce(filled, axis=1), squares, counts),
        t=observed_t,
        sessions=counts,
        p=np.where(magnitude > 0, np.maximum(estimated, floor), 1.0),
        maxima=_joined(found),
        exact=exact,
    )


def values_of(positions: np.ndarray, returns: np.ndarray, contrast: bool) -> np.ndarray:
    """Each session's value of a cell's positions against its returns, along the last axis.

    NaN where either is. Under either payoff a session's return is weighted so that the weights
    sum to zero over the sessions holding a value: a constant added to every return leaves the
    values' sum where it was, and their spread is the spread of that sum, so their standard
    error is the mean's own. Long or flat, the weight is the session's position less the mean
    position over those sessions, `π − π̄`; on returns that themselves sum to zero there — the
    residuals of a fit with an intercept — the values sum to `Σ π r`, so their mean is the
    excess a session, while a position times its return alone would spread as if the returns'
    own mean were not fitted, overstating the standard error by `1 / √(1 − f)` at exposure `f`.
    Under a contrast, a session long is weighted `n / n⁺` and one short `−n / n⁻`, over the
    `n = n⁺ + n⁻` sessions holding either, so the values' mean is the mean return long less the
    mean return short; a flat session holds no value, and none does unless both sides hold at
    least one.
    """
    held = ~np.isnan(positions) & ~np.isnan(returns)
    signed = np.where(held, positions, 0.0)
    if not contrast:
        counts = np.add.reduce(held, axis=-1, keepdims=True)
        centre = np.add.reduce(signed, axis=-1, keepdims=True) / np.maximum(counts, 1)
        return np.where(held, (signed - centre) * np.where(held, returns, 0.0), np.nan)
    plus, minus = held & (signed > 0), held & (signed < 0)
    longs = np.add.reduce(plus, axis=-1, keepdims=True)
    shorts = np.add.reduce(minus, axis=-1, keepdims=True)
    both = (longs + shorts).astype(np.float64)
    weight = np.where(plus, both / np.maximum(longs, 1), -both / np.maximum(shorts, 1))
    usable = (plus | minus) & (longs > 0) & (shorts > 0)
    return np.where(usable, weight * np.where(usable, returns, 0.0), np.nan)


def blocks(values: np.ndarray, size: int) -> np.ndarray:
    """Each cell's values summed over consecutive blocks of `size` sessions; NaN for a block in
    which the cell held none."""
    cells, sessions = values.shape
    count = -(-sessions // size)
    padded = np.full((cells, count * size), np.nan, dtype=np.float64)
    padded[:, :sessions] = values
    grouped = padded.reshape(cells, count, size)
    held = ~np.isnan(grouped)
    sums = np.add.reduce(np.where(held, grouped, 0.0), axis=2)
    return np.where(held.any(axis=2), sums, np.nan)


def circular(
    positions: np.ndarray,
    returns: np.ndarray,
    contrast: bool,
    draws: int,
    seeded: int,
) -> Evidence:
    """Each cell's mean, standard error and t of its values, and its max-T p over circular
    shifts of every cell's positions against its returns, one shift shared by the family.

    `positions` and `returns` are cells x sessions, NaN where a cell held nothing. Every shift
    from 1 to N − 1 is a draw, or `draws` of them when they are more; a family of fewer than
    two sessions has none, and every cell of it reads p one.
    """
    cells, sessions = positions.shape
    observed = values_of(positions, returns, contrast)
    held = ~np.isnan(observed)
    filled = np.where(held, observed, 0.0)
    counts = held.sum(axis=1)
    sums = np.add.reduce(filled, axis=1)
    squares = np.add.reduce(filled * filled, axis=1)
    observed_t = _t(sums, squares, counts)
    magnitude = np.abs(observed_t)
    exact = sessions - 1 <= draws
    shifts = _shifts(sessions, draws, seeded)
    found: list[np.ndarray] = []
    column = np.arange(sessions, dtype=np.int64)
    for start in range(0, len(shifts) if cells else 0, BATCH):
        batch = shifts[start : start + BATCH]
        moved = positions[:, (column[None, :] - batch[:, None]) % sessions].transpose(1, 0, 2)
        values = values_of(moved, returns[None, :, :], contrast)
        inside = ~np.isnan(values)
        kept = np.where(inside, values, 0.0)
        drawn_t = _t(
            np.add.reduce(kept, axis=2), np.add.reduce(kept * kept, axis=2), inside.sum(axis=2)
        )
        found.append(np.max(np.abs(drawn_t), axis=1))
    maxima = _joined(found)
    exceed = np.add.reduce(maxima[:, None] >= magnitude[None, :], axis=0)
    mean = np.divide(sums, counts, out=np.zeros(cells), where=counts > 0)
    return Evidence(
        mean=mean,
        se=_se(sums, squares, counts),
        t=observed_t,
        sessions=counts,
        p=np.where(magnitude > 0, (1 + exceed) / (len(shifts) + 1), 1.0),
        maxima=maxima,
        exact=exact,
    )


def _shifts(sessions: int, draws: int, seeded: int) -> np.ndarray:
    """The shifts a circular null takes over `sessions`: every one from 1 to `sessions − 1`
    when they are no more than `draws`, else `draws` of them, drawn without replacement from
    the measure's own generator."""
    if sessions - 1 <= draws:
        return np.arange(1, max(sessions, 1), dtype=np.int64)
    generator = np.random.Generator(np.random.PCG64(seeded))
    return np.asarray(generator.choice(sessions - 1, size=draws, replace=False), np.int64) + 1


def critical(maxima: np.ndarray, alpha: float, enumerated_flips: bool) -> float | None:
    """The |t| a cell must exceed for its p to be at most `alpha`, read off the null's maxima;
    `None` when no |t| can.

    `enumerated_flips` is the rule of an enumeration of every sign vector, whose p is the share
    of maxima reaching |t|; every other null's p is `(1 + reaching) / (draws + 1)`. A cell above
    the j-th largest maximum is reached by at most j of them, so j is the most a p of at most
    `alpha` allows, counted in the arithmetic the verdict compares p in.
    """
    ordered = np.sort(maxima)[::-1]
    total = len(ordered)

    def p(reaching: int) -> float:
        return reaching / total if enumerated_flips else (1 + reaching) / (total + 1)

    reached = math.floor(alpha * total) if enumerated_flips else math.floor(alpha * (total + 1)) - 1
    while reached + 1 <= total and p(reached + 1) <= alpha:
        reached += 1
    while reached >= 0 and p(reached) > alpha:
        reached -= 1
    if reached < 0:
        return None
    return float(ordered[reached]) if reached < total else 0.0


def fold_means(row: np.ndarray, fold_of: np.ndarray, folds: int) -> list[float | None]:
    """The mean of a row's values inside each calendar fold, `None` for a fold holding none."""
    means: list[float | None] = []
    for fold in range(folds):
        inside = row[(fold_of == fold) & ~np.isnan(row)]
        means.append(float(np.add.reduce(inside) / len(inside)) if len(inside) else None)
    return means


def same_sign(folds: Sequence[float | None], mean: float) -> int:
    """How many folds' means share the sign of the whole; none when the whole is zero."""
    if mean == 0.0:
        return 0
    return sum(1 for value in folds if value is not None and value * mean > 0)


def _joined(maxima: list[np.ndarray]) -> np.ndarray:
    return np.concatenate(maxima) if maxima else np.zeros(0, dtype=np.float64)


def _sums(signs: np.ndarray, filled: np.ndarray) -> np.ndarray:
    """Every cell's signed sum under each vector of signs, in one fixed order of reduction."""
    return np.asarray(np.add.reduce(signs[:, None, :] * filled[None, :, :], axis=2))


def _exhaustive(sessions: int) -> Iterator[np.ndarray]:
    """Every vector of signs over `sessions`, `BATCH` at a time, the observed one first."""
    bits = np.arange(sessions, dtype=np.int64)
    for start in range(0, 2**sessions, BATCH):
        index = np.arange(start, min(start + BATCH, 2**sessions), dtype=np.int64)
        yield 1 - 2 * ((index[:, None] >> bits[None, :]) & 1)


def _drawn(sessions: int, draws: int, seeded: int) -> Iterator[np.ndarray]:
    """`draws` vectors of signs from the measure's own generator, `BATCH` at a time."""
    generator = np.random.Generator(np.random.PCG64(seeded))
    done = 0
    while done < draws:
        size = min(BATCH, draws - done)
        yield generator.integers(0, 2, size=(size, sessions)) * 2 - 1
        done += size


def _se(sums: np.ndarray, squares: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """The standard error of the mean from the sums, the sums of squares and the counts."""
    n = counts.astype(np.float64)
    usable = counts >= 2
    mean = np.divide(sums, n, out=np.zeros_like(sums, dtype=np.float64), where=usable)
    variance = np.divide(
        squares - n * mean * mean,
        n - 1.0,
        out=np.zeros_like(sums, dtype=np.float64),
        where=usable,
    )
    variance = np.maximum(variance, 0.0)
    return np.asarray(np.sqrt(np.divide(variance, n, out=np.zeros_like(variance), where=usable)))


def _t(sums: np.ndarray, squares: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """t = mean / standard error; zero where there is no spread to judge against."""
    se = _se(sums, squares, counts)
    n = counts.astype(np.float64)
    mean = np.divide(sums, n, out=np.zeros_like(se), where=counts >= 2)
    tiny = se <= 1e-12 * np.maximum(np.abs(mean), 1e-300)
    return np.asarray(np.divide(mean, se, out=np.zeros_like(se), where=~tiny & (counts >= 2)))
