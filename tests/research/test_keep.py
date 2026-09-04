"""The keep rule: a strict improvement over a noise floor, doubled for a bigger file."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from kanso.research.keep import COMPLEXITY_FACTOR, grew_by, keep, line_count, threshold
from kanso.schemas import ObjectiveParams

PARAMS = {"min_delta": 0.0, "k_se": 1.0}


def test_the_first_keep_has_nothing_to_beat() -> None:
    assert keep(-5.0, 2.0, None, PARAMS, grew=1000, max_lines=40)


def test_an_improvement_must_clear_the_noise_floor() -> None:
    assert keep(1.01, 1.0, 0.0, PARAMS, grew=0, max_lines=40)
    assert not keep(0.99, 1.0, 0.0, PARAMS, grew=0, max_lines=40)


def test_an_equal_metric_is_not_an_improvement() -> None:
    assert not keep(1.0, 0.0, 1.0, {"min_delta": 0.0, "k_se": 0.5}, grew=0, max_lines=40)


def test_min_delta_is_the_floor_when_the_folds_agree() -> None:
    params = {"min_delta": 0.5, "k_se": 1.0}
    assert not keep(0.4, 0.0, 0.0, params, grew=0, max_lines=40)
    assert keep(0.6, 0.0, 0.0, params, grew=0, max_lines=40)


def test_growing_the_file_past_the_budget_doubles_the_coefficient() -> None:
    assert threshold(1.0, PARAMS, grew=40, max_lines=40) == 1.0
    assert threshold(1.0, PARAMS, grew=41, max_lines=40) == COMPLEXITY_FACTOR
    assert keep(1.5, 1.0, 0.0, PARAMS, grew=40, max_lines=40)
    assert not keep(1.5, 1.0, 0.0, PARAMS, grew=41, max_lines=40)


def test_the_parameters_may_arrive_as_the_model_that_holds_them() -> None:
    params = ObjectiveParams(min_delta=0.5, k_se=1.0)
    assert threshold(0.1, params, grew=0, max_lines=40) == 0.5


def test_growth_is_measured_in_lines() -> None:
    assert line_count(b"a\nb\n") == 2
    assert line_count(b"a\nb") == 2
    assert grew_by(b"a\nb\nc\n", b"a\n") == 2
    assert grew_by(b"a\n", b"a\nb\nc\n") == -2


@given(
    metric=st.floats(-1e6, 1e6),
    best=st.floats(-1e6, 1e6),
    se=st.floats(0.0, 1e3),
    min_delta=st.floats(0.0, 1e3),
    k_se=st.floats(0.5, 3.0),
    grew=st.integers(-100, 100),
)
def test_a_keep_always_strictly_beats_its_own_threshold(
    metric: float, best: float, se: float, min_delta: float, k_se: float, grew: int
) -> None:
    params = {"min_delta": min_delta, "k_se": k_se}
    kept = keep(metric, se, best, params, grew, max_lines=40)
    margin = threshold(se, params, grew, max_lines=40)
    assert kept == (metric - best > margin)
    assert margin >= 0.0
