"""Dataset identity, the manifest's invariants and the span arithmetic coverage rests on."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kanso.data import manifest as m
from kanso.errors import Exit, PreconditionError, ValidationError
from tests.data.catalog.conftest import FakeWorkspace

JAN = date(2024, 1, 1)
CHECKSUM = "a" * 64


def a_manifest(**overrides: object) -> m.Manifest:
    values: dict[str, object] = {
        "source": "synthetic",
        "instrument": "AAPL.XNAS",
        "type": "bar",
        "resolution": "1d",
        "span": (date(2024, 1, 1), date(2024, 1, 31)),
        "adjusted": False,
        "row_count": 21,
        "checksum": CHECKSUM,
    }
    values.update(overrides)
    values.setdefault(
        "dataset_id",
        m.dataset_id(
            str(values["instrument"]),
            str(values["type"]),
            values["resolution"],
            bool(values["adjusted"]),
            values["span"][1],
        ),
    )
    return m.Manifest(**values)


def test_dataset_id_carries_every_dimension() -> None:
    base = m.dataset_id("AAPL.XNAS", "bar", "1d", False, date(2024, 1, 31))
    assert base == "AAPL.XNAS-bar-1d-raw-20240131"
    assert base != m.dataset_id("MSFT.XNAS", "bar", "1d", False, date(2024, 1, 31))
    assert base != m.dataset_id("AAPL.XNAS", "quote", "1d", False, date(2024, 1, 31))
    assert base != m.dataset_id("AAPL.XNAS", "bar", "1m", False, date(2024, 1, 31))
    assert base != m.dataset_id("AAPL.XNAS", "bar", "1d", True, date(2024, 1, 31))
    assert base != m.dataset_id("AAPL.XNAS", "bar", "1d", False, date(2024, 2, 29))


def test_dataset_id_is_stable_and_ignores_where_the_span_starts() -> None:
    """Same dimensions, same id — which is what makes a re-load a replacement."""
    once = m.dataset_id("AAPL.XNAS", "bar", "1d", False, date(2024, 1, 31))
    again = m.dataset_id("AAPL.XNAS", "bar", "1d", False, date(2024, 1, 31))
    assert once == again


def test_dataset_id_spells_an_absent_resolution() -> None:
    assert m.dataset_id("AAPL.XNAS", "quote", None, False, JAN).split("-")[2] == m.NO_RESOLUTION


@given(
    instrument=st.text(min_size=1, max_size=30),
    type_=st.text(min_size=1, max_size=20),
    resolution=st.one_of(st.none(), st.text(min_size=1, max_size=10)),
    adjusted=st.booleans(),
    end=st.dates(),
)
def test_dataset_id_is_always_filename_safe(
    instrument: str, type_: str, resolution: str | None, adjusted: bool, end: date
) -> None:
    identifier = m.dataset_id(instrument, type_, resolution, adjusted, end)
    assert m.is_dataset_id(identifier)
    assert re.match(r"^[A-Za-z0-9._-]+$", identifier)
    assert Path(identifier).name == identifier


def test_is_dataset_id_rejects_a_name_that_is_not_one() -> None:
    assert not m.is_dataset_id("AAPL.XNAS-bar-1d-raw")
    assert not m.is_dataset_id("AAPL/XNAS-bar-1d-raw-20240131")


def test_sanitise_never_returns_nothing() -> None:
    assert m.sanitise("/") == "_"
    assert m.sanitise("") == "_"


def test_manifest_refuses_an_id_it_did_not_derive() -> None:
    with pytest.raises(ValidationError) as raised:
        a_manifest(dataset_id="hand-written-id-raw-20240131")
    assert "derived, never chosen" in raised.value.message
    assert raised.value.code is Exit.VALIDATION


def test_manifest_refuses_a_reversed_span() -> None:
    with pytest.raises(ValidationError, match="before"):
        a_manifest(span=(date(2024, 1, 31), date(2024, 1, 1)), dataset_id="x")


def test_manifest_refuses_an_adjusted_dataset_without_a_basis() -> None:
    with pytest.raises(ValidationError, match="adjustment_basis"):
        a_manifest(adjusted=True)


def test_manifest_accepts_an_adjusted_dataset_that_names_its_basis() -> None:
    assert a_manifest(adjusted=True, adjustment_basis="close 2024-01-31").adjusted


def test_manifest_refuses_a_delayed_dataset_without_a_rule() -> None:
    with pytest.raises(ValidationError, match="publication_rule"):
        a_manifest(publication="delayed")


def test_manifest_exposes_its_span_and_series() -> None:
    manifest = a_manifest()
    assert manifest.start == date(2024, 1, 1)
    assert manifest.end == date(2024, 1, 31)
    assert manifest.filed_under == ("AAPL.XNAS", "bar", "1d")
    assert manifest.covers((date(2024, 1, 2), date(2024, 1, 3)))
    assert not manifest.covers((date(2023, 12, 31), date(2024, 1, 3)))


def test_as_publication_refuses_a_class_nobody_declared() -> None:
    assert m.as_publication("delayed") == "delayed"
    with pytest.raises(ValidationError, match="realtime"):
        m.as_publication("eventually")


def test_overlaps_is_inclusive_at_the_edges() -> None:
    assert m.overlaps((JAN, date(2024, 1, 5)), (date(2024, 1, 5), date(2024, 1, 9)))
    assert not m.overlaps((JAN, date(2024, 1, 5)), (date(2024, 1, 6), date(2024, 1, 9)))


def test_merge_joins_touching_days() -> None:
    merged = m.merge([(JAN, date(2024, 1, 5)), (date(2024, 1, 6), date(2024, 1, 9))])
    assert merged == [(JAN, date(2024, 1, 9))]


def test_merge_leaves_a_hole_where_a_day_is_missing() -> None:
    merged = m.merge([(JAN, date(2024, 1, 5)), (date(2024, 1, 7), date(2024, 1, 9))])
    assert len(merged) == 2


def test_contains_needs_one_merged_span_to_hold_the_window() -> None:
    spans = [(JAN, date(2024, 1, 5)), (date(2024, 1, 6), date(2024, 1, 9))]
    assert m.contains(spans, (date(2024, 1, 3), date(2024, 1, 8)))
    assert not m.contains(spans, (date(2024, 1, 3), date(2024, 1, 10)))
    assert not m.contains([], (JAN, JAN))


@given(spans=st.lists(st.tuples(st.dates(), st.dates()), min_size=1, max_size=6))
def test_merge_never_loses_a_day(spans: list[tuple[date, date]]) -> None:
    ordered = [(min(a, b), max(a, b)) for a, b in spans]
    merged = m.merge(ordered)
    assert merged == sorted(merged)
    for span in ordered:
        assert m.contains(merged, span)


def test_shortfall_is_silent_when_the_request_was_served() -> None:
    assert m.shortfall((JAN, date(2024, 1, 5)), (JAN, date(2024, 1, 5))) is None


def test_shortfall_names_both_missing_ends() -> None:
    said = m.shortfall((JAN, date(2024, 1, 10)), (date(2024, 1, 3), date(2024, 1, 8)))
    assert said is not None
    assert "2024-01-01 to 2024-01-02 at the start" in said
    assert "2024-01-09 to 2024-01-10 at the end" in said


def test_paths_hang_off_the_workspace(tmp_path: Path) -> None:
    ws = FakeWorkspace(root=tmp_path)
    assert m.catalog_path(ws) == tmp_path / "catalog"
    assert m.data_path(ws) == tmp_path / "catalog" / "data"
    assert m.manifests_path(ws).name == "manifests"
    assert m.snapshots_path(ws).name == "snapshots"
    assert m.cache_path(ws).name == ".cache"


def test_manifest_round_trips_through_its_file(ws: FakeWorkspace) -> None:
    written = a_manifest(vendor="acme", request_params={"symbol": "AAPL"})
    path = m.write_manifest(ws, written)
    assert path.name == f"{written.dataset_id}.yaml"
    assert m.read_manifest(ws, written.dataset_id) == written
    assert m.manifests(ws) == {written.dataset_id: written}


def test_manifests_are_empty_before_anything_is_written(ws: FakeWorkspace) -> None:
    assert m.manifests(ws) == {}


def test_reading_a_dataset_the_workspace_does_not_hold_is_a_precondition(ws: FakeWorkspace) -> None:
    with pytest.raises(PreconditionError) as raised:
        m.read_manifest(ws, m.dataset_id("AAPL.XNAS", "bar", "1d", False, JAN))
    assert raised.value.code is Exit.PRECONDITION


def test_a_name_that_is_not_a_dataset_id_never_becomes_a_path(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="not a dataset id"):
        m.manifest_file(ws, "../escape")


def test_removing_a_manifest_twice_is_silent(ws: FakeWorkspace) -> None:
    written = a_manifest()
    m.write_manifest(ws, written)
    m.remove_manifest(ws, written.dataset_id)
    m.remove_manifest(ws, written.dataset_id)
    assert m.manifests(ws) == {}
