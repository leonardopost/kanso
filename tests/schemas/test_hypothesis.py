"""`hypothesis.yaml`: the rules that make a hypothesis admissible."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given

from kanso.errors import Exit, ValidationError
from kanso.schemas import Hypothesis, PeriodTarget, PriorTrials, embargo, embargo_days
from kanso.schemas.hypothesis import WEEKDAYS
from tests.schemas.strategies import hypotheses, period_targets, prior_trials

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


def test_tick_resolution_accepts_trades_quotes_or_the_book() -> None:
    assert build(resolution="tick", data_requirements=["trade"]).resolution == "tick"
    assert build(resolution="tick", data_requirements=["quote"]).resolution == "tick"
    assert build(resolution="tick", data_requirements=["book", "trade"]).resolution == "tick"
    with pytest.raises(ValidationError, match="needs 'trade', 'quote' or 'book'"):
        build(resolution="tick", data_requirements=["bar"])


TICK_BARS: dict[str, Any] = {
    "universe": ["SPY.ARCX", "ESZ5.XCME"],
    "resolution": "tick",
    "data_requirements": ["quote", "trade", "bar"],
    "data_by_instrument": {"SPY.ARCX": ["quote", "trade"], "ESZ5.XCME": ["bar"]},
}
"""Ticks of an equity beside a future's minute bars: the shape a cross-asset lane is."""


def test_bars_beside_ticks_are_read_at_the_size_the_file_states() -> None:
    hyp = build(**TICK_BARS, bar_resolution="1m")

    assert (hyp.resolution, hyp.bar_resolution, hyp.bar_size) == ("tick", "1m", "1m")
    assert hyp.required_of("ESZ5.XCME") == ("bar",)


def test_a_bar_size_resolution_reads_its_bars_at_itself() -> None:
    hyp = build()

    assert (hyp.bar_resolution, hyp.bar_size) == (None, "1m")


@pytest.mark.parametrize("resolution", ["tick", "quote", "trade"])
def test_bars_beside_ticks_without_a_size_are_refused_with_the_remedy(resolution: str) -> None:
    """The combination that used to pass validation and be refused at `research begin` for want
    of a snapshot at its resolution: a sleeve with no bar size, reading bars of none."""
    with pytest.raises(ValidationError) as caught:
        build(**{**TICK_BARS, "resolution": resolution})

    assert "bar_resolution:" in caught.value.message
    assert "nothing says what size the bars are read at" in caught.value.message
    assert caught.value.code is Exit.VALIDATION
    assert (caught.value.remedy or "").startswith("state bar_resolution")


def test_a_bar_resolution_beside_a_bar_size_resolution_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        build(bar_resolution="5m")

    assert "resolution 1m is a bar size" in caught.value.message
    assert caught.value.remedy == (
        "drop bar_resolution (resolution 1m is the bar size), or keep it and set resolution to "
        "tick, quote or trade"
    )


def test_a_bar_resolution_with_no_bars_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        build(resolution="tick", data_requirements=["quote"], bar_resolution="1m")

    assert "'bar' is not required" in caught.value.message
    assert (caught.value.remedy or "").startswith("add bar to data_requirements")


def test_a_zero_bar_resolution_is_refused() -> None:
    with pytest.raises(ValidationError, match="bar_resolution: must be longer than zero"):
        build(**TICK_BARS, bar_resolution="0m")


@given(hypotheses())
def test_a_bar_resolution_survives_the_round_trip(hyp: Hypothesis) -> None:
    again = Hypothesis.model_validate(hyp.model_dump(by_alias=True, mode="json"))
    assert (again.bar_resolution, again.bar_size) == (hyp.bar_resolution, hyp.bar_size)


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


def test_derived_from_names_other_hypotheses_once_each() -> None:
    assert build().derived_from == []
    assert build(derived_from=["demo_parent", "demo_other"]).derived_from == [
        "demo_parent",
        "demo_other",
    ]
    with pytest.raises(ValidationError, match="derived_from: repeats an id"):
        build(derived_from=["demo_parent", "demo_parent"])
    with pytest.raises(ValidationError, match="cannot derive from itself"):
        build(derived_from=[build().id])
    with pytest.raises(ValidationError, match="derived_from"):
        build(derived_from=["Not-An-Id"])


GRID: dict[str, Any] = {"count": 27, "source": "evidence/grid.md", "sha256": "3f" * 32}
"""An offline grid of 27 settings and the evidence it is stated from."""


def test_prior_trials_is_optional_and_reads_back_as_stated() -> None:
    assert build().prior_trials is None
    stated = build(prior_trials=GRID).prior_trials
    assert stated is not None
    assert stated.model_dump() == GRID


@pytest.mark.parametrize(
    ("block", "field"),
    [
        ({**GRID, "count": 0}, "prior_trials.count"),
        ({**GRID, "count": -27}, "prior_trials.count"),
        ({**GRID, "source": ""}, "prior_trials.source"),
        ({**GRID, "sha256": "3F" * 32}, "prior_trials.sha256"),
        ({**GRID, "sha256": "3f5c"}, "prior_trials.sha256"),
        ({"count": 27, "source": "evidence/grid.md"}, "prior_trials.sha256"),
        ({**GRID, "settings": 27}, "prior_trials.settings"),
    ],
)
def test_a_grid_is_a_count_of_one_or_more_with_its_evidence_and_nothing_else(
    block: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValidationError, match=field.replace(".", r"\.")):
        build(prior_trials=block)


@given(prior_trials())
def test_prior_trials_round_trips(grid: PriorTrials) -> None:
    assert PriorTrials.model_validate(grid.model_dump(mode="json")) == grid
    assert build(prior_trials=grid.model_dump(mode="json")).prior_trials == grid


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


def test_the_card_gates_put_the_operator_first_and_drop_a_repriced_one() -> None:
    """The whole authority rule: a model may add to what an operator requires."""
    hyp = build(
        required_constraints=[{"id": "min_trades", "params": {"min": 500}}],
        construct={"id": "sleeve", "rationale": "stands alone"},
        objective={"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 1.0}},
        constraints=[
            {"id": "min_trades", "params": {"min": 12}},
            {"id": "strategy_integrity"},
        ],
    )

    gates = hyp.card_gates

    assert [ref.id for ref in gates] == ["min_trades", "strategy_integrity"]
    assert gates[0].params == {"min": 500}, "the operator's number, not the model's 12"
    assert hyp.constraints is not None
    assert hyp.constraints[0].params == {"min": 12}, "and the model's answer is still on file"


def test_card_gates_is_the_classifier_s_list_when_the_operator_required_nothing() -> None:
    hyp = build(
        construct={"id": "sleeve", "rationale": "stands alone"},
        objective={"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 1.0}},
        constraints=[{"id": "strategy_integrity"}],
    )
    assert [ref.id for ref in hyp.card_gates] == ["strategy_integrity"]
    assert hyp.required_constraints is None


def test_an_unclassified_hypothesis_may_still_require_gates() -> None:
    """They are the operator's, so they do not wait on a classification."""
    hyp = build(required_constraints=[{"id": "max_drawdown"}])
    assert [ref.id for ref in hyp.card_gates] == ["max_drawdown"]
    assert hyp.constraints is None


def test_a_rationale_is_bounded() -> None:
    with pytest.raises(ValidationError, match="rationale"):
        build(construct={"id": "sleeve", "rationale": "x" * 241})


@given(hypotheses())
def test_every_generated_hypothesis_keeps_its_embargo(hyp: Hypothesis) -> None:
    gap = hyp.windows.certification.start - hyp.windows.research.end
    assert gap >= timedelta(days=embargo_days(hyp.horizon))


def test_a_sizing_budget_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="sizing.budget"):
        build(sizing={"mode": "full_book", "budget": 0})


def test_a_sizing_mode_outside_full_book_is_refused() -> None:
    with pytest.raises(ValidationError, match="sizing.mode"):
        build(sizing={"mode": "fixed", "budget": 1000})


def test_sizing_is_optional_and_carries_its_budget() -> None:
    assert build().sizing is None
    sized = build(sizing={"mode": "full_book", "budget": 30_000})
    assert sized.sizing is not None
    assert (sized.sizing.mode, sized.sizing.budget) == ("full_book", 30_000.0)


def test_a_scope_reads_a_custom_series_the_hypothesis_requires() -> None:
    hyp = build(
        universe=["DEMO.SIM", "OTHER.SIM"],
        data_requirements=["bar", "kanso_scope_tape"],
        session_scope={"series": "kanso_scope_tape", "flag": "in_play", "always": ["DEMO.SIM"]},
    )

    assert hyp.session_scope is not None
    assert hyp.session_scope.always == ["DEMO.SIM"]


@pytest.mark.parametrize(
    ("scope", "message"),
    [
        ({"series": "bar", "flag": "volume"}, "session_scope.series"),
        ({"series": "kanso_scope_tape", "flag": "in_play"}, "session_scope.series"),
        (
            {"series": "kanso_scope_tape", "flag": "in_play", "always": ["ELSE.SIM"]},
            "session_scope.always",
        ),
        (
            {"series": "kanso_scope_tape", "flag": "in_play", "always": ["DEMO.SIM", "DEMO.SIM"]},
            "session_scope.always",
        ),
    ],
)
def test_a_scope_must_name_a_required_series_and_only_the_universe(
    scope: dict[str, Any], message: str
) -> None:
    required = (
        ["bar", "kanso_scope_tape"]
        if scope["series"] != "kanso_scope_tape" or "always" in scope
        else ["bar"]
    )
    with pytest.raises(ValidationError, match=message):
        build(universe=["DEMO.SIM"], data_requirements=required, session_scope=scope)


def test_a_warmup_must_ask_for_at_least_one_session() -> None:
    with pytest.raises(ValidationError, match="warmup.sessions"):
        build(warmup={"sessions": 0})


def test_a_warmup_takes_only_its_session_count() -> None:
    with pytest.raises(ValidationError, match="warmup.days"):
        build(warmup={"sessions": 3, "days": 5})


def test_warmup_is_optional_and_carries_its_sessions() -> None:
    assert build().warmup is None
    warmed = build(warmup={"sessions": 20})
    assert warmed.warmup is not None
    assert warmed.warmup.sessions == 20


def test_a_benchmark_is_a_hold_of_the_first_leg() -> None:
    with pytest.raises(ValidationError, match="benchmark.hold"):
        build(benchmark={"hold": "last_leg"})


def test_benchmark_is_optional_and_names_what_is_held() -> None:
    assert build().benchmark is None
    held = build(benchmark={"hold": "first_leg"})
    assert held.benchmark is not None
    assert held.benchmark.hold == "first_leg"


@given(hypotheses())
def test_a_benchmark_survives_the_round_trip(hyp: Hypothesis) -> None:
    again = Hypothesis.model_validate(hyp.model_dump(by_alias=True, mode="json"))
    assert again.benchmark == hyp.benchmark


@given(hypotheses())
def test_a_warmup_survives_the_round_trip(hyp: Hypothesis) -> None:
    again = Hypothesis.model_validate(hyp.model_dump(by_alias=True, mode="json"))
    assert again.warmup == hyp.warmup


def test_a_book_policy_defaults_to_no_reset_no_carry_and_no_floor() -> None:
    assert build().book is None
    booked = build(book={})
    assert booked.book is not None
    assert (booked.book.reset, booked.book.financing_rate_bps, booked.book.maintenance_pct) == (
        "none",
        0.0,
        None,
    )
    assert not booked.book.funded


def test_a_book_policy_carries_its_three_rules() -> None:
    booked = build(book={"reset": "monthly", "financing_rate_bps": 250, "maintenance_pct": 30})
    assert booked.book is not None
    assert booked.book.reset == "monthly"
    assert booked.book.financing_rate_bps == 250.0
    assert booked.book.maintenance_pct == 30.0
    assert booked.book.funded


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reset", "weekly"),
        ("financing_rate_bps", -1),
        ("maintenance_pct", 0),
        ("maintenance_pct", 101),
    ],
)
def test_a_book_policy_refuses_a_value_outside_its_range(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=f"book.{field}"):
        build(book={field: value})


def test_a_maintenance_floor_above_what_the_leverage_holds_at_entry_is_refused() -> None:
    """A book levered twice holds 50% equity over gross before any price moves."""
    limits = {"max_position_pct": 100, "max_drawdown_pct": 15, "max_leverage": 2}
    with pytest.raises(ValidationError, match="book.maintenance_pct: 60% is above the 50%"):
        build(book={"maintenance_pct": 60}, risk_limits=limits)
    held = build(book={"maintenance_pct": 50}, risk_limits=limits)
    assert held.book is not None and held.book.maintenance_pct == 50.0


@given(hypotheses())
def test_a_book_policy_survives_the_round_trip(hyp: Hypothesis) -> None:
    again = Hypothesis.model_validate(hyp.model_dump(by_alias=True, mode="json"))
    assert again.book == hyp.book


TICK_BOOK: dict[str, Any] = {"resolution": "tick", "data_requirements": ["book", "trade"]}


def test_depth_is_optional_and_carries_its_grid_and_levels() -> None:
    assert build().depth is None
    shown = build(**TICK_BOOK, depth={"every_ms": 100, "levels": 3})
    assert shown.depth is not None
    assert (shown.depth.every_ms, shown.depth.levels, shown.depth.every_ns) == (
        100,
        3,
        100_000_000,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("every_ms", 0), ("every_ms", -100), ("levels", 0), ("levels", 401)],
)
def test_depth_refuses_a_value_outside_its_range(field: str, value: int) -> None:
    depth = {"every_ms": 100, "levels": 3, field: value}
    with pytest.raises(ValidationError, match=f"depth.{field}"):
        build(**TICK_BOOK, depth=depth)


def test_depth_takes_every_level_the_sampled_channel_serves() -> None:
    assert build(**TICK_BOOK, depth={"every_ms": 1, "levels": 400}).depth is not None


def test_depth_needs_the_book_it_is_a_view_of() -> None:
    with pytest.raises(ValidationError, match="depth: needs 'book' in data_requirements"):
        build(
            resolution="tick",
            data_requirements=["trade"],
            depth={"every_ms": 100, "levels": 3},
        )


def test_depth_refuses_a_quote_series_beside_its_own_level_one() -> None:
    with pytest.raises(ValidationError, match="depth: refuses 'quote' in data_requirements"):
        build(
            resolution="tick",
            data_requirements=["book", "quote"],
            depth={"every_ms": 100, "levels": 3},
        )


@given(hypotheses())
def test_depth_survives_the_round_trip(hyp: Hypothesis) -> None:
    again = Hypothesis.model_validate(hyp.model_dump(by_alias=True, mode="json"))
    assert again.depth == hyp.depth


WEEKLY: dict[str, Any] = {
    "period": "week",
    "opens": "Mon 04:00",
    "tz": "America/New_York",
    "target_bp": 1000,
    "of": "capital",
}


def refusal(**changes: Any) -> ValidationError:
    with pytest.raises(ValidationError) as refused:
        build(**changes)
    return refused.value


def test_a_period_target_carries_its_week_its_return_and_its_base() -> None:
    assert build().period_target is None
    target = build(period_target=WEEKLY).period_target
    assert target is not None
    assert (target.period, target.opens, target.tz, target.target_bp, target.of) == (
        "week",
        "Mon 04:00",
        "America/New_York",
        1000.0,
        "capital",
    )
    assert (target.weekday, target.minute) == (0, 240)
    later = build(period_target={**WEEKLY, "opens": "Fri 23:59", "of": "period_open"})
    assert later.period_target is not None
    assert (later.period_target.weekday, later.period_target.minute) == (4, 1439)


def test_the_classifying_model_reads_a_period_target_and_its_docstring_says_so() -> None:
    """kanso never writes `period_target` into the operator's file, and the document a
    classification sends the model carries it whole, as it carries `sizing`; the key's
    docstring says that, not that classification never reads it (`docs/backlog.md` row 278
    records the older keys that still do), nor that no file kanso writes holds it: a draft
    `kanso hyp explore` writes is the model's whole file (row 277)."""
    from kanso.classify.classify import _document

    shown = _document(build(period_target=WEEKLY))
    said = " ".join((PeriodTarget.__doc__ or "").split())

    assert shown["period_target"] == {**WEEKLY, "target_bp": 1000.0}
    assert "kanso never writes it into the operator's file, and the classifying model" in said
    assert "`kanso hyp explore` drafts is a file the model writes whole" in said
    assert "row 277" in said
    assert "neither reads nor writes" not in said


@pytest.mark.parametrize("missing", sorted(WEEKLY))
def test_every_key_of_a_period_target_is_the_operator_s_and_none_has_a_default(
    missing: str,
) -> None:
    stated = {key: value for key, value in WEEKLY.items() if key != missing}
    refused = refusal(period_target=stated)
    assert f"period_target: states no {missing}; every key" in refused.message
    assert "of: capital or of: period_open" in (refused.remedy or "")


@pytest.mark.parametrize(
    ("field", "value", "said", "remedy"),
    [
        ("tz", "UTC", "period_target: tz: 'UTC' is not America/New_York", "America/New_York"),
        ("tz", "America/Chicago", "is not America/New_York", "set period_target.tz"),
        ("opens", "Mon 4:00", "period_target: opens: 'Mon 4:00' is not a weekday", "HH:MM"),
        ("opens", "Monday 04:00", "is not a weekday and a 24-hour wall time", '"Mon 04:00"'),
        ("opens", "Mon 24:00", "is not a weekday and a 24-hour wall time", '"Mon 04:00"'),
        ("opens", "mon 04:00", "is not a weekday and a 24-hour wall time", '"Mon 04:00"'),
        ("opens", "Mon 04:00\n", "opens: 'Mon 04:00\\n' is not a weekday", '"Mon 04:00"'),
        ("target_bp", 0, "period_target: target_bp: 0 is not a return above zero", "1000"),
        ("target_bp", -50, "target_bp: -50 is not a return above zero", "+10%"),
        ("target_bp", float("nan"), "target_bp: nan is not a return above zero", "+10%"),
        ("target_bp", float("inf"), "target_bp: inf is not a return above zero", "+10%"),
    ],
)
def test_a_period_target_that_cannot_be_read_is_refused_with_its_remedy(
    field: str, value: object, said: str, remedy: str
) -> None:
    refused = refusal(period_target={**WEEKLY, field: value})
    assert said in refused.message
    assert remedy in (refused.remedy or "")


@pytest.mark.parametrize(("field", "value"), [("period", "month"), ("of", "equity")])
def test_a_period_target_names_the_one_period_and_the_two_bases_built(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=f"period_target.{field}"):
        build(period_target={**WEEKLY, field: value})


@pytest.mark.parametrize(
    ("changes", "said", "remedy"),
    [
        ({"book": {"reset": "monthly"}}, "beside book.reset: monthly", "set book.reset to none"),
        ({"benchmark": {"hold": "first_leg"}}, "beside benchmark", "drop benchmark"),
        (
            {"resolution": "tick", "data_requirements": ["book", "trade"]},
            "requires 'book'",
            "drop book from data_requirements",
        ),
    ],
)
def test_a_period_target_beside_what_it_cannot_be_applied_with_is_refused(
    changes: dict[str, Any], said: str, remedy: str
) -> None:
    refused = refusal(period_target=WEEKLY, **changes)
    assert said in refused.message
    assert remedy in (refused.remedy or "")
    assert build(period_target=WEEKLY, book={"financing_rate_bps": 250}).book is not None


@pytest.mark.parametrize(
    ("opens", "day"),
    [("Sun 02:30", "2024-03-10"), ("Sun 01:30", "2024-11-03"), ("Sun 01:00", "2024-11-03")],
)
def test_an_opening_a_change_of_clocks_skips_or_repeats_is_refused(opens: str, day: str) -> None:
    """New York's clocks change at 02:00 on a Sunday: in March 02:00 to 02:59 never reads, and in
    November 01:00 to 01:59 reads twice."""
    refused = refusal(period_target={**WEEKLY, "opens": opens})
    assert (
        f"period_target.opens: {opens} America/New_York does not exist, or exists twice, on "
        in (refused.message)
    )
    assert day in refused.message
    assert '"Mon 04:00" never is' in (refused.remedy or "")


def test_an_opening_is_held_to_the_forward_window_s_first_year_too() -> None:
    """No research or certification date meets a change, and the open forward window's first
    year does."""
    windows = {
        "research": {"start": "2024-04-01", "end": "2024-06-30"},
        "certification": {"start": "2024-07-08", "end": "2024-09-30"},
        "forward": {"start": "2024-10-07"},
    }
    refused = refusal(windows=windows, period_target={**WEEKLY, "opens": "Sun 01:30"})
    assert "on 2024-11-03" in refused.message
    assert build(windows=windows, period_target={**WEEKLY, "opens": "Sun 03:00"}).period_target


@given(hypotheses())
def test_a_period_target_survives_the_round_trip(hyp: Hypothesis) -> None:
    again = Hypothesis.model_validate(hyp.model_dump(by_alias=True, mode="json"))
    assert again.period_target == hyp.period_target


@given(period_targets())
def test_a_period_target_reads_back_its_weekday_and_minute(target: PeriodTarget) -> None:
    again = PeriodTarget.model_validate(target.model_dump(mode="json"))
    assert again == target
    assert f"{WEEKDAYS[again.weekday]} {again.minute // 60:02d}:{again.minute % 60:02d}" == (
        target.opens
    )


MIXED: dict[str, Any] = {
    "universe": ["ETH-USD.COINBASE", "BMNR.XNYS"],
    "resolution": "trade",
    "data_requirements": ["trade", "quote"],
}


def test_an_instrument_may_be_asked_for_a_subset_of_the_types() -> None:
    """A crypto exchange's prints beside an equity's quotes: one universe, two sources."""
    hyp = build(**MIXED, data_by_instrument={"ETH-USD.COINBASE": ["trade"]})

    assert hyp.required_of("ETH-USD.COINBASE") == ("trade",)
    assert hyp.required_of("BMNR.XNYS") == ("trade", "quote")


@pytest.mark.parametrize(
    ("by_instrument", "message"),
    [
        ({"SOL-USD.COINBASE": ["trade"]}, "is not in the universe"),
        ({"ETH-USD.COINBASE": []}, "names no type"),
        ({"ETH-USD.COINBASE": ["trade", "trade"]}, "repeats a type"),
        ({"ETH-USD.COINBASE": ["bar"]}, "is not in data_requirements"),
        (
            {"ETH-USD.COINBASE": ["trade"], "BMNR.XNYS": ["trade"]},
            "no instrument is asked for quote",
        ),
    ],
)
def test_a_list_that_adds_or_leaves_a_type_is_refused(
    by_instrument: dict[str, list[str]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        build(**MIXED, data_by_instrument=by_instrument)
