"""The max-T nulls — session sign flips and circular shifts of positions — deterministic,
family-wise, and honest about thin cells."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kanso.screen import demean, nulls


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


def test_a_long_or_flat_value_is_the_position_less_the_mean_position_times_the_return() -> None:
    positions = np.asarray([[1.0, 1.0, 1.0, 0.0, np.nan], [-1.0, 0.0, 0.0, 0.0, -1.0]])
    returns = np.asarray([[2.0, -4.0, 6.0, -4.0, 9.0], [1.0, 2.0, np.nan, 3.0, 4.0]])

    found = nulls.values_of(positions, returns, contrast=False)

    # Over the four sessions each row holds, the mean position is 3/4 and -1/2.
    expected = [[0.5, -1.0, 1.5, 3.0, np.nan], [-0.5, 1.0, np.nan, 1.5, -2.0]]
    assert np.allclose(found, expected, equal_nan=True)
    # The first row's returns sum to zero, as residuals with an intercept do: the values sum to
    # the position times the return, so their mean is the excess a session.
    assert float(np.nansum(found[0])) == pytest.approx(2.0 - 4.0 + 6.0)
    shifted = nulls.values_of(positions, returns + 7.0, contrast=False)
    assert np.allclose(np.nansum(shifted, axis=1), np.nansum(found, axis=1)), "nor a constant"


def test_with_nothing_planted_long_or_flat_sign_flips_hold_their_level() -> None:
    # Residuals of a drifting follower sum to zero, so a position times its residual spreads
    # less than sign flips of it assume: measured on these seeds, uncentred values put 1.3% of
    # p at or under 0.05 at an exposure of a half, t spreading 0.73, and none at nine tenths,
    # t spreading 0.32; centred, 5.7% and 4.7%, t spreading 1.03 and 0.99.
    for exposure in (0.5, 0.9):
        ps, ts = [], []
        for seed in range(1000):
            rng = np.random.default_rng(7000 + seed)
            positions = (rng.random(200) < exposure).astype(np.float64)
            returns = 10.0 + rng.normal(size=200) * 30.0
            residuals = returns - float(np.mean(returns))
            values = nulls.values_of(positions[None, :], residuals[None, :], contrast=False)
            assert float(np.sum(values)) == pytest.approx(float(np.sum(positions * residuals)))
            found = nulls.evidence(nulls.blocks(values, 3), 199, seed)
            ps.append(found.p[0])
            ts.append(found.t[0])
        rejected(np.asarray(ps), 0.05, 10 / 200)
        assert 0.9 <= float(np.std(ts)) <= 1.1, "t spreads as a t, whatever the exposure"


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


def weekday_design(sessions: int) -> tuple[np.ndarray, list[str]]:
    """Weekday dummies over `sessions` consecutive weekdays from a Monday."""
    days = [date(2024, 1, 1) + timedelta(days=7 * (n // 5) + n % 5) for n in range(sessions)]
    return demean.design(days, ["weekday"], {}, None)


def leaning(rng: np.random.Generator, sessions: int, contrast: bool) -> np.ndarray:
    """A state that leans on Mondays and Tuesdays, so the weekday design shares about half of
    it; under a contrast one session in five is in neither state."""
    lean = np.arange(sessions) % 5 < 2
    long = rng.random(sessions) < np.where(lean, 0.85, 0.15)
    if not contrast:
        return long.astype(np.float64)
    return np.where(rng.random(sessions) < 0.2, 0.0, np.where(long, 1.0, -1.0))


@pytest.mark.parametrize("contrast", [False, True])
def test_residualised_on_the_design_the_values_spread_as_the_estimate_does(contrast: bool) -> None:
    # A state sharing half of itself with a weekday design. Residualised, the mean se was
    # measured here at 1.011 times the estimate's spread over the redraws long or flat and 0.991
    # under a contrast; the weights alone at 1.467 and 1.324, against 1 / sqrt(1 - R²) of 1.447
    # and 1.477 at the R² of 0.522 and 0.541 the state has on the weekdays.
    rng = np.random.default_rng(8)
    sessions = 500
    matrix, names = weekday_design(sessions)
    spanned = demean.spanned(matrix, names)
    positions = leaning(rng, sessions, contrast)
    moving = positions != 0 if contrast else np.ones(sessions, dtype=bool)
    r2 = demean.r2(matrix[moving], names, (positions[moving] > 0).astype(np.float64))
    assert r2 is not None and 0.5 <= r2 <= 0.55
    estimates, ses, naive = [], [], []
    for _ in range(2000):
        returns = rng.normal(size=sessions) * 30 + 5.0 * (np.arange(sessions) % 5)
        residuals = demean.residuals(matrix, names, returns)[0]
        values = nulls.values_of(positions[None], residuals[None], contrast, spanned[None])
        plain = nulls.values_of(positions[None], residuals[None], contrast)
        estimates.append(float(np.nanmean(plain)))
        assert float(np.nanmean(values)) == pytest.approx(estimates[-1], abs=1e-9)
        ses.append(float(nulls.evidence(values, 1, 0).se[0]))
        naive.append(float(nulls.evidence(plain, 1, 0).se[0]))
    spread = float(np.std(estimates, ddof=1))
    assert 0.94 <= float(np.mean(ses)) / spread <= 1.06
    overstated = float(np.mean(naive)) / spread
    assert overstated >= 1.25, "the weights alone overstate it"
    inflation = 1 / math.sqrt(1 - r2)
    if contrast:
        assert overstated < 0.95 * inflation, "by less than 1 / sqrt(1 - R²) against a comparison"
    else:
        assert overstated == pytest.approx(inflation, rel=0.05), "by 1 / sqrt(1 - R²) long or flat"


def test_a_contrast_s_flat_session_holds_a_value_only_when_the_design_spans_more() -> None:
    positions = np.asarray([[1.0, 1.0, -1.0, 0.0, -1.0, 0.0, 1.0, -1.0, 1.0, 0.0]])
    returns = np.asarray([[3.0, -1.0, 2.0, 5.0, -4.0, 1.0, 2.0, -3.0, 0.5, -5.5]])
    matrix, names = weekday_design(10)
    spanned = demean.spanned(matrix, names)

    alone = nulls.values_of(positions, returns, True, np.zeros((1, 0, 10)))
    spanning = nulls.values_of(positions, returns, True, spanned[None])

    assert np.array_equal(np.isnan(alone), positions == 0), "the intercept alone: as before"
    assert np.array_equal(alone, nulls.values_of(positions, returns, True), equal_nan=True)
    assert not np.isnan(spanning).any(), "every held session holds one"
    # Four sessions long and three short of ten: each weighed 10/4 and -10/3, a flat one 0, and
    # every weight residualised on the weekdays, as the residuals would be.
    weights = np.where(positions[0] > 0, 10 / 4, np.where(positions[0] < 0, -10 / 3, 0.0))
    left = weights - matrix @ np.linalg.lstsq(matrix, weights, rcond=None)[0]
    assert np.allclose(spanning[0], left * returns[0], rtol=0, atol=1e-12)


def test_a_circular_shift_residualises_the_moved_positions_afresh() -> None:
    rng = np.random.default_rng(3)
    matrix, names = weekday_design(60)
    spanned = demean.spanned(matrix, names)
    positions = leaning(rng, 60, contrast=False)
    residuals = demean.residuals(matrix, names, rng.normal(size=60))[0]

    found = nulls.circular(positions[None], residuals[None], False, 999, 1, spanned[None])
    plain = nulls.circular(positions[None], residuals[None], False, 999, 1)

    assert found.mean[0] == pytest.approx(plain.mean[0])
    assert found.se[0] < plain.se[0], "the state shares the weekdays, so its own se is less"
    for shift in (1, 7, 30, 59):
        moved = nulls.values_of(
            np.roll(positions, shift)[None], residuals[None], False, spanned[None]
        )
        assert found.maxima[shift - 1] == pytest.approx(abs(nulls.evidence(moved, 1, 0).t[0]))


def calendar_year() -> tuple[np.ndarray, list[str]]:
    """Weekday and month dummies over the 250 weekdays from Monday 2 January 2023."""
    days = [date(2023, 1, 2) + timedelta(days=7 * (n // 5) + n % 5) for n in range(250)]
    return demean.design(days, ["weekday", "month"], {}, None)


def test_residualised_block_sums_run_above_their_level_where_a_design_group_spans_blocks() -> None:
    # A month is about four blocks of five here, and its residuals sum to zero over it, as do the
    # residualised weights: their block sums then share the month's fitted mean across blocks,
    # and spread less than their total does. Measured on these seeds, 6.95% of p fell at or under
    # 0.05 and 12.25% at or under 0.10 residualised, and 3.05% and 6.85% on the weights alone —
    # which is why sign flips read the weights alone, and only circular shifts residualise.
    matrix, names = calendar_year()
    spanned = demean.spanned(matrix, names)
    found: dict[bool, list[float]] = {False: [], True: []}
    for seed in range(2000):
        rng = np.random.default_rng(60000 + seed)
        runs = persistent(rng, 250, 10.0)
        residuals = demean.residuals(matrix, names, rng.normal(size=250))[0]
        for residualised in found:
            values = nulls.values_of(
                runs[None], residuals[None], True, spanned[None] if residualised else None
            )
            found[residualised].append(
                float(nulls.evidence(nulls.blocks(values, 5), 199, seed).p[0])
            )
    above, alone = np.asarray(found[True]), np.asarray(found[False])
    assert float(np.mean(alone <= 0.05)) <= 0.04 and float(np.mean(above <= 0.05)) >= 0.06
    assert float(np.mean(alone <= 0.10)) <= 0.08 and float(np.mean(above <= 0.10)) >= 0.11


def test_with_nothing_planted_residualised_shifts_hold_the_level_on_a_calendar() -> None:
    matrix, names = calendar_year()
    spanned = demean.spanned(matrix, names)
    ps = []
    for seed in range(1500):
        rng = np.random.default_rng(70000 + seed)
        runs = persistent(rng, 250, 30.0)
        residuals = demean.residuals(matrix, names, rng.normal(size=250))[0]
        ps.append(nulls.circular(runs[None], residuals[None], True, 9999, seed, spanned[None]).p[0])
    # p is (1 + reaching) / 250: at most 0.05 is reaching eleven or fewer, 0.10 twenty-four.
    rejected(np.asarray(ps), 0.05, 12 / 250)
    rejected(np.asarray(ps), 0.10, 25 / 250)


def test_a_circular_null_is_exact_when_its_shifts_are_exactly_the_draws() -> None:
    positions, returns = planted(20, 4)

    assert nulls.circular(positions, returns, True, 19, 1).exact is True
    assert nulls.circular(positions, returns, True, 18, 1).exact is False


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


def persistent(rng: np.random.Generator, sessions: int, mean: float) -> np.ndarray:
    """Positions of one sign for runs of geometric length averaging `mean` sessions."""
    found = np.empty(sessions)
    at, side = 0, float(rng.choice([-1.0, 1.0]))
    while at < sessions:
        length = int(rng.geometric(1 / mean))
        found[at : at + length] = side
        at, side = at + length, -side
    return found


def rejected(ps: np.ndarray, alpha: float, nominal: float) -> None:
    """The share of `ps` at or under `alpha` lies within binomial 99.9% bounds of `nominal`."""
    se = math.sqrt(nominal * (1 - nominal) / len(ps))
    share = float(np.mean(ps <= alpha))
    assert nominal - 3.3 * se <= share <= nominal + 3.3 * se, (alpha, share, nominal)


def test_an_alignment_no_shift_reaches_reads_the_floor_of_its_shifts() -> None:
    positions, returns = planted(60, 1, effect=50.0)

    found = nulls.circular(positions, returns, True, 999, 7)

    assert found.exact is True and len(found.maxima) == 60 - 1
    assert found.p[0] == pytest.approx(1 / 60)
    assert found.t[0] > 5 and found.sessions[0] == 60
    assert found.mean[0] == pytest.approx(
        float(np.mean(returns[0][positions[0] > 0]) - np.mean(returns[0][positions[0] < 0]))
    )


def test_every_shift_is_taken_once_when_they_are_no_more_than_the_draws() -> None:
    assert nulls._shifts(7, 6, 1).tolist() == [1, 2, 3, 4, 5, 6]
    assert nulls._shifts(7, 9999, 1).tolist() == [1, 2, 3, 4, 5, 6]
    assert nulls._shifts(1, 9999, 1).tolist() == [] and nulls._shifts(0, 9999, 1).tolist() == []


def test_shifts_more_than_the_draws_are_drawn_from_every_one_but_the_identity() -> None:
    drawn = [nulls._shifts(9, 5, seeded) for seeded in range(60)]

    for shifts in drawn:
        assert len(shifts) == 5 and len(set(shifts.tolist())) == 5, "never one twice"
    assert set(np.concatenate(drawn).tolist()) == set(range(1, 9)), "1 and N - 1 included"
    assert nulls._shifts(9, 5, 3).tobytes() == drawn[3].tobytes()


def test_drawn_shifts_are_a_sample_of_every_shift_s_maxima() -> None:
    positions, returns = planted(80, 2, effect=50.0)
    every = nulls.circular(positions, returns, False, 9999, 3)

    drawn = nulls.circular(positions, returns, False, 20, 3)

    assert every.exact is True and drawn.exact is False and len(drawn.maxima) == 20
    assert drawn.maxima.tolist() == every.maxima[nulls._shifts(80, 20, 3) - 1].tolist()
    assert abs(drawn.t[0]) not in set(every.maxima.tolist()), "the identity is never a draw"
    again = nulls.circular(positions, returns, False, 20, 3)
    assert (
        again.p.tobytes() == drawn.p.tobytes() and again.maxima.tobytes() == drawn.maxima.tobytes()
    )


def test_one_shift_moves_every_cell_of_the_family_together() -> None:
    positions, returns = planted(50, 3)
    twice = nulls.circular(
        np.vstack([positions, positions]), np.vstack([returns, returns]), True, 999, 1
    )
    once = nulls.circular(positions, returns, True, 999, 1)

    assert twice.t[0] == twice.t[1] == once.t[0]
    assert twice.maxima.tobytes() == once.maxima.tobytes(), "a copy of a cell adds no chance"
    assert twice.p.tolist() == [once.p[0], once.p[0]]


def test_a_family_with_no_session_or_one_reads_nothing_and_two_have_one_shift() -> None:
    for sessions in (0, 1):
        found = nulls.circular(np.ones((2, sessions)), np.ones((2, sessions)), False, 99, 1)
        assert found.sessions.tolist() == [sessions, sessions] and found.p.tolist() == [1.0, 1.0]
        assert len(found.maxima) == 0 and found.exact is True

    two = nulls.circular(np.asarray([[1.0, 0.0]]), np.asarray([[3.0, 1.0]]), False, 99, 1)

    assert len(two.maxima) == 1 and two.sessions.tolist() == [2]
    assert two.p[0] in (0.5, 1.0), "the observed alignment and the one shift there is"


def test_a_family_with_no_cell_reads_nothing() -> None:
    found = nulls.circular(np.zeros((0, 30)), np.zeros((0, 30)), True, 99, 1)

    assert len(found.p) == 0 and len(found.maxima) == 0


def test_with_nothing_planted_block_sign_flips_hold_their_level() -> None:
    ps = []
    for seed in range(400):
        rng = np.random.default_rng(1000 + seed)
        runs = np.vstack([persistent(rng, 100, 6.0) for _ in range(3)])
        returns = rng.normal(size=(3, 100))
        values = nulls.values_of(runs, returns, contrast=True)
        ps.append(np.min(nulls.evidence(nulls.blocks(values, 4), 99, seed).p))
    # Blocks of four never line up with runs averaging six sessions.
    rejected(np.asarray(ps), 0.1, 0.1)


def test_with_nothing_planted_every_shift_holds_the_level_at_a_small_alpha() -> None:
    # Runs averaging 30 of 150 sessions: a null that left out every shift under 10 either way
    # was measured on these seeds putting 1.75% of p at or under 0.01; this one puts 0.55%.
    ps = []
    for seed in range(2000):
        rng = np.random.default_rng(seed)
        runs = persistent(rng, 150, 30.0)
        returns = rng.normal(size=150)
        returns -= returns.mean()
        ps.append(nulls.circular(runs[None, :], returns[None, :], True, 9999, seed).p[0])
    found = np.asarray(ps)
    # p is (1 + reaching) / 150: at most 0.01 is reaching none, at most 0.05 reaching six.
    rejected(found, 0.01, 1 / 150)
    rejected(found, 0.05, 7 / 150)


def test_under_a_wandering_mean_flips_run_above_their_level_and_shifts_hold_it() -> None:
    # The returns' mean is an AR(1) of coefficient 0.99 whose stationary standard deviation is
    # 0.3 of the noise's, so every session of a run of positions shares its excursion. Measured
    # on these seeds, flips one session at a time put 12.25% of p at or under 0.05 and 19.3% at
    # or under 0.10, and circular shifts 5.25% and 10.15%.
    flips, shifts = [], []
    for seed in range(2000):
        rng = np.random.default_rng(80000 + seed)
        runs = persistent(rng, 250, 20.0)
        level, wandering = rng.normal() * 0.3, np.empty(250)
        for at in range(250):
            level = 0.99 * level + 0.3 * math.sqrt(1 - 0.99**2) * rng.normal()
            wandering[at] = level
        returns = rng.normal(size=250) + wandering
        returns -= returns.mean()
        values = nulls.values_of(runs[None], returns[None], contrast=True)
        flips.append(nulls.evidence(values, 199, seed).p[0])
        shifts.append(nulls.circular(runs[None], returns[None], True, 9999, seed).p[0])
    flipped = np.asarray(flips)
    # Flips: p is (1 + reaching) / 200, at most 0.05 when nine or fewer reach, 0.10 nineteen.
    for alpha, nominal in ((0.05, 10 / 200), (0.10, 20 / 200)):
        bound = nominal + 3.3 * math.sqrt(nominal * (1 - nominal) / len(flipped))
        assert float(np.mean(flipped <= alpha)) > bound, (alpha, "above the level")
    # Shifts: every one of the 249 is taken, and p is (1 + reaching) / 250.
    rejected(np.asarray(shifts), 0.05, 12 / 250)
    rejected(np.asarray(shifts), 0.10, 25 / 250)


def test_with_nothing_planted_drawn_shifts_hold_the_level() -> None:
    ps = []
    for seed in range(1000):
        rng = np.random.default_rng(5000 + seed)
        runs = np.vstack([persistent(rng, 150, 12.0) for _ in range(2)])
        returns = rng.normal(size=(2, 150))
        ps.append(np.min(nulls.circular(runs, returns, True, 49, seed).p))
    # 149 shifts and 49 draws: p is (1 + reaching) / 50, at most 0.1 when four or fewer reach.
    rejected(np.asarray(ps), 0.1, 5 / 50)


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

    one = nulls.circular(positions, returns, contrast, 999, 1)
    two = nulls.circular(
        np.roll(positions, shift, axis=1), np.roll(returns, shift, axis=1), contrast, 999, 1
    )

    # Each shift pairs the same positions with the same returns either way, summed in another
    # order: t and the maxima agree to rounding, and p does wherever no maximum, and no zero,
    # lies within rounding of |t| — a t of zero reads p one, and one of 1e-17 does not.
    assert one.exact and two.exact
    assert np.allclose(one.t, two.t, rtol=0, atol=1e-9)
    assert np.allclose(one.maxima, two.maxima, rtol=0, atol=1e-9)
    for row, t in enumerate(np.abs(one.t).tolist()):
        if t > 1e-9 and not np.any(np.abs(one.maxima - t) <= 1e-9):
            assert one.p[row] == two.p[row]


@given(seed=st.integers(0, 10_000), sessions=st.integers(2, 40), draws=st.sampled_from([9, 99]))
@settings(max_examples=40, deadline=None)
def test_every_circular_p_lies_between_its_floor_and_one(
    seed: int, sessions: int, draws: int
) -> None:
    positions, returns = planted(sessions, seed, effect=float(seed % 4))

    found = nulls.circular(positions, returns, bool(seed % 2), draws, seed)

    floor = 1 / (min(sessions - 1, draws) + 1)
    assert all(floor - 1e-12 <= p <= 1.0 for p in found.p.tolist())
