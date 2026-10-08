"""Writing into the engine's own catalog: coverage, immutability and availability."""

from __future__ import annotations

from collections.abc import Iterator
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
    quote,
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


def test_a_supersede_takes_the_place_of_the_pinned_dataset_it_names(ws: FakeWorkspace) -> None:
    """The one way a pinned mistake is corrected: the successor overlaps what it names, the old
    files and manifest go, and the successor records what it followed."""
    first = write_bars(ws)
    define(ws)
    snap.freeze(ws)

    later = cat.write(
        ws,
        bars(date(2024, 1, 3), 5),
        ref=Ref(span=(date(2024, 1, 3), date(2024, 1, 7))),
        source="synthetic",
        supersedes=first.manifest.dataset_id,
    )

    assert later.replaced == (first.manifest.dataset_id,)
    assert later.manifest.supersedes == first.manifest.dataset_id
    assert first.manifest.dataset_id not in m.manifests(ws)
    assert later.manifest.dataset_id in m.manifests(ws)


def test_a_supersede_lifts_the_pin_of_the_dataset_it_names_and_no_other(
    ws: FakeWorkspace,
) -> None:
    first = write_bars(ws)
    other = write_bars(ws, start=date(2024, 1, 8))
    define(ws)
    snap.freeze(ws)

    with pytest.raises(
        PreconditionError, match=f"{other.manifest.dataset_id} is named by a snapshot"
    ):
        cat.write(
            ws,
            bars(date(2024, 1, 3), 8),
            ref=Ref(span=(date(2024, 1, 3), date(2024, 1, 10))),
            source="synthetic",
            supersedes=first.manifest.dataset_id,
        )
    assert first.manifest.dataset_id in m.manifests(ws)


def test_the_pinned_refusal_names_the_supersede_that_would_lift_it(ws: FakeWorkspace) -> None:
    first = write_bars(ws)
    define(ws)
    snap.freeze(ws)
    with pytest.raises(PreconditionError) as raised:
        write_bars(ws, start=date(2024, 1, 3))
    assert raised.value.remedy is not None
    assert f"--supersedes {first.manifest.dataset_id}" in raised.value.remedy


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


# --- what a write reads ----------------------------------------------------------

UNREADABLE = "a write that opens this manifest fails: it is not one it can clash with\n"


def unreadable(ws: FakeWorkspace, *held: cat.Written) -> None:
    """Make each held dataset's manifest fail to parse, so a write that opens one fails."""
    for written in held:
        m.manifest_file(ws, written.manifest.dataset_id).write_text(UNREADABLE, encoding="utf-8")


def test_a_write_opens_no_manifest_of_another_series(ws: FakeWorkspace) -> None:
    """What a write can clash with is its own series — the instrument, type and resolution the
    store files it under, adjusted or not — plus the dataset it names in `supersedes`. Every
    other series' manifest is unreadable here, and every way of writing still succeeds, so
    none of them opened one: what a write costs does not grow with what else is held."""
    minutes = Ref(resolution="1m", span=(JAN1, JAN1))
    others = [
        cat.write(ws, bars(JAN1, 5, MSFT), ref=Ref(instrument=MSFT), source="synthetic"),
        cat.write(ws, quotes(JAN1, 5), ref=Ref(type="quote", resolution=None), source="synthetic"),
        cat.write(ws, bars(JAN1, 1, spec="1-MINUTE-LAST-EXTERNAL"), ref=minutes, source="s"),
    ]
    held = write_bars(ws)
    define(ws, AAPL, MSFT)
    snap.freeze(ws)
    unreadable(ws, *others)

    later = write_bars(ws, start=date(2024, 1, 8), count=3)
    batched = cat.write(
        ws,
        iter(bars(date(2024, 1, 11), 3)),
        ref=Ref(span=(date(2024, 1, 11), date(2024, 1, 13))),
        source="synthetic",
        batch=2,
    )
    replaced = cat.write(
        ws,
        bars(date(2024, 1, 8), 3),
        ref=Ref(span=(date(2024, 1, 8), date(2024, 1, 10))),
        source="synthetic",
        replace=True,
    )
    successor = cat.write(
        ws, bars(JAN1, 5), ref=Ref(), source="synthetic", supersedes=held.manifest.dataset_id
    )

    assert replaced.replaced == (later.manifest.dataset_id,)
    assert successor.replaced == (held.manifest.dataset_id,)
    assert batched.manifest.span == (date(2024, 1, 11), date(2024, 1, 13))
    for written in others:
        path = m.manifest_file(ws, written.manifest.dataset_id)
        assert path.read_text(encoding="utf-8") == UNREADABLE


def test_a_write_opens_no_manifest_of_its_series_that_ends_before_it_begins(
    ws: FakeWorkspace,
) -> None:
    """A dataset that ends before a write begins can neither overlap it nor share its id, and
    an id carries its end, so a chunked load or a backfill written forwards opens none of
    the chunks it already wrote: its cost does not grow with what it has written."""
    written: list[cat.Written] = []
    for start in [date(2024, 1, 1 + 7 * n) for n in range(4)]:
        unreadable(ws, *written)
        chunk = (start, date.fromordinal(start.toordinal() + 6))
        written.append(
            cat.write(ws, iter(bars(start, 7)), ref=Ref(span=chunk), source="synthetic", batch=3)
        )

    assert len(cat.open_catalog(ws).bars()) == 28


def test_a_write_still_refuses_the_clash_it_reads_by_name(ws: FakeWorkspace) -> None:
    """Reading by name keeps every clash: the same span unadjusted and adjusted, an overlap
    ending later than the write, and a `supersedes` that names no held dataset or no id."""
    write_bars(ws)
    with pytest.raises(PreconditionError, match="overlaps the held dataset"):
        cat.write(
            ws,
            bars(date(2023, 12, 30), 3),
            ref=Ref(adjusted=True, span=(date(2023, 12, 30), JAN1)),
            source="vendor_file",
            adjustment_basis="close 2024-06-01",
        )
    for named in ("AAPL.XNAS-bar-1d-raw-20240106", "not a dataset id"):
        with pytest.raises(PreconditionError, match="not a dataset this workspace holds"):
            cat.write(
                ws,
                bars(date(2024, 1, 8), 2),
                ref=Ref(span=(date(2024, 1, 8), date(2024, 1, 9))),
                source="synthetic",
                supersedes=named,
            )


def test_a_file_another_series_gains_during_a_write_is_not_the_write_s(
    ws: FakeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write lists only the directory its series is filed in, so a file another series
    gains meanwhile — another process loading another name — is neither recorded as this
    dataset's bytes nor removed when this write fails."""
    cat.write(ws, bars(JAN1, 5, MSFT), ref=Ref(instrument=MSFT), source="synthetic")
    elsewhere = next((m.data_path(ws) / "bar").glob("MSFT*")) / "gained.parquet"
    engine = cat.ParquetDataCatalog.write_data

    def alongside(self: cat.ParquetDataCatalog, data: list[object]) -> None:
        engine(self, data)
        elsewhere.write_bytes(b"another series' bytes")

    monkeypatch.setattr(cat.ParquetDataCatalog, "write_data", alongside)
    written = write_bars(ws)
    elsewhere.unlink()

    assert written.files == (
        "bar/AAPL.XNAS-1-DAY-LAST-EXTERNAL/"
        "2024-01-01T16-00-00-000000000Z_2024-01-05T16-00-00-000000000Z.parquet",
    )
    assert cat._checksum(m.data_path(ws), written.files) == written.manifest.checksum

    def failing(self: cat.ParquetDataCatalog, data: list[object]) -> None:
        elsewhere.write_bytes(b"another series' bytes")
        raise OSError("no space left on device")

    monkeypatch.setattr(cat.ParquetDataCatalog, "write_data", failing)
    with pytest.raises(OSError, match="no space"):
        write_bars(ws, start=date(2024, 1, 8), count=2)
    assert elsewhere.is_file()


# --- a write a batch at a time --------------------------------------------------


def prints(start: date = JAN1, count: int = 5, per_instant: int = 3, lag_ns: int = 0) -> list:
    """`per_instant` prints at each day's close: several points share every instant."""
    return [
        trade(AAPL, day, lag_ns, seq)
        for day in [date.fromordinal(start.toordinal() + n) for n in range(count)]
        for seq in range(per_instant)
    ]


TRADES = Ref(type="trade", resolution=None)


def test_a_batched_write_cuts_between_instants_and_reads_back_as_one_dataset(
    ws: FakeWorkspace, other_ws: FakeWorkspace
) -> None:
    """Fifteen prints, three to an instant, in batches of four: no instant is split, every
    file holds whole instants, and the store reads back what a whole write reads back."""
    batched = cat.write(ws, iter(prints()), ref=TRADES, source="synthetic", batch=4)
    whole = cat.write(other_ws, prints(), ref=TRADES, source="synthetic")

    assert len(batched.files) == 3
    assert batched.manifest.row_count == whole.manifest.row_count == 15
    assert batched.manifest.span == whole.manifest.span == (JAN1, date(2024, 1, 5))
    assert batched.manifest.dataset_id == whole.manifest.dataset_id
    assert batched.manifest.checksum == cat._checksum(m.data_path(ws), batched.files)
    assert m.read_manifest(ws, batched.manifest.dataset_id) == batched.manifest
    read = cat.open_catalog(ws).trade_ticks()
    assert [t.to_dict(t) for t in read] == [
        t.to_dict(t) for t in cat.open_catalog(other_ws).trade_ticks()
    ]
    intervals = sorted(cat.open_catalog(ws).get_intervals(TradeTick, AAPL))
    assert len(intervals) == 3
    assert all(a[1] < b[0] for a, b in zip(intervals, intervals[1:], strict=False))


def test_a_refusal_in_a_later_batch_leaves_no_file_and_no_manifest(ws: FakeWorkspace) -> None:
    late = prints(count=2) + prints(date(2024, 1, 3), count=1, lag_ns=-1)
    with pytest.raises(ValidationError, match="cannot precede"):
        cat.write(ws, iter(late), ref=TRADES, source="synthetic", batch=3)

    assert not [p for p in m.data_path(ws).rglob("*") if p.is_file()]
    assert m.manifests(ws) == {}


def test_a_batched_write_refuses_points_that_go_back_in_time(ws: FakeWorkspace) -> None:
    backwards = prints(date(2024, 1, 2), count=1) + prints(JAN1, count=1)
    with pytest.raises(ValidationError, match="availability order"):
        cat.write(ws, iter(backwards), ref=TRADES, source="synthetic", batch=2)
    assert m.manifests(ws) == {}


def test_a_batched_write_of_nothing_is_refused(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="no points were served"):
        cat.write(ws, iter(()), ref=TRADES, source="synthetic", batch=2)


def test_a_batched_write_holds_one_series_in_every_batch(ws: FakeWorkspace) -> None:
    mixed = prints(count=2) + [quote(AAPL, date(2024, 1, 3))]
    with pytest.raises(ValidationError, match="holds one series"):
        cat.write(ws, iter(mixed), ref=TRADES, source="synthetic", batch=3)
    assert not [p for p in m.data_path(ws).rglob("*") if p.is_file()]


def test_a_batched_write_checks_the_declared_instrument(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="declares"):
        cat.write(ws, iter(prints()), ref=Ref(MSFT, "trade", None), source="x", batch=4)


def test_a_batched_delayed_dataset_still_needs_its_rule(ws: FakeWorkspace) -> None:
    with pytest.raises(ValidationError, match="must name the rule"):
        cat.write(
            ws,
            iter(quotes(JAN1, 3, lag_ns=FIFTEEN_MINUTES)),
            ref=Ref(type="quote", resolution=None, publication="delayed"),
            source="synthetic",
            batch=2,
        )


def test_a_batched_write_is_refused_the_clash_a_whole_write_is(ws: FakeWorkspace) -> None:
    """The clash is checked once, over the span asked for, before the first file."""
    held = cat.write(ws, prints(), ref=TRADES, source="synthetic")
    with pytest.raises(PreconditionError, match="overlaps"):
        cat.write(ws, iter(prints()), ref=TRADES, source="synthetic", batch=4)
    with pytest.raises(PreconditionError, match="not a dataset this workspace holds"):
        cat.write(ws, iter(prints()), ref=TRADES, source="x", batch=4, supersedes="nope")

    again = cat.write(ws, iter(prints()), ref=TRADES, source="x", batch=4, replace=True)
    assert again.replaced == (held.manifest.dataset_id,)
    assert len(cat.open_catalog(ws).trade_ticks()) == 15


def test_a_batched_write_that_produces_no_bytes_is_refused(ws: FakeWorkspace) -> None:
    written = cat.write(ws, prints(), ref=TRADES, source="synthetic")
    m.remove_manifest(ws, written.manifest.dataset_id)
    with pytest.raises(PreconditionError, match="wrote no bytes"):
        cat.write(ws, iter(prints()), ref=TRADES, source="synthetic", batch=100)


def broken(after: int) -> Iterator[TradeTick]:
    """A source that serves `after` days of prints and then raises, as a loader that meets a
    refused message part-way through a day does."""
    yield from prints(count=after)
    raise ValidationError("the source refused a message part-way")


def kept(ws: FakeWorkspace, held: cat.Written) -> None:
    """`held` is in the store as it was written: its manifest, its files, its prints."""
    assert m.manifests(ws) == {held.manifest.dataset_id: held.manifest}
    assert cat._checksum(m.data_path(ws), held.files) == held.manifest.checksum
    assert len(cat.open_catalog(ws).trade_ticks()) == 15
    assert not (m.catalog_path(ws) / cat.ASIDE_DIR).exists()


def test_a_batched_replace_that_fails_later_keeps_the_dataset_it_replaced(
    ws: FakeWorkspace,
) -> None:
    """The replace removes the held dataset before the first file, so a failure two batches
    later must put it back: files, prints and manifest."""
    held = cat.write(ws, prints(), ref=TRADES, source="synthetic")

    with pytest.raises(ValidationError, match="part-way"):
        cat.write(ws, broken(2), ref=TRADES, source="x", batch=3, replace=True)

    kept(ws, held)


def test_a_batched_supersede_that_fails_later_keeps_the_pinned_dataset(
    ws: FakeWorkspace,
) -> None:
    held = cat.write(ws, prints(), ref=TRADES, source="synthetic")
    define(ws)
    snap.freeze(ws)

    with pytest.raises(ValidationError, match="part-way"):
        cat.write(
            ws, broken(2), ref=TRADES, source="x", batch=3, supersedes=held.manifest.dataset_id
        )

    kept(ws, held)
    with pytest.raises(PreconditionError, match="named by a snapshot"):
        cat.write(ws, iter(prints()), ref=TRADES, source="x", batch=4, replace=True)


def test_an_interrupted_batched_replace_keeps_the_dataset_it_replaced(ws: FakeWorkspace) -> None:
    held = cat.write(ws, prints(), ref=TRADES, source="synthetic")

    def interrupted() -> Iterator[TradeTick]:
        yield from prints(count=2)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cat.write(ws, interrupted(), ref=TRADES, source="x", batch=3, replace=True)

    kept(ws, held)


def test_a_whole_replace_the_engine_fails_to_write_keeps_the_dataset_it_replaced(
    ws: FakeWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = cat.write(ws, prints(), ref=TRADES, source="synthetic")

    def failing(self: object, data: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(cat.ParquetDataCatalog, "write_data", failing)
    with pytest.raises(OSError, match="no space"):
        cat.write(ws, prints(), ref=TRADES, source="x", replace=True)
    monkeypatch.undo()

    kept(ws, held)


def test_a_replace_that_is_written_lets_the_old_files_go(ws: FakeWorkspace) -> None:
    cat.write(ws, prints(), ref=TRADES, source="synthetic")

    again = cat.write(ws, iter(prints()), ref=TRADES, source="x", batch=4, replace=True)

    assert not (m.catalog_path(ws) / cat.ASIDE_DIR).exists()
    root = m.data_path(ws)
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*.parquet")) == list(
        again.files
    )
