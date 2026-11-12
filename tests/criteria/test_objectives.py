"""The five objectives, the fold arithmetic they aggregate with, and their applicability."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import product
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria import applicable_objectives, catalogue, objectives
from kanso.criteria.objectives import (
    ABSOLUTE,
    BENCHMARK,
    REGISTRY,
    RELATIVE,
    SHARPE_FAMILY,
    STATISTICS,
    applies_to,
    benchmark_level,
    difference,
    history_days,
    marginal_net_edge_bps,
    marginal_wf_contribution_bps,
    marginal_wf_sharpe,
    measures_benchmark,
    net_edge_bps,
    resolution_seconds,
    wf_contribution_bps,
    wf_sharpe_net,
    wf_sharpe_vs_hold,
)
from kanso.criteria.quantities import (
    contribution_series,
    long_run_factor,
    moments,
    sharpe_variance,
    variance,
)
from kanso.criteria.run import midnight_ns
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


def test_the_metric_is_the_mean_of_the_folds_and_the_error_that_of_the_per_trade_mean() -> None:
    """Four trades at 100, 200, 300 and 400 bp, one a fold: the variance of a trade is
    50,000/3, and a series that climbs is persistent, so the allowance widens it by 1.25 —
    one plus the Bartlett-weighted first autocovariance, 3,125 over 12,500 at lag one."""
    metric, error = net_edge_bps.compute(edges(100.0, 200.0, 300.0, 400.0), 4)
    assert metric == pytest.approx(250.0)
    assert error == pytest.approx((1.25 * 50_000 / 3) ** 0.5 / 2)


def test_the_error_reads_the_sessions_where_the_spread_of_folds_read_nothing() -> None:
    """Every fold closes a 100 one session and a 300 the next: four fold means of 200 agree
    exactly, and the spread of the folds called that a zero error. Eight sessions with a
    standard deviation of 106.9 say the mean is known to 106.9/sqrt(8); they alternate, so
    nothing is widened."""
    days = [date(2024, 1, 1 + i) for i in range(8)]
    run = build_run(
        (0.0,) * 8,
        trades=tuple(
            trade(day, pnl=pnl) for day, pnl in zip(days, (100.0, 300.0) * 4, strict=True)
        ),
    )
    metric, error = net_edge_bps.compute(run, 4)
    assert metric == pytest.approx(200.0)
    assert net_edge_bps.fold_values(run, 4) == pytest.approx((200.0,) * 4)
    assert error == pytest.approx((80_000 / 7) ** 0.5 / 8**0.5)


def test_trades_one_session_moves_together_are_priced_as_one_session() -> None:
    """Ten clips a session at one edge are one draw of that session, not ten: the edge and
    its error are those of the same sessions closing one trade each. Read trade by trade the
    error would be a third as wide, and every floor it sets would be too."""
    days = [date(2024, 1, 1 + i) for i in range(8)]
    session = (40.0, -10.0, 25.0, 5.0, 60.0, -30.0, 15.0, 20.0)
    once = build_run(
        (0.0,) * 8,
        trades=tuple(trade(day, pnl=pnl) for day, pnl in zip(days, session, strict=True)),
    )
    clipped = build_run(
        (0.0,) * 8,
        trades=tuple(
            trade(day, pnl=pnl) for day, pnl in zip(days, session, strict=True) for _ in range(10)
        ),
    )
    assert net_edge_bps.compute(clipped, 4) == pytest.approx(net_edge_bps.compute(once, 4))
    assert net_edge_bps.compute(once, 4)[1] > 0.0


def test_a_fold_that_holds_no_period_end_holds_its_trades_in_one() -> None:
    """Two days in four folds of half a day, periods struck at 06:00 and trades closed at
    15:00: the afternoon folds hold a trade each and no period end, and each is one period.
    The morning folds closed nothing and score zero. Two periods at 10 and 30 bp about the
    window's edge of 20 vary by 200, over one period a fold."""
    days = [date(2024, 1, 1), date(2024, 1, 2)]
    run = build_run(
        (0.0, 0.0),
        ends=tuple(midnight_ns(day) + 6 * 3_600 * 10**9 for day in days),
        trades=tuple(trade(day, pnl=pnl) for day, pnl in zip(days, (10.0, 30.0), strict=True)),
    )
    assert [len(fold.period_ends_ns) for fold in run.folds(4)] == [1, 0, 1, 0]
    assert [len(fold.trades) for fold in run.folds(4)] == [0, 1, 0, 1]
    metric, error = net_edge_bps.compute(run, 4)
    assert metric == pytest.approx((0.0 + 10.0 + 0.0 + 30.0) / 4)
    assert error == pytest.approx((200.0 + 200.0) ** 0.5 / 4)


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
    assert error == pytest.approx(17_500**0.5 / 2)


def test_a_relative_objective_refuses_to_measure_without_a_host() -> None:
    with pytest.raises(PreconditionError, match="host's run"):
        marginal_wf_sharpe.compute(edges(1.0, 2.0, 3.0, 4.0), 4)


def test_a_benchmark_objective_differences_the_folds_against_the_benchmark() -> None:
    """The pairing is the relative one's, against the hold rather than a host."""
    candidate = build_run((1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0))
    hold = build_run((1.0, 2.0, 1.0, 2.0, 3.0, 1.0, 3.0, 1.0))
    own = wf_sharpe_net.fold_values(candidate, 4)
    against = wf_sharpe_net.fold_values(hold, 4)
    values = wf_sharpe_vs_hold.fold_values(candidate, 4, benchmark=hold)
    assert values == pytest.approx(tuple(a - b for a, b in zip(own, against, strict=True)))
    metric, _ = wf_sharpe_vs_hold.compute(candidate, 4, benchmark=hold)
    assert metric == pytest.approx(sum(values) / 4)


def test_a_benchmark_objective_never_reads_the_host_slot() -> None:
    """A host run beside a benchmark objective is a construct's, not what it has to beat."""
    run = edges(1.0, 2.0, 3.0, 4.0)
    with pytest.raises(PreconditionError, match="benchmark's run"):
        wf_sharpe_vs_hold.compute(run, 4, host=run)


def test_a_relative_objective_never_reads_the_benchmark_slot() -> None:
    run = edges(1.0, 2.0, 3.0, 4.0)
    with pytest.raises(PreconditionError, match="host's run"):
        marginal_wf_sharpe.compute(run, 4, benchmark=run)


def test_the_benchmark_level_is_what_the_hold_scored_and_zero_without_one() -> None:
    hold = build_run((1.0, 2.0, 1.0, 2.0, 3.0, 1.0, 3.0, 1.0))
    assert benchmark_level(wf_sharpe_vs_hold, 4, hold) == pytest.approx(
        wf_sharpe_net.compute(hold, 4)[0]
    )
    assert benchmark_level(wf_sharpe_net, 4, hold) == 0.0
    with pytest.raises(PreconditionError, match="benchmark's run"):
        benchmark_level(wf_sharpe_vs_hold, 4, None)


def test_the_benchmark_objective_is_a_sharpe() -> None:
    assert wf_sharpe_vs_hold.mode == BENCHMARK
    assert "wf_sharpe_vs_hold" in SHARPE_FAMILY


def test_only_a_benchmark_objective_asks_for_a_benchmark_run() -> None:
    params = {"min_delta": 0.0, "k_se": 1.0}
    held = make_hyp(
        benchmark={"hold": "first_leg"}, objective={"id": "wf_sharpe_vs_hold", "params": params}
    )
    assert measures_benchmark(held)
    assert not measures_benchmark(make_hyp())
    assert not measures_benchmark(make_hyp(objective=None, construct=None, constraints=None))


def test_the_sharpe_objectives_read_the_return_series() -> None:
    run = build_run((1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0))
    metric, _ = wf_sharpe_net.compute(run, 4)
    assert metric > 0
    flat = build_run((2.0, 2.0, 2.0, 2.0))
    assert wf_sharpe_net.compute(flat, 4) == (0.0, 0.0)


def test_the_shipped_objectives_declare_their_mode() -> None:
    assert (wf_sharpe_net.mode, net_edge_bps.mode) == (ABSOLUTE, ABSOLUTE)
    assert (marginal_wf_sharpe.mode, marginal_net_edge_bps.mode) == (RELATIVE, RELATIVE)
    assert (wf_contribution_bps.mode, marginal_wf_contribution_bps.mode) == (ABSOLUTE, RELATIVE)


NAMED = {ABSOLUTE: "wf_contribution_bps", RELATIVE: "marginal_wf_contribution_bps"}
"""The objectives chosen by name: applicable everywhere, and never the default."""


def test_the_contribution_is_the_return_per_period_on_the_capital_fold_by_fold() -> None:
    """$100k of capital earning $10, $20, $30 and $40 on four days: 1, 2, 3 and 4 bp."""
    run = build_run((10.0, 20.0, 30.0, 40.0), capital=100_000.0)
    assert wf_contribution_bps.fold_values(run, 4) == pytest.approx((1.0, 2.0, 3.0, 4.0))
    metric, error = wf_contribution_bps.compute(run, 4)
    assert (metric, error) == pytest.approx((2.5, (1.25 * 5 / 3) ** 0.5 / 2))


def test_a_period_in_cash_contributes_zero_and_pulls_the_mean_down() -> None:
    """The same $100 over four days, made on two of them or spread over all: the per-trade
    edge is indifferent to it; the contribution is what the capital earned either way."""
    idle = build_run((50.0, 0.0, 50.0, 0.0), capital=100_000.0)
    busy = build_run((25.0, 25.0, 25.0, 25.0), capital=100_000.0)
    assert wf_contribution_bps.compute(idle, 4)[0] == pytest.approx(
        wf_contribution_bps.compute(busy, 4)[0]
    )
    assert wf_contribution_bps.compute(idle, 4)[1] > wf_contribution_bps.compute(busy, 4)[1]


WEEK = (40.0, 80.0, 120.0, 160.0, 200.0)
"""Monday to Friday on $40k of capital: 10, 20, 30, 40 and 50 bp, 30 bp a session."""


def test_a_short_window_weighs_each_fold_alike_and_not_each_period() -> None:
    """Five sessions in four calendar folds of a day and a quarter: a period ends at its
    session's last instant, so Thursday and Friday share the last fold and each weighs half
    what Monday does. One fold is the whole window, and that is the mean per period."""
    run = build_run(WEEK, capital=40_000.0)
    assert wf_contribution_bps.fold_values(run, 4) == pytest.approx((10.0, 20.0, 30.0, 45.0))
    assert wf_contribution_bps.compute(run, 4)[0] == pytest.approx(26.25)
    assert wf_contribution_bps.compute(run, 1)[0] == pytest.approx(30.0)


def test_a_fold_that_holds_no_period_scores_zero_and_is_averaged_in() -> None:
    """Monday to Sunday in four folds of a day and three quarters: the last runs from
    Saturday morning, holds no session, and takes a quarter of the metric with it."""
    run = build_run(WEEK, capital=40_000.0, days=7)
    assert wf_contribution_bps.fold_values(run, 4) == pytest.approx((10.0, 25.0, 45.0, 0.0))
    assert wf_contribution_bps.compute(run, 4)[0] == pytest.approx(20.0)
    empty = run.folds(4)[-1]
    assert empty.returns == ()
    assert {name: measure(empty) for name, measure in STATISTICS.items()} == dict.fromkeys(
        STATISTICS, 0.0
    )


def test_the_marginal_contribution_differences_the_folds_against_the_host() -> None:
    combined = build_run((30.0, 40.0, 50.0, 60.0), capital=100_000.0)
    host = build_run((10.0, 20.0, 30.0, 40.0), capital=100_000.0)
    assert marginal_wf_contribution_bps.fold_values(combined, 4, host=host) == pytest.approx(
        (2.0, 2.0, 2.0, 2.0)
    )
    with pytest.raises(PreconditionError):
        marginal_wf_contribution_bps.fold_values(combined, 4)


def test_a_named_objective_applies_at_every_horizon_below_every_default() -> None:
    for mode, horizon in product((ABSOLUTE, RELATIVE), ("30m", "23h", "1d", "5d")):
        found = applicable_objectives(make_hyp(horizon=horizon), mode)
        assert found[-1] == (catalogue()[NAMED[mode]].priority, NAMED[mode]), (mode, horizon, found)
        assert found[0][0] < found[-1][0], (mode, horizon, found)


def test_every_catalogued_objective_resolves_to_its_implementation() -> None:
    found = objectives()
    assert set(found) == {
        "wf_sharpe_net",
        "net_edge_bps",
        "marginal_wf_sharpe",
        "marginal_net_edge_bps",
        "wf_sharpe_vs_hold",
        "wf_contribution_bps",
        "marginal_wf_contribution_bps",
    }
    assert all(objective.id == name for name, objective in found.items())


def test_the_objective_set_is_total_over_mechanism_mode_horizon_and_benchmark() -> None:
    for mechanism, mode, horizon, benchmark in product(
        MECHANISMS, (ABSOLUTE, RELATIVE), ("12h", "1d"), (None, {"hold": "first_leg"})
    ):
        hyp = make_hyp(mechanism=mechanism, horizon=horizon, benchmark=benchmark)
        case = f"{mechanism} / {mode} / {horizon} / {benchmark}"
        found = applicable_objectives(hyp, mode)
        assert found, f"no objective applies to {case}"
        defaults = [name for priority, name in found if priority == found[0][0]]
        assert len(defaults) == 1, f"{case} is ambiguous: {found}"
        assert [name for _, name in found[1:]] == [NAMED[mode]], f"{case}: {found}"


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
    assert applicable_objectives(hyp, mode)[0] == (catalogue()[expected].priority, expected)


@pytest.mark.parametrize(
    ("mode", "horizon", "expected"),
    [
        (ABSOLUTE, "1d", "wf_sharpe_vs_hold"),
        (ABSOLUTE, "5d", "wf_sharpe_vs_hold"),
        (ABSOLUTE, "23h", "net_edge_bps"),
        (RELATIVE, "1d", "marginal_wf_sharpe"),
    ],
)
def test_a_declared_benchmark_displaces_only_the_absolute_sharpe(
    mode: str, horizon: str, expected: str
) -> None:
    hyp = make_hyp(horizon=horizon, benchmark={"hold": "first_leg"})
    assert applicable_objectives(hyp, mode)[0] == (catalogue()[expected].priority, expected)


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
        ({"benchmark": False}, True),
        ({"benchmark": True}, False),
    ],
)
def test_every_clause_of_the_predicate_is_evaluated(clauses: Any, holds: bool) -> None:
    assert applies_to(_predicate(**clauses), make_hyp(), ABSOLUTE) is holds


def test_a_benchmark_clause_reads_whether_the_hypothesis_declares_one() -> None:
    held = make_hyp(benchmark={"hold": "first_leg"})
    assert applies_to(_predicate(benchmark=True), held, ABSOLUTE)
    assert not applies_to(_predicate(benchmark=False), held, ABSOLUTE)
    assert applies_to(_predicate(), held, ABSOLUTE)


def test_an_empty_predicate_applies_to_everything() -> None:
    assert applies_to(_predicate(), make_hyp(), RELATIVE)


def test_an_unaggregated_resolution_is_the_finest_grain_there_is() -> None:
    assert resolution_seconds("1m") == 60.0
    assert resolution_seconds("tick") == 0.0


def test_the_history_length_counts_both_end_days() -> None:
    hyp: Hypothesis = make_hyp()
    assert history_days(hyp) == 366


# -- the error each objective reports ---------------------------------------------------


def shifted(run: Any, hours: int) -> Any:
    """The same run with every period struck `hours` earlier: the same returns, not the same
    instants, so it pairs with nothing struck at midnight."""
    return build_run(
        run.returns,
        trades=run.trades,
        capital=run.capital,
        ends=tuple(end - hours * 3_600 * 10**9 for end in run.period_ends_ns),
    )


def test_a_fold_that_holds_no_period_adds_nothing_to_the_error() -> None:
    """The week of seven days in four folds: the last holds nothing and scores the constant
    zero. The other three hold one, two and two sessions of a five-session series."""
    run = build_run(WEEK, capital=40_000.0, days=7)
    series = contribution_series(run)
    spread = variance(series) * long_run_factor(series)
    _, error = wf_contribution_bps.compute(run, 4)
    assert error == pytest.approx((spread / 1 + spread / 2 + spread / 2) ** 0.5 / 4)


def test_a_marginal_contribution_that_adds_a_constant_is_known_exactly() -> None:
    """Paired by period, a modifier that adds 2 bp to every session of a host whose sessions
    swing widely has a difference that does not vary at all."""
    host = build_run((10.0, -50.0, 80.0, -30.0), capital=100_000.0)
    combined = build_run((30.0, -30.0, 100.0, -10.0), capital=100_000.0)
    metric, error = marginal_wf_contribution_bps.compute(combined, 4, host=host)
    assert (metric, error) == pytest.approx((2.0, 0.0))


def test_a_marginal_contribution_whose_periods_do_not_pair_sums_both_errors() -> None:
    host = build_run((10.0, -50.0, 80.0, -30.0), capital=100_000.0)
    combined = build_run((30.0, -30.0, 100.0, -10.0), capital=100_000.0)
    _, own = wf_contribution_bps.compute(combined, 4)
    _, against = wf_contribution_bps.compute(host, 4)
    _, error = marginal_wf_contribution_bps.compute(combined, 4, host=shifted(host, 1))
    assert error == pytest.approx((own**2 + against**2) ** 0.5)


def test_a_marginal_edge_that_adds_a_constant_to_every_trade_is_known_exactly() -> None:
    """Paired by the period each trade closed in, a modifier that improves every trade of a
    host by 10 bp moves each period's influence on the edge not at all."""
    days = [date(2024, 1, 1 + i) for i in range(4)]
    host = build_run(
        FOUR_DAYS,
        trades=tuple(trade(d, pnl=p) for d, p in zip(days, (100, 300, -200, 500), strict=True)),
    )
    combined = build_run(
        FOUR_DAYS,
        trades=tuple(
            trade(d, pnl=p + 10) for d, p in zip(days, (100, 300, -200, 500), strict=True)
        ),
    )
    metric, error = marginal_net_edge_bps.compute(combined, 4, host=host)
    assert metric == pytest.approx(10.0)
    assert error == pytest.approx(0.0)
    _, apart = marginal_net_edge_bps.compute(combined, 4, host=shifted(host, 1))
    _, own = net_edge_bps.compute(combined, 4)
    _, against = net_edge_bps.compute(host, 4)
    assert apart == pytest.approx((own**2 + against**2) ** 0.5)


def test_a_marginal_edge_reads_only_the_folds_a_run_traded_in() -> None:
    """Monday to Sunday in four folds, periods struck at 16:00 on the five weekdays. The
    host trades Monday to Thursday and the combined run Monday and Tuesday, then Thursday
    at 20:00 — after Thursday's period, which is the last of its fold, so it counts there.
    The third fold, Wednesday afternoon to Friday, holds a trade of each; the last, from
    Saturday morning, holds no period at all and adds nothing."""
    days = [date(2024, 1, 1 + i) for i in range(7)]
    ends = tuple(midnight_ns(day) + 16 * 3_600 * 10**9 for day in days[:5])
    host = build_run(
        (0.0,) * 5,
        days=7,
        ends=ends,
        trades=tuple(trade(day, pnl=p) for day, p in zip(days, (10, 30, 20, 40), strict=False)),
    )
    late = trade(days[3], pnl=70.0)
    late = replace(late, closed_ns=midnight_ns(days[3]) + 20 * 3_600 * 10**9)
    combined = build_run(
        (0.0,) * 5,
        days=7,
        ends=ends,
        trades=(trade(days[0], pnl=12.0), trade(days[1], pnl=28.0), late),
    )
    assert [len(fold.period_ends_ns) for fold in combined.folds(4)] == [2, 1, 2, 0]
    _, error = marginal_net_edge_bps.compute(combined, 4, host=host)
    assert error > 0.0
    alone = build_run((0.0,) * 5, days=7, ends=ends, trades=host.trades[:1])
    assert marginal_net_edge_bps.compute(alone, 4, host=alone) == pytest.approx((0.0, 0.0))


def test_an_absolute_sharpe_varies_by_mertens_fold_by_fold() -> None:
    returns = (1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0)
    run = build_run(returns)
    shape = moments(returns)
    factor = long_run_factor(returns)
    expected = sum(
        sharpe_variance(fold.returns, fold.window, factor, shape) for fold in run.folds(4)
    )
    assert wf_sharpe_net.compute(run, 4)[1] == pytest.approx(expected**0.5 / 4)


def test_a_marginal_sharpe_of_a_modifier_that_changes_nothing_is_known_exactly() -> None:
    """Paired by Memmel, the same returns twice differ by nothing; struck at other instants
    they are two independent Sharpes, and the error is both of theirs."""
    run = build_run((1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0))
    assert marginal_wf_sharpe.compute(run, 4, host=run) == pytest.approx((0.0, 0.0))
    _, own = wf_sharpe_net.compute(run, 4)
    _, apart = marginal_wf_sharpe.compute(run, 4, host=shifted(run, 1))
    assert apart == pytest.approx((2 * own**2) ** 0.5)


def test_a_benchmark_sharpe_is_paired_against_the_hold_as_a_marginal_one_is_against_a_host() -> (
    None
):
    candidate = build_run((1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0))
    hold = build_run((1.0, 2.0, 1.0, 2.0, 3.0, 1.0, 3.0, 1.0))
    assert wf_sharpe_vs_hold.compute(candidate, 4, benchmark=hold)[1] == pytest.approx(
        marginal_wf_sharpe.compute(candidate, 4, host=hold)[1]
    )


def test_one_card_less_another_is_scored_as_a_marginal_improvement_on_it() -> None:
    """The keep rule's difference: every objective's, in every mode, is its statistic's
    relative objective with the other card in the host's place, its error paired period by
    period — for an absolute objective, the difference of the two metrics — and a card less
    itself is known exactly."""
    days = [date(2024, 1, 1 + i) for i in range(8)]
    one = build_run(
        (1.0, 3.0, 2.0, 4.0, 1.0, 5.0, 2.0, 6.0),
        trades=tuple(
            trade(day, pnl=p) for day, p in zip(days, (5, -3, 8, 1, -2, 9, 4, 7), strict=True)
        ),
    )
    two = build_run(
        (1.0, 2.0, 1.0, 2.0, 3.0, 1.0, 3.0, 1.0),
        trades=tuple(
            trade(day, pnl=p) for day, p in zip(days, (2, 6, -1, 3, 5, -4, 8, 2), strict=True)
        ),
    )
    assert {objective.statistic for objective in REGISTRY.values()} == set(STATISTICS)
    for objective in REGISTRY.values():
        (relative,) = (
            found
            for found in REGISTRY.values()
            if found.mode == RELATIVE and found.statistic == objective.statistic
        )
        assert difference(objective, one, two, 4) == relative.compute(one, 4, host=two)
        assert difference(objective, one, one, 4) == pytest.approx((0.0, 0.0), abs=1e-9)
        if objective.mode == ABSOLUTE:
            gap = objective.compute(one, 4)[0] - objective.compute(two, 4)[0]
            assert difference(objective, one, two, 4)[0] == pytest.approx(gap)


@given(st.lists(st.floats(-1_000.0, 1_000.0, allow_nan=False), min_size=1, max_size=40))
def test_one_fold_s_error_is_the_per_period_error_of_the_mean_widened(returns: list[float]) -> None:
    run = build_run(tuple(returns), capital=100_000.0)
    series = contribution_series(run)
    _, error = wf_contribution_bps.compute(run, 1)
    assert error >= 0.0
    assert error == pytest.approx(
        (variance(series) * long_run_factor(series) / len(series)) ** 0.5, abs=1e-12
    )
