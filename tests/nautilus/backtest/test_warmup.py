"""The warmup prefix on the runner: resolved in the parent, delivered whole, measured after.

The window's own bounds are what they were; only the data fed before it widens. Every test
runs the real engine over real parquet, because the claims are about what the venue saw
before the open and what the extraction did with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kanso.criteria import Fill
from kanso.criteria.run import NS_PER_DAY, day_of, midnight_ns
from kanso.data.closures import US_EQUITY
from kanso.data.types import CorporateAction
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.backtest import (
    _equity,
    execute,
    folded,
    run,
    warmup_prefix,
    window_data,
)
from kanso.schemas import Hypothesis, Warmup

from .conftest import (
    CAPITAL,
    CERTIFICATION,
    INSTRUMENT,
    RESEARCH,
    SECOND_NS,
    SLEEVE,
    SYMBOL,
    VENUE,
    bars,
    catalog,
    hypothesis,
    instrument,
)

SESSIONS = 5
DECEMBER = (date(2023, 12, 1), date(2023, 12, 31))
PREFIX = (date(2023, 12, 27), date(2023, 12, 31))
"""The last five calendar days of December: what five sessions resolve to over a series of
daily bars that prints every day, each bar counted on the UTC day it is stamped, whatever the
market's calendar says of it."""

OTHER = "OTHR.XNAS"


def warmed(sessions: int = SESSIONS, **changes: object) -> Hypothesis:
    """The demo hypothesis, warming on this many sessions."""
    return hypothesis(**changes).model_copy(  # type: ignore[arg-type]
        update={"warmup": Warmup(sessions=sessions)}
    )


@pytest.fixture
def deep(tmp_path: Path) -> Path:
    """A catalog holding December as well as both windows, so a prefix can be resolved."""
    points = [*bars(DECEMBER), *bars(RESEARCH), *bars(CERTIFICATION)]
    return catalog(tmp_path / "deep", points, [instrument()])


# --- resolving the prefix -----------------------------------------------------


def test_a_hypothesis_without_a_warmup_resolves_no_prefix(deep: Path) -> None:
    assert warmup_prefix(hypothesis(), RESEARCH, deep) is None


def test_the_prefix_is_the_last_n_sessions_before_the_window(deep: Path) -> None:
    assert warmup_prefix(warmed(), RESEARCH, deep) == PREFIX


def test_a_session_is_a_day_that_printed_and_a_gap_is_not_one(tmp_path: Path) -> None:
    """Six sessions over a series with a three-day hole reach back past the hole."""
    printed = [
        *bars((date(2023, 12, 20), date(2023, 12, 24))),
        *bars((date(2023, 12, 28), date(2023, 12, 31))),
    ]
    store = catalog(tmp_path / "gapped", [*printed, *bars(RESEARCH)], [instrument()])

    assert warmup_prefix(warmed(6), RESEARCH, store) == (date(2023, 12, 23), date(2023, 12, 31))


def test_the_lookback_doubles_until_the_sessions_are_found(tmp_path: Path) -> None:
    """Two sessions asked for; the last two prints are twenty days back, past 2N and 4N."""
    far = bars((date(2023, 12, 10), date(2023, 12, 11)))
    store = catalog(tmp_path / "far", [*far, *bars(RESEARCH)], [instrument()])

    assert warmup_prefix(warmed(2), RESEARCH, store) == (date(2023, 12, 10), date(2023, 12, 11))


def test_too_few_sessions_inside_the_bound_is_a_refusal_naming_what_was_found(
    tmp_path: Path,
) -> None:
    store = catalog(
        tmp_path / "shallow",
        [*bars((date(2023, 12, 29), date(2023, 12, 31))), *bars(RESEARCH)],
        [instrument()],
    )

    with pytest.raises(PreconditionError) as refused:
        warmup_prefix(warmed(), RESEARCH, store)

    assert (
        "asks for 5 session(s) before 2024-01-01 and the catalog holds 3" in refused.value.message
    )
    assert "in the 160 calendar days before it" in refused.value.message
    assert "back to at least 2023-07-25" in (refused.value.remedy or "")
    assert "lower warmup.sessions" in (refused.value.remedy or "")


def test_a_clock_resolves_the_sessions_at_or_before_it(deep: Path) -> None:
    """A stage restart warms on what it replayed: the day its clock stands in counts once
    that day has printed, and not before."""
    from .conftest import CLOSE_NS

    tenth = midnight_ns(date(2024, 1, 10))
    published = tenth + CLOSE_NS + SECOND_NS

    before_the_print = warmup_prefix(warmed(3), RESEARCH, deep, until_ns=published - 1)
    at_the_print = warmup_prefix(warmed(3), RESEARCH, deep, until_ns=published)

    assert before_the_print == (date(2024, 1, 7), date(2024, 1, 9))
    assert at_the_print == (date(2024, 1, 8), date(2024, 1, 10))


def test_too_few_sessions_before_a_clock_names_the_clock(tmp_path: Path) -> None:
    store = catalog(tmp_path / "shallow", bars(RESEARCH), [instrument()])
    clock = midnight_ns(date(2024, 1, 3))

    with pytest.raises(PreconditionError) as refused:
        warmup_prefix(warmed(), RESEARCH, store, until_ns=clock)

    assert f"session(s) at or before 2024-01-03 ({clock})" in refused.value.message
    assert "the catalog holds 2" in refused.value.message


def test_an_unresolved_name_is_refused_before_any_lookback(deep: Path) -> None:
    with pytest.raises(PreconditionError, match="holds no definition for OTHR.XNAS"):
        warmup_prefix(warmed(universe=[INSTRUMENT, OTHER]), RESEARCH, deep)


def test_sessions_are_counted_on_quotes_when_the_hypothesis_requires_no_bars(
    tmp_path: Path,
) -> None:
    from .conftest import quotes

    store = catalog(tmp_path / "quoted", [*quotes(DECEMBER), *quotes(RESEARCH)], [instrument()])
    quoted = Hypothesis.model_validate(
        {
            **hypothesis().model_dump(by_alias=True, mode="json"),
            "resolution": "quote",
            "data_requirements": ["quote"],
            "warmup": {"sessions": 3},
        }
    )

    assert warmup_prefix(quoted, RESEARCH, store) == (date(2023, 12, 27), date(2023, 12, 29))


# --- a session is a day the market was open -------------------------------------

NEW_YORK = ZoneInfo("America/New_York")


def extended(first: date, last: date, kind: str = "bar", symbol: str = SYMBOL) -> list[object]:
    """One US equity's 1m bars, or its quotes or prints a minute apart, over 04:00 to 20:00 New
    York on each weekday from `first` to `last`, every point stamped at its minute's close with
    `ts_init == ts_event`: so the evening's end is stamped on the next UTC date — the bar
    closing at 20:00 alone in summer, those closing at 19:00 to 20:00 in winter.

    That is the vendor's stamping of bars, measured at kanso 0.20.0 in an operator's workspace
    over SOXL.ARCX's and SOXS.ARCX's 1m bars, loaded for 2022-09-01 to 2023-12-29 (the
    measurement was handed to this change without the date it was made on): the UTC day
    2022-09-03 held only the bar stamped 20:00 EDT on Friday 2022-09-02, and 2023-01-07 only
    the bars stamped 19:00 to 20:00 EST on Friday 2023-01-06. The synthetic loader stamps a
    bar at its close as the vendor's bar loader does, and a quote or a print at its own instant
    (`kanso.data.loaders.synthetic`); the post-market runs to 20:00 New York
    (`kanso.data.closures`), so a winter evening's last hour of quotes and prints is on the
    next UTC date as its bars are. It knows no holiday, serving every weekday as a session, so
    each range here is chosen to hold none, but for the tests that say they hold one."""
    from kanso.data.loaders.synthetic import SyntheticLoader

    loader = SyntheticLoader()
    (ref,) = loader.discover(
        {
            "loader": "synthetic",
            "seed": 3,
            "instruments": [symbol],
            "venue": VENUE,
            "resolution": "1m",
            "types": [kind],
            "start": first,
            "end": last,
            "session_start": "04:00",
            "session_end": "20:00",
        }
    )
    return list(loader.load(ref, ref.span))


def evening(day: date, hour: int, minute: int) -> int:
    """The instant of `hour:minute` in New York on `day`, in nanoseconds."""
    at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=NEW_YORK)
    return int(at.timestamp()) * SECOND_NS


def tail(points: Sequence[object], day: date) -> list[int]:
    """The instants of the points stamped on the UTC date `day`."""
    return [int(point.ts_init) for point in points if day_of(int(point.ts_init)) == day]  # type: ignore[attr-defined]


def ticked(kind: str, sessions: int) -> Hypothesis:
    """The demo hypothesis clocked on quotes or prints, warming on this many sessions."""
    return Hypothesis.model_validate(
        {
            **hypothesis().model_dump(by_alias=True, mode="json"),
            "resolution": kind,
            "data_requirements": [kind],
            "warmup": {"sessions": sessions},
        }
    )


SUMMER = (date(2022, 8, 29), date(2022, 9, 2), date(2022, 9, 6))
"""Monday to Friday before Labor Day 2022, and the Tuesday the window opens on after it."""
WINTER = (date(2023, 1, 3), date(2023, 1, 6), date(2023, 1, 9))
"""Tuesday to Friday after New Year's Day observed, 2023, and the Monday after."""


@pytest.mark.parametrize(
    ("days", "stamped"),
    [
        pytest.param(SUMMER, [(20, 0)], id="EDT: the bar closing at 20:00"),
        pytest.param(
            WINTER,
            [(19 + minute // 60, minute % 60) for minute in range(61)],
            id="EST: the bars closing at 19:00 to 20:00",
        ),
    ],
)
def test_a_friday_s_post_market_stamped_on_saturday_is_no_session(
    tmp_path: Path, days: tuple[date, date, date], stamped: list[tuple[int, int]]
) -> None:
    """The evening's end of a Friday is stamped on Saturday's UTC date, which the market did
    not open on: N sessions reach N weekdays back, and asked one more, the catalog holds N."""
    first, last, opens = days
    points = extended(first, last)
    saturday = last + timedelta(days=1)
    store = catalog(tmp_path / "extended", points, [instrument()])
    weekdays = (last - first).days + 1

    assert tail(points, saturday) == [evening(last, *at) for at in stamped]
    assert saturday.weekday() == 5
    assert warmup_prefix(warmed(weekdays, resolution="1m"), (opens, opens), store) == (first, last)
    with pytest.raises(PreconditionError, match=f"the catalog holds {weekdays} in"):
        warmup_prefix(warmed(weekdays + 1, resolution="1m"), (opens, opens), store)


@pytest.mark.parametrize(
    ("first", "last", "minutes"),
    [
        pytest.param(date(2023, 11, 20), date(2023, 11, 22), 61, id="Thanksgiving, EST"),
        pytest.param(date(2024, 3, 25), date(2024, 3, 28), 1, id="Good Friday, EDT"),
    ],
)
def test_the_evening_before_a_holiday_stamped_on_the_holiday_is_no_session(
    tmp_path: Path, first: date, last: date, minutes: int
) -> None:
    """The evening's end of the day before a weekday holiday is stamped on the holiday's UTC
    date, and a day the market did not open adds nothing, whatever it holds."""
    points = extended(first, last)
    holiday = last + timedelta(days=1)
    store = catalog(tmp_path / "holiday", points, [instrument()])
    opens = holiday + timedelta(days=1)
    weekdays = (last - first).days + 1

    assert US_EQUITY.closed(holiday) and holiday.weekday() < 5
    assert len(tail(points, holiday)) == minutes
    assert warmup_prefix(warmed(weekdays, resolution="1m"), (opens, opens), store) == (first, last)
    with pytest.raises(PreconditionError, match=f"the catalog holds {weekdays} in"):
        warmup_prefix(warmed(weekdays + 1, resolution="1m"), (opens, opens), store)


@pytest.mark.parametrize("kind", ["quote", "trade"])
def test_quotes_and_prints_stamped_on_saturday_are_no_session_either(
    tmp_path: Path, kind: str
) -> None:
    """A sleeve clocked on quotes or prints counts its sessions off the files' `ts_init`
    column (`_days_filed`), and the market's closed days are taken out of those the same way."""
    first, last, opens = WINTER
    points = extended(first, last, kind)
    store = catalog(tmp_path / kind, points, [instrument()])

    assert len(tail(points, last + timedelta(days=1))) == 61
    assert warmup_prefix(ticked(kind, 4), (opens, opens), store) == (first, last)
    with pytest.raises(PreconditionError, match="the catalog holds 4 in"):
        warmup_prefix(ticked(kind, 5), (opens, opens), store)


def weekdays(first: date, last: date) -> list[date]:
    """Every weekday from `first` to `last`."""
    every = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return [day for day in every if day.weekday() < 5]


def new_york_day(stamp: int) -> date:
    """The New York date of the minute a point stamped at its close covers."""
    return datetime.fromtimestamp((stamp - 1) // SECOND_NS, tz=NEW_YORK).date()


@pytest.mark.parametrize(
    ("first", "last"),
    [
        pytest.param(date(2022, 8, 22), date(2022, 9, 2), id="EDT"),
        pytest.param(date(2023, 1, 23), date(2023, 2, 3), id="EST"),
    ],
)
@pytest.mark.parametrize(
    ("hour", "minute", "back", "begun"),
    [
        pytest.param(20, 0, 0, False, id="the evening's last bar"),
        pytest.param(12, 0, 0, True, id="a bar at noon"),
        pytest.param(4, 0, 1, False, id="before the day's first bar"),
    ],
)
def test_a_stage_restart_re_feeds_n_whole_sessions_on_every_night_of_the_week(
    tmp_path: Path,
    request_for,
    first: date,
    last: date,
    hour: int,
    minute: int,
    back: int,
    begun: bool,
) -> None:
    """A stage restart warms on the sessions at or before its clock, the instant its node last
    reached. On the bar a US equity's evening ends on, stamped past 00:00 UTC on the next
    date, that evening's day is the last of them on every night of the week, Friday's
    included: the next date holds nothing of its own session at the clock, so it is no
    session, and N sessions re-feed N whole ones. A clock inside a day counts the day, whose
    morning is re-fed beside N - 1 whole sessions, and one before the day's first bar the
    day before. The window opens on the clock's day and the request takes the prefix as it
    is."""
    points = extended(first, last)
    store = catalog(tmp_path / "extended", points, [instrument()])
    hyp = warmed(3, resolution="1m")
    sessions = weekdays(first, last)
    for at, day in enumerate(sessions[-5:], start=len(sessions) - 5):
        clock = evening(day, hour, minute)
        window = (day_of(clock), day_of(clock) + timedelta(days=6))
        warmed_on = sessions[at - back - 2 : at - back + 1]

        prefix = warmup_prefix(hyp, window, store, until_ns=clock)

        assert prefix == (warmed_on[0], warmed_on[-1]), day
        opens, _ = request_for(window, prefix=prefix, hypothesis_=hyp).delivered
        stamps = [int(point.ts_init) for point in points]  # type: ignore[attr-defined]
        fed = {new_york_day(stamp) for stamp in stamps if opens <= stamp <= clock}
        whole = {
            session
            for session in fed
            if all(opens <= stamp <= clock for stamp in stamps if new_york_day(stamp) == session)
        }
        assert whole == set(warmed_on[:-1] if begun else warmed_on), day
        assert warmed_on[-1] in fed, day
    assert len(tail(points, last + timedelta(days=1))) == (1 if first.month == 8 else 61)


def test_a_name_with_nothing_before_the_window_adds_no_session(tmp_path: Path) -> None:
    """A name the catalog holds only from the window's open — listed, or loaded, after the
    sessions it would warm on — adds none of them, and the prefix is the other name's."""
    first, last, opens = WINTER
    points = [*extended(first, last), *extended(opens, opens, symbol="OTHR")]
    store = catalog(tmp_path / "late", points, [instrument(), instrument("OTHR")])
    both = [INSTRUMENT, OTHER]

    assert warmup_prefix(warmed(4, universe=both, resolution="1m"), (opens, opens), store) == (
        first,
        last,
    )


def test_a_day_the_market_was_closed_adds_nothing_whatever_is_dated_to_it(
    tmp_path: Path,
) -> None:
    """The synthetic loader knows no holiday and serves Martin Luther King Jr. Day, 2023-01-16,
    as any weekday (`kanso.data.loaders.synthetic`): its bars there stand for whatever a source
    holds on a day the calendar marks closed, which no vendor measured for this change served.
    Dated to that day in New York, they add no session, so four reach back over it to the
    Thursday before, and a fifth is not there."""
    first, last = date(2023, 1, 12), date(2023, 1, 18)
    points = extended(first, last)
    store = catalog(tmp_path / "holiday", points, [instrument()])
    opens = last + timedelta(days=1)
    holiday = date(2023, 1, 16)

    assert US_EQUITY.closed(holiday)
    assert sum(new_york_day(int(point.ts_init)) == holiday for point in points) == 960  # type: ignore[attr-defined]
    assert warmup_prefix(warmed(4, resolution="1m"), (opens, opens), store) == (first, last)
    with pytest.raises(PreconditionError, match="the catalog holds 4 in"):
        warmup_prefix(warmed(5, resolution="1m"), (opens, opens), store)


def test_outside_the_span_on_file_every_day_reads_open_and_the_zone_still_dates_each_point(
    tmp_path: Path,
) -> None:
    """Past the last day the calendar is on file for, 2027-09-06, no day is closed, as for
    coverage, so Thanksgiving 2027 is a session of a series that holds bars on it; but the zone
    is no dated fact, and the Friday's last hour, stamped on Saturday's UTC date, is still the
    Friday's, so three days are three sessions and a fourth is not there."""
    first, last = date(2027, 11, 24), date(2027, 11, 26)
    points = extended(first, last)
    store = catalog(tmp_path / "beyond", points, [instrument()])
    opens = date(2027, 11, 29)

    assert first > US_EQUITY.last and not US_EQUITY.closed(date(2027, 11, 25))
    assert len(tail(points, last + timedelta(days=1))) == 61
    assert warmup_prefix(warmed(3, resolution="1m"), (opens, opens), store) == (first, last)
    with pytest.raises(PreconditionError, match="the catalog holds 3 in"):
        warmup_prefix(warmed(4, resolution="1m"), (opens, opens), store)


def perpetual_bars(first: date, last: date, resolution: str) -> list[object]:
    """A perpetual's bars round the clock, every day from `first` to `last`, as the synthetic
    loader makes them under its `continuous` calendar: each stamped at its close."""
    from kanso.data.loaders.synthetic import SyntheticLoader

    loader = SyntheticLoader()
    (ref,) = loader.discover(
        {
            "loader": "synthetic",
            "seed": 11,
            "instruments": ["BTCUSDT-PERP"],
            "venue": "SIM",
            "resolution": resolution,
            "start": first,
            "end": last,
            "start_price": 10_000.0,
            "price_precision": 1,
            "calendar": "continuous",
        }
    )
    return list(loader.load(ref, ref.span))


def test_a_market_with_no_calendar_counts_every_day_it_printed_weekends_included(
    tmp_path: Path,
) -> None:
    """A perpetual's venue keeps no calendar on file (`closures.never`), so its days are UTC's:
    its hourly bars over New Year's week, Sunday the 31st to Saturday the 6th, are seven
    sessions, both weekends' days among them. The last bar is stamped at 00:00 UTC on Sunday
    the 7th and covers Saturday's last hour, so it is Saturday's, as the bar a stage restart's
    clock stands on at a midnight is the day's it closed."""
    from .conftest import PERP, perpetual

    points = perpetual_bars(date(2023, 12, 31), date(2024, 1, 6), "1h")
    store = catalog(tmp_path / "perp", points, [perpetual()])
    opens = date(2024, 1, 8)
    week = [date(2023, 12, 31) + timedelta(days=offset) for offset in range(7)]
    midnight = midnight_ns(date(2024, 1, 3))

    assert {day_of(int(point.ts_init)) for point in points} == {*week, date(2024, 1, 7)}  # type: ignore[attr-defined]
    assert tail(points, date(2024, 1, 7)) == [midnight_ns(date(2024, 1, 7))]
    assert warmup_prefix(warmed(7, universe=[PERP], resolution="1h"), (opens, opens), store) == (
        week[0],
        week[-1],
    )
    with pytest.raises(PreconditionError, match="the catalog holds 7 in"):
        warmup_prefix(warmed(8, universe=[PERP], resolution="1h"), (opens, opens), store)
    assert midnight in [int(point.ts_init) for point in points]  # type: ignore[attr-defined]
    restarted = warmed(2, universe=[PERP], resolution="1h")
    window = (date(2024, 1, 3), date(2024, 1, 9))
    assert warmup_prefix(restarted, window, store, until_ns=midnight) == (
        date(2024, 1, 1),
        date(2024, 1, 2),
    )


def test_a_day_counts_when_any_name_printed_on_it_while_its_own_market_was_open(
    tmp_path: Path,
) -> None:
    """An equity's 1m bars from Tuesday to Friday beside a perpetual's on the Sunday alone:
    the perpetual's Sunday counts, and the Saturday that holds the equity's Friday evening, on
    which nothing else printed, does not, so five sessions reach back to the Tuesday and a
    sixth is not there."""
    from .conftest import PERP, perpetual

    first, last, opens = WINTER
    sunday = last + timedelta(days=2)
    points = [*extended(first, last), *perpetual_bars(sunday, sunday, "1m")]
    store = catalog(tmp_path / "mixed", points, [instrument(), perpetual()])
    both = [INSTRUMENT, PERP]

    assert sunday.weekday() == 6 and len(tail(points, last + timedelta(days=1))) == 61
    assert warmup_prefix(warmed(5, universe=both, resolution="1m"), (opens, opens), store) == (
        first,
        sunday,
    )
    with pytest.raises(PreconditionError, match="the catalog holds 5 in"):
        warmup_prefix(warmed(6, universe=both, resolution="1m"), (opens, opens), store)


# --- a bar of a day is its session's, wherever it is stamped ----------------------


def daily(first: date, last: date) -> tuple[list[date], list[object]]:
    """One US equity's sessions from `first` to `last` and their daily bars, each stamped as
    kanso's vendor loaders stamp one: at the close of the vendor's window, which opens at
    midnight New York, so at the next New York midnight — 05:00 UTC in winter, 04:00 in
    summer — and on the UTC date after the session's own.

    The window's opening is measured: for the consolidated US tape it is midnight New York,
    05:00Z in winter and 04:00Z in summer (`docs/backlog.md` row 20), and the vendor's bulk
    loader records one session's measured at 2026-08-03 04:00Z, in nanoseconds in its store
    and in milliseconds in its REST answer (`kanso.data.adapters.massive.loaders.bulk`). A
    bar is stamped at its window's start plus its size, with `ts_init == ts_event`
    (`build_bar` in the vendor's bar loader). The sessions are the days `kanso.data.closures`
    holds open, which were measured as the weekdays SPY printed a daily bar on."""
    from nautilus_trader.model.data import Bar
    from nautilus_trader.model.objects import Price, Quantity

    from .conftest import bar_type

    sessions = [
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
        if not US_EQUITY.closed(first + timedelta(days=offset))
    ]
    made: list[object] = []
    for index, day in enumerate(sessions):
        opened = datetime(day.year, day.month, day.day, tzinfo=NEW_YORK)
        stamp = (int(opened.timestamp()) + 86_400) * SECOND_NS
        close = Price(10.0 + 0.25 * (index % 4), 2)
        made.append(
            Bar(bar_type(), close, close, close, close, Quantity.from_int(10_000), stamp, stamp)
        )
    return sessions, made


DAILY = [
    pytest.param(date(2023, 1, 3), date(2023, 1, 20), date(2023, 1, 13), id="MLK Day, EST"),
    pytest.param(date(2023, 3, 27), date(2023, 4, 14), date(2023, 4, 6), id="Good Friday, EDT"),
    pytest.param(date(2023, 11, 13), date(2023, 12, 1), date(2023, 11, 22), id="Thanksgiving"),
]
"""Three weeks of sessions ending on a Friday, and the session before a holiday inside them."""


@pytest.mark.parametrize(("first", "last", "eve"), DAILY)
def test_n_daily_bars_are_n_sessions_though_a_friday_s_is_stamped_on_saturday(
    tmp_path: Path, request_for, first: date, last: date, eve: date
) -> None:
    """A Friday's daily bar is stamped on Saturday's UTC date and the bar of the session
    before a holiday on the holiday's, both days the market was closed; each is its session's
    only point and counts there, so N sessions before the Monday after deliver exactly the
    last N daily bars, for every N the catalog holds, and one more is refused."""
    sessions, points = daily(first, last)
    store = catalog(tmp_path / "daily", points, [instrument()])
    window = (last + timedelta(days=3), last + timedelta(days=9))
    stamped = {day: day_of(int(bar.ts_init)) for day, bar in zip(sessions, points, strict=True)}  # type: ignore[attr-defined]

    assert stamped[last] == last + timedelta(days=1) and US_EQUITY.closed(stamped[last])
    assert stamped[eve] == eve + timedelta(days=1) and US_EQUITY.closed(stamped[eve])
    for wanted in range(1, len(sessions) + 1):
        hyp = warmed(wanted)
        prefix = warmup_prefix(hyp, window, store)
        assert prefix is not None
        opens, _ = request_for(window, prefix=prefix, hypothesis_=hyp).delivered
        fed = [day for day, bar in zip(sessions, points, strict=True) if int(bar.ts_init) >= opens]  # type: ignore[attr-defined]
        assert fed == sessions[-wanted:], wanted
    with pytest.raises(PreconditionError, match=f"the catalog holds {len(sessions)} in"):
        warmup_prefix(warmed(len(sessions) + 1), window, store)


def test_a_clock_on_a_friday_s_daily_bar_replays_n_daily_bars(tmp_path: Path, request_for) -> None:
    """A stage restart whose clock stands on Friday's daily bar, stamped at 05:00 UTC on
    Saturday, warms on the sessions at or before it: that Saturday is the Friday's, and five
    reach back over Martin Luther King Jr. Day to the Friday before, five bars."""
    sessions, points = daily(date(2023, 1, 3), date(2023, 1, 20))
    store = catalog(tmp_path / "daily", points, [instrument()])
    clock = int(points[-1].ts_init)  # type: ignore[attr-defined]
    window = (day_of(clock), day_of(clock) + timedelta(days=6))
    hyp = warmed(5)

    prefix = warmup_prefix(hyp, window, store, until_ns=clock)

    assert prefix == (date(2023, 1, 14), date(2023, 1, 21))
    opens, _ = request_for(window, prefix=prefix, hypothesis_=hyp).delivered
    replayed = [
        day for day, bar in zip(sessions, points, strict=True) if opens <= int(bar.ts_init) <= clock
    ]  # type: ignore[attr-defined]
    assert replayed == [date(2023, 1, 13), *(date(2023, 1, day) for day in (17, 18, 19, 20))]


SPAN_DAYS = 6


@st.composite
def _filed(draw: st.DrawFn) -> tuple[str, dict[str, list[list[object]]], int, int]:
    """Quotes or prints of one or two names over six days, each name's points in one to three
    files with disjoint spans, some at a midnight or a nanosecond before one; and a span
    whose ends may fall on a point, before the first or after the last."""
    from nautilus_trader.model.data import QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide
    from nautilus_trader.model.identifiers import InstrumentId, TradeId
    from nautilus_trader.model.objects import Price, Quantity

    kind = draw(st.sampled_from(("quote", "trade")))
    base = midnight_ns(date(2023, 12, 26))
    offsets = st.sampled_from((0, 1, NS_PER_DAY // 2, NS_PER_DAY - 1)) | st.integers(
        0, NS_PER_DAY - 1
    )
    filed: dict[str, list[list[object]]] = {}
    instants: list[int] = []
    for name in draw(st.sampled_from(((INSTRUMENT,), (INSTRUMENT, OTHER)))):
        stamps = sorted(
            {
                base + day * NS_PER_DAY + offset
                for day, offset in draw(
                    st.lists(st.tuples(st.integers(0, SPAN_DAYS - 1), offsets), max_size=12)
                )
            }
        )
        if not stamps:
            continue
        instants.extend(stamps)
        ident = InstrumentId.from_str(name)
        points: list[object] = [
            QuoteTick(
                ident,
                Price(9.99, 2),
                Price(10.01, 2),
                Quantity.from_int(100),
                Quantity.from_int(100),
                ts_event=stamp,
                ts_init=stamp,
            )
            if kind == "quote"
            else TradeTick(
                ident,
                Price(10.0, 2),
                Quantity.from_int(100),
                AggressorSide.BUYER,
                TradeId(f"T-{index}"),
                ts_event=stamp,
                ts_init=stamp,
            )
            for index, stamp in enumerate(stamps)
        ]
        cuts = sorted(draw(st.sets(st.integers(1, max(1, len(points) - 1)), max_size=2)))
        bounds = [0, *(cut for cut in cuts if cut < len(points)), len(points)]
        filed[name] = [points[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]
    ends = st.sampled_from(instants) if instants else st.just(base)
    start = draw(ends | st.integers(base - NS_PER_DAY, base + SPAN_DAYS * NS_PER_DAY))
    end = draw(ends | st.integers(start, base + (SPAN_DAYS + 1) * NS_PER_DAY))
    return kind, filed, start, max(start, end)


@settings(max_examples=40, deadline=None)
@given(drawn=_filed())
def test_the_days_counted_are_the_days_of_the_points_a_query_returns(
    drawn: tuple[str, dict[str, list[list[object]]], int, int],
) -> None:
    """Over quotes or prints in several files a name, the days a warmup counts, read off the
    files' `ts_init` alone, are the New York days of the points the window's own read returns
    over the same span, each the date of the instant before its stamp as the harness's clock
    reads it (`kanso.nautilus.clock.session_of`), less the weekend of 30 and 31 December,
    which the US equity market did not open on: a point at a UTC midnight, or a nanosecond
    before one, is the New York evening's before."""
    import tempfile

    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    from kanso.nautilus.backtest import _catalog, _held, _primary_days, market_points
    from kanso.nautilus.clock import session_of

    kind, filed, start, end = drawn
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "catalog"
        root.mkdir()
        store = ParquetDataCatalog(str(root))
        store.write_data([instrument(), instrument("OTHR")])
        for batches in filed.values():
            for batch in batches:
                store.write_data(batch)
        names = list(filed) or [INSTRUMENT]
        quoted = Hypothesis.model_validate(
            {
                **hypothesis(universe=names).model_dump(by_alias=True, mode="json"),
                "resolution": kind,
                "data_requirements": [kind],
            }
        )
        catalog_ = _catalog(root)
        held = _held(catalog_, quoted)

        counted = _primary_days(catalog_, quoted, held, kind, start, end)
        returned = {
            session_of(int(point.ts_init)).day  # type: ignore[attr-defined]
            for name in names
            for point in market_points(catalog_, kind, held[name], kind, start, end)
        }

    assert counted == {day for day in returned if not US_EQUITY.closed(day)}


def test_a_quote_file_gone_after_the_lookback_listed_it_is_counted_as_the_catalog_holds_it_now(
    tmp_path: Path,
) -> None:
    """A lookback's catalog keeps the listing it first read (`_catalog`). A quote file that has
    gone by its next read is a miss, listed afresh and read again (`_read_listed`), as the
    window's own query misses it: the days counted are those the catalog holds now, as a read
    listing afresh returns them, and nothing is raised."""
    import os

    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    from kanso.nautilus.backtest import _catalog, _held, _primary_days, market_points

    from .conftest import quotes

    root = tmp_path / "catalog"
    root.mkdir()
    store = ParquetDataCatalog(str(root))
    store.write_data([instrument()])
    store.write_data(quotes((DECEMBER[0], date(2023, 12, 15))))
    store.write_data(quotes((date(2023, 12, 16), DECEMBER[1])))
    quoted = Hypothesis.model_validate(
        {
            **hypothesis().model_dump(by_alias=True, mode="json"),
            "resolution": "quote",
            "data_requirements": ["quote"],
        }
    )
    span = (midnight_ns(DECEMBER[0]), midnight_ns(DECEMBER[1] + timedelta(days=1)) - 1)
    lookback = _catalog(root)
    held = _held(lookback, quoted)

    before = _primary_days(lookback, quoted, held, "quote", *span)
    listed = lookback.get_file_list_from_data_cls(QuoteTick)
    os.remove(max(listed))
    after = _primary_days(lookback, quoted, held, "quote", *span)
    returned = {
        day_of(int(point.ts_init))  # type: ignore[attr-defined]
        for point in market_points(_catalog(root), "quote", held[INSTRUMENT], "quote", *span)
    }

    def opened(days: range) -> set[date]:
        return {
            day
            for day in (DECEMBER[0] + timedelta(days=offset) for offset in days)
            if not US_EQUITY.closed(day)
        }

    assert len(listed) == 2
    assert before == opened(range(31))
    assert after == {day for day in returned if not US_EQUITY.closed(day)} == opened(range(15))


# --- the request ---------------------------------------------------------------


def test_a_prefix_ends_before_the_window_opens_or_on_the_day_it_opens(request_for) -> None:
    """A stage restart's window opens on its clock's day, and the prefix may end there."""
    with pytest.raises(ValidationError, match="is not a span of sessions before the window"):
        request_for(RESEARCH, prefix=(date(2023, 12, 27), date(2024, 1, 2)))
    with pytest.raises(ValidationError, match="is not a span of sessions before the window"):
        request_for(RESEARCH, prefix=(date(2023, 12, 31), date(2023, 12, 27)))

    restarted = request_for(RESEARCH, prefix=(date(2023, 12, 30), date(2024, 1, 1)))

    assert restarted.span == (date(2023, 12, 30), RESEARCH[1])
    assert restarted.bounds == request_for(RESEARCH).bounds


def test_the_delivered_span_widens_the_lower_bound_and_only_that(request_for) -> None:
    cold = request_for(RESEARCH)
    warm = request_for(RESEARCH, prefix=PREFIX)

    assert warm.bounds == cold.bounds
    assert warm.span == (PREFIX[0], RESEARCH[1])
    assert warm.delivered == (midnight_ns(PREFIX[0]), cold.bounds[1])
    assert cold.delivered == cold.bounds


# --- loading and checking ------------------------------------------------------


def test_the_prefix_is_loaded_with_the_window_and_nothing_before_it(
    deep: Path, request_for
) -> None:
    _, groups = window_data(request_for(RESEARCH, prefix=PREFIX), deep)

    assert sum(len(group) for group in groups) == 31 + SESSIONS
    first = min(int(point.ts_init) for group in groups for point in group)
    assert first >= midnight_ns(PREFIX[0])


def test_a_point_after_the_window_is_still_refused_with_a_prefix_declared(
    deep: Path, request_for
) -> None:
    """The embargo: only the lower bound widens."""
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, _ = window_data(request, deep)

    with pytest.raises(PreconditionError, match="lies outside the requested window"):
        execute(request, instruments, [tuple(bars(CERTIFICATION))])


def test_a_point_before_the_prefix_is_refused_too(deep: Path, request_for) -> None:
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, _ = window_data(request, deep)

    with pytest.raises(PreconditionError, match="lies outside the requested window 2023-12-27"):
        execute(request, instruments, [tuple(bars(DECEMBER))])


def test_a_prefix_with_nothing_measured_after_it_is_not_a_run(deep: Path, request_for) -> None:
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, _ = window_data(request, deep)

    with pytest.raises(PreconditionError, match="holds nothing for demo_mr over 2024-01-01"):
        execute(request, instruments, [tuple(bars(PREFIX))])


def test_an_unscheduled_split_inside_the_prefix_is_refused(deep: Path, request_for) -> None:
    """A split adjusts no position in the prefix, but the venue restates the book at it."""
    ex = midnight_ns(date(2023, 12, 29))
    split = CorporateAction(
        instrument_id=instrument().id,
        kind="split",
        ratio=0.1,
        cash=0.0,
        currency="USD",
        ex_date_ns=ex,
        ts_event=ex,
        ts_init=ex,
    )
    request = request_for(RESEARCH, prefix=PREFIX)
    instruments, groups = window_data(request, deep)

    with pytest.raises(PreconditionError, match="split effective 2023-12-29"):
        execute(request, instruments, [*groups, (split,)])


def test_the_parent_refuses_a_split_inside_the_prefix_before_any_child_runs(
    tmp_path: Path, request_for, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child would refuse it too, but as a crash the run records; the parent's own
    check over the delivered span's corporate actions, read before the child is started, is
    what makes it a refusal the operator reads."""
    from kanso.nautilus import backtest

    ex = midnight_ns(date(2023, 12, 29))
    split = CorporateAction(
        instrument_id=instrument().id,
        kind="split",
        ratio=0.1,
        cash=0.0,
        currency="USD",
        ex_date_ns=ex,
        ts_event=ex,
        ts_init=ex,
    )
    store = catalog(tmp_path / "split", [*bars(DECEMBER), *bars(RESEARCH), split], [instrument()])
    hyp = hypothesis(data_requirements=("bar", "corporate_action"))
    monkeypatch.setattr(
        backtest.subprocess, "Popen", lambda *_a, **_k: pytest.fail("a card was started")
    )
    lane = tmp_path / "lane"
    lane.mkdir()

    with pytest.raises(PreconditionError, match="split effective 2023-12-29"):
        backtest.run_subprocess(request_for(RESEARCH, prefix=PREFIX, hypothesis_=hyp), store, lane)


# --- what the run measures -----------------------------------------------------


def test_the_strategy_is_fed_the_prefix_and_measured_from_the_open(deep: Path, request_for) -> None:
    cold = run(request_for(RESEARCH), deep)
    warm = run(request_for(RESEARCH, prefix=PREFIX), deep)
    opens, closes = warm.run.bounds

    assert warm.run.window == RESEARCH
    assert len(warm.run.period_ends_ns) == 31
    assert all(opens <= ts < closes for ts in warm.run.period_ends_ns)
    assert all(opens <= fill.ts_ns < closes for fill in warm.run.fills)
    # The demo sleeve enters every sixth bar it sees, counting from the first: cold, the
    # first is the window's first day; warmed on five sessions, it is the second.
    assert cold.intents[0][0] == bars(RESEARCH)[0].ts_event
    assert warm.intents[0][0] == bars(RESEARCH)[1].ts_event


def test_the_last_prefix_print_marks_the_first_measured_period(tmp_path: Path, request_for) -> None:
    """A name bought at the open off its prefix print is worth that print, not nothing."""
    source = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    pass


class Strategy(KansoStrategy):
    """Buys the other name on the first bar of the window it sees, whatever printed."""

    config_cls = Config

    def on_start(self):
        self.bought = False

    def on_bar(self, bar):
        if not self.bought:
            self.bought = self.submit_entry("OTHR.XNAS", "BUY", notional=1_000.0) is not None
'''
    points = [*bars(PREFIX), *bars(RESEARCH), *bars(PREFIX, "OTHR")]
    store = catalog(tmp_path / "two", points, [instrument(), instrument("OTHR")])
    hyp = warmed(universe=[INSTRUMENT, OTHER])
    request = request_for(RESEARCH, source=source, hypothesis_=hyp, prefix=PREFIX)

    card = run(request, store).run
    (fill,) = card.fills

    assert fill.instrument_id == OTHER
    assert fill.ts_ns >= card.bounds[0], "filled at the open, against the prefix print"
    assert card.equity[0] == pytest.approx(CAPITAL - fill.cost)


def test_a_fill_before_the_open_is_refused_by_the_extraction(request_for) -> None:
    request = request_for(RESEARCH, prefix=PREFIX)
    early = Fill(
        ts_ns=midnight_ns(PREFIX[1]), instrument_id=INSTRUMENT, side="BUY", qty=1, px=10, cost=0
    )

    with pytest.raises(ValidationError, match="precedes the window opening"):
        _equity(
            request,
            folded(request, [(midnight_ns(RESEARCH[0]), INSTRUMENT, 10.0, 9.0, 11.0)]),
            [early],
            {},
        )


def test_a_warmed_card_runs_in_its_child_on_the_span_it_was_handed(
    deep: Path, tmp_path: Path, request_for
) -> None:
    from kanso.nautilus.backtest import run_subprocess

    lane = tmp_path / "lane"
    lane.mkdir()

    result = run_subprocess(request_for(RESEARCH, prefix=PREFIX, source=SLEEVE), deep, lane)

    assert result.crashed is False
    assert len(result.run.period_ends_ns) == 31
    assert result.intents[0][0] == bars(RESEARCH)[1].ts_event


def test_the_prefix_does_not_reach_the_certification_window_either(
    deep: Path, tmp_path: Path, request_for
) -> None:
    """A warmed card still refuses the window that judges it."""
    from kanso.nautilus.backtest import run_subprocess

    lane = tmp_path / "lane"
    lane.mkdir()
    before = (CERTIFICATION[0] - timedelta(days=SESSIONS), CERTIFICATION[0] - timedelta(days=1))

    with pytest.raises(PreconditionError, match="certification window"):
        run_subprocess(request_for(CERTIFICATION, prefix=before), deep, lane)
