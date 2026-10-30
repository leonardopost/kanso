"""When in its periods a card traded, read off the run the engine struck.

A book that trades the first seconds of each period and nothing after them — a sleeve whose
signal fires at the first points a session hands it and whose day a stop ends a second later
— scores like any other, and its trades, holds and edges say nothing of when it traded. Its
digest says it: the period the digest reads a fill in is the one the runner cut the window
into and booked the fill to, opening at the window's first midnight plus whole periods, so
at a daily period the offsets are the UTC time of day, whatever venue the session belongs to.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from nautilus_trader.model.data import Bar
from nautilus_trader.model.objects import Price, Quantity

from kanso.criteria.digest import digest
from kanso.criteria.run import NS_PER_SECOND, midnight_ns
from kanso.nautilus.backtest import execute

from .conftest import hypothesis, instrument, second_bar_type

OPENER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    """Buys the first bar of each UTC day, sells the third, and trades nothing after it."""

    config_cls = KansoConfig

    def on_start(self) -> None:
        self.day = None
        self.seen = 0

    def on_bar(self, bar) -> None:
        day = bar.ts_init // 86_400_000_000_000
        if day != self.day:
            self.day, self.seen = day, 0
        self.seen += 1
        if self.seen == 1:
            self.submit_entry(bar.bar_type.instrument_id, "BUY", notional=1_000.0)
        elif self.seen == 3:
            self.submit_exit(bar.bar_type.instrument_id)
'''

DAYS = tuple(date(2024, 1, 2) + timedelta(days=day) for day in range(4))
"""Four sessions inside the research window, so the medians are read off four periods."""

HOUR_S = 3_600


def session(day: date, opening_s: int) -> list[Bar]:
    """A bar for each of the first ten seconds after `opening_s`, then one an hour to the end
    of the UTC day: a session busy in its first seconds and printing all day after them."""
    seconds = [opening_s + step for step in range(1, 11)]
    seconds += range(opening_s + HOUR_S, 86_400, HOUR_S)
    made: list[Bar] = []
    for second in seconds:
        ts = midnight_ns(day) + second * NS_PER_SECOND
        made.append(
            Bar(
                second_bar_type(),
                Price(10.0, 2),
                Price(10.0, 2),
                Price(10.0, 2),
                Price(10.0, 2),
                Quantity.from_int(10_000),
                ts_event=ts,
                ts_init=ts,
            )
        )
    return made


@pytest.mark.parametrize(
    "opening_s",
    [0, 14 * HOUR_S + 30 * 60],
    ids=["a round-the-clock name opening at its period's midnight", "a session at 14:30Z"],
)
def test_a_sleeve_that_trades_only_the_first_seconds_of_each_period_reads_so(
    request_for, opening_s: int
) -> None:
    """It buys a second after its session's first instant and sells two seconds later, every
    session, and the session prints for the rest of the day: the digest reads the first fill
    `opening_s + 1` seconds into each period, the last `opening_s + 3`, two seconds between
    them, on every period the runner struck."""
    request = request_for(source=OPENER, hypothesis_=hypothesis(resolution="1s"))
    points = [bar for day in DAYS for bar in session(day, opening_s)]

    run = execute(request, [instrument()], [points]).run
    made = digest(run)

    assert run.period_ns == request.period_ns, "the length the runner cut the window into"
    assert len(run.period_ends_ns) == made.filled_periods == len(DAYS)
    assert made.fills == 2 * len(DAYS)
    assert made.first_fill_s == opening_s + 1.0
    assert made.last_fill_s == opening_s + 3.0
    assert made.active_s == 2.0
    opens, _ = run.bounds
    for fill in run.fills:
        booked = next(end for end in run.period_ends_ns if end >= fill.ts_ns)
        assert (fill.ts_ns - opens) // run.period_ns == (booked - opens) // run.period_ns, (
            "the period the digest reads a fill in is the one the runner booked it to"
        )
