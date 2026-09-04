"""The dated arithmetic the data commands share: chunks, holes, and what a dataset reports.

These are the three places where an off-by-one day is invisible until a backfill silently
skips a session, so they are exercised directly rather than only through a command.
"""

from __future__ import annotations

from datetime import date

import pytest

from kanso.data.commands import CHUNK_DAYS, Series, chunked, missing
from kanso.data.manifest import Manifest


def day(text: str) -> date:
    return date.fromisoformat(text)


def span(start: str, end: str) -> tuple[date, date]:
    return (day(start), day(end))


def test_a_span_shorter_than_a_chunk_is_one_chunk() -> None:
    [chunk] = chunked(span("2024-01-02", "2024-01-05"))

    assert (chunk.start, chunk.end) == span("2024-01-02", "2024-01-05")
    assert chunk.days == 4


def test_chunks_are_consecutive_and_cover_the_span_exactly() -> None:
    chunks = chunked(span("2024-01-01", "2024-03-31"))

    assert chunks[0].start == day("2024-01-01")
    assert chunks[-1].end == day("2024-03-31")
    assert all(
        left.end + (right.start - left.end) == right.start
        for left, right in zip(chunks, chunks[1:], strict=False)
    )
    assert all(
        (right.start - left.end).days == 1 for left, right in zip(chunks, chunks[1:], strict=False)
    )
    assert max(chunk.days for chunk in chunks) <= CHUNK_DAYS
    assert sum(chunk.days for chunk in chunks) == 91


def test_nothing_held_means_the_whole_window_is_missing() -> None:
    assert missing([], span("2024-01-01", "2024-01-31")) == [span("2024-01-01", "2024-01-31")]


def test_a_span_covering_the_window_leaves_nothing_missing() -> None:
    assert missing([span("2023-12-01", "2024-02-01")], span("2024-01-01", "2024-01-31")) == []


def test_a_span_entirely_before_the_window_is_stepped_over() -> None:
    held = [span("2023-01-01", "2023-06-30")]

    assert missing(held, span("2024-01-01", "2024-01-31")) == [span("2024-01-01", "2024-01-31")]


def test_a_span_entirely_after_the_window_ends_the_walk() -> None:
    held = [span("2025-01-01", "2025-06-30")]

    assert missing(held, span("2024-01-01", "2024-01-31")) == [span("2024-01-01", "2024-01-31")]


def test_a_hole_inside_the_window_is_what_comes_back() -> None:
    held = [span("2024-01-01", "2024-01-10"), span("2024-01-21", "2024-01-31")]

    assert missing(held, span("2024-01-01", "2024-01-31")) == [span("2024-01-11", "2024-01-20")]


def test_a_held_span_overhanging_the_window_is_clipped_to_it() -> None:
    held = [span("2024-01-20", "2024-06-30")]

    assert missing(held, span("2024-01-01", "2024-01-31")) == [span("2024-01-01", "2024-01-19")]


def test_back_to_back_spans_leave_no_hole_between_them() -> None:
    """Coverage is counted in whole days, so a span ending the day another begins joins it."""
    held = [span("2024-01-01", "2024-01-15"), span("2024-01-16", "2024-01-31")]

    assert missing(held, span("2024-01-01", "2024-01-31")) == []


def manifest(**changes: object) -> Manifest:
    fields: dict[str, object] = {
        "schema": 1,
        "dataset_id": "DEMO.SIM-bar-1d-raw-20240131",
        "source": "vendor",
        "instrument": "DEMO.SIM",
        "type": "bar",
        "resolution": "1d",
        "span": span("2024-01-01", "2024-01-31"),
        "adjusted": False,
        "row_count": 22,
        "checksum": "a" * 64,
        "publication": "realtime",
    }
    return Manifest.model_validate({**fields, **changes})


def test_a_series_reports_the_holes_between_its_datasets() -> None:
    early = manifest(
        dataset_id="DEMO.SIM-bar-1d-raw-20240110", span=span("2024-01-01", "2024-01-10")
    )
    late = manifest(
        dataset_id="DEMO.SIM-bar-1d-raw-20240131", span=span("2024-01-21", "2024-01-31")
    )

    series = Series("DEMO.SIM", "bar", "1d", (early, late))

    assert series.spans == [span("2024-01-01", "2024-01-10"), span("2024-01-21", "2024-01-31")]
    assert series.gaps == [span("2024-01-11", "2024-01-20")]
    assert series.rows == 44


def test_a_dataset_reports_the_provenance_it_has_and_omits_the_rest() -> None:
    """A vendor dataset carries fields a file dataset does not, and neither is padded."""
    plain = Series("DEMO.SIM", "bar", "1d", (manifest(),)).payload()["datasets"][0]
    assert set(plain) == {
        "dataset_id",
        "source",
        "span",
        "rows",
        "adjusted",
        "publication",
        "checksum",
    }

    vendor = Series(
        "DEMO.SIM",
        "bar",
        "1d",
        (
            manifest(
                dataset_id="DEMO.SIM-bar-1d-adj-20240131",
                vendor="somebody",
                vendor_dataset="eod",
                publication="delayed",
                publication_rule="daily_bar_close",
                as_of=day("2024-02-01"),
                adjusted=True,
                adjustment_basis="split_and_dividend",
                supersedes="DEMO.SIM-bar-1d-raw-20231231",
            ),
        ),
    ).payload()["datasets"][0]

    assert vendor["vendor"] == "somebody"
    assert vendor["publication_rule"] == "daily_bar_close"
    assert vendor["as_of"] == "2024-02-01"
    assert vendor["adjustment_basis"] == "split_and_dividend"
    assert vendor["supersedes"] == "DEMO.SIM-bar-1d-raw-20231231"


@pytest.mark.parametrize("bad", ["2024-01-31", "2023-12-31"])
def test_a_window_that_starts_after_it_ends_asks_for_nothing(bad: str) -> None:
    assert missing([span("2024-01-01", "2024-12-31")], (day(bad), day("2023-01-01"))) == []
