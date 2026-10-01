"""The closure calendar, held to the record it claims to state.

`fixtures/us_equity_sessions.txt` is a vendor's measurement and the exchanges' published
schedule, both recorded verbatim with the requests that produced them. The calendar is a
claim about a market, so it is checked against that record day by day and in both
directions — every weekday the record shows closed is closed, and every other weekday of
the span is open — because a day called closed that was open would let a snapshot pin a
hole, and a day called open that was closed only costs a refusal.
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from pathlib import Path

from nautilus_trader.test_kit.providers import TestInstrumentProvider

from kanso.data import closures
from tests.data.catalog.conftest import equity

RECORD = Path(__file__).parent / "fixtures" / "us_equity_sessions.txt"
DAY = timedelta(days=1)


def sections() -> tuple[dict[str, str], list[dict[str, str]]]:
    """The record's two answers: the session breaks, and the published schedule's rows."""
    text = RECORD.read_text(encoding="utf-8")
    sessions = text.split("[sessions]\n")[1].split("\n\n[upcoming]\n")[0]
    upcoming = text.split("[upcoming]\n")[1]
    [measured] = list(csv.DictReader(io.StringIO(sessions)))
    return measured, list(csv.DictReader(io.StringIO(upcoming)))


def days(first: date, last: date) -> list[date]:
    return [first + DAY * n for n in range((last - first).days + 1)]


def measured_closures(measured: dict[str, str]) -> set[date]:
    """The weekdays strictly inside each break between two consecutive sessions."""
    closed: set[date] = set()
    for pair in measured["breaks"].split():
        before, after = (date.fromisoformat(end) for end in pair.split(">"))
        closed |= {day for day in days(before + DAY, after - DAY) if day.weekday() < 5}
    return closed


def test_the_record_is_the_one_its_header_describes() -> None:
    """213 breaks holding 215 closures, and exactly as many weekday sessions as bars."""
    measured, _ = sections()
    closed = measured_closures(measured)
    first, last = date.fromisoformat(measured["first"]), date.fromisoformat(measured["last"])
    weekdays = [day for day in days(first, last) if day.weekday() < 5]

    assert len(measured["breaks"].split()) == 213
    assert len(closed) == 215
    assert len(weekdays) - len(closed) == int(measured["sessions"]) == 5798


def test_every_measured_day_is_closed_exactly_when_the_market_did_not_open() -> None:
    measured, _ = sections()
    closed = measured_closures(measured)
    first, last = date.fromisoformat(measured["first"]), date.fromisoformat(measured["last"])

    wrong = [
        day
        for day in days(first, last)
        if closures.US_EQUITY.closed(day) != (day.weekday() >= 5 or day in closed)
    ]

    assert first == closures.US_EQUITY.first
    assert wrong == []


def test_the_published_schedule_is_on_file_to_its_last_closure() -> None:
    """An early close is a session, and every weekday the schedule does not close is open."""
    measured, published = sections()
    after = date.fromisoformat(measured["last"])
    closed = {date.fromisoformat(row["date"]) for row in published if row["status"] == "closed"}
    early = {date.fromisoformat(row["date"]) for row in published if row["status"] == "early-close"}

    wrong = [
        day
        for day in days(after + DAY, closures.US_EQUITY.last)
        if closures.US_EQUITY.closed(day) != (day.weekday() >= 5 or day in closed)
    ]

    assert closures.US_EQUITY.last == max(closed)
    assert early and not any(closures.US_EQUITY.closed(day) for day in early)
    assert wrong == []


def test_nothing_is_closed_outside_the_span_on_file() -> None:
    """Past either end the calendar knows nothing, so it excuses nothing, weekends included."""
    before = closures.US_EQUITY.first - DAY * 4
    beyond = closures.US_EQUITY.last + DAY * 5

    assert before.weekday() >= 5 and beyond.weekday() >= 5
    assert not closures.US_EQUITY.closed(before)
    assert not closures.US_EQUITY.closed(beyond)


def test_every_row_of_the_table_is_a_weekday_inside_its_span() -> None:
    table = closures.US_EQUITY.closures

    assert len(table) == 225
    assert all(day.weekday() < 5 for day in table)
    assert min(table) >= closures.US_EQUITY.first
    assert max(table) == closures.US_EQUITY.last


def test_an_equity_reads_the_us_calendar_whatever_its_venue_is_spelled() -> None:
    """The venue is the operator's to spell, so the calendar is found by the asset class."""
    sunday = date(2021, 4, 18)
    found = closures.by_instrument([equity("SOXL.ARCA"), equity("SOXL.ARCX")])

    assert set(found) == {"SOXL.ARCA", "SOXL.ARCX"}
    assert all(closed(sunday) for closed in found.values())
    assert closures.calendar("EQUITY", "ARCA") is closures.US_EQUITY


def test_a_market_with_no_calendar_on_file_is_never_closed() -> None:
    pair = TestInstrumentProvider.default_fx_ccy("EUR/USD")
    [closed] = closures.by_instrument([pair]).values()

    assert closures.calendar("FX", "SIM") is None
    assert closures.closed_days("FX", "SIM") is closures.never
    assert not closed(date(2021, 4, 18))
    assert not closures.never(date(2021, 4, 18))
