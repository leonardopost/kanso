"""A screen's sessions: which days, which hours, and which points, read the way a card reads."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kanso.errors import PreconditionError
from kanso.nautilus.backtest import market_points
from kanso.schemas.screen import Screen
from kanso.screen import sessions

from .conftest import bars, deltas, ns, quotes, store, trades

NEW_YORK = {"tz": "America/New_York", "span": "09:30-16:00"}


def screen(hours: Any = "overlap", **legs: dict[str, Any]) -> Screen:
    """A free screen over these legs, with one lead-lag measure so it is admissible."""
    legs = legs or {"a": {"instrument": "AAA.XNAS", "type": "trade"}}
    names = list(legs)
    return Screen.model_validate(
        {
            "schema": 1,
            "id": "probe",
            "title": "probe",
            "thesis": "probe",
            "window": {"start": "2026-03-02", "end": "2026-03-31"},
            "legs": legs,
            "clock": {"grid": "1m", "hours": hours},
            "measures": [
                {
                    "id": "lead_lag",
                    "from": names[0],
                    "to": names[-1],
                    "estimator": "hy",
                    "lags": ["1s"],
                }
            ],
        }
    )


def test_new_york_hours_move_with_daylight_saving_in_both_directions() -> None:
    spec = screen(NEW_YORK)

    # 2026-03-08 and 2026-11-01 are the two changes: the session opens an hour earlier in
    # UTC after the spring one and an hour later after the autumn one.
    assert sessions.hours_of(spec, date(2026, 3, 6)) == (
        ns("2026-03-06T14:30:00+00:00"),
        ns("2026-03-06T21:00:00+00:00"),
    )
    assert sessions.hours_of(spec, date(2026, 3, 9)) == (
        ns("2026-03-09T13:30:00+00:00"),
        ns("2026-03-09T20:00:00+00:00"),
    )
    assert sessions.hours_of(spec, date(2026, 10, 30))[0] == ns("2026-10-30T13:30:00+00:00")
    assert sessions.hours_of(spec, date(2026, 11, 2))[0] == ns("2026-11-02T14:30:00+00:00")


def test_overlap_reads_the_whole_utc_day() -> None:
    assert sessions.hours_of(screen(), date(2026, 3, 9)) == (
        ns("2026-03-09T00:00:00+00:00"),
        ns("2026-03-10T00:00:00+00:00"),
    )
    assert sessions.zone_of(screen()).key == "UTC"


def test_the_days_are_those_whose_hours_meet_the_window() -> None:
    utc = sessions.days(screen(), (date(2026, 3, 2), date(2026, 3, 4)))
    assert utc == [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]
    local = sessions.days(screen(NEW_YORK), (date(2026, 3, 2), date(2026, 3, 4)))
    assert local == [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)]
    # A whole New York day begins at 05:00Z, so the day before the window still meets it
    # under an all-day span, and every read is clamped to the window.
    all_day = screen({"tz": "America/New_York", "span": "00:00-24:00"})
    assert sessions.days(all_day, (date(2026, 3, 2), date(2026, 3, 2))) == [
        date(2026, 3, 1),
        date(2026, 3, 2),
    ]


def test_a_leg_is_the_points_a_card_is_handed_in_their_order(tmp_path: Path) -> None:
    # Two prints share each instant and the hour boundary, so an hour read must keep them
    # in file order exactly as one read of the span does.
    instants = [ns("2026-03-03T13:59:59+00:00"), ns("2026-03-03T14:00:00+00:00")]
    rows = [(ts, cents) for ts in instants for cents in (10_000, 10_007)]
    rows += [(ns("2026-03-03T15:30:00+00:00"), 10_003)]
    catalog = store(tmp_path, trades("AAA", rows), ["AAA"])
    spec = screen()
    held = sessions.definitions(catalog, spec)

    read = sessions.read(
        catalog, held, spec, ["a"], date(2026, 3, 3), (date(2026, 3, 2), date(2026, 3, 31))
    )

    whole = market_points(
        catalog,
        "trade",
        held["AAA.XNAS"],
        "",
        ns("2026-03-03T00:00:00+00:00"),
        ns("2026-03-04T00:00:00+00:00") - 1,
    )
    assert read["a"].ts.tolist() == [point.ts_init for point in whole]
    assert read["a"].price.tolist() == [float(point.price) for point in whole]
    assert read["a"].price.tolist() == [100.0, 100.07, 100.0, 100.07, 100.03]


def test_every_read_is_clamped_to_the_window(tmp_path: Path) -> None:
    # 19:00 New York on the window's last day is 00:00Z the day after: outside it.
    inside = ns("2026-03-31T19:59:00+00:00")
    after = ns("2026-04-01T00:30:00+00:00")
    catalog = store(tmp_path, bars("AAA", [(inside, 10_000), (after, 10_100)]), ["AAA"])
    spec = screen(
        {"tz": "America/New_York", "span": "00:00-24:00"},
        a={"instrument": "AAA.XNAS", "type": "bar", "resolution": "1m"},
    )
    held = sessions.definitions(catalog, spec)

    read = sessions.read(
        catalog, held, spec, ["a"], date(2026, 3, 31), (date(2026, 3, 2), date(2026, 3, 31))
    )

    assert read["a"].ts.tolist() == [inside]


def test_one_instrument_read_as_two_legs_is_read_once(tmp_path: Path) -> None:
    rows = [(ns("2026-03-03T14:00:00+00:00"), 10_000)]
    catalog = store(tmp_path, trades("AAA", rows), ["AAA"])
    spec = screen(
        a={"instrument": "AAA.XNAS", "type": "trade"},
        b={"instrument": "AAA.XNAS", "type": "trade"},
    )
    held = sessions.definitions(catalog, spec)

    read = sessions.read(
        catalog, held, spec, ["a", "b"], date(2026, 3, 3), (date(2026, 3, 2), date(2026, 3, 31))
    )

    assert read["a"] is read["b"]


def test_a_quote_leg_carries_its_touch_and_its_mid(tmp_path: Path) -> None:
    rows = [(ns("2026-03-03T14:00:00+00:00"), 9_998, 10_002)]
    catalog = store(tmp_path, quotes("AAA", rows), ["AAA"])
    spec = screen(a={"instrument": "AAA.XNAS", "type": "quote"})
    held = sessions.definitions(catalog, spec)

    read = sessions.read(
        catalog, held, spec, ["a"], date(2026, 3, 3), (date(2026, 3, 2), date(2026, 3, 31))
    )["a"]

    assert read.price.tolist() == [100.0]
    assert read.bid is not None and read.ask is not None
    assert (read.bid.tolist(), read.ask.tolist()) == ([99.98], [100.02])


def test_a_book_is_built_from_the_day_s_start_and_kept_inside_its_hours(tmp_path: Path) -> None:
    # The book is set at 08:00 New York, before the hours open, and moves at 10:00 twice in
    # one instant: the touch recorded is the one the instant left, never the one between.
    early = ns("2026-03-03T08:00:00-05:00")
    ten = ns("2026-03-03T10:00:00-05:00")
    rows = [
        (early, "add", "bid", 9_990, 10),
        (early, "add", "ask", 10_010, 10),
        (ten, "add", "ask", 10_005, 5),
        (ten, "delete", "ask", 10_005, 0),
        (ten, "add", "bid", 9_995, 3),
    ]
    catalog = store(tmp_path, deltas("AAA", rows), ["AAA"])
    spec = screen(NEW_YORK, a={"instrument": "AAA.XNAS", "type": "book"})
    held = sessions.definitions(catalog, spec)

    read = sessions.read(
        catalog, held, spec, ["a"], date(2026, 3, 3), (date(2026, 3, 2), date(2026, 3, 31))
    )["a"]

    assert read.ts.tolist() == [ten]
    assert read.bid is not None and read.ask is not None
    assert (read.bid.tolist(), read.ask.tolist()) == ([99.95], [100.10])
    assert read.price.tolist() == [pytest.approx(100.025)]


def test_a_leg_that_printed_nothing_is_empty(tmp_path: Path) -> None:
    catalog = store(tmp_path, trades("AAA", [(ns("2026-03-03T14:00:00+00:00"), 1)]), ["AAA", "BBB"])
    spec = screen(
        a={"instrument": "AAA.XNAS", "type": "trade"},
        b={"instrument": "BBB.XNAS", "type": "quote"},
        c={"instrument": "BBB.XNAS", "type": "book"},
        d={"instrument": "BBB.XNAS", "type": "bar", "resolution": "1m"},
    )
    held = sessions.definitions(catalog, spec)

    read = sessions.read(
        catalog,
        held,
        spec,
        ["a", "b", "c", "d"],
        date(2026, 3, 4),
        (date(2026, 3, 2), date(2026, 3, 31)),
    )

    assert all(series.empty for series in read.values())
    assert read["b"].bid is not None and read["d"].bid is None
    outside = sessions.read(
        catalog, held, spec, ["a", "b"], date(2026, 4, 2), (date(2026, 3, 2), date(2026, 3, 31))
    )
    assert outside["a"].empty and outside["b"].bid is not None


def test_a_leg_the_store_does_not_define_is_refused(tmp_path: Path) -> None:
    catalog = store(tmp_path, [], ["AAA"])
    spec = screen(a={"instrument": "ZZZ.SIM", "type": "trade"})

    with pytest.raises(PreconditionError, match="no definition for ZZZ.SIM") as refused:
        sessions.definitions(catalog, spec)
    assert "kanso data instruments resolve" in (refused.value.remedy or "")


def test_an_instant_is_exact_to_the_nanosecond() -> None:
    assert sessions.window_ns((date(2026, 3, 2), date(2026, 3, 2))) == (
        ns("2026-03-02T00:00:00+00:00"),
        ns("2026-03-03T00:00:00+00:00"),
    )
    assert np.int64(sessions.window_ns((date(1970, 1, 1), date(1970, 1, 1)))[1]) == 86_400 * 10**9


def test_a_book_with_one_side_has_no_touch_until_it_has_both(tmp_path: Path) -> None:
    ten = ns("2026-03-03T10:00:00-05:00")
    eleven = ns("2026-03-03T10:30:00-05:00")
    noon = ns("2026-03-03T10:45:00-05:00")
    rows = [
        (ten, "add", "bid", 9_990, 10),
        (eleven, "add", "bid", 9_991, 10),
        (noon, "add", "ask", 10_010, 10),
    ]
    catalog = store(tmp_path, deltas("AAA", rows), ["AAA"])
    spec = screen(NEW_YORK, a={"instrument": "AAA.XNAS", "type": "book"})
    held = sessions.definitions(catalog, spec)
    window = (date(2026, 3, 2), date(2026, 3, 31))

    read = sessions.read(catalog, held, spec, ["a"], date(2026, 3, 3), window)["a"]

    assert read.ts.tolist() == [noon]
    one_sided = store(tmp_path / "one", deltas("AAA", rows[:2]), ["AAA"])
    assert sessions.read(one_sided, held, spec, ["a"], date(2026, 3, 3), window)["a"].empty
