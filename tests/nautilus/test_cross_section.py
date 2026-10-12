"""Cohort marking: identity for a single series, markers for a coincident grain."""

from __future__ import annotations

import pickle

from hypothesis import given, settings
from hypothesis import strategies as st
from nautilus_trader.model.data import Bar, QuoteTick

from kanso.nautilus.cross_section import (
    KansoCrossSection,
    is_marker,
    ordered,
    with_cross_section,
    without_markers,
)
from tests.nautilus.strategy.conftest import DEMO, HEDGE, bar, quote, second_bar, trade


def test_a_single_series_is_unmarked() -> None:
    points = tuple(bar(DEMO, i, 10.0 + i) for i in range(3))
    marked = with_cross_section(points)

    assert len(marked) == len(points)
    assert all(left is right for left, right in zip(marked, points, strict=True))
    assert not any(is_marker(point) for point in marked)


def test_a_coincident_pair_is_followed_by_one_marker_per_leg() -> None:
    first = bar(DEMO, 0, 10.0)
    second = bar(HEDGE, 0, 11.0)
    stream = with_cross_section((first, second))

    assert stream[0] is first
    assert stream[1] is second
    assert is_marker(stream[2]) and is_marker(stream[3])
    assert len(stream) == 4
    assert int(stream[2].ts_init) == int(first.ts_init)


def test_an_incomplete_instant_still_flushes_once_a_pair_exists() -> None:
    a0, b0 = bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)
    a1 = bar(DEMO, 1, 12.0)
    stream = with_cross_section((a0, b0, a1))

    assert stream[0] is a0 and stream[1] is b0
    assert is_marker(stream[2]) and is_marker(stream[3])
    assert stream[4] is a1
    assert is_marker(stream[5])
    assert len(stream) == 6


def test_bars_flush_before_quotes_at_the_same_instant() -> None:
    close = bar(DEMO, 0, 10.0)
    other = bar(HEDGE, 0, 11.0)
    mid = quote(DEMO, 0, 99.0)
    stream = with_cross_section((close, other, mid))

    assert stream[0] is close
    assert stream[1] is other
    assert is_marker(stream[2]) and is_marker(stream[3])
    assert stream[4] is mid
    assert is_marker(stream[5])


def test_marking_is_idempotent() -> None:
    first, second = bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)
    once = with_cross_section((first, second))
    twice = with_cross_section(once)

    assert [is_marker(point) for point in twice] == [is_marker(point) for point in once]
    assert twice[0] is once[0] and twice[1] is once[1]


def test_ordered_is_the_stable_sort_then_the_markers() -> None:
    first = [bar(DEMO, i, 10.0) for i in range(2)]
    second = [bar(HEDGE, i, 11.0) for i in range(2)]
    stream = ordered((tuple(first), tuple(second)))

    assert stream[0] is first[0]
    assert stream[1] is second[0]
    assert is_marker(stream[2]) and is_marker(stream[3])
    assert stream[4] is first[1]
    assert stream[5] is second[1]


def test_an_empty_stream_stays_empty() -> None:
    assert with_cross_section(()) == ()
    assert without_markers(()) == ()


def test_markers_alone_are_not_a_stream() -> None:
    """Dropping flushes first, then the empty remainder is empty — not unmarked identity."""
    marker = with_cross_section((bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)))[2]

    assert with_cross_section((marker, marker)) == ()


def test_without_markers_is_the_session_stream() -> None:
    """A session's released count is a prefix of this, not of the marked feed."""
    first, second = bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)
    stream = with_cross_section((first, second))

    assert without_markers(stream) == (first, second)
    assert without_markers(stream)[:2] == (first, second)
    assert without_markers(stream[:3]) == (first, second)


def test_slicing_the_marked_feed_by_the_market_count_drops_the_later_leg() -> None:
    """`released` is a market-point count; `points[:released]` is not those points."""
    a0, b0 = bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)
    a1 = bar(DEMO, 1, 12.0)
    points = with_cross_section((a0, b0, a1))
    released = 3

    assert without_markers(points)[:released] == (a0, b0, a1)
    assert without_markers(points[:released]) == (a0, b0)


def test_a_non_market_pair_is_still_a_cohort() -> None:
    class Point:
        def __init__(self, ts: int) -> None:
            self.ts_init = ts
            self.ts_event = ts

    first, second = Point(1), Point(1)
    stream = with_cross_section((first, second))

    assert stream[0] is first
    assert is_marker(stream[2])


def test_a_marker_round_trips_through_pickle() -> None:
    wrapped = with_cross_section((bar(DEMO, 0, 10.0), bar(HEDGE, 0, 11.0)))[2]
    loaded = pickle.loads(pickle.dumps(wrapped))

    assert is_marker(loaded)
    assert isinstance(loaded.data, KansoCrossSection)


def test_different_bar_grains_at_the_same_instant_are_not_one_book() -> None:
    minute = bar(DEMO, 0, 10.0)
    second = second_bar(HEDGE, 0, 11.0, ts_init=int(minute.ts_init))
    stream = with_cross_section((minute, second))

    assert stream == (minute, second)
    assert not any(is_marker(point) for point in stream)


def _made(kind: str, name: object, index: int) -> object:
    if kind == "bar":
        return bar(name, index, 10.0)  # type: ignore[arg-type]
    if kind == "second":
        return second_bar(name, 0, 10.0, ts_init=int(bar(DEMO, index, 1.0).ts_init))  # type: ignore[arg-type]
    if kind == "quote":
        return quote(name, index)  # type: ignore[arg-type]
    return trade(name, index)  # type: ignore[arg-type]


def _grain(point: object) -> str:
    if isinstance(point, Bar):
        return str(point.bar_type.spec)
    return "quote" if isinstance(point, QuoteTick) else "trade"


@settings(max_examples=80, deadline=None)
@given(
    drawn=st.lists(
        st.tuples(
            st.sampled_from(("bar", "second", "quote", "trade")),
            st.sampled_from((DEMO, HEDGE)),
            st.integers(min_value=0, max_value=3),
        ),
        max_size=30,
    ),
    coincident=st.booleans(),
)
def test_every_run_of_one_instant_and_grain_is_followed_by_a_marker_a_point(
    drawn: list[tuple[str, object, int]], coincident: bool
) -> None:
    """The whole marking rule, against a reading of it by hand: consecutive points sharing an
    instant and a grain are a cohort, and a marked stream follows each with one marker per
    point, stamped at its instant; an unmarked stream is handed back as it came."""
    points = tuple(sorted((_made(*each) for each in drawn), key=lambda p: int(p.ts_init)))
    cohorts: list[list[object]] = []
    for point in points:
        if cohorts and (cohorts[-1][0].ts_init, _grain(cohorts[-1][0])) == (
            point.ts_init,
            _grain(point),
        ):
            cohorts[-1].append(point)
        else:
            cohorts.append([point])
    stream = with_cross_section(points, coincident=coincident)

    if not coincident and all(len(cohort) == 1 for cohort in cohorts):
        assert stream == points
        return
    at = 0
    for cohort in cohorts:
        for point in cohort:
            assert stream[at] is point
            at += 1
        for _ in cohort:
            assert is_marker(stream[at]) and stream[at].ts_init == cohort[0].ts_init
            at += 1
    assert at == len(stream)
