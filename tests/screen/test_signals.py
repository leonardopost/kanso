"""A screen's signals: refused unless registered and numeric, read as of an instant and no later.

The tape is the suite's own synthetic custom type (`tests/screen/conftest.py`). Every point is
stated at a named instant, so each test reads its answer off its own arguments.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus import backtest
from kanso.schemas.screen import Screen
from kanso.screen import signals

from .conftest import TAPE_ID, ns, store, tape

WINDOW = (date(2026, 3, 2), date(2026, 3, 6))
DAY = 86_400_000_000_000


def screen(**declared: dict[str, str]) -> Screen:
    return Screen.model_validate(
        {
            "schema": 1,
            "id": "probe",
            "title": "probe",
            "thesis": "probe",
            "window": {"start": str(WINDOW[0]), "end": str(WINDOW[1])},
            "legs": {"a": {"instrument": "AAA.XNAS", "type": "bar", "resolution": "1m"}},
            "signals": declared
            or {
                "lvl": {"type": TAPE_ID, "field": "level", "instrument": "AAA.XNAS"},
                "flg": {"type": TAPE_ID, "field": "flag", "instrument": "AAA.XNAS"},
                "swt": {"type": TAPE_ID, "field": "switch", "instrument": "AAA.XNAS"},
            },
            "clock": {"hours": "overlap"},
            "measures": [
                {"id": "lead_lag", "from": "a", "to": "a", "estimator": "hy", "lags": ["1m"]}
            ],
        }
    )


@pytest.fixture
def held(tmp_path: Path, tape_cls: type) -> Any:
    """Two days' points of AAA and one of BBB, published at named instants."""
    points = [
        *tape(
            tape_cls,
            "AAA.XNAS",
            [
                (ns("2026-03-01T23:00:00+00:00"), 9.0, 1),  # before the window: never read
                (ns("2026-03-02T13:00:00+00:00"), 1.5, 0),
                (ns("2026-03-02T13:00:00+00:00"), 2.5, 1),
                (ns("2026-03-04T13:00:00+00:00"), -1.0, 0),
            ],
            events=[
                ns("2026-03-01T00:00:00+00:00"),
                ns("2026-03-02T00:00:00+00:00"),
                ns("2026-03-02T00:00:00+00:00"),
                ns("2026-03-04T00:00:00+00:00"),
            ],
        ),
        *tape(tape_cls, "BBB.XNAS", [(ns("2026-03-03T13:00:00+00:00"), 7.0, 1)]),
    ]
    return store(tmp_path / "catalog", points, ["AAA", "BBB"])


def test_a_registered_numeric_field_is_admitted_and_anything_else_refused(tape_cls: type) -> None:
    signals.check(screen())

    with pytest.raises(ValidationError, match=r"signals\.x\.type: 'nobody' is not a known") as one:
        signals.check(screen(x={"type": "nobody", "field": "level", "instrument": "AAA.XNAS"}))
    assert "kanso ext show" in (one.value.remedy or "")
    with pytest.raises(ValidationError, match="numeric fields are flag, level, switch"):
        signals.check(screen(x={"type": TAPE_ID, "field": "levels", "instrument": "AAA.XNAS"}))
    with pytest.raises(ValidationError, match=rf"{TAPE_ID}\.label is annotated str") as three:
        signals.check(screen(x={"type": TAPE_ID, "field": "label", "instrument": "AAA.XNAS"}))
    assert "float, int or bool" in (three.value.remedy or "")


def test_the_definitions_held_are_handed_over_and_none_is_required(held: Any) -> None:
    assert list(signals.definitions(held, screen())) == ["AAA.XNAS"]
    elsewhere = screen(x={"type": TAPE_ID, "field": "level", "instrument": "NONE.XNAS"})
    assert signals.definitions(held, elsewhere) == {}
    unsignalled = screen().model_copy(update={"signals": {}})
    assert signals.definitions(held, unsignalled) == {}


def test_a_value_is_read_as_of_each_instant_and_never_ahead(held: Any, tape_cls: type) -> None:
    asked = np.asarray(
        [
            ns("2026-03-04T13:00:00+00:00"),  # exactly at a point: read
            ns("2026-03-02T12:59:59+00:00"),  # before the window's first point: nothing
            ns("2026-03-03T20:00:00+00:00"),  # carried across a day with no AAA point
            ns("2026-03-02T13:00:00+00:00"),  # two points at one instant: the last handed over
        ],
        dtype=np.int64,
    )

    found = signals.asof(held, screen(), {"lvl": asked, "flg": asked[:1], "swt": asked}, WINDOW)

    handed = backtest._custom_points(held, tape_cls, ["AAA.XNAS"], *utc_day(WINDOW[0]))
    last = float(handed[-1].data.level)  # type: ignore[attr-defined]
    lvl = found["lvl"]
    assert lvl.value[0] == -1.0 and np.isnan(lvl.value[1])
    assert lvl.value[2] == last and lvl.value[3] == last
    assert lvl.ts_init.tolist() == [asked[0], -1, asked[3], asked[3]]
    assert lvl.ts_event.tolist() == [
        ns("2026-03-04T00:00:00+00:00"),
        -1,
        ns("2026-03-02T00:00:00+00:00"),
        ns("2026-03-02T00:00:00+00:00"),
    ]
    assert found["flg"].value.tolist() == [0.0]
    # A bool field is read as the number a comparison compares: 1 on, 0 off.
    switch = float(handed[-1].data.switch)  # type: ignore[attr-defined]
    assert switch in (0.0, 1.0) and found["swt"].value[0] == 0.0
    assert np.isnan(found["swt"].value[1]) and found["swt"].value[2:].tolist() == [switch] * 2


def utc_day(day: date) -> tuple[int, int]:
    """The first and last nanosecond of one UTC day."""
    start = ns(f"{day.isoformat()}T00:00:00+00:00")
    return start, start + DAY - 1


def test_one_read_a_day_answers_every_signal_of_a_type(
    held: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []
    real = backtest._custom_points

    def counted(catalog: Any, cls: type, universe: Any, start: int, end: int) -> Any:
        calls.append((start, end))
        assert list(universe) == ["AAA.XNAS"]
        return real(catalog, cls, universe, start, end)

    monkeypatch.setattr("kanso.screen.signals.backtest._custom_points", counted)
    last = np.asarray([ns("2026-03-04T15:00:00+00:00")], dtype=np.int64)

    signals.asof(held, screen(), {"lvl": last, "flg": last}, WINDOW)

    first = utc_day(WINDOW[0])[0]
    assert calls == [
        (first, first + DAY - 1),
        (first + DAY, first + 2 * DAY - 1),
        (first + 2 * DAY, int(last[0])),
    ]
    # Three UTC days to the last instant asked, read once for both signals, and nothing after it.


def test_signals_of_one_type_filed_under_two_instruments_are_read_together(
    held: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[list[str]] = []
    real = backtest._custom_points

    def counted(catalog: Any, cls: type, universe: Any, start: int, end: int) -> Any:
        asked.append(list(universe))
        return real(catalog, cls, universe, start, end)

    monkeypatch.setattr("kanso.screen.signals.backtest._custom_points", counted)
    two = screen(
        here={"type": TAPE_ID, "field": "level", "instrument": "AAA.XNAS"},
        there={"type": TAPE_ID, "field": "level", "instrument": "BBB.XNAS"},
    )
    last = np.asarray([ns("2026-03-04T15:00:00+00:00")], dtype=np.int64)

    found = signals.asof(held, two, {"here": last, "there": last}, WINDOW)

    # Each reads the points filed under its own instrument, from one read a day for both.
    assert found["here"].value.tolist() == [-1.0] and found["there"].value.tolist() == [7.0]
    assert found["there"].ts_init.tolist() == [ns("2026-03-03T13:00:00+00:00")]
    assert asked == [["AAA.XNAS", "BBB.XNAS"]] * 3


def test_nothing_is_read_for_no_instant_or_past_the_window(
    held: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: object) -> None:
        raise AssertionError("no instant asks for a point")

    monkeypatch.setattr("kanso.screen.signals.backtest._custom_points", forbidden)
    early = np.asarray([ns("2026-03-01T12:00:00+00:00")], dtype=np.int64)

    found = signals.asof(held, screen(), {"lvl": np.zeros(0, np.int64), "flg": early}, WINDOW)

    assert len(found["lvl"].value) == 0 and np.isnan(found["flg"].value[0])


def test_another_instrument_s_points_and_market_wide_ones_are_no_signal_s() -> None:
    def point(instrument: str | None, level: float, at: int) -> SimpleNamespace:
        data = SimpleNamespace(level=level)
        if instrument is not None:
            data.instrument_id = instrument
        return SimpleNamespace(data=data, ts_init=at, ts_event=at)

    points = [point("BBB.XNAS", 7.0, 1), point(None, 8.0, 2), point("AAA.XNAS", 3.0, 3)]

    start, end, stamps, values, events = signals._points(screen(), "lvl", points, 0, 10)

    assert (start, end) == (0, 10)
    assert stamps.tolist() == [3] and values.tolist() == [3.0] and events.tolist() == [3]
    for value in ("nan", "inf", "-inf"):
        with pytest.raises(PreconditionError, match=f"is {value} at ts_init 4 on AAA.XNAS") as one:
            signals._points(screen(), "lvl", [point("AAA.XNAS", float(value), 4)], 0, 10)
        assert "kanso data load --loader csv_parquet --spec <spec> --replace" in (
            one.value.remedy or ""
        )


INSTANTS = st.lists(st.integers(-50, 5 * 100 - 1), max_size=40)
"""Instants over five days of a hundred units each, and some before the first of them."""


@given(stamps=st.lists(st.integers(0, 5 * 100 - 1), max_size=40), asked=INSTANTS)
@settings(deadline=None)
def test_a_join_a_day_at_a_time_is_the_join_of_the_whole_series(
    stamps: list[int], asked: list[int]
) -> None:
    ts = np.asarray(sorted(stamps), dtype=np.int64)
    values = np.arange(len(ts), dtype=np.float64)
    days = []
    for start in range(0, 500, 100):
        inside = (ts >= start) & (ts < start + 100)
        days.append((start, start + 100, ts[inside], values[inside], ts[inside]))
    instants = np.asarray(asked, dtype=np.int64)

    found = signals.join(days, instants)

    index = np.searchsorted(ts, instants, side="right") - 1
    expected = [float(values[i]) if i >= 0 else np.nan for i in index]
    assert np.array_equal(found.value, np.asarray(expected), equal_nan=True)
    assert found.ts_init.tolist() == [int(ts[i]) if i >= 0 else -1 for i in index]
    assert found.ts_event.tolist() == found.ts_init.tolist()
