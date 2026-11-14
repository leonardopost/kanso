"""The harness under a dated spread: handed the table apart, reserving a width no day moves,
and reading each fill's instant off the venue's record the extraction reads.

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
from kanso.nautilus.struck import Struck
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


def _sleeve(latency_ms: float = 0.0, struck: Struck | None = None) -> KansoStrategy:
    sleeve = KansoStrategy(config(venue_model={"costs": {**COSTS, "latency_ms": latency_ms}}))
    dated(sleeve, ROWS, Struck() if struck is None else struck)
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
    struck = Struck()
    backtest.booked(sleeve, request, struck)
    assert {row.instrument for row in sleeve._spread_rows} == {
        f"{FIRST}.XNAS",
        "LAGG.XNAS",
    }
    assert sleeve._spread_struck is struck


def test_a_dated_sleeve_booked_without_the_venue_s_record_is_refused() -> None:
    """The harness reads each fill's instant off the record its venue keeps, so a path that
    kept none would charge each fill another instant than the extraction does."""
    request = _request(10.0)
    cls, configured = backtest._sleeve(request)

    with pytest.raises(PreconditionError, match="keeps no record of the instant"):
        backtest.booked(cls(config=configured), request)


@given(
    at=st.integers(
        min_value=int(1_262_304_000e9), max_value=int(1_703_980_800e9)
    ),  # 2010 to the end of 2023
    price=st.floats(min_value=0.5, max_value=500.0),
)
def test_the_reserve_covers_a_fill_priced_where_it_sized_and_reads_no_date(
    at: int, price: float
) -> None:
    """What `cost_rate_at` reserves for a name at the part of the session the point being
    handled is in covers what a fill priced in that part is charged, in any year; and it is
    the same at that part of the session in every year. A fill priced in a later part of the
    session than it was sized in can pay more (`docs/backlog.md` row 261)."""
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


def _filled(event_id: str, name: str, ts: int) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, instrument_id=SimpleNamespace(value=name), ts_event=ts)


def test_a_fill_is_charged_at_the_instant_the_venue_recorded_for_it() -> None:
    """The record the venue keeps names the bar a fill's price came from, which can be another
    part of the session than the fill's own instant; a fill it recorded nothing for is read
    at its own."""
    struck = Struck()
    sleeve = _sleeve(struck=struck)
    before, at = _ns(DAY, "09:30"), _ns(DAY, "09:31")
    struck._at["priced"] = before

    priced = sleeve._half_of(None, _filled("priced", NAME, at))
    unheard = sleeve._half_of(None, _filled("unheard", NAME, at))

    assert priced == dated_half_spread(ROWS, NAME, before)
    assert dated_part(before)[0] == "pre"
    assert unheard == dated_half_spread(ROWS, NAME, at)
    assert dated_part(at)[0] == "open"
