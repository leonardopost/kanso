"""`paper_forward`: long enough, inside the band, inside the drawdown, and honest about trades."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from kanso.criteria.gates.paper_forward import (
    NOT_A_PAPER_TEST,
    TRADES_UNREACHABLE,
    gate,
)

from .builders import flat_run, gate_context, hypothesis, run_over, stage_record

PARAMS = {"min_duration": "5d", "horizon_mult": 5.0}


def test_a_version_that_waited_and_behaved_passes() -> None:
    """Twenty stage days, a flat run scoring zero, and a band that contains zero."""
    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record()))

    assert result.passed
    assert result.skipped is None
    assert result.evidence["realised"] == 0.0
    assert result.evidence["required_s"] == 5 * 86_400


def test_the_required_window_is_the_longer_of_the_two_the_plan_names() -> None:
    """A ten-horizon multiple on a one-day horizon beats a five-day absolute minimum."""
    result = gate.evaluate(
        gate_context(params={"min_duration": "5d", "horizon_mult": 10.0}, record=stage_record())
    )

    assert result.evidence["required_s"] == 10 * 86_400


def test_a_version_that_has_not_waited_long_enough_fails() -> None:
    """Elapsed is measured on the stage clock, from the current join."""
    record = stage_record(joined=date(2024, 3, 19))

    result = gate.evaluate(gate_context(params=PARAMS, record=record))

    assert not result.passed
    assert result.evidence["elapsed_s"] < result.evidence["required_s"]


def test_a_redeploy_restarts_the_clock() -> None:
    """The window is the one since the current join, not the life of the version."""
    early = gate.evaluate(gate_context(params=PARAMS, record=stage_record(joined=date(2024, 3, 1))))
    rejoined = gate.evaluate(
        gate_context(params=PARAMS, record=stage_record(joined=date(2024, 3, 18)))
    )

    assert early.passed
    assert not rejoined.passed


def test_a_result_below_the_band_fails() -> None:
    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record(), ci90=(1.0, 2.0)))

    assert not result.passed
    assert result.evidence["ci90"] == [1.0, 2.0]


def test_a_result_above_the_band_fails_too() -> None:
    """A paper stage that beats its own certified model has not reproduced it."""
    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record(), ci90=(-2.0, -1.0)))

    assert not result.passed


def test_a_drawdown_past_the_hypothesis_limit_fails() -> None:
    """The one card-stage constraint a paper window can still judge."""
    sank = run_over((100.0, -30_000.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0))

    result = gate.evaluate(
        gate_context(params=PARAMS, record=stage_record(), run=sank, ci90=(-100.0, 100.0))
    )

    assert not result.passed
    assert result.evidence["max_drawdown_pct"] > 15.0
    assert result.evidence["constraints"]["max_drawdown"] is False


def test_min_trades_is_skipped_with_the_reason_it_cannot_apply() -> None:
    """A research-window trade count in a paper window would make promotable unreachable."""
    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record()))

    assert result.evidence["constraints"]["min_trades"] == TRADES_UNREACHABLE
    assert result.passed


def test_a_constraint_a_deployment_cannot_run_is_named_as_such() -> None:
    """`strategy_integrity` inspects a lane directory, which a stage does not have."""
    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record()))

    assert result.evidence["constraints"]["strategy_integrity"] == NOT_A_PAPER_TEST


def test_a_sleeve_without_a_drawdown_constraint_judges_no_drawdown() -> None:
    """Only the sleeve's own constraints are evaluated, so an undeclared one is not."""
    bare = hypothesis(constraints=[{"id": "strategy_integrity"}])
    sank = run_over((100.0, -30_000.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0))

    result = gate.evaluate(
        gate_context(params=PARAMS, record=stage_record(), run=sank, hyp=bare, ci90=(-100.0, 100.0))
    )

    assert result.passed
    assert "max_drawdown" not in result.evidence["constraints"]


def test_a_sleeve_with_no_constraints_at_all_judges_none() -> None:
    result = gate.evaluate(
        gate_context(params=PARAMS, record=stage_record(), hyp=hypothesis(constraints=None))
    )

    assert result.passed
    assert result.evidence["constraints"] == {}


def test_it_skips_without_a_chosen_window() -> None:
    assert gate.evaluate(gate_context(record=stage_record())).skipped is not None
    assert gate.evaluate(gate_context(params={"min_duration": "5d"})).skipped is not None
    assert gate.evaluate(gate_context(params={"horizon_mult": 5.0})).skipped is not None


def test_it_skips_without_a_stage_record() -> None:
    """A gate evaluated outside a deployment has no clock to measure against."""
    result = gate.evaluate(gate_context(params=PARAMS))

    assert result.passed
    assert result.skipped is not None


def test_it_skips_without_an_expectation() -> None:
    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record(), ci90=None))

    assert result.passed
    assert result.skipped is not None


def test_it_skips_on_a_malformed_band() -> None:
    broken = replace(
        gate_context(params=PARAMS, record=stage_record()), expectation={"ci90": [0.0]}
    )

    assert gate.evaluate(broken).skipped is not None


def test_it_skips_without_an_objective() -> None:
    unclassified = hypothesis(objective=None)

    result = gate.evaluate(gate_context(params=PARAMS, record=stage_record(), hyp=unclassified))

    assert result.passed
    assert result.skipped is not None


def test_a_flat_run_scores_zero_on_every_fold() -> None:
    """The arithmetic the rest of this module rests on, stated once."""
    from kanso.criteria.objectives import wf_sharpe_net

    assert wf_sharpe_net.compute(flat_run(), 4)[0] == 0.0
