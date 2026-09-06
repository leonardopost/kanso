"""Writing into the engine's own catalog: coverage, immutability and availability."""

from __future__ import annotations

from datetime import date

import pytest
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick

from kanso.data import catalog as cat
from kanso.data import manifest as m
from kanso.data import snapshot as snap
from kanso.errors import Exit, PreconditionError, ValidationError
from tests.data.catalog.conftest import (
    AAPL,
    DAILY,
    MSFT,
    FakeWorkspace,
    Ref,
    bar,
    bars,
    define,
    equity,
    quotes,
    trade,
)

JAN1 = date(2024, 1, 1)
FIFTEEN_MINUTES = 15 * 60 * cat.NANOS_PER_SECOND
BAR_TYPE = f"{AAPL}-{DAILY}"


def write_bars(
    ws: FakeWorkspace,
    start: date = JAN1,
    count: int = 5,
    *,
    requested: tuple[date, date] | None = None,
    **overrides: object,
) -> cat.Written:
    span = requested or (start, date.fromordinal(start.toordinal() + count - 1))
    ref = Ref(span=span, **overrides)
    return cat.write(ws, bars(start, count), ref=ref, source="synthetic")


def test_a_dataset_round_trips_through_the_store(ws: FakeWorkspace) -> None:
    written = write_bars(ws)
    manifest = written.manifest

    assert manifest.dataset_id == "AAPL.XNAS-bar-1d-raw-20240105"
    assert manifest.span == (JAN1, date(2024, 1, 5))
    assert manifest.row_count == 5
    assert manifest.source == "synthetic"
    assert not written.truncated
    assert written.shortfall is None
    assert m.read_manifest(ws, manifest.dataset_id) == manifest

    read = cat.open_catalog(ws).bars()
    assert len(read) == 5
    assert {str(point.bar_type) for point in read} == {BAR_TYPE}


def test_the_checksum_covers_the_bytes_that_were_written(ws: FakeWorkspace) -> None:
    written = write_bars(ws)
    assert written.files
    assert all(name.startswith("bar/") for name in written.files)
    assert len(written.manifest.checksum) == 64


def test_two_stores_written_the_same_way_agree(ws: FakeWorkspace, other_ws: FakeWorkspace) -> None:
    """The checksum is a fact about the data, not about the machine or the moment."""
    assert write_bars(ws).manifest.checksum == write_bars(other_ws).manifest.checksum


def test_coverage_is_what_was_served_not_what_was_asked(ws: FakeWorkspace) -> None:
    written = write_bars(ws, count=5, requested=(JAN1, date(2024, 1, 10)))

    assert written.requested == (JAN1, date(2024, 1, 10))
    assert written.served == (JAN1, date(2024, 1, 5))
    assert written.truncated
    assert written.shortfall is not None
    assert "2024-01-06 to 2024-01-10 at the end" in written.shortfall
    assert written.manifest.span == written.served
    assert written.manifest.dataset_id.endswith("20240105")


def test_an_empty_result_is_not_a_dataset(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError) as raised:
        cat.write(ws, [], ref=Ref(), source="synthetic")
    assert raised.value.code is Exit.VALIDATION
    assert "no points were served" in raised.value.message


def test_a_dataset_holds_one_series(ws: FakeWorkspace) -> None:
    mixed = bars(JAN1, 2) + bars(JAN1, 2, instrument=MSFT)
    with pytest.raises(ValidationError, match="a dataset holds one series"):
        cat.write(ws, mixed, ref=Ref(), source="synthetic")


def test_the_declared_instrument_must_be_the_one_in_the_points(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="but its points carry"):
        cat.write(ws, bars(JAN1, 2), ref=Ref(instrument=MSFT), source="synthetic")


def test_a_successor_must_name_a_dataset_the_workspace_holds(ws: FakeWorkspace) -> None:
    with pytest.raises(PreconditionError, match="not a dataset this workspace holds"):
        cat.write(
            ws,
            bars(JAN1, 2),
            ref=Ref(span=(JAN1, date(2024, 1, 2))),
            source="synthetic",
            supersedes="AAPL.XNAS-bar-1d-raw-20231231",
        )


def test_a_successor_records_what_it_follows(ws: FakeWorkspace) -> None:
    first = write_bars(ws)
    define(ws)
    snap.freeze(ws)
    later = cat.write(
        ws,
        bars(date(2024, 1, 8), 3),
        ref=Ref(span=(date(2024, 1, 8), date(2024, 1, 10))),
        source="synthetic",
        supersedes=first.manifest.dataset_id,
    )
    assert later.manifest.supersedes == first.manifest.dataset_id
    assert later.manifest.dataset_id != first.manifest.dataset_id


def test_a_pinned_dataset_cannot_be_overwritten(ws: FakeWorkspace) -> None:
    write_bars(ws)
    define(ws)
    snap.freeze(ws)
    with pytest.raises(PreconditionError) as raised:
        write_bars(ws, start=date(2024, 1, 3))
    assert raised.value.code is Exit.PRECONDITION
    assert "named by a snapshot" in raised.value.message
    assert raised.value.remedy is not None
    assert "supersedes" in raised.value.remedy


def test_replace_does_not_lift_a_snapshot_pin(ws: FakeWorkspace) -> None:
    write_bars(ws)
    define(ws)
    snap.freeze(ws)
    with pytest.raises(PreconditionError, match="named by a snapshot"):
        cat.write(ws, bars(JAN1, 5), ref=Ref(), source="synthetic", replace=True)


def test_an_overlapping_write_into_unpinned_data_is_refused(ws: FakeWorkspace) -> None:
    write_bars(ws)
    with pytest.raises(PreconditionError) as raised:
        write_bars(ws, start=date(2024, 1, 3))
    assert raised.value.code is Exit.PRECONDITION
    assert raised.value.remedy is not None
    assert "--replace" in raised.value.remedy


def test_an_explicit_replace_rewrites_the_overlapped_span(ws: FakeWorkspace) -> None:
    first = write_bars(ws)
    second = cat.write(
        ws,
        bars(date(2024, 1, 3), 5),
        ref=Ref(span=(date(2024, 1, 3), date(2024, 1, 7))),
        source="synthetic",
        replace=True,
    )
    assert second.replaced == (first.manifest.dataset_id,)
    assert set(m.manifests(ws)) == {second.manifest.dataset_id}
    read = cat.open_catalog(ws).bars()
    assert cat.served_span(read) == (date(2024, 1, 3), date(2024, 1, 7))


def test_a_disjoint_write_of_the_same_series_is_a_second_dataset(ws: FakeWorkspace) -> None:
    first = write_bars(ws)
    second = write_bars(ws, start=date(2024, 1, 8), count=3)
    assert first.manifest.dataset_id != second.manifest.dataset_id
    assert set(m.manifests(ws)) == {first.manifest.dataset_id, second.manifest.dataset_id}
    assert len(cat.open_catalog(ws).bars()) == 8


def test_a_write_that_produces_no_bytes_is_refused(ws: FakeWorkspace) -> None:
    """A store already holding the interval would otherwise be recorded as freshly written."""
    written = write_bars(ws)
    m.remove_manifest(ws, written.manifest.dataset_id)
    with pytest.raises(PreconditionError) as raised:
        write_bars(ws)
    assert "wrote no bytes" in raised.value.message


def test_availability_before_the_reference_time_is_refused(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError) as raised:
        cat.write(ws, bars(JAN1, 3, lag_ns=-1), ref=Ref(), source="synthetic")
    assert raised.value.code is Exit.VALIDATION
    assert "cannot precede" in raised.value.message


def test_a_delayed_dataset_with_no_rule_is_refused(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="must name the rule"):
        cat.write(
            ws,
            quotes(JAN1, 3, lag_ns=FIFTEEN_MINUTES),
            ref=Ref(type="quote", resolution=None, publication="delayed"),
            source="synthetic",
        )


def test_a_delayed_dataset_stamped_from_its_reference_time_is_refused(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="delay is not in its timestamps"):
        cat.write(
            ws,
            quotes(JAN1, 3),
            ref=Ref(
                type="quote",
                resolution=None,
                publication="delayed",
                publication_rule="delayed_quote",
            ),
            source="synthetic",
        )


def test_a_delayed_dataset_the_rule_derives_is_written(ws: FakeWorkspace) -> None:
    written = cat.write(
        ws,
        quotes(JAN1, 3, lag_ns=FIFTEEN_MINUTES),
        ref=Ref(
            span=(JAN1, date(2024, 1, 3)),
            type="quote",
            resolution=None,
            publication="delayed",
            publication_rule="delayed_quote",
        ),
        source="synthetic",
    )
    assert written.manifest.publication == "delayed"
    assert written.manifest.publication_rule == "delayed_quote"
    assert written.manifest.dataset_id == "AAPL.XNAS-quote-none-raw-20240103"
    assert len(cat.open_catalog(ws).quote_ticks()) == 3


def test_an_adjusted_dataset_must_name_its_basis(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="adjustment_basis"):
        cat.write(ws, bars(JAN1, 3), ref=Ref(adjusted=True), source="synthetic")


def test_an_adjusted_dataset_records_the_date_it_was_adjusted_as_of(ws: FakeWorkspace) -> None:
    written = cat.write(
        ws,
        bars(JAN1, 3),
        ref=Ref(span=(JAN1, date(2024, 1, 3)), adjusted=True),
        source="vendor_file",
        adjustment_basis="close 2024-06-01",
        as_of=date(2024, 6, 1),
    )
    assert written.manifest.adjusted
    assert written.manifest.as_of == date(2024, 6, 1)
    assert written.manifest.dataset_id.split("-")[3] == "adj"


def test_the_vendor_and_its_request_travel_with_the_dataset(ws: FakeWorkspace) -> None:
    written = cat.write(
        ws,
        bars(JAN1, 3),
        ref=Ref(
            span=(JAN1, date(2024, 1, 3)),
            vendor="acme",
            vendor_dataset="US_EQUITY_EOD",
            request_params={"symbol": "AAPL", "adjusted": "false"},
        ),
        source="acme",
    )
    assert written.manifest.vendor == "acme"
    assert written.manifest.request_params == {"symbol": "AAPL", "adjusted": "false"}


def test_a_window_is_read_by_availability(ws: FakeWorkspace) -> None:
    write_bars(ws, count=10)
    loaded = cat.load_window(ws, Bar, BAR_TYPE, (date(2024, 1, 3), date(2024, 1, 5)))
    assert len(loaded) == 3
    assert loaded.primer is None
    assert cat.served_span(loaded.points) == (date(2024, 1, 3), date(2024, 1, 5))


def test_a_primed_window_also_loads_the_last_point_published_before_it(
    ws: FakeWorkspace,
) -> None:
    published = [
        trade(AAPL, date(2023, 12, 1), seq=1),
        trade(AAPL, date(2023, 12, 20), seq=2),
        trade(AAPL, date(2024, 1, 3), seq=3),
    ]
    cat.write(
        ws,
        published,
        ref=Ref(
            type="fundamental",
            resolution=None,
            span=(date(2023, 12, 1), date(2024, 1, 3)),
        ),
        source="synthetic",
    )
    loaded = cat.load_window(ws, TradeTick, AAPL, (JAN1, date(2024, 1, 31)), rule="fundamental")
    assert len(loaded) == 1
    assert loaded.primer is not None
    assert cat.day_of(loaded.primer.ts_init) == date(2023, 12, 20)


def test_an_unprimed_data_class_gets_no_primer(ws: FakeWorkspace) -> None:
    cat.write(
        ws,
        [trade(AAPL, date(2023, 12, 20), seq=1), trade(AAPL, date(2024, 1, 3), seq=2)],
        ref=Ref(type="trade", resolution=None, span=(date(2023, 12, 20), date(2024, 1, 3))),
        source="synthetic",
    )
    loaded = cat.load_window(ws, TradeTick, AAPL, (JAN1, date(2024, 1, 31)))
    assert loaded.primer is None


def test_a_window_opening_at_the_epoch_has_nothing_to_prime_from(ws: FakeWorkspace) -> None:
    cat.write(
        ws,
        [trade(AAPL, date(1970, 1, 1), seq=1)],
        ref=Ref(type="fundamental", resolution=None, span=(date(1970, 1, 1), date(1970, 1, 1))),
        source="synthetic",
    )
    loaded = cat.load_window(
        ws, TradeTick, AAPL, (date(1970, 1, 1), date(1970, 1, 2)), rule="fundamental"
    )
    assert len(loaded) == 1
    assert loaded.primer is None


def test_a_window_may_be_read_without_naming_an_instrument(ws: FakeWorkspace) -> None:
    cat.write(
        ws,
        quotes(JAN1, 3),
        ref=Ref(type="quote", resolution=None, span=(JAN1, date(2024, 1, 3))),
        source="synthetic",
    )
    loaded = cat.load_window(ws, QuoteTick, None, (JAN1, date(2024, 1, 3)))
    assert len(loaded) == 3


def test_the_instrument_definitions_are_in_the_same_store(ws: FakeWorkspace) -> None:
    empty = cat.resolved_instruments_checksum(ws)
    cat.open_catalog(ws).write_data([equity()])
    resolved = cat.resolved_instruments_checksum(ws)

    assert resolved != empty
    assert cat.resolved_instruments_checksum(ws) == resolved
    assert [str(found.id) for found in cat.open_catalog(ws).instruments()] == [AAPL]


def test_a_reassigned_tick_size_changes_the_instrument_checksum(
    ws: FakeWorkspace, other_ws: FakeWorkspace
) -> None:
    cat.open_catalog(ws).write_data([equity()])
    cat.open_catalog(other_ws).write_data([equity(increment="0.0001")])
    assert cat.resolved_instruments_checksum(ws) != cat.resolved_instruments_checksum(other_ws)


def test_a_dated_window_covers_whole_utc_days() -> None:
    start, end = cat.window_ns((JAN1, JAN1))
    assert cat.day_of(start) == JAN1
    assert cat.day_of(end) == JAN1
    assert end - start == cat.DAY_END_NANOS


def test_a_point_is_filed_under_its_bar_type_or_its_instrument() -> None:
    assert cat.identity(bar(AAPL, JAN1)) == (Bar, BAR_TYPE, AAPL)
    assert cat.identity(trade(AAPL, JAN1)) == (TradeTick, AAPL, AAPL)


def test_a_market_wide_series_belongs_to_no_instrument() -> None:
    """The store files such a series under its class alone, and so does the writer."""

    class MarketWide:
        ts_event = 0
        ts_init = 0

    assert cat.identity(MarketWide()) == (MarketWide, None, None)


def test_an_adjusted_series_clashes_with_the_unadjusted_one_it_shares_a_file_with(
    ws: FakeWorkspace,
) -> None:
    """Two datasets to kanso, one bar type to the store — so only one of them fits."""
    write_bars(ws)
    with pytest.raises(PreconditionError) as raised:
        cat.write(
            ws,
            bars(JAN1, 5),
            ref=Ref(adjusted=True),
            source="vendor_file",
            adjustment_basis="close 2024-06-01",
        )
    assert raised.value.remedy is not None
    assert "--replace" in raised.value.remedy
