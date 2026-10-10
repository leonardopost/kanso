"""The reads a window is made in: as many steps as the files say hold few points, and the
stream they deliver the stream an hour or a day at a time delivered."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from kanso.criteria.run import midnight_ns
from kanso.data.types.funding import Funding
from kanso.nautilus import backtest as runner
from kanso.nautilus.cross_section import coincident
from kanso.schemas import Hypothesis

from .conftest import (
    QUIET,
    RESEARCH,
    SECOND_NS,
    SYMBOL,
    _venue,
    bars,
    catalog,
    hypothesis,
    instrument,
)

HOUR_NS = 3_600 * SECOND_NS
CAP = 5_000
"""The points a lengthened read may hold here: more than a week of the sparse names' quotes
and prints, less than an hour of the dense name's prints."""

Counted = list[tuple[int, int, int]]
Reads = Callable[[Sequence[tuple[int, int, int]], int, int, tuple[int, int], int], Iterator[Any]]


def _held(counted: Counted, start: int, end: int) -> int:
    """The rows of the groups that meet `[start, end]`, counted one by one."""
    return sum(rows for first, last, rows in counted if first <= end and last >= start)


@given(
    st.lists(st.tuples(st.integers(-30, 300), st.integers(0, 60), st.integers(0, 40)), max_size=25),
    st.integers(1, 9),
    st.integers(0, 8),
    st.integers(1, 300),
    st.integers(0, 120),
)
def test_a_read_past_its_step_holds_at_most_the_cap_and_stops_where_the_next_would_not(
    drawn: list[tuple[int, int, int]], step: int, offset: int, length: int, cap: int
) -> None:
    """The reads tile the span in whole steps from its edge; a read longer than one step
    meets row groups holding at most `cap` rows, whose points are all it can hold; and every
    read but the last would meet more than `cap` with one step more, so there are as few
    reads as the cap allows."""
    counted = [(first, first + width, rows) for first, width, rows in drawn]
    opens = offset % step
    closes = opens + length
    reads = list(runner._reads(counted, 0, step, (opens, closes), cap))

    assert reads[0][0] == opens and reads[-1][1] == closes - 1
    assert all(later[0] == earlier[1] + 1 for earlier, later in pairwise(reads))
    assert all(start % step == 0 for start, _ in reads[1:])
    assert all((end + 1) % step == 0 for _, end in reads[:-1])
    for start, end in reads:
        first, last = start // step, end // step
        if last > first:
            assert _held(counted, first * step, (last + 1) * step - 1) <= cap
    for start, end in reads[:-1]:
        assert _held(counted, start // step * step, end + step) > cap


def test_a_window_no_file_meets_is_one_read() -> None:
    assert list(runner._reads([], 0, 10, (3, 95), 0)) == [(3, 94)]


def test_row_groups_are_read_off_the_footers(tmp_path: Path) -> None:
    """The engine's file states each group's rows and `ts_init` range; a file stating no
    range, or holding no `ts_init`, is counted at every instant."""
    import pyarrow as pa  # type: ignore[import-untyped]
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    written = ParquetDataCatalog(str(tmp_path / "engine"))
    stamps = [SECOND_NS * (10 + index // 2) for index in range(12_001)]
    written.write_data([_print(SYMBOL, ts, index) for index, ts in enumerate(stamps)])
    (engine,) = written.get_file_list_from_data_cls(TradeTick)
    bare, other = tmp_path / "bare.parquet", tmp_path / "other.parquet"
    pq.write_table(pa.table({"ts_init": [5, 9, 7]}), bare, write_statistics=False)
    pq.write_table(pa.table({"stamp": [1, 2]}), other)

    assert runner._row_groups([engine, str(bare), str(other)]) == [
        (stamps[0], stamps[4_999], 5_000),
        (stamps[5_000], stamps[9_999], 5_000),
        (stamps[10_000], stamps[12_000], 2_001),
        (0, 2**64 - 1, 3),
        (0, 2**64 - 1, 2),
    ]


# --- the stream, read the old way and the new ----------------------------------------------

DAYS = (date(2024, 1, 2), date(2024, 1, 12))
"""The window the tick shapes are read over: nine sessions, 264 hours."""


def _quote(symbol: str, ts: int) -> QuoteTick:
    return QuoteTick(
        InstrumentId(Symbol(symbol), _venue()),
        Price(9.99, 2),
        Price(10.01, 2),
        Quantity.from_int(100),
        Quantity.from_int(100),
        ts,
        ts,
    )


def _print(symbol: str, ts: int, index: int) -> TradeTick:
    return TradeTick(
        InstrumentId(Symbol(symbol), _venue()),
        Price(10.0, 2),
        Quantity.from_int(1 + index % 3),
        AggressorSide.BUYER,
        TradeId(str(index)),
        ts,
        ts,
    )


def _sparse(root: Path) -> Path:
    """Two names' quotes at each whole minute of 14:30-20:59 UTC and four prints a session,
    two of them at one instant, the quotes filed a week a file and the prints one: the shape
    of a workspace of minute-sampled ticks, where most hours hold nothing."""
    store = ParquetDataCatalog(str(root))
    store.write_data([instrument(), instrument(QUIET)])
    sessions = [DAYS[0] + timedelta(days=n) for n in range((DAYS[1] - DAYS[0]).days + 1)]
    for symbol in (SYMBOL, QUIET):
        weeks: dict[int, list[QuoteTick]] = {}
        prints: list[TradeTick] = []
        for day in (day for day in sessions if day.weekday() < 5):
            opens = midnight_ns(day) + (14 * 3_600 + 30 * 60) * SECOND_NS
            weeks.setdefault(day.isocalendar().week, []).extend(
                _quote(symbol, opens + minute * 60 * SECOND_NS) for minute in range(390)
            )
            for minute in (7, 7, 200, 389):
                prints.append(_print(symbol, opens + minute * 60 * SECOND_NS + 5, len(prints)))
        for week in sorted(weeks):
            store.write_data(weeks[week])
        store.write_data(prints)
    return root


def _dense(root: Path) -> Path:
    """One name's prints, three a second from 13:00 to 15:00 UTC on one day, every other
    second two of them at one instant: 10,800 an hour, more than a read past its step may
    hold."""
    base = midnight_ns(date(2024, 1, 3)) + 13 * HOUR_NS
    made: list[object] = []
    for second in range(2 * 3_600):
        instants = (0, 0, 500) if second % 2 else (0, 300, 600)
        made += [
            _print(SYMBOL, base + second * SECOND_NS + ms * 1_000_000, len(made) + k)
            for k, ms in enumerate(instants)
        ]
    return catalog(root, made, [instrument()])


def _ticks(universe: Sequence[str], requirements: Sequence[str]) -> Hypothesis:
    document = hypothesis(universe=universe).model_dump(mode="json")
    document.update(resolution="tick", data_requirements=list(requirements))
    document["windows"]["research"] = {"start": DAYS[0].isoformat(), "end": DAYS[1].isoformat()}
    return Hypothesis.model_validate(document)


def _by_step(
    counted: Sequence[tuple[int, int, int]], edge: int, step: int, span: tuple[int, int], cap: int
) -> Iterator[tuple[int, int]]:
    """The reads every window was made in before: one step each, from the step's edge."""
    opens, closes = span
    for start in range(edge, closes, step):
        yield max(opens, start), min(closes, start + step) - 1


def _streamed(
    request: runner.RunRequest, store: Path, reads: Reads | None
) -> tuple[list[str], list[tuple[int, int, int]]]:
    """The ordered stream a card is handed, every point and marker, each chunk ordered and
    marked by the hypothesis's rule as `checked_chunks` does it, and each read's span and
    points, read with `reads` or with the runner's own."""
    made: list[tuple[int, int, int]] = []
    inner = runner._window_points

    def recording(*args: Any) -> Any:
        groups, loaded = inner(*args)
        made.append((args[-2], args[-1], sum(len(group) for group in groups)))
        return groups, loaded

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_window_points", recording)
        patch.setattr(runner, "READ_POINTS", CAP)
        if reads is not None:
            patch.setattr(runner, "_reads", reads)
        _, chunks = runner.window_chunks(request, store)
        marked = coincident(request.hyp)
        stream = [
            f"{type(point).__name__} {point!r}"
            for chunk in chunks
            for point in runner._ordered(chunk, coincident=marked)
        ]
    return stream, made


def test_a_sparse_tick_window_is_streamed_as_by_the_hour_in_a_handful_of_reads(
    tmp_path: Path, request_for
) -> None:
    """Read by the hour, the nine sessions take 264 reads, 63 of them holding anything; read
    as its files allow, the same points in the same order take two, a week's file each, and
    neither past the cap."""
    universe = (f"{SYMBOL}.XNAS", f"{QUIET}.XNAS")
    request = request_for(DAYS, hypothesis_=_ticks(universe, ["quote", "trade"]))
    store = _sparse(tmp_path / "sparse")

    hourly, before = _streamed(request, store, _by_step)
    planned, after = _streamed(request, store, None)

    assert planned == hourly
    assert sum(points for *_, points in before) == 2 * 9 * 394
    assert len(before) == 264 and sum(1 for *_, points in before if points) == 63
    assert [points for *_, points in after] == [2 * 4 * 394, 2 * 5 * 394]


def test_a_dense_tick_window_is_streamed_as_by_the_hour_and_its_full_hours_read_alone(
    tmp_path: Path, request_for
) -> None:
    """An hour holding more than the cap is read alone, as every hour was, so the largest
    read is the largest hour; the empty hours either side are a read each."""
    request = request_for(DAYS, hypothesis_=_ticks((f"{SYMBOL}.XNAS",), ["trade"]))
    store = _dense(tmp_path / "dense")

    hourly, before = _streamed(request, store, _by_step)
    planned, after = _streamed(request, store, None)

    assert planned == hourly
    assert sum(points for *_, points in before) == 21_600
    full = [(start, end) for start, end, points in after if points > CAP]
    assert full == [(start, end) for start, end, points in before if points > CAP]
    assert all((end + 1 - start) == HOUR_NS for start, end in full) and len(full) == 2
    assert max(points for *_, points in after) == max(points for *_, points in before)
    assert len(after) == 4


def test_two_names_daily_bars_are_streamed_as_by_the_day_in_one_read(
    tmp_path: Path, request_for
) -> None:
    """Two names' month of daily bars, a feed marked however its instants fall, read a day
    at a time before, is one read."""
    universe = (f"{SYMBOL}.XNAS", f"{QUIET}.XNAS")
    request = request_for(hypothesis_=hypothesis(universe=universe))
    points = [*bars(RESEARCH), *bars(RESEARCH, QUIET)]
    store = catalog(tmp_path / "bars", points, [instrument(), instrument(QUIET)])

    daily, before = _streamed(request, store, _by_step)
    planned, after = _streamed(request, store, None)

    assert planned == daily and daily
    assert len(before) == 31 and len(after) == 1


def test_one_name_is_read_a_day_at_a_time_where_it_holds_data_as_its_chunks_mark_it(
    tmp_path: Path, request_for
) -> None:
    """One name's daily bars and two settlements at one instant on 10 January: a feed marked
    chunk by chunk, so only the chunk holding the two is marked. Read as before, that chunk
    is the day; read in one, the days around it would be marked too, which under a latency
    moves where a command lands. So the window is read a day at a time wherever a row group
    meets the day, and only the ten days no file meets are one read."""
    ident = InstrumentId(Symbol(SYMBOL), _venue())
    instant = midnight_ns(date(2024, 1, 10)) + 8 * HOUR_NS
    settled = [
        Funding(instrument_id=ident, rate=rate, ts_event=instant, ts_init=instant)
        for rate in (1e-4, 2e-4)
    ]
    held = bars(RESEARCH)
    root = tmp_path / "gap"
    store = ParquetDataCatalog(str(root))
    store.write_data([instrument()])
    store.write_data(held[:10])
    store.write_data(held[20:])
    store.write_data(settled)
    request = request_for(hypothesis_=hypothesis(data_requirements=("bar", "funding")))

    planner = runner._reads

    def lengthened(*args: Any) -> Iterator[tuple[int, int]]:
        return planner(*args[:-1], CAP)

    daily, before = _streamed(request, root, _by_step)
    planned, after = _streamed(request, root, None)
    forced, _ = _streamed(request, root, lengthened)

    assert not coincident(request.hyp)
    assert planned == daily
    assert sum(line.startswith("CustomData") for line in daily) == 2 + 3, "the 10th's markers"
    assert forced != daily, "read in one, the days around the 10th are marked"
    assert len(before) == 31 and len(after) == 10 + 1 + 11
