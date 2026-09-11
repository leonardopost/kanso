"""Every implemented gate, on hand-built runs whose answer is arithmetic."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from kanso.criteria import DatasetFacts, Held, Trade, gates
from kanso.criteria.gates import (
    book_correlation,
    bootstrap,
    capacity_vs_adv,
    cost_stress,
    deflated_sharpe,
    embargoed_window,
    max_drawdown,
    max_hold,
    min_trades,
    position_size,
    publication_lag,
    strategy_integrity,
    stressed,
    walk_forward_consistency,
)
from kanso.criteria.run import NS_PER_SECOND, Fill
from tests.criteria.builders import (
    START,
    at,
    book,
    build_run,
    context,
    fill,
    held,
    make_hyp,
    trade,
)

DAYS = [date(2024, 1, 1 + i) for i in range(4)]
FLAT = (0.0, 0.0, 0.0, 0.0)
SHARPE_HYP = {"objective": {"id": "wf_sharpe_net", "params": {"min_delta": 0.0, "k_se": 1.0}}}


def trading_run(*pnl: float, cost: float = 0.0) -> Any:
    """A four-day run closing one trade a day on a 10,000 notional."""
    return build_run(
        FLAT,
        trades=tuple(
            trade(day, pnl=value, notional=10_000.0, cost=cost)
            for day, value in zip(DAYS, pnl, strict=True)
        ),
    )


# --- max_hold ---------------------------------------------------------------------


def closed(instrument: str, opened_ns: int, closed_ns: int) -> Trade:
    """A position of one hundred shares, opened and closed at these instants."""
    return Trade(
        opened_ns=opened_ns,
        closed_ns=closed_ns,
        instrument_id=instrument,
        qty=100.0,
        avg_open=100.0,
        avg_close=100.0,
        pnl_net=0.0,
        cost=0.0,
        fills=(),
    )


def open_at_the_close(run: Any, instrument: str = "DEMO", qty: float = 100.0) -> Any:
    """The run with `instrument` still held at its last period end."""
    mark = Held(ts_ns=run.period_ends_ns[-1], instrument_id=instrument, qty=qty, notional=10_000.0)
    return replace(run, held=(*run.held, mark))


def test_max_hold_times_a_closed_position_from_its_entry_fill_to_its_exit_fill() -> None:
    run = build_run(
        FLAT * 2, trades=(closed("DEMO", at(START, 9), at(START + timedelta(days=3), 9)),)
    )

    result = max_hold.evaluate(context(run, params={"days": 3}))

    assert result.passed
    assert result.evidence["longest_days"] == 3.0
    assert result.evidence["measured_on"] == "fills"
    assert result.evidence["unit"] == "calendar days"
    assert not max_hold.evaluate(context(run, params={"days": 2})).passed


def test_max_hold_counts_a_weekend_inside_a_hold_and_refuses_one_hour_over() -> None:
    """Fills are instants, so the days between them are the days there were."""
    friday, monday = date(2024, 3, 8), date(2024, 3, 11)
    exact = build_run(
        FLAT * 2, start=friday, trades=(closed("DEMO", at(friday, 9), at(monday, 9)),)
    )
    late = build_run(
        FLAT * 2, start=friday, trades=(closed("DEMO", at(friday, 9), at(monday, 10)),)
    )

    assert max_hold.evaluate(context(exact, params={"days": 3})).evidence["longest_days"] == 3.0
    assert max_hold.evaluate(context(exact, params={"days": 3})).passed
    assert not max_hold.evaluate(context(late, params={"days": 3})).passed


def test_max_hold_times_a_position_still_open_at_the_window_close_from_its_first_fill() -> None:
    """No trade ever closed, so a trade count alone would see nothing to refuse."""
    run = open_at_the_close(build_run(FLAT * 2, fills=(fill(START + timedelta(days=1)),)))

    result = max_hold.evaluate(context(run, params={"days": 7}))

    assert result.passed
    assert result.evidence["longest_days"] == pytest.approx(6.5)
    assert result.evidence["n_positions"] == 1
    assert not max_hold.evaluate(context(run, params={"days": 6})).passed


def test_max_hold_reads_a_reopened_position_from_the_fill_after_the_last_close() -> None:
    day = [START + timedelta(days=i) for i in range(8)]
    run = open_at_the_close(
        build_run(
            FLAT * 2,
            trades=(closed("DEMO", at(day[0]), at(day[1])),),
            fills=(fill(day[0]), fill(day[1]), fill(day[3])),
        )
    )

    result = max_hold.evaluate(context(run, params={"days": 5}))

    assert result.evidence["n_positions"] == 2
    assert result.evidence["longest_days"] == pytest.approx(4.5), "day 3 noon to the close of day 7"
    assert result.passed


def test_max_hold_without_a_limit_or_a_position_judges_nothing() -> None:
    assert max_hold.evaluate(context(build_run(FLAT))).skipped is not None
    assert max_hold.evaluate(context(build_run(FLAT), params={"days": 5})).skipped is not None


def test_max_hold_times_an_attached_construct_on_period_ends_by_instrument_and_sign() -> None:
    """The candidate's fills are its host's too, so its positions are read at period ends."""
    host = build_run(FLAT * 2)
    marks = tuple(
        Held(ts_ns=end, instrument_id="DEMO", qty=100.0 if i < 4 else -100.0, notional=10_000.0)
        for i, end in enumerate(host.period_ends_ns)
    )
    combined = replace(host, held=marks)

    result = max_hold.evaluate(context(combined, host_run=host, params={"days": 4}))

    assert result.passed
    assert result.evidence["measured_on"] == "period_ends"
    assert result.evidence["n_positions"] == 2, "a reversal in one instrument is two positions"
    assert result.evidence["longest_days"] == 4.0
    assert not max_hold.evaluate(context(combined, host_run=host, params={"days": 3})).passed


def test_max_hold_on_period_ends_is_unmoved_by_a_clock_change_inside_the_stretch() -> None:
    host = build_run(FLAT * 2)
    shifted = list(host.period_ends_ns)
    shifted[4:] = [end + 3600 * NS_PER_SECOND for end in shifted[4:]]
    host = replace(host, period_ends_ns=tuple(shifted))
    marks = tuple(
        Held(ts_ns=end, instrument_id="DEMO", qty=100.0, notional=10_000.0) for end in shifted
    )

    result = max_hold.evaluate(
        context(replace(host, held=marks), host_run=host, params={"days": 8})
    )

    assert result.passed
    assert result.evidence["longest_days"] == 8.0


def test_max_hold_for_an_attached_construct_that_added_nothing_judges_nothing() -> None:
    host = build_run(FLAT, holdings=tuple(held(DAYS[i], 10_000.0) for i in range(4)))

    result = max_hold.evaluate(context(host, host_run=host, params={"days": 1}))

    assert result.passed and result.skipped is not None


# --- min_trades -------------------------------------------------------------------


def test_min_trades_counts_the_window_and_every_fold() -> None:
    result = min_trades.evaluate(context(trading_run(1.0, 1.0, 1.0, 1.0), params={"min": 4}))
    assert result.passed
    assert result.evidence["trades_per_fold"] == [1, 1, 1, 1]


def test_min_trades_fails_a_window_with_too_few() -> None:
    assert not min_trades.evaluate(
        context(trading_run(1.0, 1.0, 1.0, 1.0), params={"min": 5})
    ).passed


def test_min_trades_fails_when_a_fold_traded_nothing() -> None:
    run = build_run(FLAT, trades=tuple(trade(DAYS[0], pnl=1.0) for _ in range(9)))
    assert not min_trades.evaluate(context(run, params={"min": 4})).passed


def test_min_trades_without_a_minimum_judges_nothing() -> None:
    result = min_trades.evaluate(context(trading_run(1.0, 1.0, 1.0, 1.0)))
    assert result.passed and result.skipped is not None


# --- max_drawdown -----------------------------------------------------------------


def test_max_drawdown_reads_the_hypothesis_own_limit() -> None:
    run = build_run((100.0, -200.0, 50.0), capital=1_000.0)
    result = max_drawdown.evaluate(context(run))
    assert not result.passed
    assert result.evidence == {"max_drawdown_pct": pytest.approx(20.0), "limit_pct": 15.0}


def test_max_drawdown_passes_inside_the_limit() -> None:
    run = build_run((100.0, -50.0, 50.0), capital=1_000.0)
    assert max_drawdown.evaluate(context(run)).passed


# --- strategy_integrity -----------------------------------------------------------


def test_strategy_integrity_passes_a_clean_lane(lane: Path) -> None:
    result = strategy_integrity.evaluate(context(build_run((1.0,)), lane_dir=lane))
    assert result.passed
    assert result.evidence == {"n_problems": 0, "problems": []}


def test_strategy_integrity_fails_a_lane_that_reaches_the_catalog(lane: Path) -> None:
    (lane / "strategy.py").write_text("import nautilus_trader.persistence\n", encoding="utf-8")
    result = strategy_integrity.evaluate(context(build_run((1.0,)), lane_dir=lane))
    assert not result.passed
    assert result.evidence["n_problems"] == 1


def test_strategy_integrity_without_a_lane_directory_judges_nothing() -> None:
    result = strategy_integrity.evaluate(context(build_run((1.0,))))
    assert result.passed and result.skipped is not None


# --- embargoed_window -------------------------------------------------------------


def test_embargoed_window_compares_the_two_windows() -> None:
    result = embargoed_window.evaluate(
        context(
            trading_run(150.0, 150.0, 150.0, 150.0),
            params={"min_fraction": 0.5},
            research_run=trading_run(200.0, 200.0, 200.0, 200.0),
        )
    )
    assert result.passed
    assert result.evidence["certification"] == pytest.approx(150.0)
    assert result.evidence["research"] == pytest.approx(200.0)


def test_embargoed_window_fails_a_collapse_past_the_embargo() -> None:
    assert not embargoed_window.evaluate(
        context(
            trading_run(150.0, 150.0, 150.0, 150.0),
            params={"min_fraction": 0.9},
            research_run=trading_run(200.0, 200.0, 200.0, 200.0),
        )
    ).passed


def test_embargoed_window_fails_a_negative_certification_metric() -> None:
    assert not embargoed_window.evaluate(
        context(
            trading_run(-10.0, -10.0, -10.0, -10.0),
            params={"min_fraction": 0.0},
            research_run=trading_run(-100.0, -100.0, -100.0, -100.0),
        )
    ).passed


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"params": {"min_fraction": 0.5}},
        {"params": {"min_fraction": 0.5}, "hyp": make_hyp(objective=None, constraints=None)},
    ],
)
def test_embargoed_window_without_its_context_judges_nothing(overrides: Any) -> None:
    result = embargoed_window.evaluate(context(trading_run(1.0, 1.0, 1.0, 1.0), **overrides))
    assert result.passed and result.skipped is not None


# --- walk_forward_consistency -----------------------------------------------------


def test_walk_forward_consistency_counts_positive_folds() -> None:
    result = walk_forward_consistency.evaluate(
        context(
            trading_run(50.0, 50.0, 50.0, 50.0),
            params={"min_positive_folds": 3},
            research_run=trading_run(100.0, -50.0, 200.0, 300.0),
        )
    )
    assert result.passed
    assert result.evidence["positive_folds"] == 3
    assert result.evidence["folds"] == pytest.approx([100.0, -50.0, 200.0, 300.0])


def test_walk_forward_consistency_fails_too_few_positive_folds() -> None:
    assert not walk_forward_consistency.evaluate(
        context(
            trading_run(50.0, 50.0, 50.0, 50.0),
            params={"min_positive_folds": 4},
            research_run=trading_run(100.0, -50.0, 200.0, 300.0),
        )
    ).passed


def test_walk_forward_consistency_fails_when_certification_is_the_worst_fold() -> None:
    assert not walk_forward_consistency.evaluate(
        context(
            trading_run(-100.0, -100.0, -100.0, -100.0),
            params={"min_positive_folds": 1},
            research_run=trading_run(100.0, -50.0, 200.0, 300.0),
        )
    ).passed


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"params": {"min_positive_folds": 1}},
        {"params": {"min_positive_folds": 1}, "hyp": make_hyp(objective=None, constraints=None)},
    ],
)
def test_walk_forward_consistency_without_its_context_judges_nothing(overrides: Any) -> None:
    result = walk_forward_consistency.evaluate(
        context(trading_run(1.0, 1.0, 1.0, 1.0), **overrides)
    )
    assert result.passed and result.skipped is not None


# --- position_size ------------------------------------------------------------------


def sized(*notionals: float, capital: float = 10_000.0, **overrides: Any) -> Any:
    """A run holding one instrument at each of these marked values."""
    run = build_run(
        (0.0,) * max(len(notionals), 1),
        capital=capital,
        holdings=tuple(held(START + timedelta(days=i), n) for i, n in enumerate(notionals)),
    )
    return context(run, **overrides)


def test_position_size_passes_a_position_held_inside_the_band() -> None:
    result = position_size.evaluate(
        sized(9_900.0, 10_000.0, 10_050.0, params={"min_pct": 95.0, "max_pct": 105.0})
    )
    assert result.passed
    assert result.evidence["n_held"] == 3
    assert result.evidence["n_outside"] == 0


def test_position_size_refuses_a_strategy_holding_a_fraction_of_what_was_asked() -> None:
    """The defect it exists for: every risk limit is a ceiling, so a tenth satisfies them."""
    result = position_size.evaluate(
        sized(1_761.75, 2_099.98, params={"min_pct": 95.0, "max_pct": 105.0})
    )
    assert not result.passed
    assert result.evidence["n_outside"] == 2
    assert result.evidence["largest_pct"] < 95.0


def test_position_size_judges_every_period_and_not_the_middle_one() -> None:
    """A size instruction is broken by one period that breaks it."""
    result = position_size.evaluate(
        sized(10_000.0, 10_000.0, 3_000.0, params={"min_pct": 95.0, "max_pct": 105.0})
    )
    assert not result.passed
    assert result.evidence["n_outside"] == 1
    assert result.evidence["median_pct"] == 100.0, "the median would have passed it"


def test_position_size_reads_a_floor_on_its_own() -> None:
    """`risk_limits` can already say 'no larger than'. Only this can say 'no smaller'."""
    result = position_size.evaluate(sized(3_000.0, params={"min_pct": 95.0}))
    assert not result.passed
    assert result.evidence["max_pct"] is None


def test_position_size_refuses_a_band_no_position_could_satisfy(caplog: Any) -> None:
    """An inverted band would otherwise discard every card forever and say nothing."""
    result = position_size.evaluate(sized(10_000.0, params={"min_pct": 100.0, "max_pct": 99.0}))
    assert not result.passed
    assert "above max_pct" in str(result.evidence["refused"])


def test_position_size_measures_only_what_a_modifier_added_to_its_host() -> None:
    """A modifier's run holds the host's positions too, so the host's are subtracted."""
    day = START
    host = build_run((0.0,), capital=10_000.0, holdings=(held(day, 10_000.0, qty=100.0),))
    both = build_run((0.0,), capital=10_000.0, holdings=(held(day, 12_000.0, qty=120.0),))

    result = position_size.evaluate(
        context(both, host_run=host, params={"min_pct": 15.0, "max_pct": 25.0})
    )

    assert result.passed, "20 shares of its own at 100, not the host's 120"
    assert result.evidence["n_held"] == 1
    assert result.evidence["largest_pct"] == 20.0
    assert not position_size.evaluate(
        context(both, params={"min_pct": 15.0, "max_pct": 25.0})
    ).passed, "without the host it would judge the host's 120% as the candidate's"


def test_position_size_ignores_a_period_where_a_modifier_changed_nothing() -> None:
    """Holding exactly what the host holds is holding nothing of your own."""
    first, second = START, START + timedelta(days=1)
    host = build_run(
        (0.0, 0.0),
        capital=10_000.0,
        holdings=(held(first, 10_000.0, qty=100.0), held(second, 10_000.0, qty=100.0)),
    )
    both = build_run(
        (0.0, 0.0),
        capital=10_000.0,
        holdings=(held(first, 10_000.0, qty=100.0), held(second, 12_000.0, qty=120.0)),
    )

    result = position_size.evaluate(
        context(both, host_run=host, params={"min_pct": 15.0, "max_pct": 25.0})
    )

    assert result.passed
    assert result.evidence["n_held"] == 1, "the first period was all the host's"


def test_a_fold_carries_only_the_holdings_inside_it() -> None:
    """`between` rebuilds every fold, and `replace` would hand each one the whole run."""
    run = build_run(
        (0.0, 0.0, 0.0, 0.0),
        capital=10_000.0,
        holdings=tuple(held(START + timedelta(days=i), 10_000.0) for i in range(4)),
    )

    folds = run.folds(2)

    assert [len(fold.held) for fold in folds] == [2, 2], "not four in each"
    first, second = ({item.ts_ns for item in fold.held} for fold in folds)
    assert first.isdisjoint(second), "each fold holds its own periods and no others"
    assert first | second == {item.ts_ns for item in run.held}, "and between them, all of it"


def test_position_size_without_a_band_or_a_holding_judges_nothing() -> None:
    assert position_size.evaluate(sized(10_000.0)).skipped is not None
    assert position_size.evaluate(sized(params={"min_pct": 95.0})).skipped is not None


# --- deflated_sharpe --------------------------------------------------------------


def sharpe_context(**overrides: Any) -> Any:
    research = build_run((3.0, -1.0, 4.0, 1.0, 5.0, -2.0, 6.0, 2.0))
    fields: dict[str, Any] = {
        "hyp": make_hyp(**SHARPE_HYP),
        "params": {"min_dsr": 0.5},
        "research_run": research,
        "trial_metrics": (1.0, 1.2, 0.9, 1.1),
    }
    return context(research, **{**fields, **overrides})


def test_deflated_sharpe_deflates_the_research_window_estimate() -> None:
    result = deflated_sharpe.evaluate(sharpe_context())
    assert 0.0 <= result.evidence["dsr"] <= 1.0
    assert result.evidence["trials"] == 4


def test_a_wider_search_deflates_a_sharpe_further() -> None:
    """The same spread of results, arrived at over more trials, deflates further."""
    narrow = deflated_sharpe.evaluate(sharpe_context(trial_metrics=(1.0, 1.2))).evidence["dsr"]
    wide = deflated_sharpe.evaluate(sharpe_context(trial_metrics=(1.0, 1.2) * 2500)).evidence["dsr"]
    assert wide < narrow


def test_a_noisier_search_deflates_a_sharpe_further() -> None:
    tight = deflated_sharpe.evaluate(sharpe_context(trial_metrics=(1.0, 1.01))).evidence["dsr"]
    loose = deflated_sharpe.evaluate(sharpe_context(trial_metrics=(-40.0, 40.0))).evidence["dsr"]
    assert loose < tight


def test_deflated_sharpe_fails_below_the_floor() -> None:
    assert not deflated_sharpe.evaluate(
        sharpe_context(params={"min_dsr": 0.999}, trial_metrics=(-40.0, 40.0))
    ).passed


def test_deflated_sharpe_is_skipped_on_a_per_trade_objective() -> None:
    result = deflated_sharpe.evaluate(sharpe_context(hyp=make_hyp()))
    assert result.passed
    assert result.skipped is not None and "not a Sharpe" in result.skipped


@pytest.mark.parametrize(
    "overrides",
    [
        {"params": {}},
        {"hyp": make_hyp(objective=None, constraints=None)},
        {"trial_metrics": (1.0,)},
        {"research_run": None},
        {"research_run": build_run((1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0))},
    ],
)
def test_deflated_sharpe_without_its_context_judges_nothing(overrides: Any) -> None:
    result = deflated_sharpe.evaluate(sharpe_context(**overrides))
    assert result.passed and result.skipped is not None


def test_deflated_sharpe_is_skipped_when_the_deflation_denominator_collapses() -> None:
    skewed = build_run((1.0, 1.0, 1.0, 1.0, 1.0, 1.2, 1.2, 1.2))
    result = deflated_sharpe.evaluate(
        sharpe_context(research_run=skewed, run=skewed, research_folds=1)
    )
    assert result.passed
    assert result.skipped is not None and "not positive" in result.skipped


# --- cost_stress ------------------------------------------------------------------


def test_stressed_recomputes_the_series_from_the_recorded_fills() -> None:
    run = build_run(
        (100.0, 100.0),
        trades=(trade(DAYS[0], pnl=100.0, cost=20.0),),
        fills=(fill(DAYS[0], cost=20.0), fill(DAYS[1], cost=30.0)),
    )
    doubled = stressed(run, 2.0)
    assert doubled.returns == pytest.approx((80.0, 70.0))
    assert doubled.equity == pytest.approx((100_080.0, 100_150.0))
    assert doubled.trades[0].pnl_net == pytest.approx(80.0)
    assert [f.cost for f in doubled.fills] == pytest.approx([40.0, 60.0])


def test_a_fill_after_the_last_period_end_changes_no_return() -> None:
    run = build_run((10.0,), fills=(fill(date(2024, 1, 9), cost=100.0),))
    assert stressed(run, 5.0).returns == (10.0,)


def test_cost_stress_recomputes_the_metric_at_two_multiples() -> None:
    run = trading_run(100.0, 100.0, 100.0, 100.0, cost=50.0)
    result = cost_stress.evaluate(context(run, params={"mult_a": 2.0, "mult_b": 3.0}))
    assert result.passed
    assert result.evidence["metric_a"] == pytest.approx(50.0)
    assert result.evidence["metric_b"] == pytest.approx(0.0)


def test_cost_stress_fails_an_edge_that_is_only_the_cost_model() -> None:
    run = trading_run(100.0, 100.0, 100.0, 100.0, cost=50.0)
    assert not cost_stress.evaluate(context(run, params={"mult_a": 3.0, "mult_b": 4.0})).passed


@pytest.mark.parametrize(
    "overrides",
    [
        {"params": {"mult_a": 2.0}},
        {
            "params": {"mult_a": 2.0, "mult_b": 3.0},
            "hyp": make_hyp(objective=None, constraints=None),
        },
    ],
)
def test_cost_stress_without_its_context_judges_nothing(overrides: Any) -> None:
    result = cost_stress.evaluate(context(trading_run(1.0, 1.0, 1.0, 1.0), **overrides))
    assert result.passed and result.skipped is not None


# --- bootstrap --------------------------------------------------------------------


def test_bootstrap_resamples_the_trade_sequence() -> None:
    run = build_run(
        FLAT,
        trades=tuple(trade(day, pnl=-10.0) for day in DAYS),
        capital=1_000.0,
    )
    result = bootstrap.evaluate(context(run, params={"n": 200}, strategy_sha="a" * 64))
    assert result.evidence["mdd_p95"] == pytest.approx(4.0)
    assert result.evidence["objective_ci90"] == pytest.approx([-10.0, -10.0])
    assert result.passed


def test_bootstrap_fails_a_drawdown_distribution_beyond_the_limit() -> None:
    run = build_run(FLAT, trades=tuple(trade(day, pnl=-50.0) for day in DAYS), capital=1_000.0)
    assert not bootstrap.evaluate(context(run, params={"n": 200})).passed


def test_bootstrap_is_deterministic_for_one_strategy() -> None:
    run = trading_run(100.0, -40.0, 30.0, -10.0)
    first = bootstrap.evaluate(context(run, params={"n": 300}, strategy_sha="b" * 64))
    second = bootstrap.evaluate(context(run, params={"n": 300}, strategy_sha="b" * 64))
    assert first.evidence == second.evidence


def test_bootstrap_of_a_sharpe_objective_reports_a_sharpe_interval() -> None:
    run = build_run(
        FLAT,
        trades=tuple(
            trade(day, pnl=value)
            for day, value in zip(DAYS, (100.0, -40.0, 30.0, 60.0), strict=True)
        ),
    )
    result = bootstrap.evaluate(
        context(run, hyp=make_hyp(**SHARPE_HYP), params={"n": 2_000}, strategy_sha="c" * 64)
    )
    low, high = result.evidence["objective_ci90"]
    assert low < high


@pytest.mark.parametrize(
    "overrides",
    [
        {"params": {}},
        {"params": {"n": 100}, "hyp": make_hyp(objective=None, constraints=None)},
        {"params": {"n": 100}, "run": build_run((1.0, 2.0))},
    ],
)
def test_bootstrap_without_its_context_judges_nothing(overrides: Any) -> None:
    result = bootstrap.evaluate(context(trading_run(1.0, 1.0, 1.0, 1.0), **overrides))
    assert result.passed and result.skipped is not None


# --- book_correlation -------------------------------------------------------------


def test_book_correlation_measures_against_every_deployed_book() -> None:
    run = build_run((1.0, 2.0, 3.0, 4.0))
    result = book_correlation.evaluate(
        context(
            run,
            params={"max_corr": 0.8},
            deployed=[book("twin", run, (2.0, 4.0, 6.0, 8.0))],
        )
    )
    assert not result.passed
    assert result.evidence["correlations"]["twin"] == pytest.approx(1.0)


def test_book_correlation_passes_a_diversifying_book() -> None:
    run = build_run((1.0, 2.0, 3.0, 4.0))
    assert book_correlation.evaluate(
        context(run, params={"max_corr": 0.5}, deployed=[book("mirror", run, (4.0, 3.0, 2.0, 1.0))])
    ).passed


def test_book_correlation_with_nothing_deployed_judges_nothing() -> None:
    result = book_correlation.evaluate(context(build_run((1.0, 2.0)), params={"max_corr": 0.5}))
    assert result.passed and result.skipped is not None


def test_book_correlation_with_no_overlap_judges_nothing() -> None:
    run = build_run((1.0, 2.0, 3.0, 4.0))
    elsewhere = build_run((1.0, 2.0, 3.0, 4.0), start=date(2025, 6, 1))
    result = book_correlation.evaluate(
        context(
            run, params={"max_corr": 0.5}, deployed=[book("later", elsewhere, (1.0, 2.0, 3.0, 4.0))]
        )
    )
    assert result.passed and result.skipped is not None


def test_book_correlation_without_a_ceiling_judges_nothing() -> None:
    assert book_correlation.evaluate(context(build_run((1.0, 2.0)))).skipped is not None


# --- publication_lag --------------------------------------------------------------


def dataset(publication: str, observed: float = 0.0, required: float = 0.0) -> DatasetFacts:
    return DatasetFacts(
        dataset_id=f"demo_{publication}",
        type="bar",
        publication=publication,
        min_lag_s=observed,
        required_lag_s=required,
    )


def test_publication_lag_passes_a_snapshot_published_late_enough() -> None:
    result = publication_lag.evaluate(
        context(
            build_run((1.0,)),
            params={"tolerance_s": 0.0},
            datasets=[dataset("realtime"), dataset("delayed", observed=900.0, required=900.0)],
        )
    )
    assert result.passed
    assert result.evidence["n_datasets"] == 2


def test_publication_lag_fails_a_dataset_published_too_early() -> None:
    result = publication_lag.evaluate(
        context(
            build_run((1.0,)),
            params={"tolerance_s": 10.0},
            datasets=[dataset("delayed", observed=100.0, required=900.0)],
        )
    )
    assert not result.passed
    assert result.evidence["published_too_early"] == ["demo_delayed"]


def test_publication_lag_fails_a_dataset_of_unknown_publication() -> None:
    result = publication_lag.evaluate(
        context(build_run((1.0,)), params={"tolerance_s": 0.0}, datasets=[dataset("unknown")])
    )
    assert not result.passed
    assert result.evidence["unknown"] == ["demo_unknown"]


@pytest.mark.parametrize("overrides", [{}, {"params": {"tolerance_s": 0.0}}])
def test_publication_lag_without_its_context_judges_nothing(overrides: Any) -> None:
    result = publication_lag.evaluate(context(build_run((1.0,)), **overrides))
    assert result.passed and result.skipped is not None


# --- capacity_vs_adv --------------------------------------------------------------


def test_capacity_vs_adv_compares_the_busiest_day_to_average_volume() -> None:
    run = build_run(
        FLAT,
        fills=(fill(DAYS[0], qty=10.0), fill(DAYS[0], qty=10.0), fill(DAYS[1], qty=5.0)),
    )
    result = capacity_vs_adv.evaluate(
        context(
            run, params={"participation": 0.1, "adv_days": 5}, daily_volume={"DEMO": [40_000.0]}
        )
    )
    assert result.passed
    assert result.evidence["instruments"]["DEMO"]["peak_notional"] == pytest.approx(2_000.0)


def test_capacity_vs_adv_fails_a_day_that_would_move_the_market() -> None:
    run = build_run(FLAT, fills=(fill(DAYS[0], qty=1_000.0),))
    assert not capacity_vs_adv.evaluate(
        context(
            run, params={"participation": 0.1, "adv_days": 5}, daily_volume={"DEMO": [40_000.0]}
        )
    ).passed


def test_capacity_vs_adv_averages_only_the_days_it_was_asked_for() -> None:
    run = build_run(FLAT, fills=(fill(DAYS[0], qty=10.0),))
    result = capacity_vs_adv.evaluate(
        context(
            run,
            params={"participation": 1.0, "adv_days": 2},
            daily_volume={"DEMO": [10.0, 1_000.0, 3_000.0]},
        )
    )
    assert result.evidence["instruments"]["DEMO"]["adv"] == pytest.approx(2_000.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"params": {"participation": 0.1}},
        {"params": {"participation": 0.1, "adv_days": 5}},
        {"params": {"participation": 0.1, "adv_days": 5}, "daily_volume": {"OTHER": [1.0]}},
    ],
)
def test_capacity_vs_adv_without_its_context_judges_nothing(overrides: Any) -> None:
    run = build_run(FLAT, fills=(fill(DAYS[0], qty=10.0),))
    result = capacity_vs_adv.evaluate(context(run, **overrides))
    assert result.passed and result.skipped is not None


# --- the toolbox as a whole -------------------------------------------------------


def test_every_implemented_gate_answers_a_bare_context() -> None:
    ctx = context(trading_run(1.0, 1.0, 1.0, 1.0))
    for name, gate in gates().items():
        result = gate.evaluate(ctx)
        assert result.id == name
        assert result.passed or result.skipped is None


# --- position_size under a sizing rule: entry fills over the budget ----------------


def _fill(day: date, side: str, qty: float, px: float, instrument_id: str = "DEMO") -> Fill:
    return Fill(ts_ns=at(day), instrument_id=instrument_id, side=side, qty=qty, px=px, cost=0.0)


def filled(
    *fills: Fill, budget: float = 10_000.0, host: tuple[Fill, ...] | None = None, **overrides: Any
) -> Any:
    hyp = make_hyp(sizing={"mode": "full_book", "budget": budget})
    run = build_run((0.0,) * 3, fills=fills)
    if host is not None:
        overrides["host_run"] = build_run((0.0,) * 3, fills=host)
    return context(run, hyp=hyp, **overrides)


def test_position_size_judges_entry_fills_against_the_budget_under_sizing() -> None:
    result = position_size.evaluate(
        filled(
            _fill(START, "BUY", 99.0, 100.5),
            _fill(START + timedelta(days=1), "SELL", 99.0, 101.0),
            _fill(START + timedelta(days=2), "BUY", 98.0, 101.5),
            params={"min_pct": 98.0, "max_pct": 100.0},
        )
    )

    assert result.passed, result.evidence
    assert result.evidence["basis"] == "entry_fills"
    assert result.evidence["n_entries"] == 2
    assert result.evidence["budget"] == 10_000.0


def test_position_size_gathers_a_walked_order_before_judging_it() -> None:
    """The venue fills a large market order as two events; together they are one entry."""
    result = position_size.evaluate(
        filled(
            _fill(START, "BUY", 75.0, 100.0),
            _fill(START, "BUY", 24.0, 100.01),
            params={"min_pct": 98.0, "max_pct": 100.0},
        )
    )

    assert result.passed, result.evidence
    assert result.evidence["n_entries"] == 1


def test_position_size_refuses_a_half_sized_entry_under_sizing() -> None:
    result = position_size.evaluate(
        filled(_fill(START, "BUY", 50.0, 100.0), params={"min_pct": 98.0, "max_pct": 100.0})
    )

    assert not result.passed
    assert result.evidence["smallest_pct"] == 50.0


def test_position_size_subtracts_the_host_s_fills_for_an_attached_construct() -> None:
    host = (_fill(START, "BUY", 99.0, 100.0), _fill(START + timedelta(days=2), "SELL", 99.0, 100.0))
    own = (_fill(START + timedelta(days=1), "BUY", 49.0, 100.0, "HEDGE"),)
    result = position_size.evaluate(
        filled(*host, *own, budget=5_000.0, host=host, params={"min_pct": 98.0, "max_pct": 100.0})
    )

    assert result.passed, result.evidence
    assert result.evidence["n_entries"] == 1


def test_position_size_with_no_entry_filled_judges_nothing_under_sizing() -> None:
    assert position_size.evaluate(filled(params={"min_pct": 98.0})).skipped is not None
