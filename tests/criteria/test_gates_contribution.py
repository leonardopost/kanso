"""The three gates a per-period objective and a per-share account needed: the contribution
deflated for the search that found it, a floor on the sessions an edge was seen on, and the
recorded fills re-priced under another cost model."""

from __future__ import annotations

from datetime import timedelta
from math import fsum
from typing import Any

import pytest

from kanso.criteria.gates import (
    cost_scenario,
    deflated_contribution,
    min_event_days,
    repriced,
)
from kanso.criteria.run import Fill
from tests.criteria.builders import START, at, build_run, context, fill, make_hyp, trade

CONTRIBUTION_HYP = {
    "objective": {"id": "wf_contribution_bps", "params": {"min_delta": 0.5, "k_se": 1.0}}
}
MARGINAL_HYP = {
    "objective": {"id": "marginal_wf_contribution_bps", "params": {"min_delta": 0.5, "k_se": 1.0}}
}
RETURNS = (30.0, -10.0, 40.0, 10.0, 50.0, -20.0, 60.0, 20.0)


def contribution_context(**overrides: Any) -> Any:
    research = build_run(RETURNS)
    fields: dict[str, Any] = {
        "hyp": make_hyp(**CONTRIBUTION_HYP),
        "stage": "cert",
        "params": {"min_probability": 0.5},
        "research_run": research,
        "trial_metrics": (2.0, 2.4, 1.8, 2.2),
    }
    return context(research, **{**fields, **overrides})


# --- deflated_contribution ----------------------------------------------------


def test_deflated_contribution_reports_a_probability_in_the_units_of_the_objective() -> None:
    result = deflated_contribution.evaluate(contribution_context())

    assert 0.0 <= result.evidence["probability"] <= 1.0
    assert result.evidence["trials"] == 4 and result.evidence["periods"] == 8
    assert result.evidence["contribution_bps"] > 0
    assert result.evidence["standard_error_bps"] > 0


def test_a_wider_or_noisier_search_deflates_a_contribution_further() -> None:
    narrow = deflated_contribution.evaluate(contribution_context(trial_metrics=(2.0, 2.4)))
    wide = deflated_contribution.evaluate(contribution_context(trial_metrics=(2.0, 2.4) * 2500))
    tight = deflated_contribution.evaluate(contribution_context(trial_metrics=(2.0, 2.01)))
    loose = deflated_contribution.evaluate(contribution_context(trial_metrics=(-40.0, 40.0)))

    assert wide.evidence["probability"] < narrow.evidence["probability"]
    assert loose.evidence["probability"] < tight.evidence["probability"]


def test_deflated_contribution_fails_below_the_floor() -> None:
    result = deflated_contribution.evaluate(
        contribution_context(params={"min_probability": 0.999}, trial_metrics=(-40.0, 40.0))
    )

    assert not result.passed and result.skipped is None


def test_the_marginal_form_deflates_the_differences_against_the_host() -> None:
    host = build_run(tuple(r / 2 for r in RETURNS))
    over = deflated_contribution.evaluate(
        contribution_context(hyp=make_hyp(**MARGINAL_HYP), host_research_run=host, host_run=host)
    )
    alone = deflated_contribution.evaluate(contribution_context())

    assert over.evidence["contribution_bps"] < alone.evidence["contribution_bps"]
    assert over.evidence["contribution_bps"] == pytest.approx(
        alone.evidence["contribution_bps"] / 2
    )


def test_deflated_contribution_is_skipped_on_a_sharpe_objective() -> None:
    result = deflated_contribution.evaluate(
        contribution_context(
            hyp=make_hyp(
                objective={"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 1.0}}
            )
        )
    )

    assert result.passed and result.skipped is not None and "not a contribution" in result.skipped


@pytest.mark.parametrize(
    "overrides",
    [
        {"params": {}},
        {"hyp": make_hyp(objective=None, constraints=None)},
        {"trial_metrics": (1.0,)},
        {"research_run": None},
        {"research_run": build_run((1.0, 1.0)), "run": build_run((1.0, 1.0)), "research_folds": 1},
        {"research_run": build_run((1.0,) * 8)},
        {"hyp": make_hyp(**MARGINAL_HYP), "host_research_run": None},
    ],
)
def test_deflated_contribution_without_its_context_judges_nothing(overrides: Any) -> None:
    result = deflated_contribution.evaluate(contribution_context(**overrides))

    assert result.passed and result.skipped is not None


# --- min_event_days -----------------------------------------------------------


def test_min_event_days_counts_the_distinct_sessions_with_a_fill() -> None:
    fills = tuple(fill(START + timedelta(days=d)) for d in (0, 0, 0, 3, 3, 5))
    run = build_run((1.0,) * 8, fills=fills)

    result = min_event_days.evaluate(context(run, params={"min": 3}))

    assert result.passed and result.evidence == {"event_days": 3, "min": 3}
    assert not min_event_days.evaluate(context(run, params={"min": 4})).passed


def test_min_event_days_without_a_minimum_judges_nothing() -> None:
    result = min_event_days.evaluate(context(build_run((1.0,) * 4)))

    assert result.passed and result.skipped is not None


# --- cost_scenario --------------------------------------------------------------


def priced_run() -> Any:
    """Four days, one round trip a day on a 10,000 notional at 100, charged 1 bp a fill."""
    trades = tuple(trade(START + timedelta(days=i), pnl=20.0, cost=1.0) for i in range(4))
    fills = tuple(f for t in trades for f in t.fills)
    return build_run((19.0, 19.0, 19.0, 19.0), trades=trades, fills=fills)


def test_repriced_recharges_every_fill_under_the_scenario_and_moves_the_series() -> None:
    run = priced_run()

    under = repriced(run, {"commission_per_share": 0.01, "fixed_bps": 2.0})

    # 100 shares at a cent is 1.00, plus half of 2 bp on 10,000 is 1.00: 2.00 a fill against 1.00
    assert [f.cost for f in under.fills] == pytest.approx([2.0] * 4)
    assert under.returns == pytest.approx(tuple(r - 1.0 for r in run.returns))
    assert [t.pnl_net for t in under.trades] == pytest.approx([19.0] * 4)
    assert under.equity[-1] == pytest.approx(run.equity[-1] - 4.0)
    assert fsum(f.cost for f in under.fills) == pytest.approx(8.0)


def test_repriced_charges_a_maker_the_stated_rate_alone_and_a_taker_the_rest() -> None:
    maker = Fill(
        ts_ns=at(START), instrument_id="DEMO", side="BUY", qty=100.0, px=100.0, cost=1.0, maker=True
    )
    taker = Fill(
        ts_ns=at(START + timedelta(days=1)),
        instrument_id="DEMO",
        side="SELL",
        qty=100.0,
        px=100.0,
        cost=1.0,
    )
    run = build_run((10.0, 10.0), fills=(maker, taker))

    under = repriced(run, {"commission_per_share": 0.005, "maker_bps": 0.0, "fixed_bps": 2.0})

    assert under.fills[0].cost == pytest.approx(0.0), "the maker pays the maker rate alone"
    assert under.fills[1].cost == pytest.approx(0.5 + 1.0), (
        "the taker pays per share and half the width"
    )


def test_cost_scenario_recomputes_the_objective_under_the_scenario() -> None:
    run = priced_run()
    ctx = context(
        run,
        hyp=make_hyp(**CONTRIBUTION_HYP),
        stage="cert",
        params={"commission_per_share": 0.01, "fixed_bps": 2.0, "min_metric": 0.0},
    )

    result = cost_scenario.evaluate(ctx)

    assert result.passed
    assert result.evidence["objective"] == "wf_contribution_bps"
    assert result.evidence["metric_scenario"] < result.evidence["metric_recorded"]
    assert result.evidence["cost_scenario"] == pytest.approx(8.0)
    assert result.evidence["scenario"] == {"commission_per_share": 0.01, "fixed_bps": 2.0}


def test_cost_scenario_fails_an_edge_the_schedule_eats() -> None:
    run = priced_run()
    ctx = context(
        run,
        hyp=make_hyp(**CONTRIBUTION_HYP),
        stage="cert",
        params={
            "commission_bps": 50.0,
            "min_metric": 0.0,
        },  # 50 bp a fill on 10,000 is 50 a fill against 20 a trade
    )

    assert not cost_scenario.evaluate(ctx).passed


def test_cost_scenario_without_a_model_judges_nothing() -> None:
    result = cost_scenario.evaluate(context(priced_run(), hyp=make_hyp(**CONTRIBUTION_HYP)))

    assert result.passed and result.skipped is not None


def test_cost_scenario_without_an_objective_judges_nothing() -> None:
    result = cost_scenario.evaluate(
        context(
            priced_run(),
            hyp=make_hyp(objective=None, constraints=None),
            params={"commission_per_share": 0.01},
        )
    )

    assert result.passed and result.skipped is not None
