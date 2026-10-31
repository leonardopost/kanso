"""The keep rule: a strict improvement over a noise floor, doubled for a bigger file."""

from __future__ import annotations

import math
from datetime import date, timedelta
from functools import cache

import numpy as np
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria.run import CardRun, midnight_ns
from kanso.research.keep import (
    COMPLEXITY_FACTOR,
    Against,
    grew_by,
    keep,
    line_count,
    threshold,
)
from kanso.research.records import Measurement
from kanso.schemas import ObjectiveParams, ObjectiveRef

PARAMS = {"min_delta": 0.0, "k_se": 1.0}


def by(difference: float, se: float, own: float | None = None) -> Against:
    """A comparison with a best, by its difference, that difference's error and the card's
    own error, which is the difference's unless it is given."""
    return Against("b" * 64, difference, se, se if own is None else own)


def test_the_first_keep_has_nothing_to_beat() -> None:
    assert keep(None, PARAMS, grew=1000, max_lines=40)


def test_an_improvement_must_clear_the_noise_floor() -> None:
    assert keep(by(1.01, 1.0), PARAMS, grew=0, max_lines=40)
    assert not keep(by(0.99, 1.0), PARAMS, grew=0, max_lines=40)


def test_an_equal_metric_is_not_an_improvement() -> None:
    assert not keep(by(0.0, 0.0), {"min_delta": 0.0, "k_se": 0.5}, grew=0, max_lines=40)


def test_min_delta_is_the_floor_when_the_folds_agree() -> None:
    params = {"min_delta": 0.5, "k_se": 1.0}
    assert not keep(by(0.4, 0.0), params, grew=0, max_lines=40)
    assert keep(by(0.6, 0.0), params, grew=0, max_lines=40)


def test_growing_the_file_past_the_budget_doubles_the_coefficient() -> None:
    assert threshold(1.0, PARAMS, grew=40, max_lines=40) == 1.0
    assert threshold(1.0, PARAMS, grew=41, max_lines=40) == COMPLEXITY_FACTOR
    assert keep(by(1.5, 1.0), PARAMS, grew=40, max_lines=40)
    assert not keep(by(1.5, 1.0), PARAMS, grew=41, max_lines=40)


def test_the_parameters_may_arrive_as_the_model_that_holds_them() -> None:
    params = ObjectiveParams(min_delta=0.5, k_se=1.0)
    assert threshold(0.1, params, grew=0, max_lines=40) == 0.5


def test_growth_is_measured_in_lines() -> None:
    assert line_count(b"a\nb\n") == 2
    assert line_count(b"a\nb") == 2
    assert grew_by(b"a\nb\nc\n", b"a\n") == 2
    assert grew_by(b"a\n", b"a\nb\nc\n") == -2


def test_a_comparison_says_whether_it_was_paired_and_why_not() -> None:
    assert by(1.0, 0.5).payload() == {
        "best": "bbbbbbb",
        "difference": 1.0,
        "se": 0.5,
        "paired": True,
    }
    unpaired = Against("c" * 64, 1.0, 2.0, 2.0, "the best card kept no measurement")
    assert unpaired.payload() == {
        "best": "ccccccc",
        "difference": 1.0,
        "se": 2.0,
        "paired": False,
        "unpaired": "the best card kept no measurement",
    }


def test_the_bar_is_struck_on_the_larger_of_the_card_s_own_error_and_its_difference_s() -> None:
    """A difference known to a tenth is still held to the card's own error, and one known
    less well than the card is held to its own."""
    assert not keep(by(1.5, se=0.1, own=2.0), PARAMS, grew=0, max_lines=40)
    assert keep(by(2.5, se=0.1, own=2.0), PARAMS, grew=0, max_lines=40)
    assert not keep(by(1.5, se=2.0, own=1.0), PARAMS, grew=0, max_lines=40)
    assert keep(by(2.5, se=2.0, own=1.0), PARAMS, grew=0, max_lines=40)
    assert by(1.0, se=0.1, own=2.0).error == by(1.0, se=2.0, own=0.1).error == 2.0
    assert by(1.0, se=math.nan, own=2.0).error == 2.0, "never below the card's own"


@given(
    difference=st.floats(-1e6, 1e6),
    se=st.floats(0.0, 1e3),
    own=st.floats(0.0, 1e3),
    min_delta=st.floats(0.0, 1e3),
    k_se=st.floats(0.5, 3.0),
    grew=st.integers(-100, 100),
)
def test_a_keep_strictly_beats_its_threshold_and_is_never_one_its_own_error_refuses(
    difference: float, se: float, own: float, min_delta: float, k_se: float, grew: int
) -> None:
    params = {"min_delta": min_delta, "k_se": k_se}
    kept = keep(by(difference, se, own), params, grew, max_lines=40)
    margin = threshold(max(se, own), params, grew, max_lines=40)
    assert kept == (difference > margin)
    assert margin >= 0.0
    assert kept <= keep(by(difference, own), params, grew, max_lines=40)


SESSIONS = tuple(
    day for day in (date(2024, 1, 1) + timedelta(days=i) for i in range(366)) if day.weekday() < 5
)[:250]
CAPITAL = 100_000.0
CONTRIBUTION = ObjectiveRef.model_validate({"id": "wf_contribution_bps", "params": PARAMS})


def planted(bps: np.ndarray) -> Measurement:
    """A card earning `bps` of the capital each of 250 weekday sessions, over four folds."""
    returns = tuple(float(x) * CAPITAL / 10_000.0 for x in bps)
    run = CardRun(
        window=(SESSIONS[0], SESSIONS[-1]),
        period="1d",
        period_ends_ns=tuple(midnight_ns(day) + 21 * 3_600 * 10**9 for day in SESSIONS),
        returns=returns,
        equity=tuple(CAPITAL + total for total in np.cumsum(returns)),
        trades=(),
        fills=(),
        capital=CAPITAL,
        currency="USD",
        venue_model={},
    )
    return Measurement(CONTRIBUTION, 4, run, criteria_version="planted")


RULES = ("own", "paired", "alone")


@cache
def planted_rates() -> dict[tuple[str, str], tuple[float, float]]:
    """How often each rule keeps three candidates against a best, at `k_se` 0.5 and 1.

    On kanso's own arithmetic, a lane earning 15 bp a day at 120 bp of spread over 250
    sessions. `refined` is the best plus a true +3 bp a day and an edit's own noise, known to
    about 1.5 bp a day in the difference; `nothing` is the same edit with nothing in it;
    `unrelated` an independent strategy as good as the best. `own` strikes the bar on the
    candidate's own error alone, as the rule did until it paired; `paired` is the rule, on
    the larger of that error and the error of the difference; `alone` is a bar on the
    difference's error alone."""
    rng = np.random.default_rng(20261031)
    draws = 200
    kept = {(name, rule): [0, 0] for name in ("refined", "nothing", "unrelated") for rule in RULES}
    for _ in range(draws):
        noise = 120.0 * rng.standard_normal(250)
        edit = 1.5 * 250**0.5 * rng.standard_normal(250)
        best = planted(15.0 + noise)
        best_metric, _ = best.compute()
        for name, bps in (
            ("refined", 15.0 + noise + 3.0 + edit),
            ("nothing", 15.0 + noise + edit),
            ("unrelated", 15.0 + 120.0 * rng.standard_normal(250)),
        ):
            card = planted(bps)
            metric, own = card.compute()
            paired = card.against(best)
            assert paired is not None
            difference, error = paired
            for slot, k_se in enumerate((0.5, 1.0)):
                params = {"min_delta": 0.0, "k_se": k_se}
                kept[name, "own"][slot] += keep(by(metric - best_metric, own), params, 0, 40)
                kept[name, "paired"][slot] += keep(by(difference, error, own), params, 0, 40)
                kept[name, "alone"][slot] += keep(by(difference, error), params, 0, 40)
    return {key: (counts[0] / draws, counts[1] / draws) for key, counts in kept.items()}


def test_a_planted_edit_is_kept_as_its_own_error_kept_it_whether_or_not_it_improved() -> None:
    """A refinement truly worth +3 bp a day is kept about a quarter of the time at `k_se` 0.5
    and almost never at 1, and the same edit with nothing in it almost never: its own error,
    five times its difference's, is the larger, so pairing changes neither rate."""
    rates = planted_rates()
    for name in ("refined", "nothing"):
        assert rates[name, "paired"] == rates[name, "own"]
    assert rates["refined", "own"][0] < 0.40 and rates["refined", "own"][1] < 0.02
    assert rates["nothing", "own"][0] < 0.03 and rates["nothing", "own"][1] < 0.01


def test_on_its_difference_s_error_alone_an_empty_edit_is_kept_at_the_rate_k_se_states() -> None:
    """Struck on the difference's error alone, the bar keeps the refinement most of the time,
    and the edit with nothing in it about `1 − Φ(k_se)` of the time: no bar on that difference
    keeps more of the one without more of the other."""
    rates = planted_rates()
    assert rates["refined", "alone"][0] > 0.85 and rates["refined", "alone"][1] > 0.75
    assert 0.20 < rates["nothing", "alone"][0] < 0.42
    assert 0.08 < rates["nothing", "alone"][1] < 0.26


def test_a_planted_unrelated_equal_is_kept_no_more_often_than_on_its_own_error() -> None:
    """An unrelated strategy no better than the best has a difference known less well than
    either card, and is held to that: kept less often than its own error kept it."""
    rates = planted_rates()
    assert rates["unrelated", "paired"] == rates["unrelated", "alone"]
    assert all(
        paired < own
        for paired, own in zip(rates["unrelated", "paired"], rates["unrelated", "own"], strict=True)
    )
