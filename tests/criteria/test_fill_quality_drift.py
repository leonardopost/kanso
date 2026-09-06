"""The live fill-quality gate: the one comparison it makes, and all it refuses to make.

The arithmetic is deliberately trivial — a venue model charging one basis point, an allowance
of two, and a realised figure chosen so the verdict is visible by eye — because everything
interesting about this gate is which evidence it will and will not judge on.
"""

from __future__ import annotations

from dataclasses import replace
from math import inf, nan
from typing import Any

import pytest

from kanso.criteria import catalogue, gates
from kanso.criteria.gates.fill_quality_drift import gate
from kanso.criteria.run import NS_PER_DAY, CardRun, midnight_ns
from kanso.monitor.stage import SIMULATED, UNKNOWN, StageRecord
from kanso.schemas import GateResult
from tests.criteria.builders import START, build_run, context

PARAMS: dict[str, Any] = {"max_excess_bps": 2.0, "min_fills": 10}
"""Two basis points of excess allowed, once ten fills have printed."""

COSTS: dict[str, Any] = {
    "commission_bps": 5.0,
    "slippage_bps": 1.0,
    "spread": "fixed_bps",
    "fixed_bps": 4.0,
}
"""A cost model whose slippage line is one basis point and whose other lines are not."""


def costed(costs: Any = COSTS) -> CardRun:
    """A flat run whose venue model charges what the test says it charges."""
    model: dict[str, Any] = {"venue": "XNAS", "account": "margin", "currency": "USD"}
    return replace(
        build_run((0.0, 0.0)),
        venue_model=model if costs is None else {**model, "costs": costs},
    )


def record(
    *,
    funding: str = "broker_paper",
    n_fills: int = 50,
    realised: float | None = 3.0,
) -> StageRecord:
    """A live stage record carrying one version's fills and what they realised."""
    opened = midnight_ns(START)
    return StageRecord(
        stage="live",
        joined_ns=opened,
        clock_ns=opened + NS_PER_DAY,
        funding=funding,
        n_fills=n_fills,
        realised_slippage_bps=realised,
    )


def judge(
    session: StageRecord | None = None,
    *,
    run: CardRun | None = None,
    params: dict[str, Any] | None = None,
) -> GateResult:
    """The gate's verdict on one stage record, with everything else at its default."""
    return gate.evaluate(
        context(
            run or costed(),
            stage="live",
            params=PARAMS if params is None else params,
            session=record() if session is None else session,
        )
    )


# --- what it measures ---------------------------------------------------------


def test_realised_slippage_within_the_allowance_passes() -> None:
    """Three realised against one modelled is two of excess, which is exactly allowed."""
    result = judge(record(funding="broker_paper", realised=3.0))

    assert result.passed
    assert result.skipped is None
    assert result.evidence == {
        "funding": "broker_paper",
        "realised_bps": 3.0,
        "modelled_bps": 1.0,
        "excess_bps": 2.0,
        "max_excess_bps": 2.0,
        "n_fills": 50,
        "min_fills": 10,
    }


def test_realised_slippage_past_the_allowance_fails() -> None:
    result = judge(record(funding="real", realised=3.01))

    assert not result.passed
    assert result.evidence["funding"] == "real"


def test_fills_better_than_the_model_are_not_a_fault() -> None:
    """The comparison is one-sided: price improvement is not evidence of drift."""
    result = judge(record(realised=0.0))

    assert result.passed
    assert result.evidence["excess_bps"] == -1.0


def test_only_the_slippage_line_is_the_assumption() -> None:
    """Commission and spread are charged too, but neither is what a fill gave up."""
    assert judge().evidence["modelled_bps"] == 1.0


# --- what it will not measure -------------------------------------------------


def test_a_simulated_client_realises_the_model_by_construction() -> None:
    result = judge(record(funding=SIMULATED, realised=99.0))

    assert result.passed
    assert result.skipped is not None and "simulated" in result.skipped


def test_a_client_that_resolves_to_no_declaration_may_be_a_simulated_one() -> None:
    """`unknown` is what the monitor records for an exec id it cannot resolve."""
    result = judge(record(funding=UNKNOWN, realised=99.0))

    assert result.passed
    assert result.skipped is not None and "no declaration" in result.skipped


def test_fewer_fills_than_the_plan_asked_for_is_not_a_measurement() -> None:
    result = judge(record(n_fills=9, realised=99.0))

    assert result.passed
    assert result.skipped is not None
    assert "9 fills" in result.skipped and "10" in result.skipped


def test_a_client_reporting_no_realised_slippage_measured_nothing() -> None:
    """Which is the honest verdict, not a pass on a comparison that never happened."""
    result = judge(record(realised=None))

    assert result.passed
    assert result.skipped is not None and "no realised slippage" in result.skipped


@pytest.mark.parametrize("reported", [nan, inf, -inf])
def test_a_realised_figure_that_is_not_finite_measured_nothing(reported: float) -> None:
    result = judge(record(realised=reported))

    assert result.passed
    assert result.skipped is not None and "not a finite number" in result.skipped


@pytest.mark.parametrize(
    "chosen",
    [
        {},
        {"max_excess_bps": 2.0},
        {"min_fills": 10},
        {"max_excess_bps": 2.0, "min_fills": 10.0},
    ],
    ids=["neither", "no count", "no allowance", "a count that is not whole"],
)
def test_without_both_of_its_values_nothing_was_required(chosen: dict[str, Any]) -> None:
    result = judge(params=chosen)

    assert result.passed
    assert result.skipped is not None and "no fill quality was required" in result.skipped


@pytest.mark.parametrize("session", [None, "live", 3], ids=["none", "a string", "a number"])
def test_without_a_stage_record_no_execution_client_was_named(session: object) -> None:
    result = gate.evaluate(context(costed(), stage="live", params=PARAMS, session=session))

    assert result.passed
    assert result.skipped is not None and "no execution client was named" in result.skipped


# --- what the run promised ----------------------------------------------------


@pytest.mark.parametrize(
    "costs",
    [
        None,
        {},
        {"slippage_bps": "one"},
        {"slippage_bps": True},
        {"slippage_bps": nan},
        [("slippage_bps", 1.0)],
    ],
    ids=["no cost model", "no line", "text", "a flag", "not a number", "not a mapping"],
)
def test_a_run_promising_no_usable_slippage_promised_none(costs: Any) -> None:
    """Then the whole realised figure is excess — strict, and never a pass it did not earn."""
    result = judge(record(realised=3.0), run=costed(costs))

    assert result.evidence["modelled_bps"] == 0.0
    assert result.evidence["excess_bps"] == 3.0
    assert not result.passed


# --- what the toolbox says it is ----------------------------------------------


def test_the_toolbox_entry_resolves_to_this_gate() -> None:
    item = catalogue()["fill_quality_drift"]

    assert item.kind == "gate"
    assert item.stage == "live"
    assert item.required is False
    assert item.params == {"max_excess_bps": "float", "min_fills": "int"}
    assert item.ranges == {"max_excess_bps": (0, 50), "min_fills": (10, 10000)}
    assert gates()["fill_quality_drift"] is gate
