"""`trading_hours`: every fill inside a stated daily session, every position flat by its close.

The instants are written in UTC and the session in New York, so each test states the clock
change it relies on: Eastern Standard Time is UTC-5 from the first Sunday of November to the
second Sunday of March, and Eastern Daylight Time UTC-4 for the rest of the year.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.criteria import Fill
from kanso.criteria.gates import TradingSession, first_reading, trading_hours
from kanso.criteria.run import NS_PER_SECOND
from tests.criteria.builders import build_run, context

NEW_YORK = ZoneInfo("America/New_York")
REGULAR = {"session": "09:30-16:00", "tz": "America/New_York"}
"""The regular session of a US equity venue."""

WINTER = date(2024, 1, 2)
"""A Tuesday on Eastern Standard Time: 09:30 to 16:00 is 14:30 to 21:00 UTC."""

SUMMER = date(2024, 7, 2)
"""A Tuesday on Eastern Daylight Time: 09:30 to 16:00 is 13:30 to 20:00 UTC."""


def utc(day: date, hour: int, minute: int = 0, second: int = 0, ns: int = 0) -> int:
    """An instant written on the UTC clock, in nanoseconds since the epoch."""
    moment = datetime.combine(day, time(hour, minute, second), tzinfo=UTC)
    return int(moment.timestamp()) * NS_PER_SECOND + ns


def trade(ts_ns: int, side: str = "BUY", qty: float = 100.0, instrument: str = "DEMO") -> Fill:
    return Fill(ts_ns=ts_ns, instrument_id=instrument, side=side, qty=qty, px=10.0, cost=0.0)


def judged(*fills: Fill, start: date = WINTER, days: int = 4, **params: Any) -> Any:
    """`trading_hours` over a daily run of `days` holding these fills, in regular hours
    unless other parameters are given."""
    run = build_run((0.0,) * days, start=start, fills=fills)
    return trading_hours.evaluate(context(run, params=params or REGULAR))


# --- what is judged -----------------------------------------------------------------


def test_a_round_trip_inside_the_session_passes() -> None:
    result = judged(trade(utc(WINTER, 15)), trade(utc(WINTER, 20, 59), "SELL"))

    assert result.passed
    assert result.evidence["n_fills"] == 2
    assert result.evidence["n_fills_outside"] == result.evidence["n_held_outside"] == 0
    assert result.evidence["earliest_fill_outside"] is None
    assert result.evidence["n_positions"] == 1


def test_a_fill_at_16_30_in_new_york_fails_naming_the_earliest_and_the_latest() -> None:
    """Bought and sold after the close on two evenings: four fills outside the session."""
    result = judged(
        trade(utc(WINTER, 21, 30)),
        trade(utc(WINTER, 21, 45), "SELL"),
        trade(utc(WINTER + timedelta(days=1), 21, 30)),
        trade(utc(WINTER + timedelta(days=1), 21, 45, 7), "SELL"),
    )

    assert not result.passed
    assert result.evidence["n_fills_outside"] == 4
    assert result.evidence["earliest_fill_outside"] == {
        "instrument": "DEMO",
        "side": "BUY",
        "ts_ns": utc(WINTER, 21, 30),
        "local": "2024-01-02T16:30:00-05:00",
    }
    assert result.evidence["latest_fill_outside"]["local"] == "2024-01-03T16:45:07-05:00"
    assert result.evidence["latest_fill_outside"]["side"] == "SELL"


def test_the_session_moves_with_daylight_saving() -> None:
    """14:00 UTC is 09:00 in New York in January and 10:00 in July: one hour of difference
    puts the same UTC instant before the open in winter and inside the session in summer."""
    winter = judged(trade(utc(WINTER, 14)), trade(utc(WINTER, 15), "SELL"))
    summer = judged(trade(utc(SUMMER, 14)), trade(utc(SUMMER, 15), "SELL"), start=SUMMER)

    assert not winter.passed
    assert winter.evidence["earliest_fill_outside"]["local"] == "2024-01-02T09:00:00-05:00"
    assert summer.passed


def test_the_week_the_clocks_change_moves_the_open_by_an_hour() -> None:
    """13:30 UTC is 08:30 on Friday 8 March 2024 and 09:30 on Monday 11 March, the first
    weekday on daylight time."""
    friday, monday = date(2024, 3, 8), date(2024, 3, 11)

    before = judged(trade(utc(friday, 13, 30)), trade(utc(friday, 14), "SELL"), start=friday)
    after = judged(trade(utc(monday, 13, 30)), trade(utc(monday, 14), "SELL"), start=monday)

    assert not before.passed
    assert after.passed


def test_the_session_is_half_open_so_a_fill_at_the_close_is_outside() -> None:
    last = utc(WINTER, 20, 59, 59, NS_PER_SECOND - 1)
    inside = judged(trade(utc(WINTER, 14, 30)), trade(last, "SELL"))
    at_the_close = judged(trade(utc(WINTER, 14, 30)), trade(utc(WINTER, 21), "SELL"))

    assert inside.passed
    assert not at_the_close.passed
    assert at_the_close.evidence["latest_fill_outside"]["local"] == "2024-01-02T16:00:00-05:00"


def test_a_position_held_overnight_fails_though_every_fill_was_inside() -> None:
    """Bought at 15:59 and sold at 09:31 the next morning: two fills inside, held all night."""
    result = judged(
        trade(utc(WINTER, 20, 59)),
        trade(utc(WINTER + timedelta(days=1), 14, 31), "SELL"),
    )

    assert not result.passed
    assert result.evidence["n_fills_outside"] == 0
    assert result.evidence["n_held_outside"] == 1
    assert result.evidence["earliest_held_outside"] == {
        "instrument": "DEMO",
        "opened": "2024-01-02T15:59:00-05:00",
        "closed": "2024-01-03T09:31:00-05:00",
        "session_closed": "2024-01-02T16:00:00-05:00",
    }


def test_a_position_still_open_at_the_window_close_is_held_to_it() -> None:
    result = judged(trade(utc(WINTER, 15)), days=1)

    assert not result.passed
    assert result.evidence["n_held_outside"] == 1
    assert result.evidence["latest_held_outside"]["closed"] == "2024-01-02T19:00:00-05:00"


def test_a_reversal_in_one_fill_keeps_the_position_open() -> None:
    """Long, then short through zero at 15:00, then flat at 15:30: one position, inside."""
    result = judged(
        trade(utc(WINTER, 15)),
        trade(utc(WINTER, 20), "SELL", qty=200.0),
        trade(utc(WINTER, 20, 30), qty=100.0),
    )

    assert result.passed
    assert result.evidence["n_positions"] == 1


def test_positions_are_counted_per_instrument_and_in_the_order_they_opened() -> None:
    result = judged(
        trade(utc(WINTER, 15), instrument="B"),
        trade(utc(WINTER, 16), instrument="A"),
        trade(utc(WINTER, 17), "SELL", instrument="A"),
        trade(utc(WINTER + timedelta(days=1), 15), "SELL", instrument="B"),
        trade(utc(WINTER + timedelta(days=1), 16), instrument="A"),
    )

    assert result.evidence["n_positions"] == 3
    assert result.evidence["n_held_outside"] == 2
    assert result.evidence["earliest_held_outside"]["instrument"] == "B"
    assert result.evidence["latest_held_outside"]["instrument"] == "A"


def test_a_fractional_lot_sold_in_pieces_comes_back_to_flat() -> None:
    """0.1 + 0.2 is not 0.3 in binary; a position summed raw would never close."""
    result = judged(
        trade(utc(WINTER, 15), qty=0.3),
        trade(utc(WINTER, 16), "SELL", qty=0.1),
        trade(utc(WINTER, 17), "SELL", qty=0.2),
    )

    assert result.passed
    assert result.evidence["n_positions"] == 1


def test_an_early_close_is_judged_against_the_stated_close() -> None:
    """No calendar of early closes is on file: Friday 29 November 2024 closed at 13:00 in
    New York, and a round trip at 14:00 and 15:00 that day is judged against 16:00."""
    friday = date(2024, 11, 29)

    result = judged(trade(utc(friday, 19)), trade(utc(friday, 20), "SELL"), start=friday)

    assert result.passed


def test_another_zone_and_session_are_read_on_their_own_clock() -> None:
    """London opens at 08:00: 07:30 UTC is 07:30 there in January and 08:30 in July."""
    london = {"session": "08:00-16:30", "tz": "Europe/London"}

    winter = judged(trade(utc(WINTER, 7, 30)), trade(utc(WINTER, 9), "SELL"), **london)
    summer = judged(
        trade(utc(SUMMER, 7, 30)), trade(utc(SUMMER, 9), "SELL"), start=SUMMER, **london
    )

    assert not winter.passed
    assert summer.passed


# --- what is not judged -----------------------------------------------------------


def test_without_a_session_nothing_is_judged() -> None:
    run = build_run((0.0,) * 4, fills=(trade(utc(WINTER, 21, 30)),))

    result = trading_hours.evaluate(context(run))

    assert result.passed and result.skipped is not None


def test_a_run_that_made_no_fill_judges_nothing() -> None:
    result = judged()

    assert result.passed and result.skipped is not None


@pytest.mark.parametrize(
    ("params", "refusal"),
    [
        ({"session": "09:30-16:00"}, "only one of session and tz"),
        ({"tz": "America/New_York"}, "only one of session and tz"),
        ({"session": "16:00-09:30", "tz": "America/New_York"}, "is not HH:MM-HH:MM"),
        ({"session": "09:30-16:00", "tz": "America/Gotham"}, "is not a time zone"),
    ],
)
def test_a_session_the_gate_cannot_read_is_a_refusal_not_a_skip(
    params: dict[str, str], refusal: str
) -> None:
    """Validation refuses these before any card runs; a context that carries one anyway
    fails every card rather than letting it through untimed."""
    result = judged(trade(utc(WINTER, 15)), trade(utc(WINTER, 16), "SELL"), **params)

    assert not result.passed
    assert refusal in result.evidence["refused"]


def test_an_attached_construct_is_judged_on_the_whole_run() -> None:
    """The host's fill outside the session is the book's fill outside the session."""
    host = build_run((0.0,) * 4, fills=(trade(utc(WINTER, 21, 30)),))
    run = build_run(
        (0.0,) * 4,
        fills=(
            trade(utc(WINTER, 15), instrument="HEDGE"),
            trade(utc(WINTER, 16), "SELL", instrument="HEDGE"),
            *host.fills,
        ),
    )

    result = trading_hours.evaluate(context(run, params=REGULAR, host_run=host))

    assert not result.passed
    assert result.evidence["n_fills_outside"] == 1


# --- the time arithmetic ------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "day", "minute", "instant"),
    [
        # 02:30 does not happen on 10 March 2024 in New York: 02:00 EST is 03:00 EDT, 07:00 UTC.
        ("America/New_York", date(2024, 3, 10), 150, datetime(2024, 3, 10, 7, tzinfo=UTC)),
        # 01:30 happens twice on 3 November 2024: first on EDT, 05:30 UTC, then on EST.
        ("America/New_York", date(2024, 11, 3), 90, datetime(2024, 11, 3, 5, 30, tzinfo=UTC)),
        # Havana's clock went from 23:59:59 on 9 March 2024 to 01:00 on the 10th, at 05:00
        # UTC, so its midnight, 00:30 and the 9th's 24:00 are all that instant.
        ("America/Havana", date(2024, 3, 10), 0, datetime(2024, 3, 10, 5, tzinfo=UTC)),
        ("America/Havana", date(2024, 3, 10), 30, datetime(2024, 3, 10, 5, tzinfo=UTC)),
        ("America/Havana", date(2024, 3, 9), 1440, datetime(2024, 3, 10, 5, tzinfo=UTC)),
        # An ordinary reading, in summer and in winter.
        ("America/New_York", SUMMER, 570, datetime(2024, 7, 2, 13, 30, tzinfo=UTC)),
        ("America/New_York", WINTER, 960, datetime(2024, 1, 2, 21, tzinfo=UTC)),
    ],
)
def test_a_skipped_reading_is_the_change_and_a_repeated_one_its_first_instant(
    zone: str, day: date, minute: int, instant: datetime
) -> None:
    assert first_reading(day, minute, ZoneInfo(zone)) == int(instant.timestamp()) * NS_PER_SECOND


def test_a_session_on_the_night_the_clocks_go_back_runs_by_its_instants() -> None:
    """00:00 to 03:00 in New York on 3 November 2024 lasts four hours, not three: the clock
    reads 01:00 to 02:00 twice. A position held across the repeated hour is held inside."""
    night = date(2024, 11, 3)
    opened, closed = utc(night, 4, 30), utc(night, 7, 30)
    result = judged(
        trade(opened),
        trade(closed, "SELL"),
        start=night,
        session="00:00-03:00",
        tz="America/New_York",
    )

    assert result.passed
    assert (closed - opened) // NS_PER_SECOND == 3 * 3600


ZONES = (
    "America/New_York",
    "Europe/London",
    "America/St_Johns",
    "Australia/Lord_Howe",
    "America/Santiago",
    "America/Havana",
    "Asia/Tehran",
    "Pacific/Chatham",
    "Pacific/Apia",
    "Asia/Kolkata",
    "UTC",
)
"""Zones whose clock changes come at the half hour, by half an hour, at midnight, at
different seasons or not at all — and Apia, which skipped 30 December 2011."""


def _changes(zone: ZoneInfo, first: int = 2005, last: int = 2030) -> list[date]:
    """The local dates either side of every clock change in these years."""
    found: list[date] = []
    day = date(first, 1, 1)
    noon = datetime.combine(day, time(12), tzinfo=UTC)
    previous = noon.astimezone(zone).utcoffset()
    while day.year <= last:
        day += timedelta(days=1)
        noon += timedelta(days=1)
        offset = noon.astimezone(zone).utcoffset()
        if offset != previous:
            local = noon.astimezone(zone).date()
            found.extend(local + timedelta(days=shift) for shift in (-2, -1, 0, 1))
        previous = offset
    return found


CHANGES = {name: _changes(ZoneInfo(name)) for name in ZONES}

zones = st.sampled_from(ZONES)


@st.composite
def dated(draw: Any) -> tuple[str, date]:
    """A zone and a local date, half the time one beside a clock change."""
    name = draw(zones)
    near = CHANGES[name]
    if near and draw(st.booleans()):
        return name, draw(st.sampled_from(near))
    return name, draw(st.dates(date(2005, 1, 1), date(2030, 12, 31)))


@st.composite
def sessions(draw: Any) -> tuple[int, int]:
    """An opening and a closing minute: closing after opening, at the latest 24:00."""
    opens = draw(st.integers(0, 1439))
    return opens, draw(st.integers(opens + 1, 1440))


def reading(ts_ns: int, zone: ZoneInfo) -> tuple[date, int]:
    """The local date and the second of it the zone's clock shows at an instant."""
    local = datetime.fromtimestamp(ts_ns // NS_PER_SECOND, zone)
    return local.date(), local.hour * 3600 + local.minute * 60 + local.second


@given(dated(), st.integers(0, 1440))
def test_a_first_reading_is_the_first_instant_the_clock_shows_that_minute_or_later(
    where: tuple[str, date], minute: int
) -> None:
    """At the instant it returns the clock reads the minute or later, and a second before
    it the clock read earlier — on every zone's every kind of clock change, a skipped or a
    repeated reading included. `24:00` is the next date's `00:00`."""
    name, day = where
    zone = ZoneInfo(name)
    target = (day + timedelta(days=minute // 1440), minute % 1440 * 60)

    found = first_reading(day, minute, zone)

    assert found % NS_PER_SECOND == 0
    assert reading(found, zone) >= target
    assert reading(found - NS_PER_SECOND, zone) < target


@given(dated(), sessions(), st.integers(0, 86_399))
def test_an_instant_is_in_session_exactly_when_its_wall_clock_is(
    where: tuple[str, date], session: tuple[int, int], second: int
) -> None:
    """For every clock reading that names one instant, the gate's judgement on that
    instant is the reading's: inside when the clock shows the opening time or later and
    not yet the closing time. A reading a clock change skips or repeats names no single
    instant and is left to the test above."""
    name, day = where
    zone = ZoneInfo(name)
    wall = datetime.combine(day, time(second // 3600, second // 60 % 60, second % 60), zone)
    instant = int(wall.timestamp())
    if instant != int(wall.replace(fold=1).timestamp()):
        return
    opens, closes = session

    held = TradingSession(opens, closes, zone).holds(instant * NS_PER_SECOND)

    assert held == (opens * 60 <= second < closes * 60)


@given(
    st.lists(
        st.tuples(st.integers(0, 400), st.integers(570, 958), st.integers(1, 30)),
        min_size=1,
        max_size=12,
    ),
    st.integers(0, 11),
)
def test_round_trips_inside_each_session_pass_and_one_carried_past_its_close_fails(
    trips: list[tuple[int, int, int]], late: int
) -> None:
    """Round trips opened and closed inside one New York session on any date, either side
    of a clock change included, pass; push one of them a nanosecond past its close and it
    is the position the evidence names."""
    start = date(2024, 1, 1)
    fills: list[Fill] = []
    instants: list[tuple[int, int, int]] = []
    for index, (offset, minute, length) in enumerate(trips):
        day = start + timedelta(days=offset)
        opened = first_reading(day, minute, NEW_YORK)
        closed = min(opened + length * 60 * NS_PER_SECOND, first_reading(day, 960, NEW_YORK) - 1)
        name = f"T{index}"
        fills += [trade(opened, instrument=name), trade(closed, "SELL", instrument=name)]
        instants.append((opened, closed, first_reading(day, 960, NEW_YORK)))
    run = build_run((0.0,) * 402, start=start, fills=tuple(fills))

    assert trading_hours.evaluate(context(run, params=REGULAR)).passed

    pushed = late % len(trips)
    closes = instants[pushed][2]
    moved = tuple(
        replace(item, ts_ns=closes + 1)
        if item.instrument_id == f"T{pushed}" and item.side == "SELL"
        else item
        for item in fills
    )
    carried = build_run((0.0,) * 402, start=start, fills=moved)

    result = trading_hours.evaluate(context(carried, params=REGULAR))

    assert not result.passed
    assert result.evidence["n_held_outside"] == 1
    assert result.evidence["earliest_held_outside"]["instrument"] == f"T{pushed}"
    assert result.evidence["n_fills_outside"] == 1
