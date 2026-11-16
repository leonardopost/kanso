"""The deflation gates at the L-th look of a lineage at its certification window.

At look L a deflation holds its probability to `1 − (1 − threshold) / L`, a Bonferroni split
of the planner's level (`kanso.criteria.gates.look_level`). What is pinned here is that the
first look is the gate as it always was — the same verdict on the same numbers, the level the
planner's own — that the level only rises with the count, that the count moves the verdict
and nothing else, and that a probability between two levels flips at the look the rule says.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria.gates import deflated_contribution, deflated_edge, deflated_sharpe, look_level
from tests.criteria.builders import build_run, context, make_hyp
from tests.criteria.test_gates_contribution import contribution_context, edge_context

SHARPE_HYP = {"objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 0.5}}}

ADDED = ("looks", "required_probability")
"""The two evidence keys the count adds; every other key is the gate's as it was."""

LEVELS = st.floats(min_value=0.5, max_value=0.999, allow_nan=False)
METRICS = st.lists(
    st.floats(min_value=-60.0, max_value=60.0, allow_nan=False), min_size=2, max_size=12
)
LOOKS = st.integers(min_value=1, max_value=60)

BETWEEN = (2.0, 2.4, 1.8, 2.2)
"""A search each gate's estimate clears with a probability above a half and below one."""


def sharpe_context(**overrides: Any) -> Any:
    research = build_run((3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0, 2.0))
    fields: dict[str, Any] = {
        "hyp": make_hyp(**SHARPE_HYP),
        "stage": "cert",
        "research_run": research,
    }
    return context(research, **{**fields, **overrides})


GATES: dict[str, tuple[Any, Any, str, str]] = {
    "deflated_contribution": (
        deflated_contribution,
        contribution_context,
        "min_probability",
        "probability",
    ),
    "deflated_edge": (deflated_edge, edge_context, "min_probability", "probability"),
    "deflated_sharpe": (deflated_sharpe, sharpe_context, "min_dsr", "dsr"),
}


def judged(name: str, level: float, metrics: list[float], looks: int | None) -> Any:
    """One deflation's result at `level` over `metrics`, at look `looks` or at none given."""
    gate, build, threshold, _ = GATES[name]
    counted = {} if looks is None else {"looks": looks}
    return gate.evaluate(build(params={threshold: level}, trial_metrics=metrics, **counted))


@pytest.mark.parametrize("name", sorted(GATES))
@given(level=LEVELS, metrics=METRICS, looks=LOOKS)
def test_the_count_moves_the_level_and_the_verdict_and_nothing_else(
    name: str, level: float, metrics: list[float], looks: int
) -> None:
    """A context that counts no looks is the first look: the planner's level, compared as it
    always was; at any count every number the gate measured is the same."""
    _, _, threshold, reading = GATES[name]
    unset = judged(name, level, metrics, None)
    first = judged(name, level, metrics, 1)
    later = judged(name, level, metrics, looks)

    assert unset == first
    if first.skipped is not None:
        assert later == first, "a gate that skips judges nothing at any look"
        return
    evidence = dict(first.evidence)
    assert evidence.pop("looks") == 1
    assert evidence.pop("required_probability") == level == evidence[threshold]
    assert first.passed == (evidence[reading] >= level)
    measured = {key: value for key, value in later.evidence.items() if key not in ADDED}
    assert measured == evidence
    assert later.evidence["looks"] == looks
    required = later.evidence["required_probability"]
    assert required == look_level(level, looks)
    assert later.passed == (evidence[reading] >= required)


@given(level=LEVELS, looks=LOOKS)
def test_the_level_is_the_planner_s_at_the_first_look_and_rises_with_every_one_after(
    level: float, looks: int
) -> None:
    assert look_level(level, 1) == level
    assert look_level(level, 0) == level, "no count is the first look"
    assert level <= look_level(level, looks) < look_level(level, looks + 1) < 1.0
    assert 1.0 - look_level(level, looks) == pytest.approx((1.0 - level) / looks)


@pytest.mark.parametrize(("looks", "required"), [(1, 0.95), (2, 0.975), (3, 0.98333), (4, 0.9875)])
def test_a_planned_ninety_five_is_held_to_the_split_level_at_each_look(
    looks: int, required: float
) -> None:
    """The levels the docs state for a plan at 0.95: a lineage with three looks behind it is
    held to 0.9875 at its fourth."""
    assert look_level(0.95, looks) == pytest.approx(required, abs=5e-6)


@pytest.mark.parametrize("name", sorted(GATES))
def test_a_probability_between_two_levels_passes_at_one_look_and_fails_at_the_next(
    name: str,
) -> None:
    """The level is struck from the probability the gate measured: its complement at three
    and a half times its own, so the third look passes and the fourth does not."""
    _, _, threshold, reading = GATES[name]
    measured = judged(name, 0.5, list(BETWEEN), None).evidence[reading]
    assert 0.5 < measured < 1.0
    level = 1.0 - (1.0 - measured) * 3.5

    third = judged(name, level, list(BETWEEN), 3)
    fourth = judged(name, level, list(BETWEEN), 4)

    assert third.passed and not fourth.passed
    assert third.evidence[reading] == fourth.evidence[reading] == measured
    assert third.evidence[threshold] == fourth.evidence[threshold] == level
