"""`hypothesis.yaml`: the rules that make a hypothesis admissible."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given

from kanso.errors import Exit, ValidationError
from kanso.schemas import Hypothesis, embargo, embargo_days
from tests.schemas.strategies import hypotheses

BASE: dict[str, Any] = {
    "schema": 1,
    "id": "demo_mr",
    "title": "Demo: mean reversion on a synthetic series",
    "thesis": "Prices revert within the hour.",
    "mechanism": "mean_reversion",
    "universe": ["DEMO"],
    "horizon": "30m",
    "resolution": "1m",
    "data_requirements": ["bar"],
    "risk_limits": {"max_position_pct": 20, "max_drawdown_pct": 15, "max_leverage": 1},
    "windows": {
        "research": {"start": "2024-01-02", "end": "2024-12-31"},
        "certification": {"start": "2025-01-06", "end": "2025-05-30"},
        "forward": {"start": "2025-06-02"},
    },
}


def build(**changes: Any) -> Hypothesis:
    return Hypothesis.model_validate({**BASE, **changes})


def test_the_demo_is_valid() -> None:
    hyp = build()
    assert hyp.id == "demo_mr"
    assert hyp.construct is None
    assert hyp.embargo == timedelta(days=1)


@pytest.mark.parametrize(
    ("horizon", "days"),
    [
        ("1s", 1),
        ("30m", 1),
        ("4h", 1),
        ("6h", 2),
        ("12h", 3),
        ("1d", 5),
        ("3d", 15),
        ("1w", 35),
    ],
)
def test_embargo_is_five_horizons_or_a_day(horizon: str, days: int) -> None:
    assert embargo(horizon) == max(5 * _parse(horizon), timedelta(days=1))
    assert embargo_days(horizon) == days


def _parse(horizon: str) -> timedelta:
    from kanso.schemas import parse_duration

    return parse_duration(horizon)


@pytest.mark.parametrize(("horizon", "days"), [("30m", 1), ("12h", 3), ("1d", 5), ("1w", 35)])
def test_certification_may_not_open_inside_the_embargo(horizon: str, days: int) -> None:
    research_end = date(2024, 12, 31)
    windows = dict(BASE["windows"])
    windows["certification"] = {
        "start": (research_end + timedelta(days=days - 1)).isoformat(),
        "end": "2025-08-30",
    }
    windows["forward"] = {"start": "2025-09-01"}
    with pytest.raises(ValidationError) as caught:
        build(horizon=horizon, windows=windows)
    assert "certification.start" in caught.value.message
    assert "embargo" in caught.value.message or "overlaps" in caught.value.message
    assert caught.value.code is Exit.VALIDATION

    windows["certification"] = {
        "start": (research_end + timedelta(days=days)).isoformat(),
        "end": "2025-08-30",
    }
    assert build(horizon=horizon, windows=windows).horizon == horizon


def test_windows_are_ordered_and_disjoint() -> None:
    windows = {
        "research": {"start": "2024-01-02", "end": "2024-12-31"},
        "certification": {"start": "2024-11-01", "end": "2025-05-30"},
        "forward": {"start": "2025-06-02"},
    }
    with pytest.raises(ValidationError, match="overlaps the research window"):
        build(windows=windows)


def test_forward_may_not_overlap_certification() -> None:
    windows = {
        "research": {"start": "2024-01-02", "end": "2024-12-31"},
        "certification": {"start": "2025-01-06", "end": "2025-05-30"},
        "forward": {"start": "2025-05-30"},
    }
    with pytest.raises(ValidationError, match="overlaps the certification window"):
        build(windows=windows)


def test_a_window_may_not_end_before_it_starts() -> None:
    windows = dict(BASE["windows"])
    windows["research"] = {"start": "2024-12-31", "end": "2024-01-02"}
    with pytest.raises(ValidationError, match="before start"):
        build(windows=windows)


def test_bar_resolution_requires_bars() -> None:
    with pytest.raises(ValidationError, match="'bar' must be required"):
        build(data_requirements=["quote"])


@pytest.mark.parametrize(
    ("resolution", "required"),
    [("quote", ["trade"]), ("trade", ["quote"])],
)
def test_a_tick_resolution_requires_its_own_type(resolution: str, required: list[str]) -> None:
    with pytest.raises(ValidationError, match="must also be required"):
        build(resolution=resolution, data_requirements=required)


def test_tick_resolution_accepts_trades_or_quotes() -> None:
    assert build(resolution="tick", data_requirements=["trade"]).resolution == "tick"
    assert build(resolution="tick", data_requirements=["quote"]).resolution == "tick"
    with pytest.raises(ValidationError, match="needs 'trade' or 'quote'"):
        build(resolution="tick", data_requirements=["bar"])


def test_a_zero_resolution_is_refused() -> None:
    with pytest.raises(ValidationError, match="resolution"):
        build(resolution="0m")


def test_a_zero_horizon_is_refused() -> None:
    with pytest.raises(ValidationError, match="horizon"):
        build(horizon="0d")


def test_a_quote_spread_needs_quotes() -> None:
    with pytest.raises(ValidationError, match="costs.spread"):
        build(costs={"spread": "quotes"})
    assert build(costs={"spread": "quotes"}, data_requirements=["bar", "quote"]).costs is not None


def test_a_fixed_spread_needs_its_width() -> None:
    with pytest.raises(ValidationError, match="costs.fixed_bps"):
        build(costs={"spread": "fixed_bps"})
    assert build(costs={"spread": "fixed_bps", "fixed_bps": 2}).costs is not None


def test_costs_may_be_any_subset() -> None:
    assert build(costs={"commission_bps": 0.5}).costs is not None


def test_constraints_must_include_strategy_integrity() -> None:
    with pytest.raises(ValidationError, match="strategy_integrity"):
        build(constraints=[{"id": "min_trades", "params": {"min": 30}}])
    assert build(constraints=[{"id": "strategy_integrity"}]).constraints is not None


def test_constraints_may_not_repeat_a_gate() -> None:
    with pytest.raises(ValidationError, match="repeats a gate id"):
        build(constraints=[{"id": "strategy_integrity"}, {"id": "strategy_integrity"}])


def test_universe_and_requirements_may_not_repeat() -> None:
    with pytest.raises(ValidationError, match="universe"):
        build(universe=["DEMO", "DEMO"])
    with pytest.raises(ValidationError, match="data_requirements"):
        build(data_requirements=["bar", "bar"])


def test_an_empty_universe_is_refused() -> None:
    with pytest.raises(ValidationError, match="universe"):
        build(universe=[])


@pytest.mark.parametrize("bad", ["ab", "Demo", "demo-mr", "d" * 41, ""])
def test_the_id_is_constrained(bad: str) -> None:
    with pytest.raises(ValidationError, match="id"):
        build(id=bad)


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        build(notes="anything")


def test_risk_limits_are_positive() -> None:
    with pytest.raises(ValidationError, match="max_drawdown_pct"):
        build(risk_limits={"max_position_pct": 20, "max_drawdown_pct": 0, "max_leverage": 1})
    with pytest.raises(ValidationError, match="max_drawdown_pct"):
        build(risk_limits={"max_position_pct": 20, "max_drawdown_pct": 101, "max_leverage": 1})


def test_the_classification_is_exposed_under_its_own_name() -> None:
    hyp = build(
        construct={"id": "filter", "host": "demo_mr", "rationale": "gates entries"},
        objective={"id": "marginal_wf_sharpe", "params": {"min_delta": 0.0, "k_se": 1.0}},
        constraints=[{"id": "strategy_integrity"}],
    )
    assert hyp.construct is not None
    assert hyp.construct.id == "filter"
    assert hyp.model_dump(by_alias=True)["construct"]["id"] == "filter"


def test_a_rationale_is_bounded() -> None:
    with pytest.raises(ValidationError, match="rationale"):
        build(construct={"id": "sleeve", "rationale": "x" * 241})


@given(hypotheses())
def test_every_generated_hypothesis_keeps_its_embargo(hyp: Hypothesis) -> None:
    gap = hyp.windows.certification.start - hyp.windows.research.end
    assert gap >= timedelta(days=embargo_days(hyp.horizon))
