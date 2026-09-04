"""The three live gates: drift below the band, the day's loss, and fill quality."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from kanso.criteria.gates.daily_loss_kill import gate as daily_loss_kill
from kanso.criteria.gates.fill_quality_drift import gate as fill_quality_drift
from kanso.criteria.gates.live_drift import gate as live_drift
from kanso.monitor.stage import SIMULATED

from .builders import gate_context, hypothesis, run_over, stage_record

DAY_S = 86_400.0
WEEK_S = 7 * DAY_S


# --- live_drift ---------------------------------------------------------------


def test_a_live_book_still_inside_its_band_passes() -> None:
    """A flat run scores zero, and a floor below zero is not fallen through."""
    result = live_drift.evaluate(
        gate_context(stage="live", record=stage_record(stage="live", paper_window_s=WEEK_S))
    )

    assert result.passed
    assert result.skipped is None
    assert result.evidence["floor"] == -0.5


def test_a_live_book_below_the_lower_bound_fails() -> None:
    result = live_drift.evaluate(
        gate_context(
            stage="live",
            record=stage_record(stage="live", paper_window_s=WEEK_S),
            ci90=(1.0, 2.0),
        )
    )

    assert not result.passed
    assert result.evidence["realised"] < result.evidence["floor"]


def test_beating_the_band_is_not_a_failure() -> None:
    """Only the lower bound demotes: doing better than expected is not a fault."""
    result = live_drift.evaluate(
        gate_context(
            stage="live",
            record=stage_record(stage="live", paper_window_s=WEEK_S),
            ci90=(-3.0, -2.0),
        )
    )

    assert result.passed


def test_the_rolling_window_is_the_paper_window_not_the_whole_book() -> None:
    """Twenty live days rolled over a week leaves seven return periods."""
    result = live_drift.evaluate(
        gate_context(stage="live", record=stage_record(stage="live", paper_window_s=WEEK_S))
    )

    assert result.evidence["n_periods"] == 7
    assert result.evidence["rolling_window"] == ["2024-03-14", "2024-03-20"]


def test_it_says_nothing_until_the_live_window_is_as_long_as_the_paper_one() -> None:
    record = stage_record(stage="live", joined=date(2024, 3, 18), paper_window_s=WEEK_S)

    result = live_drift.evaluate(gate_context(stage="live", record=record))

    assert result.passed
    assert result.skipped is not None


def test_it_skips_without_a_stage_record() -> None:
    assert live_drift.evaluate(gate_context(stage="live")).skipped is not None


def test_it_skips_when_the_paper_window_is_unknown() -> None:
    assert (
        live_drift.evaluate(gate_context(stage="live", record=stage_record(stage="live"))).skipped
        is not None
    )
    assert (
        live_drift.evaluate(
            gate_context(stage="live", record=stage_record(stage="live", paper_window_s=0.0))
        ).skipped
        is not None
    )


def test_it_skips_without_an_expectation_or_an_objective() -> None:
    record = stage_record(stage="live", paper_window_s=WEEK_S)

    assert (
        live_drift.evaluate(gate_context(stage="live", record=record, ci90=None)).skipped
        is not None
    )
    assert (
        live_drift.evaluate(
            gate_context(stage="live", record=record, hyp=hypothesis(objective=None))
        ).skipped
        is not None
    )
    assert (
        live_drift.evaluate(
            replace(gate_context(stage="live", record=record), expectation={"ci90": "wide"})
        ).skipped
        is not None
    )


# --- daily_loss_kill ----------------------------------------------------------


def test_a_day_inside_the_limit_passes() -> None:
    record = stage_record(stage="live", day_pnl=-2_000.0, capital=100_000.0, daily_loss_pct=3.0)

    result = daily_loss_kill.evaluate(gate_context(stage="live", record=record))

    assert result.passed
    assert result.evidence["limit"] == -3_000.0
    assert result.evidence["day"] == "2024-03-20"


def test_a_day_at_or_past_the_limit_fails() -> None:
    """The limit is a floor the day must stay above, so equalling it is a failure."""
    record = stage_record(stage="live", day_pnl=-3_000.0, capital=100_000.0, daily_loss_pct=3.0)

    assert not daily_loss_kill.evaluate(gate_context(stage="live", record=record)).passed


def test_the_day_judged_is_the_whole_stages_not_one_versions() -> None:
    """The record carries the stage's day, which is what the monitor summed."""
    record = stage_record(stage="live", day_pnl=-3_500.0, capital=100_000.0, daily_loss_pct=3.0)

    result = daily_loss_kill.evaluate(gate_context(stage="live", record=record))

    assert result.evidence["stage_capital"] == 100_000.0
    assert result.evidence["day_pnl"] == -3_500.0


def test_it_skips_without_a_stage_record_or_capital() -> None:
    assert daily_loss_kill.evaluate(gate_context(stage="live")).skipped is not None
    assert (
        daily_loss_kill.evaluate(
            gate_context(stage="live", record=stage_record(stage="live", capital=0.0))
        ).skipped
        is not None
    )


# --- fill_quality_drift -------------------------------------------------------

FILL_PARAMS = {"max_excess_bps": 2.0, "min_fills": 10}


def test_it_skips_on_a_simulated_execution_client() -> None:
    """Which in this version is every client the framework provides."""
    record = stage_record(stage="live", funding=SIMULATED, n_fills=100)

    result = fill_quality_drift.evaluate(
        gate_context(stage="live", params=FILL_PARAMS, record=record)
    )

    assert result.passed
    assert result.skipped is not None and "simulated" in result.skipped


def test_it_skips_when_the_client_reported_no_slippage() -> None:
    record = stage_record(stage="live", funding="real", n_fills=100)

    assert (
        fill_quality_drift.evaluate(
            gate_context(stage="live", params=FILL_PARAMS, record=record)
        ).skipped
        is not None
    )


def test_it_skips_below_the_fill_count_the_plan_asked_for() -> None:
    record = stage_record(stage="live", funding="real", n_fills=3, realised_slippage_bps=1.0)

    result = fill_quality_drift.evaluate(
        gate_context(stage="live", params=FILL_PARAMS, record=record)
    )

    assert result.skipped is not None and "3 fills" in result.skipped


def test_fills_no_worse_than_the_model_plus_the_excess_pass() -> None:
    """The run's venue model charged one basis point, so two realised is one of excess."""
    record = stage_record(stage="live", funding="real", n_fills=50, realised_slippage_bps=2.0)

    result = fill_quality_drift.evaluate(
        gate_context(stage="live", params=FILL_PARAMS, record=record)
    )

    assert result.passed
    assert result.evidence["modelled_bps"] == 1.0
    assert result.evidence["excess_bps"] == 1.0


def test_fills_worse_than_the_excess_allowed_fail() -> None:
    record = stage_record(stage="live", funding="real", n_fills=50, realised_slippage_bps=9.0)

    assert not fill_quality_drift.evaluate(
        gate_context(stage="live", params=FILL_PARAMS, record=record)
    ).passed


def test_a_run_recording_no_cost_model_charges_nothing() -> None:
    """Then the whole realised slippage is excess, which is the honest comparison."""
    bare = replace(run_over((10.0, 20.0)), venue_model={})
    record = stage_record(stage="live", funding="real", n_fills=50, realised_slippage_bps=1.0)

    result = fill_quality_drift.evaluate(
        gate_context(stage="live", params=FILL_PARAMS, record=record, run=bare)
    )

    assert result.evidence["modelled_bps"] == 0.0
    assert result.evidence["excess_bps"] == 1.0


def test_a_run_whose_cost_model_is_not_a_number_charges_nothing() -> None:
    bare = replace(run_over((10.0, 20.0)), venue_model={"costs": {"slippage_bps": "one"}})
    record = stage_record(stage="live", funding="real", n_fills=50, realised_slippage_bps=1.0)

    result = fill_quality_drift.evaluate(
        gate_context(stage="live", params=FILL_PARAMS, record=record, run=bare)
    )

    assert result.evidence["modelled_bps"] == 0.0


def test_it_skips_without_its_parameters_or_a_stage_record() -> None:
    record = stage_record(stage="live", funding="real", n_fills=50, realised_slippage_bps=1.0)

    assert fill_quality_drift.evaluate(gate_context(stage="live", record=record)).skipped
    assert fill_quality_drift.evaluate(
        gate_context(stage="live", params={"max_excess_bps": 2.0}, record=record)
    ).skipped
    assert fill_quality_drift.evaluate(gate_context(stage="live", params=FILL_PARAMS)).skipped
