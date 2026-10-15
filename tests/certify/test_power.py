"""What a certification window resolves, and how likely its window gate is to pass.

Every number here is arithmetic a reader can do: a research error scaled by the root of the
ratio of two window lengths, 2.486 of those errors for the resolvable edge, and a normal
probability either side of the window gate's bar.
"""

from __future__ import annotations

from datetime import date
from statistics import NormalDist

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.certify.power import RESOLVES, power, window_days, window_params
from kanso.errors import ValidationError
from kanso.schemas import CertificationPlan, Power

RESEARCH = (date(2024, 1, 1), date(2024, 12, 30))
"""365 days, both ends counted."""

CERTIFICATION = (date(2025, 1, 1), date(2025, 3, 31))
"""90 days: a quarter of research's length, so its error is twice research's."""


def test_a_window_counts_both_of_its_end_days() -> None:
    assert window_days(RESEARCH) == 365
    assert window_days(CERTIFICATION) == 90
    assert window_days((date(2025, 1, 1), date(2025, 1, 1))) == 1


def test_the_resolvable_edge_is_two_and_a_half_errors_of_the_certification_window() -> None:
    assert abs(RESOLVES - (1.6449 + 0.8416)) < 1e-4
    stated = power(10.0, 3.0, RESEARCH, CERTIFICATION, {"min_fraction": 0.5})
    se = 3.0 * (365 / 90) ** 0.5
    assert stated.standard_error == pytest.approx(se)
    assert stated.resolves == pytest.approx(RESOLVES * se)


def test_the_window_gate_passes_above_its_bar_at_the_research_estimate_and_at_zero() -> None:
    """The bar is half of 10, five: the truth at 10 clears it by 5/6.08 errors, the truth
    at zero falls short of it by as many."""
    stated = power(10.0, 3.0, RESEARCH, CERTIFICATION, {"min_fraction": 0.5})
    se = 3.0 * (365 / 90) ** 0.5
    assert stated.bar == 5.0
    assert stated.pass_at_research == pytest.approx(NormalDist().cdf(5.0 / se))
    assert stated.pass_at_zero == pytest.approx(NormalDist().cdf(-5.0 / se))


def test_a_bar_of_zero_passes_no_edge_half_the_time() -> None:
    """At `min_fraction` zero, or a research estimate at or below zero, the gate asks only
    for a positive estimate, and an edge of nothing is positive half the time."""
    assert power(10.0, 3.0, RESEARCH, CERTIFICATION, {"min_fraction": 0.0}).pass_at_zero == 0.5
    assert power(-4.0, 3.0, RESEARCH, CERTIFICATION, {"min_fraction": 0.5}).bar == 0.0


def test_with_no_error_the_gate_passes_exactly_what_clears_its_bar() -> None:
    stated = power(10.0, 0.0, RESEARCH, CERTIFICATION, {"min_fraction": 0.5})
    assert (stated.resolves, stated.pass_at_research, stated.pass_at_zero) == (0.0, 1.0, 0.0)
    level = power(10.0, 0.0, RESEARCH, CERTIFICATION, {"min_fraction": 1})
    assert level.pass_at_research == 1.0, "an estimate on a bar above zero meets it"
    assert power(0.0, 0.0, RESEARCH, CERTIFICATION, {"min_fraction": 1}).pass_at_research == 0.0


def test_a_plan_that_sets_no_fraction_states_what_resolves_and_no_probability() -> None:
    for params in (None, {}, {"min_fraction": True}):
        stated = power(10.0, 3.0, RESEARCH, CERTIFICATION, params)
        assert stated.bar is stated.pass_at_research is stated.pass_at_zero is None
        assert stated.resolves > 0


def test_the_window_gate_s_parameters_are_read_off_the_plan() -> None:
    gates = [
        {"id": "embargoed_window", "stage": "cert", "params": {"min_fraction": 0.4}},
        {"id": "paper_forward", "stage": "paper", "params": {}},
        {"id": "live_drift", "stage": "live", "params": {}},
    ]
    plan = CertificationPlan.model_validate(
        {
            "schema": 1,
            "hyp_id": "demo_mr",
            "plan_version": 1,
            "planned_at": "2025-01-02T00:00:00Z",
            "planned_by": "mock",
            "inputs": {
                "hypothesis_sha": "0" * 64,
                "construct": {"id": "sleeve"},
                "data_availability": {"types": [], "spans": {}},
                "n_trials": 1,
            },
            "gates": [{**gate, "rationale": "r"} for gate in gates],
        }
    )
    assert window_params(plan) == {"min_fraction": 0.4}
    without = plan.model_copy(update={"gates": plan.gates[1:]})
    assert window_params(without) is None


def test_a_power_states_both_probabilities_or_neither() -> None:
    with pytest.raises(ValidationError, match="both pass probabilities"):
        Power(research=1.0, research_se=1.0, standard_error=1.0, resolves=2.5, bar=0.5)


@given(
    st.floats(-100.0, 100.0, allow_nan=False),
    st.floats(0.0, 50.0, allow_nan=False),
    st.integers(1, 2000),
    st.integers(1, 2000),
    st.floats(0.0, 1.0, allow_nan=False),
)
def test_a_window_four_times_as_long_halves_the_edge_it_resolves(
    research: float, se: float, research_days: int, certification_days: int, fraction: float
) -> None:
    opens = date(2020, 1, 1)
    research_window = (opens, date.fromordinal(opens.toordinal() + research_days - 1))
    short = (opens, date.fromordinal(opens.toordinal() + certification_days - 1))
    long = (opens, date.fromordinal(opens.toordinal() + 4 * certification_days - 1))
    near = power(research, se, research_window, short, {"min_fraction": fraction})
    far = power(research, se, research_window, long, {"min_fraction": fraction})
    assert far.resolves == pytest.approx(near.resolves / 2)
    assert near.pass_at_zero is not None and near.pass_at_research is not None
    assert near.pass_at_zero <= near.pass_at_research or research < 0
