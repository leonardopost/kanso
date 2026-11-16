"""`forward_holdout`: judged once the forward window resolves the certified value, then above
zero and a stated fraction of it; failing, not skipping, until then."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from math import ceil, sqrt
from statistics import NormalDist
from typing import Any

import pytest

from kanso.certify.power import RESOLVES
from kanso.criteria.band import Z, forward_error, prediction
from kanso.criteria.gates.forward_holdout import (
    NO_DISPERSION,
    NO_EXPECTATION,
    NO_OBJECTIVE,
    NO_PARAMS,
    NO_STAGE,
    gate,
)
from kanso.criteria.objectives import wf_sharpe_net, wf_sharpe_vs_hold
from kanso.criteria.run import midnight_ns
from kanso.schemas import GateResult
from tests.monitor.builders import (
    DOCUMENT,
    PERIODS,
    RISING,
    gate_context,
    hypothesis,
    rising_run,
    run_over,
    stage_record,
)

PARAMS = {"resolve": 1.0, "min_fraction": 0.0}

FOLD_WEIGHT = 4 * (5 - 1) / (5 * (5 - 3))
"""Twenty days in four folds of five: each Sharpe fold weighs `(5 − 1) / (5 × 2)`."""


def judge(
    *, value: float = RISING, se: float = 0.1, params: dict[str, Any] | None = None, **rest: Any
) -> GateResult:
    """The gate on twenty rising paper days, certified at `value ± se` over 55 periods."""
    return gate.evaluate(
        gate_context(
            params=PARAMS if params is None else params,
            record=stage_record(),
            run=rest.pop("run", rising_run()),
            value=value,
            se=se,
            **rest,
        )
    )


def forward_from(start: date) -> Any:
    """The demo sleeve with its forward window opening on `start`."""
    windows = {**DOCUMENT["windows"], "forward": {"start": start.isoformat()}}
    return hypothesis(windows=windows)


# --- before it resolves -------------------------------------------------------------------


def test_it_fails_with_the_periods_it_needs_until_the_window_resolves_the_value() -> None:
    """Certified one error from zero over 55 periods, twenty forward days are about two
    certification errors wide: not judged, and a fail rather than a skip, since a skip
    passes and the version would be promotable on `paper_forward` alone."""
    result = judge(se=RISING)

    assert not result.passed and result.skipped is None
    assert result.evidence["judged"] is False
    assert result.evidence["periods"] == 20
    assert result.evidence["required_periods"] == PERIODS
    assert result.evidence["se_fwd"] == pytest.approx(RISING * sqrt(PERIODS * FOLD_WEIGHT / 16))
    assert result.evidence["realised"] == pytest.approx(RISING)


def test_the_periods_it_needs_scale_as_the_square_of_resolve_over_the_value_s_t() -> None:
    """`N × (resolve × se / value)²`, and never fewer than strike a band: a Sharpe's four a
    fold."""
    at_three = judge(se=RISING / 2, params={"resolve": 3.0, "min_fraction": 0.0})
    tight = judge(se=RISING / 100)

    assert at_three.evidence["required_periods"] == ceil(PERIODS * (3.0 / 2) ** 2)
    assert tight.evidence["required_periods"] == 16


def test_a_certified_value_at_or_below_zero_is_never_judged() -> None:
    for value in (0.0, -1.0):
        result = judge(value=value)

        assert not result.passed and result.evidence["judged"] is False
        assert result.evidence["required_periods"] is None


def test_a_sharpe_window_too_thin_for_a_band_has_not_judged() -> None:
    """Folds of two periods: a Sharpe of so few has no variance, so no forward error."""
    result = judge(run=run_over((150.0, 50.0) * 4))

    assert not result.passed and result.evidence["judged"] is False
    assert result.evidence["se_fwd"] is None
    assert result.evidence["resolves"] is result.evidence["pass_at_expected"] is None


# --- once it resolves ---------------------------------------------------------------------


def test_once_judged_it_passes_above_zero_and_the_fraction() -> None:
    result = judge()

    assert result.passed and result.evidence["judged"] is True
    assert result.evidence["realised"] == pytest.approx(RISING)
    assert result.evidence["expected"] == RISING
    assert result.evidence["bar"] == 0.0


def test_it_passes_only_strictly_above_the_bar() -> None:
    """`realised > max(0, min_fraction × expected)`: the whole certified value is the bar at
    a fraction of one, which a window realising exactly it does not clear."""
    whole = {"resolve": 1.0, "min_fraction": 1.0}
    realised = wf_sharpe_net.compute(rising_run(), 4)[0]

    at_bar = judge(value=realised, params=whole)
    under = judge(value=realised * 0.99, params=whole)
    over = judge(value=realised * 1.01, params=whole)

    assert at_bar.evidence["bar"] == pytest.approx(realised) and not at_bar.passed
    assert under.passed
    assert over.evidence["judged"] is True and not over.passed


def test_a_realised_value_at_or_below_zero_fails_once_judged() -> None:
    falling = run_over(tuple(-150.0 if index % 2 == 0 else -50.0 for index in range(20)))

    result = judge(run=falling)

    assert result.evidence["judged"] is True and result.evidence["realised"] < 0
    assert not result.passed


# --- its arithmetic is the band's ---------------------------------------------------------


def test_the_forward_error_is_the_band_s_own_term_for_the_window() -> None:
    """`paper_forward`'s half-width is `z × √(se² + se_fwd²)`: one arithmetic for both."""
    ctx = gate_context(params=PARAMS, record=stage_record(), run=rising_run(), value=1.0)
    band = prediction(wf_sharpe_net, ctx.expectation, ctx.run, 4)

    result = gate.evaluate(ctx)

    assert band is not None and band.forward is not None
    assert result.evidence["se_fwd"] == band.forward
    assert band.forward == forward_error(wf_sharpe_net, 0.1, PERIODS, band.folds, 4)
    assert band.high - band.expected == pytest.approx(Z * sqrt(0.1**2 + band.forward**2))


def test_the_evidence_states_its_power_on_every_pass() -> None:
    """What the window resolves at eighty percent, and its chance of passing at the expected
    value and at zero: half, at a fraction of zero, for a version with no edge."""
    result = judge(params={"resolve": 1.0, "min_fraction": 0.5})
    nothing = judge(se=RISING)

    se_fwd, expected, bar = (result.evidence[key] for key in ("se_fwd", "expected", "bar"))
    assert bar == pytest.approx(0.5 * expected)
    assert result.evidence["resolves"] == pytest.approx(RESOLVES * se_fwd)
    assert result.evidence["pass_at_expected"] == pytest.approx(
        NormalDist().cdf((expected - bar) / se_fwd)
    )
    assert result.evidence["pass_at_zero"] == pytest.approx(NormalDist().cdf(-bar / se_fwd))
    assert nothing.evidence["pass_at_zero"] == 0.5
    assert nothing.evidence["resolves"] == pytest.approx(RESOLVES * nothing.evidence["se_fwd"])


def test_a_certified_error_of_zero_judges_at_once_and_states_no_power() -> None:
    result = judge(se=0.0)

    assert result.passed and result.evidence["se_fwd"] == 0.0
    assert result.evidence["required_periods"] == 16
    assert result.evidence["pass_at_zero"] is None


# --- only the forward window --------------------------------------------------------------


def test_only_the_days_from_the_forward_start_are_read() -> None:
    """A stage resumed before this hypothesis's forward window opened: its first ten days
    are not the forward window and are counted nowhere."""
    late = forward_from(date(2024, 3, 11))

    result = judge(hyp=late)

    assert result.evidence["forward_start"] == "2024-03-11"
    assert result.evidence["window"] == ["2024-03-11", "2024-03-20"]
    assert result.evidence["periods"] == 10
    assert result.evidence["realised"] == pytest.approx(
        wf_sharpe_net.compute(rising_run(days=10, start=date(2024, 3, 11)), 4)[0]
    )


def test_a_window_wholly_before_the_forward_start_holds_no_period() -> None:
    result = judge(hyp=forward_from(date(2024, 4, 1)))

    assert not result.passed and result.evidence["judged"] is False
    assert result.evidence["periods"] == 0
    assert result.evidence["window"] is result.evidence["realised"] is None
    assert result.evidence["se_fwd"] is None and result.evidence["expected"] == 0.0


def test_a_benchmark_objective_is_differenced_against_the_hold_s_forward_days() -> None:
    hold_objective = {
        "benchmark": {"hold": "first_leg"},
        "objective": {"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
    }
    windows = {**DOCUMENT["windows"], "forward": {"start": "2024-03-11"}}
    hold = run_over(tuple(float(100 + (day % 4) * 40) for day in range(20)))

    result = judge(hyp=hypothesis(windows=windows, **hold_objective), benchmark=hold)

    opens, closes = midnight_ns(date(2024, 3, 11)), hold.bounds[1]
    since, held = rising_run().between(opens, closes), hold.between(opens, closes)
    assert result.evidence["realised"] == pytest.approx(
        wf_sharpe_vs_hold.compute(since, 4, benchmark=held)[0]
    )
    assert result.evidence["realised"] != pytest.approx(
        wf_sharpe_vs_hold.compute(rising_run(), 4, benchmark=hold)[0]
    )


# --- what it cannot judge -----------------------------------------------------------------


def test_it_skips_naming_what_it_lacked() -> None:
    no_objective = hypothesis().model_copy(update={"objective": None})
    unrecorded = replace(
        gate_context(params=PARAMS, record=stage_record()),
        expectation={"objective_id": "wf_sharpe_net", "value": 1.0, "ci90": [0.5, 1.5]},
    )
    cases = {
        NO_PARAMS: gate_context(params={"resolve": 1.0}, record=stage_record()),
        NO_STAGE: gate_context(params=PARAMS),
        NO_EXPECTATION: gate_context(params=PARAMS, record=stage_record(), value=None),
        NO_OBJECTIVE: gate_context(params=PARAMS, record=stage_record(), hyp=no_objective),
        NO_DISPERSION: unrecorded,
    }

    for reason, ctx in cases.items():
        result = gate.evaluate(ctx)
        assert result.passed and result.skipped == reason, reason
