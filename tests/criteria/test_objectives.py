"""The four objectives, the fold arithmetic they aggregate with, and their applicability."""

from __future__ import annotations

from datetime import date
from itertools import product
from typing import Any

import pytest

from kanso.criteria import applicable_objectives, catalogue, objectives
from kanso.criteria.objectives import (
    ABSOLUTE,
    RELATIVE,
    applies_to,
    history_days,
    marginal_net_edge_bps,
    marginal_wf_sharpe,
    net_edge_bps,
    resolution_seconds,
    wf_sharpe_net,
)
from kanso.errors import PreconditionError
from kanso.schemas import Applies, Hypothesis
from kanso.schemas.hypothesis import Mechanism
from tests.criteria.builders import build_run, make_hyp, trade

MECHANISMS: tuple[Mechanism, ...] = (
    "mean_reversion",
    "momentum",
    "microstructure",
    "stat_arb",
    "event",
    "carry",
    "vol",
    "other",
)
FOUR_DAYS = (0.0, 0.0, 0.0, 0.0)


def edges(*values: float) -> Any:
    """A four-day run closing one trade a day, at a known edge in bps on each."""
    days = [date(2024, 1, 1 + i) for i in range(4)]
    return build_run(
        FOUR_DAYS,
        trades=tuple(
            trade(day, pnl=value, notional=10_000.0)
            for day, value in zip(days, values, strict=True)
        ),
    )


def test_the_metric_is_the_mean_of_the_folds_and_the_error_their_spread() -> None:
    metric, error = net_edge_bps.compute(edges(100.0, 200.0, 300.0, 400.0), 4)
    assert metric == pytest.approx(250.0)
    assert error == pytest.approx((50_000 / 3) ** 0.5 / 2)


def test_the_fold_values_are_the_statistic_on_each_contiguous_span() -> None:
    values = net_edge_bps.fold_values(edges(100.0, 200.0, 300.0, 400.0), 4)
    assert values == pytest.approx((100.0, 200.0, 300.0, 400.0))


def test_a_single_fold_has_no_standard_error_to_report() -> None:
    metric, error = net_edge_bps.compute(edges(100.0, 100.0, 100.0, 100.0), 1)
    assert (metric, error) == pytest.approx((100.0, 0.0))


def test_a_relative_objective_differences_the_folds_pairwise() -> None:
    candidate = edges(100.0, 200.0, 300.0, 400.0)
    host = edges(50.0, 250.0, 100.0, 500.0)
    values = marginal_net_edge_bps.fold_values(candidate, 4, host)
    assert values == pytest.approx((50.0, -50.0, 200.0, -100.0))
    metric, error = marginal_net_edge_bps.compute(candidate, 4, host)
    assert metric == pytest.approx(25.0)
    assert error > 0


def test_a_relative_objective_refuses_to_measure_without_a_host() -> None:
    with pytest.raises(PreconditionError, match="host's run"):
        marginal_wf_sharpe.compute(edges(1.0, 2.0, 3.0, 4.0), 4)


def test_the_sharpe_objectives_read_the_return_series() -> None:
    run = build_run((1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0))
    metric, _ = wf_sharpe_net.compute(run, 4)
    assert metric > 0
    flat = build_run((2.0, 2.0, 2.0, 2.0))
    assert wf_sharpe_net.compute(flat, 4) == (0.0, 0.0)


def test_the_shipped_objectives_declare_their_mode() -> None:
    assert (wf_sharpe_net.mode, net_edge_bps.mode) == (ABSOLUTE, ABSOLUTE)
    assert (marginal_wf_sharpe.mode, marginal_net_edge_bps.mode) == (RELATIVE, RELATIVE)


def test_every_catalogued_objective_resolves_to_its_implementation() -> None:
    found = objectives()
    assert set(found) == {
        "wf_sharpe_net",
        "net_edge_bps",
        "marginal_wf_sharpe",
        "marginal_net_edge_bps",
    }
    assert all(objective.id == name for name, objective in found.items())


def test_the_objective_set_is_total_over_mechanism_mode_and_horizon() -> None:
    for mechanism, mode, horizon in product(MECHANISMS, (ABSOLUTE, RELATIVE), ("12h", "1d")):
        hyp = make_hyp(mechanism=mechanism, horizon=horizon)
        found = applicable_objectives(hyp, mode)
        assert found, f"no objective applies to {mechanism} / {mode} / {horizon}"
        assert len(found) == 1, f"{mechanism} / {mode} / {horizon} is ambiguous: {found}"


@pytest.mark.parametrize(
    ("mode", "horizon", "expected"),
    [
        (ABSOLUTE, "1d", "wf_sharpe_net"),
        (ABSOLUTE, "23h", "net_edge_bps"),
        (ABSOLUTE, "30m", "net_edge_bps"),
        (ABSOLUTE, "5d", "wf_sharpe_net"),
        (RELATIVE, "1d", "marginal_wf_sharpe"),
        (RELATIVE, "30m", "marginal_net_edge_bps"),
    ],
)
def test_a_day_is_the_boundary_min_inclusive_and_max_exclusive(
    mode: str, horizon: str, expected: str
) -> None:
    hyp = make_hyp(horizon=horizon)
    assert applicable_objectives(hyp, mode) == [(catalogue()[expected].priority, expected)]


def test_the_lowest_priority_wins_among_the_applicable() -> None:
    found = sorted([(20, "wf_sharpe_net"), (10, "net_edge_bps")])
    assert found[0][1] == "net_edge_bps"


def _predicate(**clauses: Any) -> Applies:
    return Applies.model_validate(clauses)


@pytest.mark.parametrize(
    ("clauses", "holds"),
    [
        ({"mechanism": ["mean_reversion"]}, True),
        ({"mechanism": ["momentum"]}, False),
        ({"objective_mode": "absolute"}, True),
        ({"objective_mode": "relative"}, False),
        ({"horizon": {"min": "30m"}}, True),
        ({"horizon": {"min": "31m"}}, False),
        ({"horizon": {"max": "30m"}}, False),
        ({"resolution": {"max": "5m"}}, True),
        ({"resolution": {"min": "5m"}}, False),
        ({"universe_size": {"min": 1}}, True),
        ({"universe_size": {"min": 2}}, False),
        ({"universe_size": {"max": 1}}, False),
        ({"data_requirements": ["bar"]}, True),
        ({"data_requirements": ["quote"]}, False),
        ({"history_days": {"min": 300}}, True),
        ({"history_days": {"max": 300}}, False),
    ],
)
def test_every_clause_of_the_predicate_is_evaluated(clauses: Any, holds: bool) -> None:
    assert applies_to(_predicate(**clauses), make_hyp(), ABSOLUTE) is holds


def test_an_empty_predicate_applies_to_everything() -> None:
    assert applies_to(_predicate(), make_hyp(), RELATIVE)


def test_an_unaggregated_resolution_is_the_finest_grain_there_is() -> None:
    assert resolution_seconds("1m") == 60.0
    assert resolution_seconds("tick") == 0.0


def test_the_history_length_counts_both_end_days() -> None:
    hyp: Hypothesis = make_hyp()
    assert history_days(hyp) == 366
