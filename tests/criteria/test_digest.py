"""A card's digest: what its trades did, read off the run it was scored on, by eye.

The run below is four daily sessions of one trade each, with round notionals, so every
number the digest states can be checked with a pencil: the edges are 10, -20, 15 and 0 bp,
the costs 2, 2, 2 and 1 bp, and the book made 100 on a capital of 100,000.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from statistics import mean, stdev

import pytest

from kanso.criteria.digest import digest, edge_per_trade, session_edge
from kanso.criteria.objectives import net_edge_bps
from kanso.criteria.quantities import edge_bps, edge_series
from kanso.criteria.run import Fill
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
    assert made.filled_periods == 1, "both fills are on the first day"
    assert (made.first_fill_s, made.last_fill_s) == (12 * 3600.0, 13 * 3600.0)
    assert made.active_s == 3600.0, "from the noon fill to the one an hour after it"


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
    assert made.filled_periods == 0, "no period was filled in, so none is timed"
    assert (made.first_fill_s, made.last_fill_s, made.active_s) == (None, None, None)


def test_a_run_with_no_period_end_has_no_session_to_divide_by() -> None:
    made = digest(build_run((), days=1))

    assert made.sessions == 0
    assert made.trades_per_session is None and made.net_bps_per_session is None


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


def instant(day: int, seconds: float) -> int:
    """`seconds` after the midnight that opens day `day` of the window, in nanoseconds."""
    return at(START + timedelta(days=day), 0) + round(seconds * 1e9)


def fills_at(*instants: int) -> tuple[Fill, ...]:
    """A fill of 100 at 100.00 at each instant, in time order."""
    return tuple(Fill(ts, "DEMO", "BUY", 100.0, 100.0, cost=0.0) for ts in sorted(instants))


def test_when_a_run_filled_is_read_from_the_opening_of_each_period_it_filled_in() -> None:
    """Three days, each filled in its first seconds and never again: the first fills come
    1, 1.5 and 0.5 s after the day opens and the last 3, 4 and 6 s after it, so the spans
    are 2, 2.5 and 5.5 s; the digest states the middle of each, the periods it read them
    off, and nothing of a period with no fill."""
    run = build_run(
        (0.0, 0.0, 0.0, 0.0),
        fills=fills_at(
            *(instant(0, s) for s in (1.0, 2.0, 3.0)),
            *(instant(1, s) for s in (1.5, 4.0)),
            *(instant(3, s) for s in (0.5, 6.0)),
        ),
    )

    made = digest(run)

    assert (made.sessions, made.filled_periods) == (4, 3)
    assert (made.first_fill_s, made.last_fill_s, made.active_s) == (1.0, 4.0, 2.5)


def test_the_middle_of_an_even_count_of_periods_is_an_offset_one_of_them_filled_at() -> None:
    """Two winter sessions open at 14:30Z and two summer ones at 13:30Z, each filled once,
    20 ms in: the mean of the two middle offsets is 14:00:00.02, an instant no session
    filled at, so the digest states the lower of the two."""
    opens = (14.5, 14.5, 13.5, 13.5)
    run = build_run(
        (0.0,) * 4,
        fills=fills_at(*(instant(day, hours * 3600 + 0.02) for day, hours in enumerate(opens))),
    )

    made = digest(run)

    assert made.filled_periods == 4
    assert made.first_fill_s == made.last_fill_s == 48_600.02, "13:30:00.02"
    assert made.active_s == 0.0, "one fill a period spans no time"


def test_a_period_opens_where_the_runner_cut_the_window_and_not_at_every_midnight() -> None:
    """Return periods are cut from the window's first midnight, one period after another: at
    two days the second midnight opens nothing, so a fill an hour into the second day is 25
    hours into the period the first midnight opened, and at an hour each hour opens its own."""
    daily = build_run((0.0, 0.0), fills=fills_at(instant(0, 23 * 3600.0), instant(1, 3600.0)))

    two_days = digest(replace(daily, period="2d"))
    hourly = digest(replace(daily, period="1h"))

    assert digest(daily).filled_periods == 2
    assert two_days.filled_periods == 1
    assert (two_days.first_fill_s, two_days.last_fill_s) == (23 * 3600.0, 25 * 3600.0)
    assert two_days.active_s == 2 * 3600.0
    assert hourly.filled_periods == 2
    assert (hourly.first_fill_s, hourly.last_fill_s, hourly.active_s) == (0.0, 0.0, 0.0)
