"""The probability that the search's selection is overfit: whether the trial a search would
pick on one half of the research window ranks at or below the median of the trials on the
other half, across every way of halving it.

**Combinatorially symmetric cross-validation, after Bailey, Borwein, López de Prado and Zhu
(2014).** The research window is cut into `S = 2 × half_blocks` equal calendar blocks, and
every trial of the search the caller can read on the subject's grid is scored on each — a
matrix of `N` rows and `S` columns (`GateContext.trial_folds`). For every set `J` of `S/2`
blocks, `C(S, S/2)` of them, each of whose complements is itself one of the sets: each row's
in-sample value is its mean over `J` and its out-of-sample value its mean over the complement;
the row the search would have picked is the first that maximises the in-sample value, in row
order; its out-of-sample value is ranked among every row's, `r = #{below} + (#{equal} + 1)/2`
with the picked row among the equal, so ties take their midrank; and the split is overfit when
`ω = r / (N + 1)` is at most a half — `logit ω ≤ 0`, so a tie at the median counts as overfit.
The probability of backtest overfitting is the share of splits that are overfit, and the gate
passes when it is at most `max_pbo`. No random number is drawn, so the evidence reproduces bit
for bit; the splits are taken a thousand at a time (`BLOCK`) as masks multiplied into the
matrix, which bounds the memory without moving the count. `oos_loss` beside it — the share of
splits whose picked row lost out of sample, its mean over the complement at or below zero —
judges nothing.

**One deviation from the authors, stated.** They recompute the performance statistic over the
concatenated periods of each half. Here each half's value is the mean of its blocks' values,
because that is kanso's own metric arithmetic: an objective is the mean of its fold values
(`kanso.criteria.objectives`), so with `J` all `S` blocks a row's mean is the metric the
objective computes at `S` folds. A block that holds no period scores zero, as a fold does; it
is a value, never a reason to skip. The skip is a grid holding fewer period ends than there are
blocks, where some block is empty by construction.

**It prices the selection, not the edge.** It asks whether the rule that chose the subject —
the best in sample — goes on choosing above the median out of sample, which is a property of the
search's trials taken together and not of any one of them. Measured on seeded synthetic
matrices, forty to a cell (`tests/criteria/test_overfit_probability.py` draws them, and fails
when a cell moves): each block's value is in units of the standard error of a trial's value over
the whole window, so its noise is the root of the block count — the same window cut finer — and
an edge of 3 or 6 is a trial three or six of those errors above noise. `noise` carries no edge;
`one row at` puts one trial at the edge among noise; `a climb to` puts one at the edge and every
other below it by a uniform share of the edge, the shape of a hill climb whose edits are worse
than its best. Each cell is the share of the forty matrices whose estimate was at or under 0.1,
0.25 and 0.5:

    trials  drawn               half_blocks 2   half_blocks 4   half_blocks 8
         2  noise               0.20 0.20 0.42  0.17 0.20 0.42  0.15 0.35 0.55
         2  one row at 3        0.78 0.78 0.93  0.70 0.75 0.90  0.62 0.80 0.93
         2  one row at 6        1.00 1.00 1.00  1.00 1.00 1.00  1.00 1.00 1.00
         2  a climb to 3        0.55 0.55 0.72  0.25 0.38 0.62  0.25 0.42 0.68
         2  a climb to 6        0.78 0.78 0.85  0.47 0.62 0.75  0.55 0.60 0.80
        50  noise               0.03 0.15 0.68  0.03 0.10 0.55  0.00 0.12 0.60
        50  one row at 3        0.30 0.55 0.90  0.05 0.38 0.78  0.10 0.50 0.93
        50  one row at 6        0.95 1.00 1.00  0.93 1.00 1.00  0.97 1.00 1.00
        50  a climb to 3        0.12 0.57 0.88  0.10 0.40 0.88  0.10 0.57 0.97
        50  a climb to 6        0.60 0.82 1.00  0.70 0.93 1.00  0.85 0.97 1.00
       500  noise               0.00 0.10 0.60  0.00 0.05 0.45  0.00 0.03 0.40
       500  one row at 3        0.10 0.28 0.70  0.07 0.25 0.80  0.00 0.07 0.65
       500  one row at 6        0.65 0.88 1.00  0.95 1.00 1.00  0.68 0.93 0.97
       500  a climb to 3        0.20 0.50 0.85  0.07 0.60 1.00  0.03 0.55 0.97
       500  a climb to 6        0.70 0.95 1.00  0.88 1.00 1.00  0.95 1.00 1.00

A value every trial adds to every block moves no rank, so a family of trials that all share one
edge scores what noise scores, draw for draw, whatever the edge's size. Noise passed a ceiling of
0.5 in 16 to 27 of its forty matrices at every size and block count, so a plan could carry the
gate there and price almost nothing; at 0.25 it passed in at most 14 with two trials and 6 with
fifty or five hundred, while one trial six errors above noise passed in 35 or more at every size.
So `max_pbo` stops at 0.25. A climb to 6 passed 0.25 in 33 or more with fifty trials or more, a
climb to 3 in 16 to 24; one trial at 3 among five hundred, about the largest of that many draws
of noise, in 3 to 11 — barely told from noise. At low settings the estimate is coarse: two
trials rank one above the other or tie, and `half_blocks` 2 makes six splits, so the estimate
moves in sixths. The gate complements a deflation and never replaces one: it carries no
`deflates`, and whether a plan includes it is the planner's choice.

**It reads stored measurements and runs nothing.** The matrix comes from the reader the
certification runner hands it, which reads every trial's newest card that shares the subject's
grid off the cards the store keeps, so the lane that stalled and the certification child read the
same matrix; a gate in `criteria` reaches nothing of `research`. The settings a family declares
tried outside kanso were never measured here, so they are no rows: the evidence states them as
`prior_trials` beside `searched`, the draws a deflation counts, and `excluded` says how many
carded trials made no row and why.

It judges nothing — and passes, saying why — without both its parameters, an objective, a
reader, a grid, two rows, or a grid of at least `S` period ends.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache
from itertools import batched, combinations
from typing import ClassVar, Final

import numpy as np
from numpy.typing import NDArray

from kanso.criteria.context import Gate, GateContext, count, number, skipped, verdict
from kanso.criteria.gates import BLOCK
from kanso.criteria.objectives import REGISTRY
from kanso.schemas import GateResult

NO_CHOICE: Final = "no block count or ceiling was chosen, so no selection was priced"
NO_OBJECTIVE: Final = "the hypothesis carries no objective to measure"
NO_READER: Final = "no reader of the trials' stored measurements was supplied"
NO_GRID: Final = (
    "the subject's newest card kept no measurement that could be read, so there is no grid to "
    "hold the trials to"
)
ONE_ROW: Final = (
    "fewer than two trials kept a measurement on the subject's grid, so there was no selection "
    "to price"
)


def overfitting(
    values: Sequence[Sequence[float]], half_blocks: int, chunk: int = BLOCK
) -> tuple[float, float, int]:
    """The probability of backtest overfitting of a matrix of block values, rows by blocks;
    the share of splits whose picked row lost out of sample; and how many splits there were.

    Every split of the `2 × half_blocks` blocks into halves is taken, `chunk` at a time, in the
    order `itertools.combinations` yields the halves. The count of splits that are overfit is
    exact, since a rank is set against the median in whole numbers: `ω ≤ ½` is `2r ≤ N + 1`, and
    `2r = 2·#{below} + #{equal} + 1` is whole. Two identical rows score identically on every
    split however the platform's matrix product orders its sums, since each distinct row is
    scored once and its scores copied to every row that repeats it — so identical results tie,
    as the midrank says they do."""
    matrix = np.asarray(values, dtype=np.float64)
    rows, blocks = matrix.shape
    distinct, repeats = np.unique(matrix, axis=0, return_inverse=True)
    order = None if len(distinct) == rows else repeats.reshape(-1)
    scored = matrix if order is None else distinct
    overfit = lost = splits = 0
    for inside in halves(blocks, half_blocks, chunk):
        width = len(inside)
        chosen = means(inside, scored, order)
        held_out = means(1.0 - inside, scored, order)
        picked = held_out[np.arange(width), np.argmax(chosen, axis=1)]
        below = np.count_nonzero(held_out < picked[:, None], axis=1)
        equal = np.count_nonzero(held_out == picked[:, None], axis=1)
        overfit += int(np.count_nonzero(2 * below + equal + 1 <= rows + 1))
        lost += int(np.count_nonzero(picked <= 0.0))
        splits += width
    return overfit / splits, lost / splits, splits


@cache
def halves(blocks: int, half_blocks: int, chunk: int) -> tuple[NDArray[np.float64], ...]:
    """Every choice of `half_blocks` of `blocks` blocks as a 0/1 mask over them, in the order
    `itertools.combinations` yields them, `chunk` masks to an array; read-only, since they are
    built once per shape and shared."""
    found: list[NDArray[np.float64]] = []
    for group in batched(combinations(range(blocks), half_blocks), chunk):
        mask = np.zeros((len(group), blocks))
        mask[np.repeat(np.arange(len(group)), half_blocks), np.asarray(group).ravel()] = 1.0
        mask.setflags(write=False)
        found.append(mask)
    return tuple(found)


def means(
    masks: NDArray[np.float64],
    matrix: NDArray[np.float64],
    order: NDArray[np.intp] | None = None,
) -> NDArray[np.float64]:
    """Each row's mean over the blocks each mask selects, masks by rows, so a split's rows lie
    together in memory and its pick is read along them; with `order`, the rows of `matrix` are
    distinct ones and `order` names which of them each row of the matrix scored is."""
    result: NDArray[np.float64] = (masks @ matrix.T) / masks.sum(axis=1)[:, None]
    return result if order is None else result[:, order]


class _OverfitProbability:
    """The share of the research window's half-splits on which the search's in-sample best
    ranks at or below the median out of sample, held to `max_pbo`."""

    id: ClassVar[str] = "overfit_probability"

    def evaluate(self, ctx: GateContext) -> GateResult:
        half = count(ctx, "half_blocks")
        ceiling = number(ctx, "max_pbo")
        if half is None or ceiling is None:
            return skipped(self.id, NO_CHOICE)
        if ctx.hyp.objective is None or ctx.hyp.objective.id not in REGISTRY:
            return skipped(self.id, NO_OBJECTIVE)
        if ctx.trial_folds is None:
            return skipped(self.id, NO_READER)
        blocks = 2 * half
        found = ctx.trial_folds(blocks)
        if found is None:
            return skipped(self.id, NO_GRID)
        if len(found.values) < 2:
            return skipped(self.id, ONE_ROW)
        if found.periods < blocks:
            return skipped(
                self.id,
                f"the subject's grid holds {found.periods} period ends, fewer than the "
                f"{blocks} blocks, so a block would be empty by construction",
            )
        pbo, loss, splits = overfitting(found.values, half)
        return verdict(
            self.id,
            pbo <= ceiling,
            {
                "pbo": pbo,
                "max_pbo": ceiling,
                "blocks": blocks,
                "splits": splits,
                "rows": len(found.values),
                "subject_row": found.subject,
                "searched": ctx.trials,
                "prior_trials": ctx.prior_trials,
                "excluded": dict(found.excluded),
                "oos_loss": loss,
            },
        )


gate: Final[Gate] = _OverfitProbability()
