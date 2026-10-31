"""When in its periods a card traded, read off the run a card's child struck from the points
its parent handed it.

A book that trades the first seconds of each session and nothing after them — a sleeve whose
signal fires at the first points a session hands it and whose day a stop ends a second later
— scores like any other, and its trades, holds and edges say nothing of when it traded. Its
digest says it in seconds from each period's first point, which the parent notes as it
streams the card its window (`RunResult.firsts_ns`): so a session that opens at 09:30 in New
York reads the same in winter, when that is 14:30Z, and in summer, when it is 13:30Z, and no
calendar is consulted to make it so.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from math import fsum
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.digest import digest
from kanso.criteria.run import NS_PER_SECOND, midnight_ns
from kanso.nautilus.backtest import run_subprocess
from kanso.schemas import Windows

from .conftest import catalog, hypothesis, instrument, second_bar_type

OPENER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Buys the second bar of each UTC day, sells the fourth, and trades nothing after it."""

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.day = None
        self.seen = 0

    def on_bar(self, bar) -> None:
        day = bar.ts_init // 86_400_000_000_000
        if day != self.day:
            self.day, self.seen = day, 0
        self.seen += 1
        if self.seen == 2:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)
        elif self.seen == 4:
            self.submit_exit(bar.bar_type.instrument_id)
'''

WINDOWS = Windows.model_validate(
    {
        "research": {"start": "2024-03-04", "end": "2024-03-15"},
        "certification": {"start": "2024-03-25", "end": "2024-04-05"},
        "forward": {"start": "2024-04-08"},
    }
)
"""A research window across New York's change of clocks on Sunday 2024-03-10."""

SESSIONS = (date(2024, 3, 7), date(2024, 3, 8), date(2024, 3, 11), date(2024, 3, 12))
"""Two sessions before the change and two after it."""

NEW_YORK = ZoneInfo("America/New_York")
HOUR_NS = 3_600 * NS_PER_SECOND


def opening(day: date) -> int:
    """09:30 in New York on `day`, in nanoseconds: 14:30Z before the change, 13:30Z after."""
    return int(datetime.combine(day, time(9, 30), NEW_YORK).timestamp()) * NS_PER_SECOND


def bar(ts: int) -> Bar:
    """A one-second bar of the name at 10.00 throughout, available at `ts`."""
    price = Price(10.0, 2)
    return Bar(
        second_bar_type(),
        price,
        price,
        price,
        price,
        Quantity.from_int(10_000),
        ts_event=ts,
        ts_init=ts,
    )


def session(day: date) -> list[Bar]:
    """A bar for each of the first ten seconds after the open, then one an hour to the end of
    the UTC day: a session busy in its first seconds and printing all day after them."""
    first = opening(day) + NS_PER_SECOND
    instants = [first + step * NS_PER_SECOND for step in range(10)]
    instants += range(first + HOUR_NS, midnight_ns(day + timedelta(days=1)), HOUR_NS)
    return [bar(ts) for ts in instants]


AFTER_HOURS = midnight_ns(date(2024, 3, 13)) + HOUR_NS // 2
"""A print at 00:30Z on 2024-03-13, 20:30 in New York the evening before: across the UTC
midnight from the last session, alone in a period of its own that the sleeve does not trade."""


def test_a_sleeve_that_trades_only_the_first_seconds_of_each_session_reads_so_in_both_seasons(
    tmp_path: Path, request_for
) -> None:
    """It buys a second after its session's first point and sells two seconds after that,
    every session, and the session prints for the rest of the UTC day. The parent notes each
    period's first point as it streams the window, and the runner ends each period at its UTC
    day's last point, the after-hours print opening and ending a period of its own; the digest
    reads the first fill 1 s into each period filled in and the last 3 s, 2 s between them, in
    the winter half and the summer half alike, and does not count the period that held a point
    and no fill. Each period's return is minus the cost of the fills the digest read in it, so
    the runner booked every fill to that period."""
    hyp = hypothesis(resolution="1s").model_copy(update={"windows": WINDOWS})
    research = (WINDOWS.research.start, WINDOWS.research.end)
    points = [*(point for day in SESSIONS for point in session(day)), bar(AFTER_HOURS)]
    store = catalog(tmp_path / "catalog", points, [instrument()])
    lane = tmp_path / "lane"
    lane.mkdir()
    request = request_for(research, source=OPENER, hypothesis_=hyp)

    result = run_subprocess(request, store, lane)
    run = result.run

    assert result.crashed is False, result.traceback_tail
    firsts = (*(opening(day) + NS_PER_SECOND for day in SESSIONS), AFTER_HOURS)
    assert result.firsts_ns == firsts, "each period's first point, as the parent handed it"
    assert run.period_ns == request.period_ns, "the length the runner cut the window into"
    lasts = (*(int(session(day)[-1].ts_init) for day in SESSIONS), AFTER_HOURS)
    assert run.period_ends_ns == lasts, "each UTC day's last point: cut from the midnight"
    made = digest(run, result.intents, result.firsts_ns)
    assert made.fills == 2 * len(SESSIONS)
    assert (made.period, made.filled_periods) == ("1d", len(SESSIONS))
    assert (made.first_fill_s, made.last_fill_s, made.active_s) == (1.0, 3.0, 2.0)
    change = midnight_ns(date(2024, 3, 10))
    opens, closes = run.bounds
    for half in (run.between(opens, change), run.between(change, closes)):
        timed = digest(half, (), result.firsts_ns)
        assert (timed.filled_periods, timed.first_fill_s, timed.last_fill_s) == (2, 1.0, 3.0)
    assert all(fill.cost > 0 for fill in run.fills), "every fill paid, so a return shows it"
    cells = [(fill.ts_ns - opens) // run.period_ns for fill in run.fills]
    paid = [
        -fsum(fill.cost for fill, cell in zip(run.fills, cells, strict=True) if cell == period)
        for period in ((end - opens) // run.period_ns for end in run.period_ends_ns)
    ]
    assert list(run.returns) == pytest.approx(paid), "a period's return is minus its fills' cost"
