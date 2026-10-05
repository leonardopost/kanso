"""A measure's evidence across sessions, and its family-wise null by session sign flips.

**The session is the unit of replication.** A cell's evidence is one value per session it
holds — a correlation at a lag, a drift-adjusted response — reported as their mean and the
standard error of their spread, which is how a card's metric gets its noise floor, one
level finer. Volatility clustering, intraday seasonality and a few busy hours carrying most
of the prints all stay inside a session, where they cannot pass for a relationship. The one
assumption, stated in every result, is that sessions are roughly independent of each other.

**Max-T over exactly the declared cells.** Under the null of no relationship a cell's
per-session values are symmetric about zero, so one draw flips the sign of every session's
value with one shared vector of signs, recomputes every cell's t and keeps the largest
absolute t across the measure. A cell's adjusted p is its rank against those maxima,
`(1 + #{draws whose maximum reaches |t|}) / (draws + 1)` — Westfall and Young's step, on
session sign flips. One vector per draw is shared by every cell, so the cells' dependence
is kept: forty lags of one pair are not forty independent chances. A flip leaves each
value's square where it was, so only the signed sums are redrawn.

**Seeds come from the pins, never from global state.** Each measure draws from its own
`numpy.random.Generator(PCG64(seed))`, the seed being the first eight bytes of the sha256
of the screen's bytes, the snapshot and the measure's index. The same pins give the same
signs; another cell in one measure changes that measure's family and no other's.

**No BLAS.** Every sum is numpy's own reduction in a fixed order, so one host gives the
same bytes every time.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Final

import numpy as np

BATCH: Final = 64
"""Draws evaluated at once: bounds the memory of a draw at `BATCH x cells x sessions` values."""


@dataclass(frozen=True)
class Evidence:
    """Per cell: the mean of its sessions, its standard error, t, the sessions it held, p."""

    mean: np.ndarray
    se: np.ndarray
    t: np.ndarray
    sessions: np.ndarray
    p: np.ndarray


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
    observed_t = _t(np.add.reduce(filled, axis=1), squares, counts)
    exceed = np.zeros(len(values), dtype=np.int64)
    if len(values) and values.shape[1]:
        generator = np.random.Generator(np.random.PCG64(seeded))
        magnitude = np.abs(observed_t)
        done = 0
        while done < draws:
            size = min(BATCH, draws - done)
            signs = generator.integers(0, 2, size=(size, values.shape[1])) * 2 - 1
            sums = np.add.reduce(signs[:, None, :] * filled[None, :, :], axis=2)
            maxima = np.max(np.abs(_t(sums, squares[None, :], counts[None, :])), axis=1)
            exceed += np.add.reduce(maxima[:, None] >= magnitude[None, :], axis=0)
            done += size
    mean = np.divide(
        np.add.reduce(filled, axis=1), counts, out=np.zeros(len(values)), where=counts > 0
    )
    return Evidence(
        mean=mean,
        se=_se(np.add.reduce(filled, axis=1), squares, counts),
        t=observed_t,
        sessions=counts,
        p=np.where(np.abs(observed_t) > 0, (1 + exceed) / (draws + 1), 1.0),
    )


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
