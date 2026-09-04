"""Snapshots: a derived id, coverage as the pinning question, and what makes one unusable."""

from __future__ import annotations

from datetime import date

import pytest

from kanso.data import catalog as cat
from kanso.data import manifest as m
from kanso.data import snapshot as snap
from kanso.errors import Exit, PreconditionError, ValidationError
from kanso.schemas.hypothesis import Windows
from tests.data.catalog.conftest import AAPL, MSFT, FakeWorkspace, Ref, bars, equity, quotes

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
    span = (start, date.fromordinal(start.toordinal() + count - 1))
    return cat.write(
        ws,
        bars(start, count, instrument=instrument),
        ref=Ref(instrument=instrument, span=span, **kw),
        source="synthetic",
        adjustment_basis=basis,
    )


def load_quotes(ws: FakeWorkspace, instrument: str = AAPL, count: int = 25) -> cat.Written:
    span = (JAN1, date.fromordinal(JAN1.toordinal() + count - 1))
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
    found = snap.covering(ws, [AAPL], ["bar"], "1d", windows())
    assert found is not None
    assert found.snapshot_id == taken.snapshot_id


def test_covering_refuses_a_snapshot_missing_a_day_of_certification(ws: FakeWorkspace) -> None:
    load_bars(ws, count=18)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows()) is None


def test_covering_refuses_a_snapshot_missing_an_instrument(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL, MSFT], ["bar"], "1d", windows()) is None


def test_covering_refuses_a_snapshot_missing_a_required_type(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar", "quote"], "1d", windows()) is None


def test_covering_accepts_the_second_instrument_once_it_is_loaded(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    load_bars(ws, count=25, instrument=MSFT)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL, MSFT], ["bar"], "1d", windows()) is not None


def test_a_gap_inside_the_span_is_not_coverage(ws: FakeWorkspace) -> None:
    """A hole inside a window is not coverage, however much data surrounds it."""
    load_bars(ws, start=JAN1, count=16)
    load_bars(ws, start=date(2024, 1, 18), count=8)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows()) is None

    load_bars(ws, start=date(2024, 1, 17), count=1)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows()) is not None


def test_covering_refuses_a_dataset_at_another_resolution(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1m", windows()) is None


def test_an_unaggregated_type_answers_at_any_resolution(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    load_quotes(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar", "quote"], "1d", windows()) is not None


def test_research_is_never_pinned_to_data_whose_publication_nobody_declared(
    ws: FakeWorkspace,
) -> None:
    load_bars(ws, count=25, publication="unknown")
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows()) is None


def test_the_newest_covering_snapshot_wins(ws: FakeWorkspace) -> None:
    load_bars(ws, count=25)
    first = snap.freeze(ws)
    load_quotes(ws, count=25)
    second = snap.freeze(ws)

    assert first.snapshot_id != second.snapshot_id
    assert second.created_at > first.created_at
    found = snap.covering(ws, [AAPL], ["bar"], "1d", windows())
    assert found is not None
    assert found.snapshot_id == second.snapshot_id


def test_a_snapshot_whose_manifest_has_gone_is_skipped(ws: FakeWorkspace) -> None:
    written = load_bars(ws, count=25)
    snap.freeze(ws)
    m.remove_manifest(ws, written.manifest.dataset_id)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows()) is None


def test_covering_ignores_the_forward_window(ws: FakeWorkspace) -> None:
    """`forward` is never backtested, so no snapshot is ever asked to hold it."""
    load_bars(ws, count=25)
    snap.freeze(ws)
    assert snap.covering(ws, [AAPL], ["bar"], "1d", windows()) is not None
