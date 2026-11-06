"""Demeaning a condition's returns on the calendar, by least squares and with no BLAS.

A follower's session returns carry the calendar — a weekday's mean, a month's, the turn of
the month, the week of the monthly expiry, the session before a holiday — and a state that
happens to lean on the calendar would read its effect as the state's. So a `condition` cell's
returns are first regressed on a design of calendar facts and the workspace's own 0/1
controls, and the cell is measured on what is left.

**The design**, one row a scored session, its columns in this order: the intercept; each
declared calendar entry; each declared control. `weekday` and `month` are dummies of the
session's local date, one for each value present but the smallest present, the baseline the
intercept absorbs. `turn_of_month: [l, m]` is one where the session is among the last `l` or the
first `m` trading days of its calendar month. `third_friday_week` is one from the Monday to the
Sunday of the week holding the month's third Friday. `pre_holiday` is one where the first day
after the session that is not a weekend day is a day the market was closed — the Thursday
before Good Friday, the day before Thanksgiving, a day before a closure by order. A trading day
is a weekday outside the calendar's weekend that is not one of its closures, read off the
calendar on file for the follower (`kanso.data.closures`), which the measure refuses to read a
turn of the month or a holiday without (`kanso.screen.condition`). A control is its value.

**Least squares by modified Gram–Schmidt**, in the design's column order, each column
orthogonalised twice against the ones kept before it. A column with nothing left — constant
where an earlier column already is, or a copy of what earlier ones span, to within `TOL` of its
own size — is dropped, and its name recorded: a weekday no scored session fell on has no dummy,
and a control that never varies says nothing the intercept does not. The residuals are the
returns less their projection on the kept columns.

**What the residuals estimate.** The state is not in the regression, so a null that moves a
cell's positions never refits it. In sample, a contrast of the residuals between two states is
the least-squares coefficient on the state indicator in a regression on it and the design,
times `1 − R²` of the indicator on the design; `r2` reports that R², so how much the calendar
shared with the state is visible, and with the intercept alone the two estimates are one.

**No BLAS.** Every inner product is `numpy.add.reduce` of an elementwise product in a fixed
order: nothing here calls `@`, `numpy.dot` or `numpy.linalg`, so one host gives the same bits
every time.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Final

import numpy as np

from kanso.data.closures import Calendar
from kanso.schemas.screen import CalendarControl, TurnOfMonth

TOL: Final = 1e-9
"""The share of its own squared length a column must keep, orthogonalised, to stay in the
design: a numerical constant of the arithmetic, not a decision about the data."""


def design(
    dates: Sequence[date],
    demean: Sequence[CalendarControl],
    controls: Mapping[str, np.ndarray],
    calendar: Calendar | None,
) -> tuple[np.ndarray, list[str]]:
    """The design matrix over `dates`, one row a date, and the name of each column."""
    columns: list[np.ndarray] = [np.ones(len(dates), dtype=np.float64)]
    names = ["intercept"]
    for item in demean:
        if isinstance(item, TurnOfMonth):
            assert calendar is not None
            columns.append(_turn(dates, *item.turn_of_month, calendar))
            names.append("turn_of_month")
        elif item == "weekday":
            _dummies(dates, lambda day: day.weekday(), "weekday", columns, names)
        elif item == "month":
            _dummies(dates, lambda day: day.month, "month", columns, names)
        elif item == "third_friday_week":
            columns.append(_flags(dates, _expiry_week))
            names.append("third_friday_week")
        else:
            assert calendar is not None
            columns.append(_flags(dates, lambda day: _before_closure(day, calendar)))
            names.append("pre_holiday")
    for name, values in controls.items():
        columns.append(np.asarray(values, dtype=np.float64))
        names.append(f"control:{name}")
    return np.column_stack(columns), names


def residuals(
    matrix: np.ndarray, names: Sequence[str], target: np.ndarray
) -> tuple[np.ndarray, list[str], list[str]]:
    """`target` less its least-squares projection on the design's columns, the names of the
    columns kept and of those dropped."""
    basis, kept, dropped = _basis(matrix, names)
    return _residual(basis, np.asarray(target, dtype=np.float64)), kept, dropped


def r2(matrix: np.ndarray, names: Sequence[str], indicator: np.ndarray) -> float | None:
    """The centred R² of `indicator` on the design; `None` when it does not vary."""
    values = np.asarray(indicator, dtype=np.float64)
    if not len(values):
        return None
    centred = values - _dot(values, np.ones(len(values))) / len(values)
    total = _dot(centred, centred)
    if total == 0.0:
        return None
    basis, _, _ = _basis(matrix, names)
    left = _residual(basis, values)
    return min(max(1.0 - _dot(left, left) / total, 0.0), 1.0)


def _basis(
    matrix: np.ndarray, names: Sequence[str]
) -> tuple[list[np.ndarray], list[str], list[str]]:
    vectors: list[np.ndarray] = []
    kept: list[str] = []
    dropped: list[str] = []
    for index, name in enumerate(names):
        column = np.ascontiguousarray(matrix[:, index], dtype=np.float64)
        left = _residual(vectors, column)
        size = _dot(left, left)
        if size <= TOL * _dot(column, column):
            dropped.append(name)
            continue
        vectors.append(left / math.sqrt(size))
        kept.append(name)
    return vectors, kept, dropped


def _residual(vectors: Sequence[np.ndarray], target: np.ndarray) -> np.ndarray:
    """`target` with its projection on each orthonormal vector taken out, twice, in order."""
    left = target.copy()
    for _ in range(2):
        for vector in vectors:
            left = left - _dot(vector, left) * vector
    return left


def _dot(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.add.reduce(a * b))


def _dummies(
    dates: Sequence[date],
    key: Callable[[date], int],
    label: str,
    columns: list[np.ndarray],
    names: list[str],
) -> None:
    """One column per value present but the smallest, the baseline the intercept holds."""
    values = np.asarray([key(day) for day in dates], dtype=np.int64)
    for value in sorted(set(values.tolist()))[1:]:
        columns.append((values == value).astype(np.float64))
        names.append(f"{label}:{value}")


def _flags(dates: Sequence[date], rule: Callable[[date], bool]) -> np.ndarray:
    return np.asarray([1.0 if rule(day) else 0.0 for day in dates], dtype=np.float64)


def _turn(dates: Sequence[date], last: int, first: int, calendar: Calendar) -> np.ndarray:
    """One for a date among the last `last` or the first `first` trading days of its month."""
    months: dict[tuple[int, int], list[date]] = {}
    found: list[float] = []
    for day in dates:
        key = (day.year, day.month)
        if key not in months:
            months[key] = _trading_days(day.year, day.month, calendar)
        trading = months[key]
        if day not in trading:
            found.append(0.0)
            continue
        rank = trading.index(day)
        found.append(1.0 if rank < first or rank >= len(trading) - last else 0.0)
    return np.asarray(found, dtype=np.float64)


def _trading_days(year: int, month: int, calendar: Calendar) -> list[date]:
    day = date(year, month, 1)
    days: list[date] = []
    while day.month == month:
        if day.weekday() not in calendar.weekend and day not in calendar.closures:
            days.append(day)
        day += timedelta(days=1)
    return days


def _expiry_week(day: date) -> bool:
    """Whether `day` lies in the Monday-to-Sunday week holding its month's third Friday."""
    first = date(day.year, day.month, 1)
    friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
    return friday - timedelta(days=4) <= day <= friday + timedelta(days=2)


def _before_closure(day: date, calendar: Calendar) -> bool:
    """Whether the first day after `day` that is no weekend day is one the market closed."""
    after = day + timedelta(days=1)
    while after.weekday() in calendar.weekend:
        after += timedelta(days=1)
    return calendar.closed(after)
