"""The max-T nulls — session sign flips and circular shifts of positions — deterministic,
family-wise, and honest about thin cells."""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kanso.errors import PreconditionError
from kanso.screen import nulls


def test_the_seed_is_the_pins_and_nothing_else() -> None:
    first = nulls.seed("a" * 64, "b" * 64, 0)
    assert first == nulls.seed("a" * 64, "b" * 64, 0)
    assert len({first, nulls.seed("c" * 64, "b" * 64, 0), nulls.seed("a" * 64, "c" * 64, 0)}) == 3
    assert nulls.seed("a" * 64, "b" * 64, 1) != first


def test_the_same_pins_give_the_same_evidence_to_the_bit() -> None:
    values = np.random.default_rng(1).normal(size=(5, 40))
    one = nulls.evidence(values, 999, 7)
    two = nulls.evidence(values.copy(), 999, 7)
    for field in ("mean", "se", "t", "sessions", "p"):
        assert getattr(one, field).tobytes() == getattr(two, field).tobytes()


def test_mean_se_and_t_are_across_sessions() -> None:
    values = np.asarray([[1.0, 2.0, 3.0, np.nan]])
    found = nulls.evidence(values, 99, 1)
    assert found.sessions.tolist() == [3]
    assert found.mean.tolist() == pytest.approx([2.0])
    assert found.se.tolist() == pytest.approx([1.0 / np.sqrt(3.0)])
    assert found.t.tolist() == pytest.approx([2.0 * np.sqrt(3.0)])


def test_a_planted_effect_ranks_above_every_draw() -> None:
    rng = np.random.default_rng(2)
    values = rng.normal(size=(20, 60))
    values[3] += 1.0
    found = nulls.evidence(values, 999, 3)
    assert found.p[3] == pytest.approx(1 / 1000)
    assert all(0 < p <= 1 for p in found.p)


def test_with_no_effect_the_family_reads_significant_at_most_as_often_as_alpha() -> None:
    hits = 0
    trials = 200
    for trial in range(trials):
        values = np.random.default_rng(100 + trial).normal(size=(10, 30))
        hits += int(np.min(nulls.evidence(values, 199, trial).p) <= 0.05)
    # The family-wise rate is at most alpha in expectation; three standard errors of a
    # binomial at 0.05 over 200 trials is about 0.046.
    assert hits / trials <= 0.05 + 0.046


def test_a_cell_with_nothing_to_judge_by_reads_nothing() -> None:
    values = np.asarray([[np.nan, 0.4, np.nan], [0.2, 0.2, 0.2], [0.0, 0.0, 0.0]])
    found = nulls.evidence(values, 99, 1)
    assert found.t.tolist() == [0.0, 0.0, 0.0]
    assert found.p.tolist() == [1.0, 1.0, 1.0]
    assert found.se.tolist() == [0.0, 0.0, 0.0]


def test_a_measure_with_no_session_reads_nothing() -> None:
    found = nulls.evidence(np.zeros((2, 0)), 99, 1)
    assert found.sessions.tolist() == [0, 0]
    assert found.p.tolist() == [1.0, 1.0]


def test_few_sessions_are_enumerated_and_their_p_is_exact() -> None:
    values = np.random.default_rng(3).normal(size=(4, 7))
    values[1] += 5.0

    found = [nulls.evidence(values, 9999, seeded) for seeded in (1, 2)]

    # 2^7 = 128 vectors, no more than the draws: each is taken once, so no seed reaches p,
    # and the observed signs and their negation are the only two that reach a planted |t|.
    assert found[0].p.tobytes() == found[1].p.tobytes()
    assert found[0].p[1] == 2 / 128


def test_no_p_is_below_what_the_sessions_a_cell_holds_can_give() -> None:
    values = np.random.default_rng(4).normal(size=(3, 14))
    values[0] += 5.0
    values[2, :10] = np.nan
    values[2, 10:] = [9.0, 10.0, 11.0, 12.0]

    drawn = np.asarray([nulls.evidence(values, 9999, seeded).p for seeded in range(1, 8)])

    # 2^14 vectors are more than the draws, so p is estimated, and an estimate that fell
    # below 2^(1 - 14) for a cell of 14 sessions, or 2^(1 - 4) for one of four, reads the
    # floor: no assignment of signs gives less, however the draws fell.
    assert drawn[:, 0].min() == 2.0**-13 and drawn[:, 2].min() == 2.0**-3
    assert nulls.evidence(values[:, :7], 9999, 5).p[0] >= 2.0**-6


def test_a_sign_flip_null_keeps_the_maxima_it_drew() -> None:
    values = np.random.default_rng(5).normal(size=(3, 12))

    drawn = nulls.evidence(values, 999, 1)
    enumerated = nulls.evidence(values[:, :6], 999, 1)

    assert len(drawn.maxima) == 999 and drawn.exact is False
    assert len(enumerated.maxima) == 2**6 and enumerated.exact is True
    assert np.all(enumerated.maxima >= 0)


def test_a_long_or_flat_value_is_the_position_times_the_return() -> None:
    positions = np.asarray([[1.0, 0.0, -1.0, np.nan]])
    returns = np.asarray([[2.0, 5.0, 3.0, 1.0]])

    found = nulls.values_of(positions, returns, contrast=False)

    assert np.array_equal(found, [[2.0, 0.0, -3.0, np.nan]], equal_nan=True)


def test_contrast_weights_average_to_the_mean_long_less_the_mean_short() -> None:
    positions = np.asarray([[1.0, 1.0, 1.0, -1.0, -1.0, 0.0, np.nan]])
    returns = np.asarray([[3.0, 6.0, 0.0, 1.0, 5.0, 9.0, 4.0]])

    found = nulls.values_of(positions, returns, contrast=True)

    held = found[~np.isnan(found)]
    assert len(held) == 5, "a flat session and an undefined one hold no value"
    assert float(np.mean(held)) == pytest.approx(3.0 - 3.0)
    lopsided = nulls.values_of(positions, returns + np.asarray([[4, 4, 4, 0, 0, 0, 0]]), True)
    assert float(np.nanmean(lopsided)) == pytest.approx(7.0 - 3.0)
    one_side = nulls.values_of(np.asarray([[1.0, 1.0, 0.0]]), np.asarray([[1.0, 2.0, 3.0]]), True)
    assert np.isnan(one_side).all(), "a contrast with one side empty reads nothing"


def test_blocks_sum_consecutive_sessions_and_hold_nothing_where_nothing_was_held() -> None:
    values = np.asarray([[1.0, 2.0, np.nan, np.nan, 5.0], [np.nan, 1.0, 1.0, 1.0, np.nan]])

    found = nulls.blocks(values, 2)

    assert np.array_equal(found, [[3.0, np.nan, 5.0], [1.0, 2.0, np.nan]], equal_nan=True)


def planted(sessions: int, seed: int, effect: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """A persistent state's positions — runs of several sessions — and returns, the effect
    added where the position is long."""
    rng = np.random.default_rng(seed)
    runs = np.repeat(rng.choice([-1.0, 1.0], size=sessions // 5 + 1), 5)[:sessions]
    returns = np.round(rng.normal(size=sessions) * 4) + effect * (runs > 0)
    return runs[None, :], returns[None, :]


def test_an_alignment_no_shift_reaches_reads_the_floor_of_its_shifts() -> None:
    positions, returns = planted(60, 1, effect=50.0)

    found = nulls.circular(positions, returns, True, 10, 999, 7)

    assert found.exact is True and len(found.maxima) == 60 - 2 * 10 + 1
    assert found.p[0] == pytest.approx(1 / (41 + 1))
    assert found.t[0] > 5 and found.sessions[0] == 60
    assert found.mean[0] == pytest.approx(
        float(np.mean(returns[0][positions[0] > 0]) - np.mean(returns[0][positions[0] < 0]))
    )


def test_shifts_more_than_the_draws_are_drawn_only_from_the_admissible_ones() -> None:
    positions, returns = planted(80, 2, effect=50.0)
    every = nulls.circular(positions, returns, False, 15, 9999, 3)

    drawn = nulls.circular(positions, returns, False, 15, 20, 3)

    assert every.exact is True and drawn.exact is False and len(drawn.maxima) == 20
    assert set(drawn.maxima.tolist()) <= set(every.maxima.tolist())
    assert abs(drawn.t[0]) not in set(every.maxima.tolist()), "the identity is never a draw"
    again = nulls.circular(positions, returns, False, 15, 20, 3)
    assert (
        again.p.tobytes() == drawn.p.tobytes() and again.maxima.tobytes() == drawn.maxima.tobytes()
    )


def test_one_shift_moves_every_cell_of_the_family_together() -> None:
    positions, returns = planted(50, 3)
    twice = nulls.circular(
        np.vstack([positions, positions]), np.vstack([returns, returns]), True, 5, 999, 1
    )
    once = nulls.circular(positions, returns, True, 5, 999, 1)

    assert twice.t[0] == twice.t[1] == once.t[0]
    assert twice.maxima.tobytes() == once.maxima.tobytes(), "a copy of a cell adds no chance"
    assert twice.p.tolist() == [once.p[0], once.p[0]]


def test_too_few_sessions_for_the_shift_are_refused() -> None:
    positions, returns = planted(19, 4)

    with pytest.raises(PreconditionError, match="19 session") as refused:
        nulls.circular(positions, returns, True, 10, 999, 1)

    assert "lower min_shift below 9.5" in (refused.value.remedy or "")


def test_a_family_with_no_cell_reads_nothing() -> None:
    found = nulls.circular(np.zeros((0, 30)), np.zeros((0, 30)), True, 5, 99, 1)

    assert len(found.p) == 0 and len(found.maxima) == 0


def test_with_nothing_planted_p_is_near_uniform_under_both_nulls() -> None:
    families = 200
    flips = shifts = 0
    for seed in range(families):
        rng = np.random.default_rng(1000 + seed)
        runs = np.repeat(rng.choice([-1.0, 1.0], size=(3, 21)), 5, axis=1)[:, :100]
        returns = rng.normal(size=(3, 100))
        values = nulls.values_of(runs, returns, contrast=True)
        flips += int(np.min(nulls.evidence(nulls.blocks(values, 5), 99, seed).p) <= 0.1)
        shifts += int(np.min(nulls.circular(runs, returns, True, 10, 99, seed).p) <= 0.1)
    # Binomial 99.9% bounds on 200 families at 0.1 are about 0.030 and 0.170.
    for hits in (flips, shifts):
        assert 0.030 <= hits / families <= 0.170, hits


def test_the_critical_t_is_the_least_a_cell_must_exceed_for_its_p_to_reach_alpha() -> None:
    maxima = np.asarray([1.0, 5.0, 3.0, 2.0, 4.0])

    assert nulls.critical(maxima, 0.5, enumerated_flips=False) == 3.0
    assert nulls.critical(maxima, 0.1, enumerated_flips=False) is None
    assert nulls.critical(maxima, 0.4, enumerated_flips=True) == 3.0
    assert nulls.critical(maxima, 1.0, enumerated_flips=True) == 0.0
    assert nulls.critical(maxima, 0.29, enumerated_flips=False) == 5.0
    # 0.29 x 100 is 28.999999999999996 in floating point: the count is the verdict's own p.
    hundred = np.arange(99, 0, -1, dtype=np.float64)
    assert nulls.critical(hundred, 0.29, enumerated_flips=False) == 71.0
    # And an alpha one ulp under 5/6 multiplies to exactly 5, which its p would exceed.
    assert nulls.critical(maxima, math.nextafter(5 / 6, 0), enumerated_flips=False) == 2.0
    # A cell just above t* reads a p at alpha or under, and one at it does not.
    for alpha in (0.2, 0.34, 0.5, 0.67, 0.99):
        found = nulls.critical(maxima, alpha, enumerated_flips=False)
        assert found is not None
        reached = int(np.sum(maxima >= found + 1e-9))
        assert (1 + reached) / 6 <= alpha
        assert (1 + int(np.sum(maxima >= found))) / 6 > alpha or found == 0.0


ROTATED = st.integers(0, 39)


@given(seed=st.integers(0, 10_000), shift=ROTATED, contrast=st.booleans())
@settings(max_examples=40, deadline=None)
def test_rotating_positions_and_returns_together_leaves_an_exact_p_unchanged(
    seed: int, shift: int, contrast: bool
) -> None:
    positions, returns = planted(40, seed, effect=float(seed % 3))

    one = nulls.circular(positions, returns, contrast, 6, 999, 1)
    two = nulls.circular(
        np.roll(positions, shift, axis=1), np.roll(returns, shift, axis=1), contrast, 6, 999, 1
    )

    assert one.exact and two.exact
    assert one.p.tolist() == two.p.tolist()


@given(seed=st.integers(0, 10_000), minimum=st.integers(1, 15), draws=st.sampled_from([9, 99]))
@settings(max_examples=40, deadline=None)
def test_every_circular_p_lies_between_its_floor_and_one(
    seed: int, minimum: int, draws: int
) -> None:
    positions, returns = planted(40, seed, effect=float(seed % 4))

    found = nulls.circular(positions, returns, bool(seed % 2), minimum, draws, seed)

    admissible = 40 - 2 * minimum + 1
    floor = 1 / (min(admissible, draws) + 1)
    assert all(floor - 1e-12 <= p <= 1.0 for p in found.p.tolist())
