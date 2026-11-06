"""Calendar demeaning: least squares on a declared design, by Gram–Schmidt and with no BLAS."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kanso.data.closures import US_EQUITY
from kanso.schemas.screen import TurnOfMonth
from kanso.screen import demean


def weekdays(start: date, count: int) -> list[date]:
    """`count` days from `start` the US equity calendar was open."""
    found: list[date] = []
    day = start
    while len(found) < count:
        if not US_EQUITY.closed(day):
            found.append(day)
        day += timedelta(days=1)
    return found


def test_the_intercept_alone_takes_out_the_mean() -> None:
    days = weekdays(date(2024, 1, 2), 5)
    returns = np.asarray([1.0, 2.0, 3.0, 4.0, 10.0])
    matrix, names = demean.design(days, [], {}, None)

    left, kept, dropped = demean.residuals(matrix, names, returns)

    assert names == kept == ["intercept"] and dropped == []
    assert left.tolist() == pytest.approx((returns - 4.0).tolist())


def test_weekday_and_month_dummies_take_the_smallest_present_as_their_baseline() -> None:
    days = [date(2024, 1, 3), date(2024, 1, 4), date(2024, 2, 5), date(2024, 2, 7)]

    matrix, names = demean.design(days, ["weekday", "month"], {}, None)

    assert names == ["intercept", "weekday:2", "weekday:3", "month:2"]
    assert matrix[:, 1].tolist() == [1.0, 0.0, 0.0, 1.0]
    assert matrix[:, 2].tolist() == [0.0, 1.0, 0.0, 0.0]
    assert matrix[:, 3].tolist() == [0.0, 0.0, 1.0, 1.0]


def test_turn_of_month_third_friday_week_and_pre_holiday_read_the_calendar() -> None:
    days = [
        date(2024, 2, 1),  # first trading day of February
        date(2024, 2, 5),  # third
        date(2024, 2, 6),  # fourth
        date(2024, 2, 29),  # last
        date(2024, 3, 11),  # the Monday of the week of the third Friday, the 15th
        date(2024, 3, 18),  # the Monday after it
        date(2024, 3, 28),  # the Thursday before Good Friday, March's last trading day
        date(2024, 11, 27),  # the day before Thanksgiving
        date(2024, 8, 30),  # the Friday before Labor Day, August's last trading day
        date(2024, 8, 23),  # a Friday before an ordinary weekend
    ]
    items = [TurnOfMonth(turn_of_month=(1, 3)), "third_friday_week", "pre_holiday"]

    matrix, names = demean.design(days, items, {}, US_EQUITY)

    assert names == ["intercept", "turn_of_month", "third_friday_week", "pre_holiday"]
    assert matrix[:, 1].tolist() == [1, 1, 0, 1, 0, 0, 1, 0, 1, 0]
    assert matrix[:, 2].tolist() == [0, 0, 0, 0, 1, 0, 0, 0, 0, 0]
    assert matrix[:, 3].tolist() == [0, 0, 0, 0, 0, 0, 1, 1, 1, 0]
    closed = demean.design([date(2024, 3, 29)], items[:1], {}, US_EQUITY)[0]
    assert closed[:, 1].tolist() == [0.0], "a closed day is no trading day of its month"
    sunday = demean.design([date(2024, 3, 17)], items[1:2], {}, US_EQUITY)[0]
    assert sunday[:, 1].tolist() == [1.0], "the week runs Monday to Sunday"


def test_a_control_enters_as_its_value_and_a_copy_or_a_constant_is_dropped_by_name() -> None:
    days = weekdays(date(2024, 1, 2), 10)
    event = np.asarray([1, 0, 0, 0, 0, 1, 0, 0, 0, 0], dtype=np.float64)
    copy = np.asarray([day.weekday() == 1 for day in days], dtype=np.float64)
    matrix, names = demean.design(
        days, ["weekday"], {"event": event, "tuesday": copy, "never": np.zeros(10)}, None
    )

    left, kept, dropped = demean.residuals(matrix, names, np.arange(10, dtype=np.float64))

    assert names[-3:] == ["control:event", "control:tuesday", "control:never"]
    assert dropped == ["control:tuesday", "control:never"], "declared order decides which goes"
    assert "control:event" in kept and "weekday:1" in kept
    for name in kept:
        column = matrix[:, names.index(name)]
        assert abs(float(np.add.reduce(column * left))) < 1e-9


def test_r2_reads_what_the_design_shares_with_the_state() -> None:
    days = weekdays(date(2024, 1, 2), 8)
    state = np.asarray([1, 1, 0, 0, 1, 1, 0, 0], dtype=np.float64)
    alone = demean.design(days, [], {}, None)
    spanned = demean.design(days, [], {"same": state}, None)

    assert demean.r2(*alone, state) == pytest.approx(0.0, abs=1e-12), "nothing attenuated"
    assert demean.r2(*spanned, state) == pytest.approx(1.0)
    assert demean.r2(*alone, np.ones(8)) is None, "a state that never varies has no R²"
    assert demean.r2(*demean.design([], [], {}, None), np.zeros(0)) is None


def test_no_blas_is_reached_and_the_bits_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*_: object, **__: object) -> None:
        raise AssertionError("demeaning calls no BLAS")

    for name in ("dot", "matmul", "inner", "vdot"):
        monkeypatch.setattr(np, name, refused)
    for name in ("lstsq", "qr", "solve", "pinv"):
        monkeypatch.setattr(np.linalg, name, refused)
    days = weekdays(date(2024, 1, 2), 60)
    matrix, names = demean.design(days, ["weekday", "month"], {}, None)
    returns = np.random.default_rng(3).normal(size=60)

    one = demean.residuals(matrix, names, returns)[0]
    two = demean.residuals(matrix.copy(), list(names), returns.copy())[0]

    assert one.tobytes() == two.tobytes()
    source = Path(demean.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]
    assert " @ " not in code and "np.dot" not in code and "linalg" not in code


@given(
    seed=st.integers(0, 10_000),
    rows=st.integers(3, 40),
    columns=st.integers(1, 6),
)
@settings(max_examples=60, deadline=None)
def test_residuals_are_orthogonal_to_every_column_kept(seed: int, rows: int, columns: int) -> None:
    rng = np.random.default_rng(seed)
    matrix = np.column_stack(
        [np.ones(rows), *(rng.integers(0, 2, size=rows).astype(float) for _ in range(columns))]
    )
    names = ["intercept", *(f"control:c{n}" for n in range(columns))]
    target = rng.normal(size=rows) * 100

    left, kept, dropped = demean.residuals(matrix, names, target)

    assert sorted(kept + dropped) == sorted(names)
    scale = float(np.sqrt(np.add.reduce(target * target))) + 1.0
    for name in kept:
        column = matrix[:, names.index(name)]
        assert abs(float(np.add.reduce(column * left))) <= 1e-9 * scale * rows
