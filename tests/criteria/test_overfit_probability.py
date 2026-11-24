"""The probability of backtest overfitting: its arithmetic by hand, its skips, and what it does
on matrices whose truth is known.

The first tests score matrices small enough to check by eye — three trials, four blocks, six
splits written out — and the properties every matrix keeps. The last one is a measurement: it
draws matrices of block values from a seeded generator, of trials that carry no edge, one that
carries one, and a climb whose edits sit below its best, scores each, and records how often the
estimate fell at or under a ceiling. That table is what the gate's docstring and
`docs/concepts.md` quote and what the range of `max_pbo` rests on, so it is asserted whole: a
change to the arithmetic that moves one cell fails here before it reaches a page.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import combinations
from math import comb
from pathlib import Path
from textwrap import indent
from typing import Any, Final

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from numpy.typing import NDArray

from kanso.criteria import catalogue, check_params
from kanso.criteria.context import TrialFolds
from kanso.criteria.gates import overfit_probability as module
from kanso.criteria.gates.overfit_probability import (
    NO_CHOICE,
    NO_GRID,
    NO_OBJECTIVE,
    NO_READER,
    ONE_ROW,
    gate,
    means,
    overfitting,
)
from kanso.criteria.objectives import REGISTRY
from tests.criteria.builders import build_run, context, make_hyp

ROOT = Path(__file__).resolve().parents[2]

HAND: Final = ((3.0, 1.0, 0.0, 2.0), (1.0, 2.0, 2.0, 1.0), (0.0, 0.0, 1.0, 1.0))
"""Three trials on four blocks, whose six splits are worked below."""

HAND_SPLITS: Final = (
    # chosen blocks, in-sample sums, the pick, out-of-sample sums, 2r, overfit
    ((0, 1), (4, 3, 0), 0, (2, 3, 2), 3, True),
    ((0, 2), (3, 3, 1), 0, (3, 3, 1), 5, False),
    ((0, 3), (5, 2, 1), 0, (1, 4, 1), 3, True),
    ((1, 2), (1, 4, 1), 1, (5, 2, 1), 4, True),
    ((1, 3), (3, 3, 1), 0, (3, 3, 1), 5, False),
    ((2, 3), (2, 3, 2), 1, (4, 3, 0), 4, True),
)
"""Each split of `HAND` by hand. The pick is the first row with the largest in-sample value;
`2r` is twice its midrank out of sample among the three, `2·#{below} + #{equal} + 1`, and the
split is overfit when `2r ≤ N + 1 = 4`: `(0, 2)` and `(1, 3)` tie the first two rows in sample,
so the first is picked, and out of sample it ties the second again at the top; `(1, 2)` and
`(2, 3)` put the pick at the median exactly, `ω = ½`, which counts as overfit."""


def folds_of(
    values: Sequence[Sequence[float]],
    *,
    subject: int = 0,
    periods: int = 8,
    excluded: dict[str, int] | None = None,
) -> Callable[[int], TrialFolds]:
    """A reader that answers every block count with this matrix, recording what it was asked."""

    def read(blocks: int) -> TrialFolds:
        read.asked.append(blocks)  # type: ignore[attr-defined]
        return TrialFolds(
            values=tuple(tuple(row) for row in values),
            subject=subject,
            excluded=excluded or {"unmeasured": 0},
            periods=periods,
        )

    read.asked = []  # type: ignore[attr-defined]
    return read


def judged(values: Sequence[Sequence[float]], half_blocks: int = 2, **fields: Any) -> Any:
    """The gate on a matrix, at a ceiling of a quarter."""
    return gate.evaluate(
        context(
            build_run((1.0,) * 8),
            stage="cert",
            params={"half_blocks": half_blocks, "max_pbo": 0.25},
            trial_folds=folds_of(values),
            **fields,
        )
    )


# --- the arithmetic by hand ------------------------------------------------------------


def test_six_splits_of_three_trials_worked_by_hand() -> None:
    assert [split for split, *_ in HAND_SPLITS] == list(combinations(range(4), 2))
    matrix = np.asarray(HAND)
    for split, sums, pick, held, twice_rank, overfit in HAND_SPLITS:
        inside = np.zeros((1, 4))
        inside[0, list(split)] = 1.0
        assert tuple(means(inside, matrix)[0] * 2) == sums
        assert tuple(means(1.0 - inside, matrix)[0] * 2) == held
        assert int(np.argmax(sums)) == pick
        below = sum(1 for value in held if value < held[pick])
        equal = sum(1 for value in held if value == held[pick])
        assert 2 * below + equal + 1 == twice_rank
        assert (twice_rank <= len(HAND) + 1) is overfit

    pbo, loss, splits = overfitting(HAND, 2)

    assert (pbo, loss, splits) == (4 / 6, 0.0, 6)


def test_the_hand_matrix_through_the_gate() -> None:
    result = judged(HAND)

    assert result.skipped is None and not result.passed, "two thirds is over a quarter"
    assert result.evidence == {
        "pbo": 4 / 6,
        "max_pbo": 0.25,
        "blocks": 4,
        "splits": 6,
        "rows": 3,
        "subject_row": 0,
        "searched": 1,
        "prior_trials": 0,
        "excluded": {"unmeasured": 0},
        "oos_loss": 0.0,
    }


def test_a_trial_that_wins_out_of_sample_on_every_split_scores_zero() -> None:
    dominant = ((9.0, 8.0, 9.5, 8.5), (1.0, -2.0, 3.0, 0.5), (0.0, 2.0, -1.0, 1.0))

    assert overfitting(dominant, 2) == (0.0, 0.0, 6)
    assert judged(dominant).passed


def test_an_in_sample_best_that_is_always_worst_out_of_sample_scores_one() -> None:
    """Every row sums to the same, so each row's out-of-sample sum is the total less its
    in-sample one, and the row the search picks is the one that does worst on the rest."""
    balanced = ((4.0, 0.0, 1.0, 1.0), (1.0, 2.0, 2.0, 1.0), (0.0, 1.0, 3.0, 2.0))
    assert {sum(row) for row in balanced} == {6.0}

    assert overfitting(balanced, 2)[0] == 1.0


def test_identical_trials_tie_at_the_median_and_count_as_overfit() -> None:
    """Two byte strings that produced one result are two trials that tie: every split puts the
    pick at the midrank `(N + 1) / 2`, `ω = ½`, and that is overfit — a selection among copies
    selected nothing."""
    row = (1.0, -2.0, 3.0, 0.5, 2.0, -1.0)

    assert overfitting([row] * 2, 3)[0] == 1.0
    assert overfitting([row] * 7, 3)[0] == 1.0
    best = (1.0, 2.0, 3.0, 0.5, 2.0, 1.0)
    assert overfitting([best, (0.0,) * 6, best], 3)[0] == 0.0, (
        "two copies of the best: the pick ties the other copy at the top, ω = 2.5 / 4"
    )


def test_the_mean_over_every_block_is_the_objective_s_own_metric_at_that_many_folds() -> None:
    """A half's value is the mean of its blocks' values, which with every block chosen is the
    number the objective itself computes at that many folds: kanso's metric arithmetic, the one
    deviation from the authors' recomputed statistic."""
    run = build_run((3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0, 2.0, -3.0, 1.0, 2.0, 0.0))
    for objective_id in ("wf_contribution_bps", "wf_sharpe_net"):
        objective = REGISTRY[objective_id]
        for blocks in (4, 6):
            row = objective.fold_values(run, blocks)
            whole = means(np.ones((1, blocks)), np.asarray([row]))[0, 0]
            assert whole == pytest.approx(objective.compute(run, blocks)[0], rel=1e-12)


def test_a_block_with_no_period_scores_zero_and_is_judged() -> None:
    """A fold that holds no period scores zero, and so does a block: it is a value, never a
    skip. Here eight periods in the first half of the window, cut into four blocks, leave the
    last two empty, and the gate judges on the zeros."""
    run = build_run((3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0, 2.0), days=16)
    row = REGISTRY["wf_contribution_bps"].fold_values(run, 4)

    assert row[2:] == (0.0, 0.0)
    assert judged([row, tuple(-value for value in row)]).skipped is None


# --- what it judges nothing on ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"params": {}}, NO_CHOICE),
        ({"params": {"half_blocks": 2}}, NO_CHOICE),
        ({"params": {"max_pbo": 0.25}}, NO_CHOICE),
        ({"hyp": make_hyp(objective=None, constraints=None)}, NO_OBJECTIVE),
        ({"trial_folds": None}, NO_READER),
        ({"trial_folds": lambda blocks: None}, NO_GRID),
        ({"trial_folds": folds_of([HAND[0]])}, ONE_ROW),
        (
            {"trial_folds": folds_of(HAND, periods=3)},
            "the subject's grid holds 3 period ends, fewer than the 4 blocks, so a block would be "
            "empty by construction",
        ),
    ],
)
def test_without_its_context_it_judges_nothing_and_says_why(
    fields: dict[str, Any], reason: str
) -> None:
    settings_: dict[str, Any] = {
        "stage": "cert",
        "params": {"half_blocks": 2, "max_pbo": 0.25},
        "trial_folds": folds_of(HAND),
        **fields,
    }

    result = gate.evaluate(context(build_run((1.0,) * 8), **settings_))

    assert (result.skipped, result.passed, result.evidence) == (reason, True, {})
    assert len(reason) <= 200


def test_the_reader_is_asked_for_twice_the_half_blocks() -> None:
    read = folds_of([(0.0,) * 10, (1.0,) * 10], periods=10)

    gate.evaluate(
        context(
            build_run((1.0,) * 10),
            stage="cert",
            params={"half_blocks": 5, "max_pbo": 0.25},
            trial_folds=read,
        )
    )

    assert read.asked == [10]  # type: ignore[attr-defined]


def test_its_evidence_states_the_search_it_cannot_read() -> None:
    """The settings a family declared tried outside kanso, and the cards that made no row, are
    stated beside the matrix: the gate prices only what shares the subject's grid."""
    excluded = {"unmeasured": 3, "other_objective": 1, "off_grid": 2, "unreadable": 0}
    result = gate.evaluate(
        context(
            build_run((1.0,) * 8),
            stage="cert",
            params={"half_blocks": 2, "max_pbo": 0.25},
            trial_folds=folds_of(HAND, subject=2, excluded=excluded),
            trials=49,
            prior_trials=40,
        )
    )

    assert result.evidence["searched"] == 49
    assert result.evidence["prior_trials"] == 40
    assert result.evidence["excluded"] == excluded
    assert result.evidence["subject_row"] == 2


# --- what every matrix keeps -----------------------------------------------------------


@pytest.mark.parametrize("half_blocks", range(2, 9))
def test_every_split_is_counted_once_whatever_the_chunk(half_blocks: int) -> None:
    rng = np.random.default_rng(half_blocks)
    matrix = rng.standard_normal((9, 2 * half_blocks))

    whole = overfitting(matrix, half_blocks)

    assert whole[2] == comb(2 * half_blocks, half_blocks)
    for chunk in (1, 7, 999, whole[2] + 1):
        assert overfitting(matrix, half_blocks, chunk) == whole


def test_the_split_counts_the_range_admits() -> None:
    counts = {half: overfitting(np.eye(2, 2 * half), half)[2] for half in (2, 4, 6, 8)}

    assert counts == {2: 6, 4: 70, 6: 924, 8: 12870}


def test_the_same_matrix_is_the_same_evidence() -> None:
    rng = np.random.default_rng(7)
    matrix = rng.standard_normal((40, 12)).tolist()

    assert judged(matrix, 6) == judged(matrix, 6)
    assert overfitting(matrix, 6) == overfitting(matrix, 6)


MATRICES = st.integers(2, 6).flatmap(
    lambda rows: st.sampled_from((2, 3)).flatmap(
        lambda half: st.lists(
            st.lists(
                st.floats(-100.0, 100.0, allow_nan=False, allow_infinity=False),
                min_size=2 * half,
                max_size=2 * half,
            ),
            min_size=rows,
            max_size=rows,
        )
    )
)


@settings(max_examples=60, deadline=None)
@given(MATRICES)
def test_the_probability_is_a_probability(values: list[list[float]]) -> None:
    pbo, loss, splits = overfitting(values, len(values[0]) // 2)

    assert 0.0 <= pbo <= 1.0
    assert 0.0 <= loss <= 1.0
    assert splits == comb(len(values[0]), len(values[0]) // 2)


def untied(matrix: NDArray[np.float64]) -> bool:
    """Whether no two rows come near sharing a half's mean on any split, in sample or out: apart
    by more than a matrix product's rounding could close, wherever in it a row sits."""
    half = matrix.shape[1] // 2
    for split in combinations(range(matrix.shape[1]), half):
        inside = np.zeros((1, matrix.shape[1]))
        inside[0, list(split)] = 1.0
        for mask in (inside, 1.0 - inside):
            scored = np.sort(means(mask, matrix)[0])
            if np.min(np.diff(scored)) <= 1e-9 * (1.0 + np.max(np.abs(scored))):
                return False
    return True


@settings(max_examples=60, deadline=None)
@given(MATRICES, st.randoms(use_true_random=False))
def test_without_ties_the_order_of_the_trials_moves_nothing(
    values: list[list[float]], shuffle: Any
) -> None:
    matrix = np.asarray(values)
    assume(untied(matrix))
    order = list(range(len(values)))
    shuffle.shuffle(order)

    assert overfitting(matrix[order], matrix.shape[1] // 2) == overfitting(
        matrix, matrix.shape[1] // 2
    )


# --- the library entry -----------------------------------------------------------------


def test_the_entry_is_an_optional_research_gate_that_deflates_nothing() -> None:
    item = catalogue()["overfit_probability"]

    assert (item.stage, item.required, item.together, item.reads) == (
        "cert",
        False,
        True,
        "research",
    )
    assert item.deflates is None and item.required_for is None
    assert item.params == {"half_blocks": "int", "max_pbo": "float"}
    assert len(item.meaningful_when or "") <= 200
    hyp = make_hyp()
    assert check_params(item, {"half_blocks": 2, "max_pbo": 0.0}, hyp, 4) == []
    assert check_params(item, {"half_blocks": 8, "max_pbo": 0.25}, hyp, 4) == []
    (half,) = check_params(item, {"half_blocks": 1, "max_pbo": 0.25}, hyp, 4)
    assert half == "half_blocks: 1 is outside the range [2, 8]"
    (bare,) = check_params(item, {"half_blocks": 2}, hyp, 4)
    assert bare.startswith("max_pbo: is not set")


# --- the measurement -------------------------------------------------------------------

SIZES: Final = (2, 50, 500)
HALVES: Final = (2, 4, 8)
CEILINGS: Final = (0.1, 0.25, 0.5)
REPEATS: Final = 40
CASES: Final = (("noise", 0.0), ("one", 3.0), ("one", 6.0), ("climb", 3.0), ("climb", 6.0))
"""The trials drawn: none with an edge; one with an edge of 3 or 6; and a climb, one row at 3 or
6 and every other below it by a uniform share of that edge."""

LABELS: Final = {"noise": "noise", "one": "one row at {:g}", "climb": "a climb to {:g}"}


def drawn(case: str, edge: float, rows: int, half: int, repeat: int) -> NDArray[np.float64]:
    """One matrix of block values, in units of the standard error of a trial's value over the
    whole window: each block's noise is the root of the block count, the same window cut finer.
    Every case draws the same noise for the same rows, halves and repeat, so the cases differ by
    their edges alone."""
    rng = np.random.default_rng([rows, half, repeat])
    blocks = 2 * half
    values = rng.standard_normal((rows, blocks)) * np.sqrt(blocks)
    if case == "one":
        values[0] += edge
    elif case == "climb":
        below = rng.uniform(0.0, edge, size=rows)
        below[0] = 0.0
        values += (edge - below)[:, None]
    return values


def measured() -> dict[tuple[int, str, float], tuple[tuple[int, ...], ...]]:
    """For every size, case and half: how many of `REPEATS` matrices scored at or under each
    ceiling."""
    table: dict[tuple[int, str, float], tuple[tuple[int, ...], ...]] = {}
    for rows in SIZES:
        for case, edge in CASES:
            cells = []
            for half in HALVES:
                scores = [
                    overfitting(drawn(case, edge, rows, half, repeat), half)[0]
                    for repeat in range(REPEATS)
                ]
                cells.append(tuple(sum(1 for pbo in scores if pbo <= c) for c in CEILINGS))
            table[(rows, case, edge)] = tuple(cells)
    return table


def rendered(table: dict[tuple[int, str, float], tuple[tuple[int, ...], ...]]) -> str:
    """The table as the gate's docstring and `docs/concepts.md` print it."""
    head = "  ".join(f"half_blocks {half}".ljust(14) for half in HALVES).rstrip()
    lines = [f"{'trials':>6}  {'drawn':<18}  {head}"]
    for (rows, case, edge), cells in table.items():
        label = LABELS[case].format(edge)
        shares = "  ".join(" ".join(f"{n / REPEATS:.2f}" for n in cell) for cell in cells)
        lines.append(f"{rows:>6}  {label:<18}  {shares}")
    return "\n".join(lines)


TABLE: Final = {
    (2, "noise", 0.0): ((8, 8, 17), (7, 8, 17), (6, 14, 22)),
    (2, "one", 3.0): ((31, 31, 37), (28, 30, 36), (25, 32, 37)),
    (2, "one", 6.0): ((40, 40, 40), (40, 40, 40), (40, 40, 40)),
    (2, "climb", 3.0): ((22, 22, 29), (10, 15, 25), (10, 17, 27)),
    (2, "climb", 6.0): ((31, 31, 34), (19, 25, 30), (22, 24, 32)),
    (50, "noise", 0.0): ((1, 6, 27), (1, 4, 22), (0, 5, 24)),
    (50, "one", 3.0): ((12, 22, 36), (2, 15, 31), (4, 20, 37)),
    (50, "one", 6.0): ((38, 40, 40), (37, 40, 40), (39, 40, 40)),
    (50, "climb", 3.0): ((5, 23, 35), (4, 16, 35), (4, 23, 39)),
    (50, "climb", 6.0): ((24, 33, 40), (28, 37, 40), (34, 39, 40)),
    (500, "noise", 0.0): ((0, 4, 24), (0, 2, 18), (0, 1, 16)),
    (500, "one", 3.0): ((4, 11, 28), (3, 10, 32), (0, 3, 26)),
    (500, "one", 6.0): ((26, 35, 40), (38, 40, 40), (27, 37, 39)),
    (500, "climb", 3.0): ((8, 20, 34), (3, 24, 40), (1, 22, 39)),
    (500, "climb", 6.0): ((28, 38, 40), (35, 40, 40), (38, 40, 40)),
}
"""How many of 40 seeded matrices scored at or under 0.1, 0.25 and 0.5, at `half_blocks` 2, 4
and 8, as `measured` draws them."""


def test_the_measured_table_is_what_the_arithmetic_gives() -> None:
    assert measured() == TABLE


def test_the_table_is_the_one_the_gate_and_the_concepts_page_quote() -> None:
    text = rendered(TABLE)

    assert indent(text, "    ") in (module.__doc__ or "")
    assert text in (ROOT / "docs" / "concepts.md").read_text(encoding="utf-8")


def test_an_edge_every_trial_shares_is_noise_draw_for_draw() -> None:
    """A value every row adds to every block moves no rank, so a family of trials that all
    share one edge scores what noise scores whatever the edge's size."""
    for rows in SIZES:
        for half in HALVES:
            for repeat in range(3):
                noise = drawn("noise", 0.0, rows, half, repeat)
                pbo, _, splits = overfitting(noise, half)
                for edge in (3.0, 30.0):
                    assert overfitting(noise + edge, half)[::2] == (pbo, splits)


def test_the_ceiling_stops_where_noise_still_passes_most_of_the_time() -> None:
    """At a ceiling of a half, noise — and every family of trials sharing one edge — passed in 16
    to 27 of forty matrices at every size and every block count measured, so a plan could carry
    the gate and price almost nothing; at a quarter it passed in at most 14 with two trials and 6
    with fifty or more, while one trial six standard errors above noise passed in 35 or more. So
    `max_pbo` stops at a quarter."""
    noise = {rows: TABLE[(rows, "noise", 0.0)] for rows in SIZES}
    assert all(16 <= cell[2] <= 27 for cells in noise.values() for cell in cells)
    assert all(cell[1] <= 14 for cell in noise[2])
    assert all(cell[1] <= 6 for rows in (50, 500) for cell in noise[rows])
    assert all(cell[1] >= 35 for rows in (50, 500) for cell in TABLE[(rows, "one", 6.0)])
    assert catalogue()["overfit_probability"].ranges["max_pbo"] == (0.0, 0.25)
