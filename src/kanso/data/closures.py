"""Closures: the days a market does not open, dated facts held beside its tick conventions.

Coverage is counted in whole UTC days, and a dataset's span runs from the first day that
carried data to the last. A market serves nothing on a day it does not open, so between
two datasets of one series a weekend or a holiday can leave days that no source could
ever fill — and a chunked backfill leaves some wherever a chunk's edge meets a closure.
**A day the market was closed is not a hole.** This module says which days those are, and
`kanso.data.manifest` counts coverage by it.

**Held here because nothing kanso depends on holds them.** A market's closures are its
exchanges' published holidays and, when something extraordinary happens, days closed by
order at a day's notice. The engine ships no trading calendar and kanso takes no calendar
dependency, so the closures are dated facts in the package, keyed exactly as
`kanso.data.conventions` keys a tick schedule: by asset class and venue, with the wildcard
`ANY` for a rule that binds every venue of a class, and a venue's own entry winning over
it. The one calendar on file is keyed `("EQUITY", ANY)` for the reason the tick schedule is:
the equity conventions kanso ships are the US market's, whose exchanges all close on the
same days, and the venue in an instrument id is the operator's to spell — `ARCX` or
`ARCA` for the same exchange — so a calendar found by one spelling would read every other
as a market that never closes.

**What is on file is what was measured.** US equities are closed on Saturdays and Sundays
and on the weekdays below, from 2003-09-10 to 2027-09-06. To 2026-09-25 the weekdays are
measured, not recalled: they are every weekday on which SPY, which trades on every US
equity venue, printed no daily bar, and no weekend day carried one. From there to
2027-09-06 they are the closures the exchanges have published. Every entry follows one
rule: New Year's Day, Martin Luther King Jr. Day, Washington's Birthday, Good Friday,
Memorial Day, Juneteenth from 2022, Independence Day, Labor Day, Thanksgiving and
Christmas, each moved to the Friday before when it falls on a Saturday and to the Monday
after on a Sunday — except New Year's Day on a Saturday, which closes nothing — and the
days closed by order: 2004-06-11, 2007-01-02, 2012-10-29 and -30, 2018-12-05 and
2025-01-09. `tests/data/catalog/fixtures/us_equity_sessions.txt` is the record every date is
checked against.

**Exact or nothing.** Outside the span on file, and for an asset class and venue with no
calendar on file, no day is closed, so coverage there is what it always was. That is the
direction in which a wrong answer costs a refusal, where the other direction would let a
snapshot pin a hole: a day called open that was closed breaks coverage, a day called
closed that was open hides a gap. So a closure ordered after this table was written reads
as a trading day, and is refused, until it is appended; extending the table is appending
dates and moving `last`, a data change rather than a code change.

**A calendar is kept in the market's own dates; coverage counts UTC days.** A US session,
04:00 to 20:00 in New York, falls inside its own UTC date except for the evening's end —
the last hour in winter, the last minute's close in summer — which is why a Friday's
post-market is stamped on Saturday. So a UTC day is closed when the market's day of the
same date was: what the evening tail of a trading day carries into the next UTC date
belongs to a day that was open, and is served, or missing, with it.

NautilusTrader facts (nautilus_trader 1.231.0): an instrument definition's `asset_class` is
an `AssetClass` member whose `name` spells the class as `instruments.yaml` does (`EQUITY`),
and `id.venue.value` is the venue as the instrument id spells it. The engine carries no
holiday or trading calendar of its own.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from kanso.data.conventions import ANY

Closed = Callable[[date], bool]
"""Whether a market was closed on a day: what coverage asks of each day no span holds."""


def never(day: date) -> bool:
    """False for every day: the closures of a market with no calendar on file."""
    return False


@dataclass(frozen=True, slots=True)
class Calendar:
    """The days one market does not open, over the span its closures are known for.

    `weekend` holds the weekdays the market never opens on, Monday being 0, and `closures`
    every other day it did not open. Both are facts only from `first` to `last`, so outside
    them `closed` answers false. `authority` is who set the dates, in words, so whatever
    reports a closure can say where it came from.
    """

    authority: str
    first: date
    last: date
    weekend: frozenset[int]
    closures: frozenset[date]

    def closed(self, day: date) -> bool:
        """Whether the market was closed on `day`; never outside the span on file."""
        return self.first <= day <= self.last and (
            day.weekday() in self.weekend or day in self.closures
        )


US_EQUITY_CLOSURES: Final[dict[int, str]] = {
    2003: "11-27 12-25",
    2004: "01-01 01-19 02-16 04-09 05-31 06-11 07-05 09-06 11-25 12-24",
    2005: "01-17 02-21 03-25 05-30 07-04 09-05 11-24 12-26",
    2006: "01-02 01-16 02-20 04-14 05-29 07-04 09-04 11-23 12-25",
    2007: "01-01 01-02 01-15 02-19 04-06 05-28 07-04 09-03 11-22 12-25",
    2008: "01-01 01-21 02-18 03-21 05-26 07-04 09-01 11-27 12-25",
    2009: "01-01 01-19 02-16 04-10 05-25 07-03 09-07 11-26 12-25",
    2010: "01-01 01-18 02-15 04-02 05-31 07-05 09-06 11-25 12-24",
    2011: "01-17 02-21 04-22 05-30 07-04 09-05 11-24 12-26",
    2012: "01-02 01-16 02-20 04-06 05-28 07-04 09-03 10-29 10-30 11-22 12-25",
    2013: "01-01 01-21 02-18 03-29 05-27 07-04 09-02 11-28 12-25",
    2014: "01-01 01-20 02-17 04-18 05-26 07-04 09-01 11-27 12-25",
    2015: "01-01 01-19 02-16 04-03 05-25 07-03 09-07 11-26 12-25",
    2016: "01-01 01-18 02-15 03-25 05-30 07-04 09-05 11-24 12-26",
    2017: "01-02 01-16 02-20 04-14 05-29 07-04 09-04 11-23 12-25",
    2018: "01-01 01-15 02-19 03-30 05-28 07-04 09-03 11-22 12-05 12-25",
    2019: "01-01 01-21 02-18 04-19 05-27 07-04 09-02 11-28 12-25",
    2020: "01-01 01-20 02-17 04-10 05-25 07-03 09-07 11-26 12-25",
    2021: "01-01 01-18 02-15 04-02 05-31 07-05 09-06 11-25 12-24",
    2022: "01-17 02-21 04-15 05-30 06-20 07-04 09-05 11-24 12-26",
    2023: "01-02 01-16 02-20 04-07 05-29 06-19 07-04 09-04 11-23 12-25",
    2024: "01-01 01-15 02-19 03-29 05-27 06-19 07-04 09-02 11-28 12-25",
    2025: "01-01 01-09 01-20 02-17 04-18 05-26 06-19 07-04 09-01 11-27 12-25",
    2026: "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
    2027: "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06",
}
"""The weekdays no US equity venue opened on, by year, as `MM-DD`.

2003 begins at 2003-09-10 and 2027 ends at 2027-09-06, the span `US_EQUITY` holds them for.
One row per year so a year reads against the exchanges' own published list at a glance."""

US_EQUITY: Final = Calendar(
    authority=(
        "the US equity exchanges' published holidays, which every one of them observes on "
        "the same days, and the days they closed by order"
    ),
    first=date(2003, 9, 10),
    last=date(2027, 9, 6),
    weekend=frozenset({5, 6}),
    closures=frozenset(
        date.fromisoformat(f"{year}-{day}")
        for year, days in US_EQUITY_CLOSURES.items()
        for day in days.split()
    ),
)

CALENDARS: Final[dict[tuple[str, str], Calendar]] = {("EQUITY", ANY): US_EQUITY}
"""Every calendar on file, keyed by asset class and venue as the tick schedules are."""


def calendar(asset_class: str, venue: str) -> Calendar | None:
    """The calendar `venue` keeps for `asset_class`: its own, else its class's, else none."""
    return CALENDARS.get((asset_class, venue)) or CALENDARS.get((asset_class, ANY))


def closed_days(asset_class: str, venue: str) -> Closed:
    """Whether a day is closed for `asset_class` on `venue`; `never` with no calendar on file."""
    found = calendar(asset_class, venue)
    return never if found is None else found.closed


def by_instrument(definitions: Iterable[Any]) -> dict[str, Closed]:
    """The closures of each engine instrument definition, keyed by its qualified id.

    A definition is what the store holds, so an instrument it does not define has no entry
    here, and whoever reads one for it reads `never`.
    """
    return {
        str(item.id): closed_days(item.asset_class.name, item.id.venue.value)
        for item in definitions
    }
