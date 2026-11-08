"""A condition cell set against holding its follower: whether the positions it trades beat
buying the follower and keeping it, by the Sharpe of each net of the round trips each pays.

A condition measure that declares `benchmark: hold` asks this of every cell beside its
contrast, and a verdict then judges the cell by it (`kanso.screen.run`). The question is the
one a lane scored on `wf_sharpe_vs_hold` is: a strategy that only rode the market it trades
scores nothing, and one that sat out or shorted its drift has to have bought more than the
drift it gave up.

1. **The positions' net, a held session at a time.** A session's value is its position — the
   side its state takes, under a contrast the other side against, flat in neither and long or
   flat flat outside the state — times its period's log return, in basis points, less the
   fills it made: a position, a run of held sessions with one direction joined across sessions
   only where its periods are contiguous, as the cell's round trips are (`kanso.screen.
   condition`), pays its entry's fill on its first session and its exit's on its last. A fill
   is the venue model's charge, the one `Hurdles.round_trip` strikes, and on a follower that
   shows a touch the crossing to it as well (`Hurdles.fills`), so a position's sessions sum to
   what a taker made less its round trip. Summed over the window that is the cell's
   `ceiling_bp_day` times its sessions, booked where each fill happens rather than all on the
   session a position left in.
2. **The hold's, on the same sessions.** Buying the follower and keeping it earns, each held
   session, its close-to-close return — from the previous session's last point it has, or the
   session's own first on the first session it has — whatever the test's entry and exit, since
   a hold is long overnight as well: an open-to-close test is set against the hold it would
   replace. It pays one round trip: a buy at the first held session's start, booked there, and a
   sale at the last one's close, booked there.
3. **Sharpe, as the toolbox defines it.** Annualised at a zero risk-free rate,
   `mean / sd × √A` with one degree of freedom, `A` the held sessions per year of the window
   (`kanso.criteria.quantities`); zero for a series that does not vary. `difference` is the
   positions' less the hold's, and its error is the paired one by Memmel's correction of
   Jobson–Korkie, widened for serial correlation, as `wf_sharpe_vs_hold`'s is.
4. **Judged by every circular shift of the positions.** The hold does not move under a shift,
   so a shift redraws the positions' Sharpe alone: the positions are rotated against the
   sessions as the measure's own null rotates them (`kanso.screen.nulls.circular`), one shift
   shared by the family, its fills recomputed where the rotated runs now begin and end, and
   each cell's difference struck again. A cell's p is one-sided and family-wise:
   `(1 + #{shifts whose largest difference across the family reaches this cell's}) / (shifts
   + 1)`, every shift from 1 to N − 1 taken when they are no more than `[screen] draws`, so the
   observed alignment's rank among the N is uniform when every rotation is as likely as it.
   Rotation keeps how long the positions are short and flat, so `null_mean` — the cell's mean
   difference over the shifts — is what that exposure makes at a random alignment: close to
   close, below zero where the follower rose and the positions were not long, each such session
   giving up its drift flat and twice it short; open to close the positions earn the session
   alone and the hold the night too, so it sits where the two series' Sharpes put it. Either way
   the null is not symmetric about zero, and sign flips cannot judge it. A verdict passes a
   cell whose p is at or under `alpha` and whose difference is above zero: random timing is
   beaten, and so is the hold.

**No BLAS.** Every sum is numpy's own reduction in a fixed order, and the observed alignment is
struck by the same arithmetic as every shift, so ties are ties.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from math import sqrt

import numpy as np

from kanso.criteria.quantities import (
    correlation,
    long_run_factor,
    moments,
    sharpe_difference_variance,
    sharpe_variance,
)
from kanso.screen import nulls


@dataclass(frozen=True)
class Book:
    """One cell's held sessions, in order, as its positions and the hold book them, in bp.

    `returns` is each period's log return; `joins` whether a position held into the session
    from the held session before runs on, its periods contiguous; `buy_in`, `sell_in`,
    `buy_out` and `sell_out` one taker fill at the period's entry and at its exit; `hold` what
    the hold made each session net of its round trip; `folds` each session's calendar fold.
    """

    returns: np.ndarray
    joins: np.ndarray
    buy_in: np.ndarray
    sell_in: np.ndarray
    buy_out: np.ndarray
    sell_out: np.ndarray
    hold: np.ndarray
    folds: np.ndarray


@dataclass(frozen=True)
class Judged:
    """Per cell of one family: the Sharpes, their difference, p and the mean difference over
    the shifts; the family's largest difference under each shift; each cell's net a session."""

    sharpe: np.ndarray
    sharpe_hold: np.ndarray
    difference: np.ndarray
    p: np.ndarray
    null_mean: np.ndarray
    maxima: np.ndarray
    net: list[np.ndarray]


def net(positions: np.ndarray, book: Book) -> np.ndarray:
    """What these positions make each held session net of their fills, along the last axis.

    A position starts where it is not flat and does not run on from the session before — a
    period not contiguous with it, another direction, or flat there — and ends where the next
    session does not run on from it.
    """
    moving = positions != 0
    same = np.zeros(positions.shape, dtype=bool)
    same[..., 1:] = book.joins[1:] & moving[..., 1:] & (positions[..., 1:] == positions[..., :-1])
    starts = moving & ~same
    ends = moving.copy()
    ends[..., :-1] &= ~same[..., 1:]
    long = positions > 0
    entered = np.where(starts, np.where(long, book.buy_in, book.sell_in), 0.0)
    left = np.where(ends, np.where(long, book.sell_out, book.buy_out), 0.0)
    return np.asarray(positions * book.returns - entered - left)


def sharpe(values: np.ndarray, annualisation: float) -> np.ndarray:
    """The annualised Sharpe of each series along the last axis; zero below two values or
    where the series does not vary."""
    count = values.shape[-1]
    if count < 2:
        return np.zeros(values.shape[:-1], dtype=np.float64)
    mean = np.add.reduce(values, axis=-1) / count
    deviations = values - mean[..., None]
    spread = np.sqrt(np.add.reduce(deviations * deviations, axis=-1) / (count - 1))
    flat = spread <= 1e-12 * np.maximum(np.abs(mean), 1e-300)
    ratio = np.divide(mean, spread, out=np.zeros_like(mean), where=~flat)
    return np.asarray(ratio * sqrt(annualisation))


def judge(
    positions: np.ndarray,
    books: Sequence[Book | None],
    annualisation: float,
    draws: int,
    seeded: int,
) -> Judged:
    """Every cell's positions against its hold, and its p over circular shifts of them.

    `positions` is cells x sessions over the family's index, and `books[c]` the cell's book
    over the same sessions, `None` for a cell that holds none, which reads a p of one and is
    no part of the family's maxima.
    """
    cells, sessions = positions.shape
    nets = [
        np.zeros(0) if book is None else net(positions[row], book) for row, book in enumerate(books)
    ]
    own = np.asarray([float(sharpe(values, annualisation)) for values in nets])
    held = np.asarray(
        [0.0 if book is None else float(sharpe(book.hold, annualisation)) for book in books]
    )
    difference = own - held
    holding = [row for row, book in enumerate(books) if book is not None]
    shifts = nulls.shifts(sessions, draws, seeded)
    column = np.arange(sessions, dtype=np.int64)
    sums = np.zeros(cells, dtype=np.float64)
    found: list[np.ndarray] = []
    for start in range(0, len(shifts) if holding else 0, nulls.BATCH):
        batch = shifts[start : start + nulls.BATCH]
        index = (column[None, :] - batch[:, None]) % sessions
        drawn = np.full((len(batch), cells), -np.inf, dtype=np.float64)
        for row in holding:
            book = books[row]
            assert book is not None
            values = sharpe(net(positions[row][index], book), annualisation) - held[row]
            drawn[:, row] = values
            sums[row] += np.add.reduce(values)
        found.append(np.max(drawn, axis=1))
    maxima = np.concatenate(found) if found else np.zeros(0, dtype=np.float64)
    exceed = np.add.reduce(maxima[:, None] >= difference[None, :], axis=0)
    p = (1 + exceed) / (len(shifts) + 1)
    blank = np.ones(cells, dtype=bool)
    blank[holding] = False
    return Judged(
        sharpe=own,
        sharpe_hold=held,
        difference=difference,
        p=np.where(blank | (len(maxima) == 0), 1.0, p),
        null_mean=sums / len(shifts) if len(shifts) and holding else np.zeros(cells),
        maxima=maxima,
        net=nets,
    )


def difference_se(own: np.ndarray, hold: np.ndarray, window: tuple[date, date]) -> float:
    """The paired error of the difference of the two Sharpes over the window, by Memmel's
    correction of Jobson–Korkie (`kanso.criteria.quantities`), widened for the serial
    correlation of the more correlated series; summed as independent where either does not
    vary, as `wf_sharpe_vs_hold`'s is."""
    mine, theirs = own.tolist(), hold.tolist()
    rho = correlation(mine, theirs)
    if rho is not None:
        factor = max(long_run_factor(mine), long_run_factor(theirs))
        return sqrt(sharpe_difference_variance(mine, theirs, window, factor, rho))
    return sqrt(
        sharpe_variance(mine, window, long_run_factor(mine), moments(mine))
        + sharpe_variance(theirs, window, long_run_factor(theirs), moments(theirs))
    )


def folds(own: np.ndarray, book: Book, annualisation: float, count: int) -> list[float | None]:
    """The difference inside each calendar fold, at the window's annualisation; `None` for a
    fold holding fewer than two sessions."""
    found: list[float | None] = []
    for fold in range(count):
        inside = book.folds == fold
        if int(np.add.reduce(inside)) < 2:
            found.append(None)
            continue
        found.append(
            float(sharpe(own[inside], annualisation))
            - float(sharpe(book.hold[inside], annualisation))
        )
    return found
