"""A card's digest: what its trades did, read off the run it was scored on, by eye.

The run below is four daily sessions of one trade each, with round notionals, so every
number the digest states can be checked with a pencil: the edges are 10, -20, 15 and 0 bp,
the costs 2, 2, 2 and 1 bp, and the book made 100 on a capital of 100,000.
"""

from __future__ import annotations

from datetime import timedelta
from statistics import mean, stdev

import pytest

from kanso.criteria.digest import digest, edge_per_trade, session_edge
from kanso.criteria.objectives import net_edge_bps
from kanso.criteria.quantities import edge_bps, edge_series
from kanso.criteria.run import Fill
from kanso.schemas import Commands
from tests.criteria.builders import START, at, build_run, trade

TRADES = (
    trade(START, 10.0, cost=2.0),
    trade(START + timedelta(days=1), -20.0, cost=2.0),
    trade(START + timedelta(days=2), 30.0, notional=20_000.0, cost=4.0),
    trade(START + timedelta(days=3), 0.0, cost=1.0),
)
FILLS = (
    Fill(at(START), "DEMO", "BUY", 100.0, 100.0, cost=1.0, maker=True, limit=100.0),
    Fill(at(START, 13), "DEMO", "SELL", 300.0, 100.0, cost=3.0),
)
INTENTS = (
    (at(START), "DEMO", "BUY", 100.0, "LIMIT", 100.0),
    (at(START), "DEMO", "BUY", 100.0, "LIMIT", 99.0),
    (at(START, 13), "DEMO", "SELL", 300.0, "MARKET", None),
)
RUN = build_run((100.0, -50.0, 30.0, 20.0), trades=TRADES, fills=FILLS)


def test_every_number_of_the_digest_is_the_one_a_pencil_gets() -> None:
    made = digest(RUN, INTENTS)

    assert (made.sessions, made.fills) == (4, 2)
    assert made.trades_per_session == 1.0
    assert made.hit_rate == 0.5, "two of the four trades made money; one made nothing"
    assert made.median_hold_s == 6 * 3600.0, "every trade is held from 09:00 to 15:00"
    assert made.maker_share == 0.25, "10,000 of 40,000 traded rested on the book"
    assert made.fill_ratio == 0.5, "100 of the 200 sent on limit orders filled"
    assert made.net_bps_per_trade == pytest.approx(mean([10.0, -20.0, 15.0, 0.0]))
    assert made.cost_bps_per_trade == pytest.approx(mean([2.0, 2.0, 2.0, 1.0]))
    assert made.gross_bps_per_trade == pytest.approx(1.25 + 1.75)
    assert made.net_bps_per_session == pytest.approx(100.0 / 100_000 * 10_000 / 4)
    assert made.cost_bps_per_session == pytest.approx(4.0 / 100_000 * 10_000 / 4)
    assert made.gross_bps_per_session == pytest.approx(2.5 + 0.1)


def test_the_net_edge_per_trade_and_its_error_are_the_per_trade_objective_over_one_fold() -> None:
    """One arithmetic: what the digest shows is what `net_edge_bps` reads and what the
    `edge_per_trade` gate judges, the error read period by period."""
    made = digest(RUN, INTENTS)

    assert (made.net_bps_per_trade, made.net_bps_per_trade_se) == net_edge_bps.compute(RUN, 1)
    assert (made.net_bps_per_trade, made.net_bps_per_trade_se, 4) == edge_per_trade(RUN)
    assert made.net_bps_per_trade == edge_bps(RUN)
    assert made.net_bps_per_trade_se is not None and made.net_bps_per_trade_se > 0


def test_trades_one_session_moves_together_are_one_session_s_evidence() -> None:
    """Five clips a session that share their session's luck: the error the digest states is
    the period-linearised one, wider than the error of twenty independent draws."""
    edges = (12.0, -8.0, 9.0, -5.0)
    clips = tuple(
        trade(START + timedelta(days=day), edge + wobble)
        for day, edge in enumerate(edges)
        for wobble in (-0.2, -0.1, 0.0, 0.1, 0.2)
    )
    run = build_run((1.0, 1.0, 1.0, 1.0), trades=clips)

    made = digest(run)

    naive = stdev(edge_series(run)) / len(clips) ** 0.5
    assert made.net_bps_per_trade_se is not None and made.net_bps_per_trade_se > naive
    assert made.trades_per_session == 5.0


def test_a_run_that_traded_nothing_has_sessions_and_no_edge() -> None:
    made = digest(build_run((0.0, 0.0, 0.0)))

    assert (made.sessions, made.fills, made.trades_per_session) == (3, 0, 0.0)
    assert (made.net_bps_per_session, made.cost_bps_per_session) == (0.0, 0.0)
    assert made.net_bps_per_trade is None and made.hit_rate is None
    assert made.maker_share is None, "no fill, so no share of them was a maker's"
    assert made.fill_ratio is None, "no order carried a limit"


def test_a_run_with_no_period_end_has_no_session_to_divide_by() -> None:
    made = digest(build_run((), days=1))

    assert made.sessions == 0
    assert made.trades_per_session is None and made.net_bps_per_session is None


def test_the_digest_carries_the_commands_the_run_counted_as_they_were_sent() -> None:
    """How fast the strategy sent its orders is the run's own count, handed on whole; a run
    handed none — a card recorded before cards kept it — carries none."""
    sent = Commands(submits=4, modifies=1, cancels=3, peak_60s=7, p99_60s=6)

    assert digest(RUN, (), sent).commands == sent
    assert digest(RUN).commands is None


def test_a_strategy_that_sent_only_market_orders_has_no_fill_ratio() -> None:
    made = digest(RUN, ((at(START), "DEMO", "BUY", 100.0, "MARKET", None),))

    assert made.fill_ratio is None


def test_trades_that_all_closed_in_one_period_state_no_error() -> None:
    """An error read period by period is zero when one period holds every trade, whatever
    the trades did, so the digest states none rather than an edge known exactly."""
    clips = tuple(trade(START, edge) for edge in (200.0, -150.0, 80.0, -120.0, 5.0))
    run = build_run((1.0,) * 20, trades=clips)

    made = digest(run)

    assert net_edge_bps.compute(run, 1)[1] == 0.0, "the objective's own error reads zero"
    assert made.net_bps_per_trade == pytest.approx(3.0)
    assert made.net_bps_per_trade_se is None
    assert edge_per_trade(run)[1:] == (None, 1)


def test_a_trade_closed_after_the_last_period_end_counts_in_the_last_period() -> None:
    """As the per-trade error reads it: the first period ending at or after its close, else
    the last — so a trade closing after every period end shares the last period."""
    late = trade(START + timedelta(days=1), 4.0)
    run = build_run((1.0, 1.0), trades=(trade(START, 2.0), late))
    shifted = build_run((1.0,), trades=(trade(START, 2.0), late), days=2)

    assert edge_per_trade(run)[2] == 2
    assert edge_per_trade(shifted)[2] == 1


def test_the_session_edge_is_the_per_trade_edge_with_its_error_read_session_by_session() -> None:
    """Daily periods of one trade each: a session is a period, so the session-clustered
    error is the period-linearised one the digest states, and the mean is the same."""
    value, se, sessions, trades = session_edge(RUN) or (None, None, 0, 0)

    edge, period_se, periods = edge_per_trade(RUN)
    assert value == edge, "one mean, struck one way"
    assert se == pytest.approx(period_se)
    assert (sessions, trades) == (periods, 4)


def test_a_session_s_clips_are_one_session_whatever_the_period() -> None:
    """Twenty clips over four sessions at an hourly period: the period-by-period error
    reads the clips of one session as several periods, the session's reads them as one."""
    edges = (12.0, -8.0, 9.0, -5.0)
    clips = tuple(
        trade(START + timedelta(days=day), edge + wobble)
        for day, edge in enumerate(edges)
        for wobble in (-0.2, -0.1, 0.0, 0.1, 0.2)
    )
    run = build_run((1.0, 1.0, 1.0, 1.0), trades=clips)

    found = session_edge(run)

    assert found is not None
    value, se, sessions, trades = found
    assert (sessions, trades) == (4, 20)
    assert value == pytest.approx(mean(edges))
    assert se is not None and se > stdev(edge_series(run)) / len(clips) ** 0.5


def test_trades_that_all_closed_on_one_session_have_no_session_error() -> None:
    clips = tuple(trade(START, edge) for edge in (200.0, -150.0, 80.0))

    assert session_edge(build_run((1.0,) * 5, trades=clips)) == (
        pytest.approx(130.0 / 3),
        None,
        1,
        3,
    )


def test_a_run_that_closed_no_trade_has_no_session_edge() -> None:
    assert session_edge(build_run((0.0, 0.0))) is None
