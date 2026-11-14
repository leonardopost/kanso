"""The New York clock, held to a daylight-saving rule written out by hand.

Since 2007 New York keeps Eastern Daylight Time, UTC-4, from 02:00 on the second Sunday of
March, which is 07:00 UTC, to 02:00 on the first Sunday of November, which is 06:00 UTC, and
Eastern Standard Time, UTC-5, for the rest of the year. `wall` below applies that rule with
integer arithmetic and no zone database, and every function of `kanso.nautilus.clock` is
held to it at instants from 2007 to 2027.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.data.closures import US_EQUITY_CLOSURES, closed_days
from kanso.nautilus.clock import Session, minute_of, segment_of, session_of, week_opening

NS = 1_000_000_000
HOUR = 3_600
DAY = 86_400
EPOCH = date(1970, 1, 1)
MONDAY = 0
FOUR_AM = 4 * 60
"""The opening the weekly target is stated at: Monday, 04:00 New York."""


def second_sunday_of_march(year: int) -> date:
    first = date(year, 3, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7)


def first_sunday_of_november(year: int) -> date:
    first = date(year, 11, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7)


def utc(day: date, hour: int, minute: int = 0) -> int:
    """An instant written on the UTC clock, in nanoseconds; an hour past 23 runs on."""
    return ((day - EPOCH).days * DAY + hour * HOUR + minute * 60) * NS


def offset(seconds: int) -> int:
    """New York's offset from UTC at a second since the epoch, by the rule since 2007."""
    year = (EPOCH + timedelta(days=seconds // DAY)).year
    starts = utc(second_sunday_of_march(year), 7) // NS
    ends = utc(first_sunday_of_november(year), 6) // NS
    return -4 * HOUR if starts <= seconds < ends else -5 * HOUR


def wall(ts_ns: int) -> tuple[date, int]:
    """The New York date, and the second of that day, the clock shows at an instant."""
    seconds = ts_ns // NS
    local = seconds + offset(seconds)
    return EPOCH + timedelta(days=local // DAY), local % DAY


def new_york(day: date, hour: int, minute: int = 0) -> int:
    """An instant written on the New York clock, at a reading the clock shows once."""
    local = (day - EPOCH).days * DAY + hour * HOUR + minute * 60
    for shift in (5 * HOUR, 4 * HOUR):
        if offset(local + shift) == -shift:
            return (local + shift) * NS
    raise AssertionError(f"{day} {hour:02}:{minute:02} is not one reading in New York")


def segment(second: int) -> str:
    """The segment a second of the New York day is in, written out."""
    if 4 * HOUR <= second < 9 * HOUR + 1800:
        return "pre"
    if 9 * HOUR + 1800 <= second < 16 * HOUR:
        return "rth"
    if 16 * HOUR <= second < 20 * HOUR:
        return "post"
    return "overnight"


FIRST = utc(date(2007, 1, 1), 0)
LAST = utc(date(2028, 1, 1), 0) - 1


@st.composite
def near_a_change(draw: Any) -> int:
    """An instant within two hours of one of New York's changes of clock, 2007 to 2027."""
    year = draw(st.integers(2007, 2027))
    change = draw(
        st.sampled_from(
            (utc(second_sunday_of_march(year), 7), utc(first_sunday_of_november(year), 6))
        )
    )
    return change + draw(st.integers(-2 * HOUR * NS, 2 * HOUR * NS))


@st.composite
def near_a_boundary(draw: Any) -> int:
    """An instant within a few nanoseconds of midnight, 04:00, 09:30, 16:00 or 20:00 in New
    York on a date from 2007 to 2027."""
    day = draw(st.dates(date(2007, 1, 1), date(2027, 12, 31)))
    hour, minute = draw(st.sampled_from(((0, 0), (4, 0), (9, 30), (16, 0), (20, 0))))
    return new_york(day, hour, minute) + draw(st.integers(-3, 3))


instants = st.one_of(st.integers(FIRST, LAST), near_a_change(), near_a_boundary())


# --- an instant, and the minute a point covers --------------------------------------


@given(instants)
def test_every_reading_is_the_one_the_rule_written_out_gives(ts_ns: int) -> None:
    """`segment_of` and `minute_of` read the instant; `session_of` the instant a nanosecond
    before it, on either side of every change of clock and at every edge of a segment."""
    day, second = wall(ts_ns)
    before_day, before = wall(ts_ns - 1)

    assert segment_of(ts_ns) == (segment(second), day)
    assert minute_of(ts_ns) == second // 60
    assert session_of(ts_ns) == Session(segment(before), before_day, before // 60)


WINTER = date(2024, 1, 2)
SUMMER = date(2024, 7, 2)


@pytest.mark.parametrize(("day", "behind"), [(WINTER, 5), (SUMMER, 4)])
@pytest.mark.parametrize(
    ("hour", "minute", "own", "covered"),
    [
        (4, 0, "pre", Session("overnight", WINTER, 239)),
        (9, 30, "rth", Session("pre", WINTER, 569)),
        (9, 31, "rth", Session("rth", WINTER, 570)),
        (16, 0, "post", Session("rth", WINTER, 959)),
        (16, 1, "post", Session("post", WINTER, 960)),
        (20, 0, "overnight", Session("post", WINTER, 1199)),
    ],
)
def test_a_bar_stamped_at_its_close_is_read_as_the_minute_it_covers(
    day: date, behind: int, hour: int, minute: int, own: str, covered: Session
) -> None:
    """The bar stamped 09:30 is the pre-market's last, 09:31 the first regular bar, 16:00 the
    last regular bar and 20:00 the post-market's last — in winter, when New York is five hours
    behind UTC, and in summer, when it is four. The instant itself is in the next segment."""
    stamp = utc(day, hour + behind, minute)

    assert session_of(stamp) == covered._replace(day=day)
    assert segment_of(stamp) == (own, day)
    assert minute_of(stamp) == hour * 60 + minute


def test_an_evening_point_keeps_its_session_s_date_and_a_daily_bar_its_last_minute() -> None:
    """20:30 in New York on a winter Tuesday is 01:30 UTC on Wednesday; a daily bar is stamped
    at the next New York midnight, and covers its session's last minute."""
    assert session_of(utc(WINTER, 25, 30)) == Session("overnight", WINTER, 1229)
    assert session_of(new_york(WINTER + timedelta(days=1), 0)) == Session("overnight", WINTER, 1439)


# --- weeks ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "opens", "next_opens"),
    [
        # The week that holds the change to daylight time is an hour short: it opens at
        # 04:00 EST, 09:00 UTC, and the next at 04:00 EDT, 08:00 UTC.
        (utc(date(2024, 3, 10), 12), utc(date(2024, 3, 4), 9), utc(date(2024, 3, 11), 8)),
        (utc(date(2024, 3, 11), 8) - 1, utc(date(2024, 3, 4), 9), utc(date(2024, 3, 11), 8)),
        (utc(date(2024, 3, 11), 8), utc(date(2024, 3, 11), 8), utc(date(2024, 3, 18), 8)),
        # The week that holds the change back is an hour long, and a Monday at 03:30 EST,
        # 08:30 UTC, which in the week before would have been past 04:00 EDT, is still in it.
        (utc(date(2024, 11, 3), 12), utc(date(2024, 10, 28), 8), utc(date(2024, 11, 4), 9)),
        (utc(date(2024, 11, 4), 8, 30), utc(date(2024, 10, 28), 8), utc(date(2024, 11, 4), 9)),
        (utc(date(2024, 11, 4), 9), utc(date(2024, 11, 4), 9), utc(date(2024, 11, 11), 9)),
    ],
)
def test_a_week_opens_at_monday_04_00_new_york_on_either_side_of_a_change(
    instant: int, opens: int, next_opens: int
) -> None:
    assert week_opening(instant, MONDAY, FOUR_AM) == (opens, next_opens)


def test_a_closed_monday_opens_its_week_at_its_own_04_00() -> None:
    """2024-02-19, Washington's Birthday, opened no US equity venue; its week opened at 04:00
    on it all the same, and its first mark is Tuesday's. So does every closed Monday on file
    from 2007, the first year New York has kept its present rule."""
    closed = closed_days("EQUITY", "ARCX")
    holiday = date(2024, 2, 19)
    assert closed(holiday)

    tuesday_open = new_york(holiday + timedelta(days=1), 9, 31)
    assert week_opening(tuesday_open, MONDAY, FOUR_AM) == (
        new_york(holiday, 4),
        new_york(holiday + timedelta(days=7), 4),
    )

    mondays = [
        day
        for year, days in US_EQUITY_CLOSURES.items()
        if year >= 2007
        for day in (date.fromisoformat(f"{year}-{month_day}") for month_day in days.split())
        if day.weekday() == MONDAY
    ]
    assert len(mondays) > 50
    for day in mondays:
        assert closed(day)
        assert week_opening(new_york(day, 12), MONDAY, FOUR_AM)[0] == new_york(day, 4), day


@given(instants, st.integers(0, 6), st.integers(0, 1439))
def test_a_week_is_the_span_from_the_last_opening_to_the_next(
    ts_ns: int, weekday: int, minute: int
) -> None:
    """Each opening is the first instant the clock reads the weekday and the minute, or later
    — a reading a change of clock skips opens at the change, and one it repeats at its first
    instant — and the instant lies between the last opening and the next, seven dates on,
    whatever the weekday and the minute. The two openings name the same week from anywhere
    inside it."""
    opens, next_opens = week_opening(ts_ns, weekday, minute)

    assert opens <= ts_ns < next_opens
    first, _ = wall(opens)
    assert first.weekday() == weekday
    for found, day in ((opens, first), (next_opens, first + timedelta(days=7))):
        assert found % NS == 0
        assert wall(found) >= (day, minute * 60)
        assert wall(found - NS) < (day, minute * 60)
    assert week_opening(opens, weekday, minute) == (opens, next_opens)
    assert week_opening(next_opens - 1, weekday, minute) == (opens, next_opens)
