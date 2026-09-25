"""Snapshots: a derived id, coverage as the pinning question, and what makes one unusable."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from nautilus_trader.model.instruments import Equity

from kanso.data import catalog as cat
from kanso.data import instruments
from kanso.data import manifest as m
from kanso.data import snapshot as snap
from kanso.errors import Exit, PreconditionError, ValidationError
from kanso.schemas.hypothesis import Windows
from kanso.state import StateStore
from tests.data.catalog.conftest import (
    AAPL,
    MSFT,
    FakeWorkspace,
    Ref,
    bars,
    define,
    equity,
    quotes,
)

JAN1 = date(2024, 1, 1)
RESEARCH = (JAN1, date(2024, 1, 10))
CERTIFICATION = (date(2024, 1, 15), date(2024, 1, 20))


def windows(
    research: tuple[date, date] = RESEARCH,
    certification: tuple[date, date] = CERTIFICATION,
) -> Windows:
    return Windows.model_validate(
        {
            "research": {"start": research[0], "end": research[1]},
            "certification": {"start": certification[0], "end": certification[1]},
            "forward": {"start": date(2024, 2, 1)},
        }
    )


def load_bars(
    ws: FakeWorkspace,
    start: date = JAN1,
    count: int = 25,
    instrument: str = AAPL,
    basis: str | None = None,
    **kw: object,
) -> cat.Written:
    """Bars for `instrument`, whose definition the store then holds: a snapshot needs it."""
    span = (start, date.fromordinal(start.toordinal() + count - 1))
    define(ws, instrument)
    return cat.write(
        ws,
        bars(start, count, instrument=instrument),
        ref=Ref(instrument=instrument, span=span, **kw),
        source="synthetic",
        adjustment_basis=basis,
    )


def load_quotes(ws: FakeWorkspace, instrument: str = AAPL, count: int = 25) -> cat.Written:
    span = (JAN1, date.fromordinal(JAN1.toordinal() + count - 1))
    define(ws, instrument)
    return cat.write(
        ws,
        quotes(JAN1, count, instrument=instrument),
        ref=Ref(instrument=instrument, type="quote", resolution=None, span=span),
        source="synthetic",
    )


def test_a_snapshot_id_is_the_sha_of_its_sorted_checksums() -> None:
    a, b, instruments = "a" * 64, "b" * 64, "c" * 64
    assert snap.snapshot_id([a, b], instruments) == snap.snapshot_id([b, a], instruments)


def test_the_instrument_checksum_is_one_of_the_values_hashed() -> None:
    checksums = ["a" * 64]
    assert snap.snapshot_id(checksums, "b" * 64) != snap.snapshot_id(checksums, "c" * 64)


def test_the_same_inputs_reach_the_same_snapshot_id(
    ws: FakeWorkspace, other_ws: FakeWorkspace
) -> None:
    for workspace in (ws, other_ws):
        cat.open_catalog(workspace).write_data([equity()])
        load_bars(workspace)
    assert snap.freeze(ws).snapshot_id == snap.freeze(other_ws).snapshot_id


def test_freezing_twice_over_unchanged_data_is_the_same_snapshot(ws: FakeWorkspace) -> None:
    load_bars(ws)
    first, second = snap.freeze(ws), snap.freeze(ws)
    assert first.snapshot_id == second.snapshot_id
    assert len(snap.snapshots(ws)) == 1


def test_a_snapshot_names_its_datasets_and_their_checksums(ws: FakeWorkspace) -> None:
    written = load_bars(ws)
    snapshot = snap.freeze(ws)
    assert snapshot.datasets == [written.manifest.dataset_id]
    assert snapshot.checksums == [written.manifest.checksum]
    assert snapshot.checksum_of == {written.manifest.dataset_id: written.manifest.checksum}
    assert snapshot.reproducible
    assert snap.read(ws, snapshot.snapshot_id) == snapshot


def test_a_snapshot_may_be_narrowed_to_named_datasets(ws: FakeWorkspace) -> None:
    first = load_bars(ws, count=25)
    load_quotes(ws)
    snapshot = snap.freeze(ws, datasets=[first.manifest.dataset_id])
    assert snapshot.datasets == [first.manifest.dataset_id]


def test_freezing_a_dataset_the_workspace_does_not_hold_is_refused(ws: FakeWorkspace) -> None:
    with pytest.raises(PreconditionError) as raised:
        snap.freeze(ws, datasets=["AAPL.XNAS-bar-1d-raw-20240105"])
    assert raised.value.code is Exit.PRECONDITION


def test_a_vendor_adjusted_dataset_makes_a_snapshot_unreproducible(ws: FakeWorkspace) -> None:
    load_bars(ws, adjusted=True, basis="close 2024-06-01")
    snapshot = snap.freeze(ws, instruments_checksum="0" * 64)
    assert not snapshot.reproducible


def test_a_snapshot_of_unadjusted_data_is_reproducible(ws: FakeWorkspace) -> None:
    load_bars(ws)
    assert snap.freeze(ws, instruments_checksum="0" * 64).reproducible


def test_a_snapshot_refuses_an_id_it_did_not_derive() -> None:
    with pytest.raises(ValidationError, match="derived, never chosen"):
        snap.Snapshot(
            snapshot_id="f" * 64,
            datasets=["AAPL.XNAS-bar-1d-raw-20240105"],
            checksums=["a" * 64],
            instruments_checksum="b" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )


def test_a_snapshot_refuses_lists_that_are_not_parallel() -> None:
    with pytest.raises(ValidationError, match="parallel"):
        snap.Snapshot(
            snapshot_id="f" * 64,
            datasets=["AAPL.XNAS-bar-1d-raw-20240105"],
            checksums=[],
            instruments_checksum="b" * 64,
            created_at="2024-01-01T00:00:00+00:00",
        )


def test_a_name_that_is_not_a_snapshot_id_never_becomes_a_path(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="not a snapshot id"):
        snap.snapshot_file(ws, "../escape")


def test_reading_a_snapshot_the_workspace_does_not_hold_is_a_precondition(
    ws: FakeWorkspace,
) -> None:
    with pytest.raises(PreconditionError, match="no snapshot"):
        snap.read(ws, "a" * 64)


def test_nothing_is_pinned_before_a_snapshot_is_taken(ws: FakeWorkspace) -> None:
    load_bars(ws)
    assert snap.pinned_datasets(ws) == frozenset()
    assert snap.snapshots(ws) == []


def test_taking_a_snapshot_pins_what_it_names(ws: FakeWorkspace) -> None:
    written = load_bars(ws)
    snap.freeze(ws)
    assert snap.pinned_datasets(ws) == frozenset({written.manifest.dataset_id})


def test_covering_finds_a_snapshot_that_holds_both_windows(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    taken = snap.freeze(ws)
    found = snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None)
    assert found is not None
    assert found.snapshot_id == taken.snapshot_id


def test_covering_refuses_a_snapshot_missing_a_day_of_certification(ws: FakeWorkspace) -> None:
    load_bars(ws, count=18)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None) is None


def test_covering_refuses_a_snapshot_missing_an_instrument(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL, MSFT], ["bar"], "1d", windows(), store=None) is None


def test_covering_refuses_a_snapshot_missing_a_required_type(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar", "quote"], "1d", windows(), store=None) is None


def test_covering_accepts_the_second_instrument_once_it_is_loaded(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    load_bars(ws, count=25, instrument=MSFT)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL, MSFT], ["bar"], "1d", windows(), store=None) is not None


def test_a_gap_inside_the_span_is_not_coverage(ws: FakeWorkspace) -> None:
    """A hole inside a window is not coverage, however much data surrounds it."""
    load_bars(ws, start=JAN1, count=16)
    load_bars(ws, start=date(2024, 1, 18), count=8)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None) is None

    load_bars(ws, start=date(2024, 1, 17), count=1)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None) is not None


def test_covering_refuses_a_dataset_at_another_resolution(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1m", windows(), store=None) is None


def test_an_unaggregated_type_answers_at_any_resolution(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    load_quotes(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar", "quote"], "1d", windows(), store=None) is not None


def test_research_is_never_pinned_to_data_whose_publication_nobody_declared(
    ws: FakeWorkspace,
) -> None:
    load_bars(ws, count=25, publication="unknown")
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None) is None


def test_the_newest_covering_snapshot_wins(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    first = snap.freeze(ws)
    load_quotes(ws, count=25)
    second = snap.freeze(ws)

    assert first.snapshot_id != second.snapshot_id
    assert second.created_at > first.created_at
    found = snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None)
    assert found is not None
    assert found.snapshot_id == second.snapshot_id


def test_a_snapshot_whose_manifest_has_gone_is_skipped(ws: FakeWorkspace) -> None:
    written = load_bars(ws, count=25)
    snap.freeze(ws)
    m.remove_manifest(ws, written.manifest.dataset_id)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None) is None


def test_covering_ignores_the_forward_window(ws: FakeWorkspace) -> None:
    """`forward` is never backtested, so no snapshot is ever asked to hold it."""
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=None) is not None


# --- the instrument pin, read back ---------------------------------------------


def covering(ws: FakeWorkspace, *universe: str) -> snap.Snapshot | None:
    return snap.covering(ws, list(universe or (AAPL,)), ["bar"], "1d", windows(), store=None)


def test_freezing_refuses_an_empty_store_over_instrument_data(ws: FakeWorkspace) -> None:
    """The checksum of nothing pins nothing, and a run reads its definitions from the store."""
    cat.write(ws, bars(JAN1, 5), ref=Ref(), source="synthetic")

    with pytest.raises(PreconditionError) as raised:
        snap.freeze(ws)

    assert raised.value.code is Exit.PRECONDITION
    assert AAPL in raised.value.message
    assert "kanso data instruments resolve" in str(raised.value.remedy)
    assert snap.snapshots(ws) == []


def test_freezing_nothing_needs_no_definition(ws: FakeWorkspace) -> None:
    assert snap.freeze(ws).datasets == []


def test_a_store_that_moved_since_the_snapshot_is_refused_by_name(ws: FakeWorkspace) -> None:
    """A run reproduces the definitions its snapshot pins, or it is not started."""
    load_bars(ws, count=25)
    taken = snap.freeze(ws)
    instruments.write_store(ws, [equity(AAPL, "0.05")], replace=True)

    with pytest.raises(PreconditionError) as raised:
        covering(ws)

    assert raised.value.code is Exit.PRECONDITION
    assert taken.snapshot_id in raised.value.message
    assert taken.instruments_checksum in raised.value.message
    assert cat.resolved_instruments_checksum(ws) in raised.value.message
    assert "kanso data snapshot" in str(raised.value.remedy)


def test_the_newest_covering_snapshot_pinning_the_stores_instruments_wins(
    ws: FakeWorkspace,
) -> None:
    load_bars(ws, count=25)
    first = snap.freeze(ws)
    instruments.write_store(ws, [equity(AAPL, "0.05")], replace=True)
    second = snap.freeze(ws)
    assert first.snapshot_id != second.snapshot_id

    found = covering(ws)
    assert found is not None
    assert found.snapshot_id == second.snapshot_id

    # The store is put back: the older snapshot is the one that pins what it holds now.
    instruments.write_store(ws, [equity(AAPL)], replace=True)
    found = covering(ws)
    assert found is not None
    assert found.snapshot_id == first.snapshot_id


def test_covering_refuses_a_universe_the_store_does_not_define(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    load_bars(ws, count=25, instrument=MSFT)
    snap.freeze(ws)
    cat.open_catalog(ws).delete_data_range(Equity, MSFT, 0, 0)

    with pytest.raises(PreconditionError) as raised:
        covering(ws, AAPL, MSFT)

    assert raised.value.message.startswith(f"the instrument store holds no definition for {MSFT}")
    assert f"kanso data instruments resolve {MSFT}" in str(raised.value.remedy)


def test_no_covering_snapshot_is_none_whatever_the_store_holds(ws: FakeWorkspace) -> None:
    load_bars(ws, count=18)
    snap.freeze(ws)
    instruments.write_store(ws, [equity(AAPL, "0.05")], replace=True)
    assert covering(ws) is None


def test_instrument_drift_names_the_snapshot_and_both_checksums(ws: FakeWorkspace) -> None:
    load_bars(ws)
    taken = snap.freeze(ws)
    assert snap.instrument_drift(ws, taken) is None

    instruments.write_store(ws, [equity(MSFT)])
    drift = snap.instrument_drift(ws, taken)

    assert drift is not None
    assert drift.snapshot_id == taken.snapshot_id
    assert drift.pinned == taken.instruments_checksum
    assert drift.held == cat.resolved_instruments_checksum(ws)


def test_newest_is_the_snapshot_taken_last(ws: FakeWorkspace) -> None:
    assert snap.newest(ws) is None
    load_bars(ws, count=25)
    first = snap.freeze(ws)
    load_quotes(ws)
    second = snap.freeze(ws)

    found = snap.newest(ws)

    assert found is not None
    assert found.snapshot_id == second.snapshot_id != first.snapshot_id


def test_covering_requires_the_warmup_sessions_as_well(ws: FakeWorkspace) -> None:
    """A warmed hypothesis loads its prefix from the pinned data, so the pin must hold it."""
    load_bars(ws, count=25)
    snap.freeze(ws)
    before = (date(2023, 12, 27), date(2023, 12, 31))
    assert (
        snap.covering(ws, [AAPL], ["bar"], "1d", windows(), prefixes=(before,), store=None) is None
    )

    load_bars(ws, start=date(2023, 12, 20), count=12)
    snap.freeze(ws)
    assert (
        snap.covering(ws, [AAPL], ["bar"], "1d", windows(), prefixes=(before,), store=None)
        is not None
    )


# --- the days a source answered empty ------------------------------------------------


@pytest.fixture
def store(ws: FakeWorkspace) -> Iterator[StateStore]:
    """A migrated state store: where the answers a source gave are read from."""
    with StateStore(ws.root / "state.db") as opened:
        opened.migrate()
        yield opened


def answer_empty(
    store: StateStore,
    start: date,
    end: date,
    series: tuple[str, str, str | None] = (AAPL, "bar", "1d"),
) -> None:
    """What a backfill records when the source answers a chunk with nothing."""
    store.event(m.EMPTY_CHUNK, m.series_subject(series), {"start": str(start), "end": str(end)})


def covers(
    ws: FakeWorkspace,
    store: StateStore | None,
    prefixes: tuple[tuple[date, date], ...] = (),
) -> bool:
    found = snap.covering(ws, [AAPL], ["bar"], "1d", windows(), prefixes, store=store)
    return found is not None


def weekend_split(ws: FakeWorkspace) -> snap.Snapshot:
    """Two chunks either side of a weekend inside the research window: Friday, then Monday."""
    load_bars(ws, start=JAN1, count=5)
    load_bars(ws, start=date(2024, 1, 8), count=13)
    return snap.freeze(ws)


def test_a_weekend_the_source_answered_empty_joins_the_spans_either_side(
    ws: FakeWorkspace, store: StateStore
) -> None:
    taken = weekend_split(ws)
    assert not covers(ws, store)

    answer_empty(store, date(2024, 1, 6), date(2024, 1, 7))

    found = snap.covering(ws, [AAPL], ["bar"], "1d", windows(), store=store)
    assert found is not None
    assert found.snapshot_id == taken.snapshot_id


def test_without_a_store_no_answer_is_counted(ws: FakeWorkspace, store: StateStore) -> None:
    """`store=None` is a place no answer was ever recorded: coverage is what was served."""
    weekend_split(ws)
    answer_empty(store, date(2024, 1, 6), date(2024, 1, 7))

    assert covers(ws, store)
    assert not covers(ws, None)


def test_a_holiday_weekend_covers_only_once_every_day_of_it_is_answered(
    ws: FakeWorkspace, store: StateStore
) -> None:
    """Friday the 12th, then Tuesday the 16th: the Monday is a holiday no calendar here knows."""
    load_bars(ws, start=JAN1, count=12)
    load_bars(ws, start=date(2024, 1, 16), count=10)
    snap.freeze(ws)

    answer_empty(store, date(2024, 1, 13), date(2024, 1, 14))
    assert not covers(ws, store)

    answer_empty(store, date(2024, 1, 15), date(2024, 1, 15))
    assert covers(ws, store)


def test_an_answer_before_the_first_served_day_or_after_the_last_covers_nothing(
    ws: FakeWorkspace, store: StateStore
) -> None:
    """Asked before the instrument listed, or past where its source ends: not coverage."""
    load_bars(ws, start=date(2024, 1, 3), count=16)
    snap.freeze(ws)

    answer_empty(store, date(2023, 12, 1), date(2024, 1, 2))
    answer_empty(store, date(2024, 1, 19), date(2024, 1, 31))

    assert not covers(ws, store)


def test_an_answer_closes_a_hole_only_in_the_series_it_was_asked_of(
    ws: FakeWorkspace, store: StateStore
) -> None:
    weekend_split(ws)
    for other in ((AAPL, "bar", "1m"), (AAPL, "quote", None), (MSFT, "bar", "1d")):
        answer_empty(store, date(2024, 1, 6), date(2024, 1, 7), other)
    assert not covers(ws, store)

    answer_empty(store, date(2024, 1, 6), date(2024, 1, 7))
    assert covers(ws, store)


def test_a_warmup_prefix_counts_the_days_answered_empty_as_well(
    ws: FakeWorkspace, store: StateStore
) -> None:
    load_bars(ws, start=date(2023, 12, 20), count=10)
    load_bars(ws, start=JAN1, count=25)
    snap.freeze(ws)
    before = ((date(2023, 12, 27), date(2023, 12, 31)),)
    assert not covers(ws, store, before)

    answer_empty(store, date(2023, 12, 30), date(2023, 12, 31))

    assert covers(ws, store, before)
