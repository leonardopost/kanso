"""The New York clock: which part of a US session an instant is in, and which week.

A US equity trades from 04:00 to 20:00 New York time, and what a point means — whether an
entry is in regular hours, which part of the session a bar covers — is read off the New York
wall clock, which daylight saving moves against UTC twice a year. The harness reads a point's
segment and minute here and only here, so a sleeve and the shared book's ledger read one
clock. They read it at different instants: the ledger labels the instant it places an entry
at (`segment_of`), and a sleeve the minute its point covers (`session_of`). So the two part
at the bars stamped 09:30 and 16:00: a sleeve reads the first as `pre` and the second as
`rth`, and the ledger places an entry sent on the first in regular hours and refuses one sent
on the second as outside them while its leverage there is unset.

**Segments.** Regular hours (`rth`) run from 09:30 to 16:00, the pre-market (`pre`) from
04:00 and the post-market (`post`) to 20:00; anything else is `overnight`. No calendar is
read: a day the market was closed has segments like any other (`kanso.data.closures` holds
whole closed days and is not consulted here), and a day that closed early is `rth` to 16:00
all the same.

**An instant, or the minute a point covers.** `segment_of` and `minute_of` read the instant
they are handed. `session_of` reads the instant a nanosecond before it, because kanso stamps
a bar at its close (`kanso.nautilus.splits`), so that is the last minute the bar covers,
whatever its size. Of one-minute bars, the bar stamped 09:30:00 covers the minute from
09:29, a pre-market minute, and the first regular bar is the one stamped 09:31:00, minute
570 of its day; the bar stamped 16:00:00 is the last regular one, minute 959, and the one
stamped 20:00:00 the last of the post-market, minute 1199. A quote or a print stamped at
09:30:00.000 exactly is read the same way, as the instant before the open. A daily bar
stamped at the next New York midnight reads as minute 1439 of its own session's date. The
date is the New York date of the instant read, so an evening point that UTC stamps on the
next date keeps its session's.

**Weeks.** `week_opening` cuts time into absolute calendar weeks: each opens at the first
instant the New York clock reads a weekday and a minute of that day, and runs to the same
reading a week later. They are not cut from a window's start, so a window that opens
mid-week, or a run resumed inside one, sees the same weeks; and a closed Monday opens its
week at its own 04:00 like any other. A reading a change skips opens at the change, and one
it repeats at its first instant — the reading the `trading_hours` gate opens a session by
(`kanso.criteria.gates.first_reading`). So the two weeks either side of a change of clocks
are together an hour short in March and an hour long in November, and the hour falls on the
week that holds the change for every opening — Monday 04:00, `week_opening(ts, 0, minute=240)`,
among them — but a Sunday one past 02:00 and no later than 03:00, which the March change
skips or meets. Such an opening is the change itself, and the week before it is a minute
short for each minute the opening is past 02:00: at 03:00 the whole hour, and the week from
the change is a full seven days. `week_closing` reads a week's close off its opening alone, so
a week closes where the rule it opened under closes it, though that rule has moved since.

One rule of the harness reads a week here, a hypothesis's `period_target`, whose weeks
open at the reading it states (`KansoStrategy.period_stopped` in `kanso.nautilus.strategy`);
the periods a run's returns are cut into are not these weeks. The runner cuts those one
`[research] return_period` after another from the UTC midnight that opens the window
(`RunRequest.bounds` in `kanso.nautilus.backtest`), so a `1w` period of a window that opens
on a Monday starts at 00:00 UTC, which is the Sunday evening in New York: eight hours before
that Monday's 04:00 under daylight saving and nine without it. The two are the same weeks only
where no point falls between the two cuts: on US equities fed from 04:00 to 20:00 New York,
with no `overnight` point, for that opening and any other on a Monday no later than 04:00 or on
a Sunday, but not for one later that Monday, whose morning's points come between, nor for one
on Tuesday to Saturday, whose evening before holds post-market points past the UTC midnight; a
feed that carries `overnight` points puts the Sunday evening's between them too. `hyp validate`
holds the windows to the target's weekday under a return period of whole weeks, which is as far
as a check that reads no point can go.

**The zone stays in the package.** A researched `strategy.py` may not import `zoneinfo`
(`kanso.criteria.integrity`), and `kanso.nautilus.strategy`, which it may import, binds
from here only `session_of` and `Session` — a function of an integer and the plain values it
returns — so a strategy is handed New York time as values (`KansoStrategy.session`) and never
the zone it was read in. What a strategy can still reach past what it is handed — the zone's
class through `splits`, a timestamp the engine converts — is `docs/backlog.md` rows 253 and
254, and what its import can change in what every sleeve beside it is handed — a store on
`kanso.nautilus.strategy.session_of` or on `Session` — is row 256. So a harness rule reads
New York time as `segment_of` and `minute_of` give it, a plain tuple and an integer, and
never as an attribute of a `Session`.

This module relies on no fact of NautilusTrader (1.231.0) beyond the one every engine
timestamp carries: an instant is an integer of nanoseconds since the Unix epoch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final, NamedTuple
from zoneinfo import ZoneInfo

from kanso.criteria.gates import MINUTES_PER_DAY, first_reading
from kanso.errors import ValidationError

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
    "week_closing",
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

    For a point stamped at its close, that is the last minute it covers.
    """
    before = ts_ns - 1
    segment, day = segment_of(before)
    return Session(segment, day, minute_of(before))


def week_opening(ts_ns: int, weekday: int, *, minute: int) -> tuple[int, int]:
    """The New York week an instant falls in, as the half-open span `[opens, next_opens)`.

    A week opens at the first instant the New York clock reads `minute` (0 to 1439) past
    midnight on `weekday` (Monday 0, as `date.weekday` counts) or later, and the next opens
    at the same reading seven days on; both are nanoseconds since the epoch. `minute` counts
    minutes, as `minute_of` does, so Monday 04:00 is `week_opening(ts_ns, 0, minute=240)`. It
    is named at every call, because a wall time written as hours and minutes (`400` for
    04:00) is a minute count too — 06:40 — and would move every opening silently. A weekday
    or a minute out of those ranges is refused, never carried onto another day: `7` is not
    Monday, and `2359` (23:59 written as hours and minutes) is not the next day's 15:19.
    """
    if not 0 <= weekday <= 6:
        raise ValidationError(
            f"weekday {weekday} is not a weekday: Monday is 0 and Sunday 6",
            remedy="name the weekday as date.weekday counts it, 0 to 6",
        )
    if not 0 <= minute < MINUTES_PER_DAY:
        raise ValidationError(
            f"minute {minute} is not a minute of a day: 0 is midnight and 1439 is 23:59",
            remedy="name the minute past midnight, as minute_of counts it: 04:00 is 240",
        )
    _, today = segment_of(ts_ns)
    day = today - timedelta(days=(today.weekday() - weekday) % 7)
    opens = first_reading(day, minute, NEW_YORK)
    if opens > ts_ns:
        day -= _WEEK
        opens = first_reading(day, minute, NEW_YORK)
    return opens, first_reading(day + _WEEK, minute, NEW_YORK)


def week_closing(opening_ns: int) -> int:
    """The instant the week that opened at `opening_ns` closes: the first instant the New York
    clock reads that opening's weekday and minute seven days on.

    A week is read from its own opening, never from a rule, so one struck under a
    `period_target.opens` closes where that rule closed it however the rule has moved since
    (`kanso.nautilus.backtest.RunRequest.resumed`). It is the next opening `week_opening` gives
    for the rule the week opened under, for every opening that reads the rule's minute — every
    one but a Sunday opening a March change skips, past 02:00 and before 03:00, which opens at
    the change and reads 03:00, and which `kanso hyp validate` refuses as an `opens`.
    """
    _, day = segment_of(opening_ns)
    return first_reading(day + _WEEK, minute_of(opening_ns), NEW_YORK)
