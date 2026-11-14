"""The New York clock: which part of a US session an instant is in, and which week.

A US equity trades from 04:00 to 20:00 New York time, and what a point means — whether an
entry is in regular hours, which part of the session a bar covers, which week it falls in —
is read off the New York wall clock, which daylight saving moves against UTC twice a year.
The harness reads it here and only here, so a sleeve and the shared book's ledger label one
instant alike.

**Segments.** Regular hours (`rth`) run from 09:30 to 16:00, the pre-market (`pre`) from
04:00 and the post-market (`post`) to 20:00; anything else is `overnight`. No calendar is
read: a day the market was closed has segments like any other (`kanso.data.closures` holds
whole closed days and is not consulted here), and a day that closed early is `rth` to 16:00
all the same.

**An instant, or the minute a point covers.** `segment_of` and `minute_of` read the instant
they are handed. `session_of` reads the instant a nanosecond before it, because kanso stamps
a bar at its close (`kanso.nautilus.splits`): the bar stamped 09:30:00 covers the minute
from 09:29, a pre-market minute, and the first regular bar is the one stamped 09:31:00,
minute 570 of its day; the bar stamped 16:00:00 is the last regular one, minute 959, and the
one stamped 20:00:00 the last of the post-market, minute 1199. A quote or a print stamped at
09:30:00.000 exactly is read the same way, as the instant before the open. A daily bar
stamped at the next New York midnight reads as minute 1439 of its own session's date. The
date is the New York date of the instant read, so an evening point that UTC stamps on the
next date keeps its session's.

**Weeks.** `week_opening` cuts time into absolute calendar weeks: each opens at the first
instant the New York clock reads a weekday and a minute of that day, and runs to the same
reading a week later. They are not cut from a window's start, so a window that opens
mid-week, or a run resumed inside one, sees the same weeks; a closed Monday opens its week
at its own 04:00 like any other; and the week that holds a change of clocks is an hour short
in March and an hour long in November. A reading a change skips opens at the change, and
one it repeats at its first instant — the reading the `trading_hours` gate opens a session
by (`kanso.criteria.gates.first_reading`).

**The zone stays in the package.** A researched `strategy.py` may not import `zoneinfo`
(`kanso.criteria.integrity`), and `kanso.nautilus.strategy`, which it may import, binds
from here only `session_of` and `Session` — a function of an integer and the plain values it
returns — so a strategy is handed New York time as values (`KansoStrategy.session`) and never
the zone it was read in.

This module relies on no fact of NautilusTrader (1.231.0) beyond the one every engine
timestamp carries: an instant is an integer of nanoseconds since the Unix epoch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final, NamedTuple
from zoneinfo import ZoneInfo

from kanso.criteria.gates import first_reading

__all__ = [
    "NEW_YORK",
    "OVERNIGHT",
    "POST",
    "PRE",
    "RTH",
    "Session",
    "minute_of",
    "segment_of",
    "session_of",
    "week_opening",
]

NEW_YORK: Final = ZoneInfo("America/New_York")
RTH: Final = "rth"
PRE: Final = "pre"
POST: Final = "post"
OVERNIGHT: Final = "overnight"
_SEGMENTS: Final = (
    (4 * 3600, PRE),
    (9 * 3600 + 1800, RTH),
    (16 * 3600, POST),
    (20 * 3600, OVERNIGHT),
)
"""Each segment from the New York second of the day it opens at; before 04:00 is overnight."""

_NS: Final = 1_000_000_000
_WEEK: Final = timedelta(days=7)


class Session(NamedTuple):
    """Where in its New York session a minute falls.

    `segment` is `pre`, `rth`, `post` or `overnight`; `day` is the New York date; `minute`
    is the minute of that day, 0 at midnight and 1439 at 23:59.
    """

    segment: str
    day: date
    minute: int


def segment_of(ts_ns: int) -> tuple[str, date]:
    """Which segment of the session an instant is in, and the New York date of its session.

    Regular hours are 09:30 to 16:00 New York time, the pre-market from 04:00 and the
    post-market to 20:00; anything else is overnight.
    """
    local = datetime.fromtimestamp(ts_ns // _NS, tz=NEW_YORK)
    second = local.hour * 3600 + local.minute * 60 + local.second
    found = OVERNIGHT
    for opens, name in _SEGMENTS:
        if second >= opens:
            found = name
    return found, local.date()


def minute_of(ts_ns: int) -> int:
    """The New York minute of the day an instant is in: 0 at midnight, 1439 at 23:59."""
    local = datetime.fromtimestamp(ts_ns // _NS, tz=NEW_YORK)
    return local.hour * 60 + local.minute


def session_of(ts_ns: int) -> Session:
    """The segment, New York date and minute of the instant a nanosecond before `ts_ns`.

    For a point stamped at its close, that is the minute it covers.
    """
    before = ts_ns - 1
    segment, day = segment_of(before)
    return Session(segment, day, minute_of(before))


def week_opening(ts_ns: int, weekday: int, minute: int) -> tuple[int, int]:
    """The New York week an instant falls in, as the half-open span `[opens, next_opens)`.

    A week opens at the first instant the New York clock reads `minute` (0 to 1439) past
    midnight on `weekday` (Monday 0, as `date.weekday` counts) or later, and the next opens
    at the same reading seven days on; both are nanoseconds since the epoch.
    """
    _, today = segment_of(ts_ns)
    day = today - timedelta(days=(today.weekday() - weekday) % 7)
    opens = first_reading(day, minute, NEW_YORK)
    if opens > ts_ns:
        day -= _WEEK
        opens = first_reading(day, minute, NEW_YORK)
    return opens, first_reading(day + _WEEK, minute, NEW_YORK)
