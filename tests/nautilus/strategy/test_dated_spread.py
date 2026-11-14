"""The harness under a dated spread: handed the table apart, reserving a width no day moves,
and reading each fill's instant as the extraction does.

The rows are each a year's median, in force from its first day, so a strategy that read one
would know a width the market had not shown yet: the configuration a sleeve is built with
carries the venue model without them, and the runner hands them to the harness through a
private attribute `strategy_integrity` denies. What the sleeve reserves when it sizes reads
every row of every year, so it says the same on any day.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.errors import PreconditionError
from kanso.nautilus import backtest
from kanso.nautilus.costs import dated_half_spread, dated_part, dated_reserve_bps
from kanso.nautilus.cross_section import dated
from kanso.nautilus.strategy import KansoStrategy
from kanso.schemas.venue import DatedSpread
from tests.nautilus.backtest.test_dated_spread import FIRST, _request
from tests.nautilus.strategy.test_sleeve import config

NAME = "DEMO.XNAS"
NEW_YORK = ZoneInfo("America/New_York")
BPS = 10_000.0
COSTS = {"commission_bps": 1.0, "slippage_bps": 2.0, "sell_fee_bps": 0.4, "spread": "dated"}

ROWS = tuple(
    DatedSpread.model_validate(
        {
            "instrument": NAME,
            "from": f"{year}-01-01",
            "pre": 900.0 / (year - 2009),
            "open": 50.0 / (year - 2009),
            "rth": 15.0 / (year - 2009),
            "post": 700.0 / (year - 2009),
            "n_sessions": 5,
            "n_open": 25,
        }
    )
    for year in range(2010, 2024)
)


def _sleeve(latency_ms: float = 0.0) -> KansoStrategy:
    sleeve = KansoStrategy(config(venue_model={"costs": {**COSTS, "latency_ms": latency_ms}}))
    dated(sleeve, ROWS)
    return sleeve


ELSEWHEN = {
    "overnight": "02:00",
    "pre": "08:00",
    "open": "09:33",
    "rth": "12:00",
    "post": "18:00",
}
"""A New York minute of each part of the session, read on another day of another year."""


def _ns(day: date, hhmm: str) -> int:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    local = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=NEW_YORK)
    return int(local.timestamp()) * 1_000_000_000


def test_the_sleeve_is_configured_without_the_table_and_handed_it_apart() -> None:
    request = _request(10.0)
    cls, configured = backtest._sleeve(request)
    sleeve = cls(config=configured)

    assert "dated" not in sleeve.venue_model["costs"]  # type: ignore[operator]
    assert sleeve.venue_model["costs"]["spread"] == "dated"  # type: ignore[index]
    assert "dated" not in sleeve.kanso_config.venue_model["costs"]  # type: ignore[operator]
    assert sleeve._spread_rows == ()
    backtest.booked(sleeve, request)
    assert {row.instrument for row in sleeve._spread_rows} == {
        f"{FIRST}.XNAS",
        "LAGG.XNAS",
    }


@given(
    at=st.integers(
        min_value=int(1_262_304_000e9), max_value=int(1_703_980_800e9)
    ),  # 2010 to the end of 2023
    price=st.floats(min_value=0.5, max_value=500.0),
)
def test_the_reserve_is_at_least_every_fill_s_charged_rate_and_reads_no_date(
    at: int, price: float
) -> None:
    """What `cost_rate_at` reserves for a name at the part of the session the point being
    handled is in covers what a fill priced at that instant is charged, in any year; and it
    is the same at that part of the session in every year."""
    sleeve = _sleeve()
    sleeve._data_time = at
    reserved = sleeve.cost_rate_at(price, instrument=NAME)
    charged = (1.0 + 2.0 + 0.4) / BPS + dated_half_spread(ROWS, NAME, at)

    assert reserved >= charged
    assert reserved == pytest.approx((1.0 + 2.0 + 0.2 + dated_reserve_bps(ROWS, NAME, at)) / BPS)
    assert sleeve.cost_rate == pytest.approx((1.0 + 2.0 + 0.2 + 900.0) / BPS)
    assert sleeve.cost_rate >= reserved
    sleeve._data_time = _ns(date(2015, 3, 2), ELSEWHEN[dated_part(at)[0]])
    assert sleeve.cost_rate_at(price, instrument=NAME) == reserved


def test_a_sleeve_charged_a_dated_spread_and_handed_no_table_refuses_to_charge_nothing() -> None:
    sleeve = KansoStrategy(config(venue_model={"costs": COSTS}))
    event = SimpleNamespace(id="F", instrument_id=SimpleNamespace(value=NAME), ts_event=0)

    with pytest.raises(PreconditionError, match="was handed no table"):
        sleeve._half_of(None, event)


DAY = date(2023, 7, 3)


@pytest.mark.parametrize("latency_ms", [0.0, 10.0])
def test_a_fill_reads_its_bar_of_the_instant_where_the_venue_had_it(latency_ms: float) -> None:
    """As the extraction reads it: with no latency every bar of the instant is had; under one,
    the name's bar of the instant only where a bar of it opened the instant — a second grain
    of the name at the instant keeps what the first said — and the bar before it otherwise."""
    sleeve = _sleeve(latency_ms)
    before, at = _ns(DAY, "09:30"), _ns(DAY, "09:31")

    assert sleeve._spread_instant(NAME, at) is None
    sleeve._spread_bar(NAME, before, True)
    assert sleeve._spread_instant(NAME, at) == before
    sleeve._spread_bar(NAME, at, False)  # another name's point opened the instant
    sleeve._spread_bar(NAME, at, False)  # and a second grain of this one followed
    assert sleeve._spread_instant(NAME, at) == (at if latency_ms == 0.0 else before)
    sleeve._spread_bar("OTHER.XNAS", at, True)
    sleeve._spread_bar("OTHER.XNAS", at, False)
    assert sleeve._spread_instant("OTHER.XNAS", at) == at


def test_a_bar_moves_only_its_own_name_s_fills_of_its_instant_not_read_at_it_yet() -> None:
    """A fill of another name, of another instant, or already read at the bar is left as it
    was; one not booked yet is only moved, and booked when the balance is next read."""
    sleeve = _sleeve(0.0)
    before, at = _ns(DAY, "09:30"), _ns(DAY, "09:31")

    def fill(event_id: str, name: str, ts: int) -> SimpleNamespace:
        return SimpleNamespace(id=event_id, instrument_id=SimpleNamespace(value=name), ts_event=ts)

    fills = [
        fill("mine", NAME, at),
        fill("other", "OTHER.XNAS", at),
        fill("earlier", NAME, before),
        fill("read", NAME, at),
    ]
    sleeve._open_fills = {event.id: event for event in fills}
    sleeve._spread_at = {"mine": before, "other": before, "earlier": before, "read": at}

    sleeve._spread_bar(NAME, at, True)

    assert sleeve._spread_at == {"mine": at, "other": before, "earlier": before, "read": at}
    assert sleeve._booked_open == {}
