"""The file loader: everything it refuses to guess, and everything it reads."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from kanso.data.loader import get_loader
from kanso.data.loaders.csv_parquet import CsvParquetLoader, columns_for
from kanso.errors import ValidationError

from .conftest import BAR_HEADER, BAR_ROWS, bar_entry, write_csv

LOADER = CsvParquetLoader()
ZONE = "America/New_York"


def spec(*entries: dict[str, Any], **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"loader": "csv_parquet", "timezone": ZONE, "files": list(entries)}
    out.update(over)
    return out


@pytest.fixture
def bars(tmp_path: Path) -> Path:
    return write_csv(tmp_path / "bars.csv", BAR_HEADER, BAR_ROWS)


def test_the_registry_serves_the_file_loader() -> None:
    assert get_loader("csv_parquet").id == "csv_parquet"


def test_a_spec_that_does_not_name_a_time_zone_is_refused(bars: Path) -> None:
    """The one fact a file cannot supply about itself, and the one whose guess is silent."""
    payload = spec(bar_entry(bars))
    del payload["timezone"]
    with pytest.raises(ValidationError, match="name the IANA time zone"):
        LOADER.discover(payload)


def test_an_unknown_time_zone_is_refused(bars: Path) -> None:
    with pytest.raises(ValidationError, match="not an IANA time zone"):
        LOADER.discover(spec(bar_entry(bars), timezone="Mars/Olympus"))


def test_a_spec_that_maps_no_columns_is_refused(bars: Path) -> None:
    entry = bar_entry(bars)
    entry["columns"] = {}
    with pytest.raises(ValidationError, match="maps no columns"):
        LOADER.discover(spec(entry))


def test_a_missing_field_in_the_column_map_is_refused(bars: Path) -> None:
    entry = bar_entry(bars)
    entry["columns"].pop("volume")
    with pytest.raises(ValidationError, match="maps no column for volume"):
        LOADER.discover(spec(entry))


def test_a_column_the_type_has_no_field_for_is_refused(bars: Path) -> None:
    entry = bar_entry(bars)
    entry["columns"]["spread"] = "s"
    with pytest.raises(ValidationError, match="which 'bar' has no field for"):
        LOADER.discover(spec(entry))


def test_a_column_the_file_does_not_hold_is_refused(bars: Path) -> None:
    entry = bar_entry(bars)
    entry["columns"]["close"] = "settlement"
    with pytest.raises(ValidationError, match="has no column 'settlement'"):
        ref = LOADER.discover(spec(entry))[0]
        list(LOADER.load(ref, ref.span))


def test_the_naive_timestamps_are_read_in_the_declared_zone(bars: Path) -> None:
    """09:35 in New York is 14:35 UTC in March, and the loader says so."""
    ref = LOADER.discover(spec(bar_entry(bars)))[0]
    points = list(LOADER.load(ref, ref.span))
    assert len(points) == 3
    assert points[0].ts_event == 1_709_562_900_000_000_000
    utc = LOADER.discover(spec(bar_entry(bars), timezone="UTC"))[0]
    assert list(LOADER.load(utc, utc.span))[0].ts_event != points[0].ts_event


def test_the_bar_columns_land_where_the_map_says(bars: Path) -> None:
    ref = LOADER.discover(spec(bar_entry(bars)))[0]
    first = list(LOADER.load(ref, ref.span))[0]
    assert str(first.bar_type) == "DEMO.XNAS-5-MINUTE-LAST-EXTERNAL"
    assert str(first.open) == "100.00"
    assert str(first.high) == "100.50"
    assert str(first.low) == "99.80"
    assert str(first.close) == "100.20"
    assert str(first.volume) == "1200"


def test_the_served_span_is_measured_not_declared(bars: Path) -> None:
    ref = LOADER.discover(spec(bar_entry(bars)))[0]
    assert ref.span == (date(2024, 3, 4), date(2024, 3, 5))
    assert ref.dataset_id == "DEMO.XNAS-bar-5m-raw-20240305"
    manifest = LOADER.manifest(ref)
    assert manifest.span == ref.span
    assert manifest.row_count == 3
    assert manifest.source == "csv_parquet"


def test_a_window_selects_rows(bars: Path) -> None:
    ref = LOADER.discover(spec(bar_entry(bars)))[0]
    day = list(LOADER.load(ref, (date(2024, 3, 5), date(2024, 3, 5))))
    assert len(day) == 1


def test_quotes_map_four_sides(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "quotes.csv",
        ["t", "bid", "ask", "bq", "aq"],
        [["2024-03-04T09:35:00", "99.98", "100.02", "300", "400"]],
    )
    entry = {
        "path": str(path),
        "instrument": "DEMO",
        "venue": "XNAS",
        "type": "quote",
        "columns": {
            "ts_event": "t",
            "bid_price": "bid",
            "ask_price": "ask",
            "bid_size": "bq",
            "ask_size": "aq",
        },
    }
    ref = LOADER.discover(spec(entry))[0]
    quote = list(LOADER.load(ref, ref.span))[0]
    assert str(quote.bid_price) == "99.98"
    assert str(quote.ask_size) == "400"
    assert ref.resolution is None


def test_trades_take_a_side_and_an_id_when_mapped(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "trades.csv",
        ["t", "p", "q", "side", "id"],
        [
            ["2024-03-04T09:35:00", "100.01", "10", "sell", "T1"],
            ["2024-03-04T09:35:01", "100.02", "20", "BUYER", "T2"],
        ],
    )
    entry = {
        "path": str(path),
        "instrument": "DEMO",
        "venue": "XNAS",
        "type": "trade",
        "columns": {
            "ts_event": "t",
            "price": "p",
            "size": "q",
            "aggressor_side": "side",
            "trade_id": "id",
        },
    }
    ref = LOADER.discover(spec(entry))[0]
    trades = list(LOADER.load(ref, ref.span))
    assert [str(trade.trade_id) for trade in trades] == ["T1", "T2"]


def test_an_unreadable_side_is_refused(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "trades.csv",
        ["t", "p", "q", "side"],
        [["2024-03-04T09:35:00", "100.01", "10", "hit"]],
    )
    entry = {
        "path": str(path),
        "instrument": "DEMO",
        "venue": "XNAS",
        "type": "trade",
        "columns": {"ts_event": "t", "price": "p", "size": "q", "aggressor_side": "side"},
    }
    ref = LOADER.discover(spec(entry))[0]
    with pytest.raises(ValidationError, match="is not a side"):
        list(LOADER.load(ref, ref.span))


def test_a_numeric_timestamp_needs_its_unit(tmp_path: Path) -> None:
    """Seconds, milliseconds and nanoseconds are one column of integers until declared."""
    path = write_csv(
        tmp_path / "epoch.csv", BAR_HEADER, [["1709562900", "1", "2", "0.5", "1.5", "10"]]
    )
    with pytest.raises(ValidationError, match="does not say their unit"):
        LOADER.discover(spec(bar_entry(path)))
    ref = LOADER.discover(spec(bar_entry(path, timestamp_unit="s")))[0]
    assert list(LOADER.load(ref, ref.span))[0].ts_event == 1_709_562_900_000_000_000


@pytest.mark.parametrize(
    ("unit", "raw"),
    [("s", "1709562900"), ("ms", "1709562900000"), ("us", "1709562900000000")],
)
def test_every_epoch_unit_reaches_the_same_instant(tmp_path: Path, unit: str, raw: str) -> None:
    path = write_csv(tmp_path / f"{unit}.csv", BAR_HEADER, [[raw, "1", "2", "0.5", "1.5", "10"]])
    ref = LOADER.discover(spec(bar_entry(path, timestamp_unit=unit)))[0]
    assert list(LOADER.load(ref, ref.span))[0].ts_event == 1_709_562_900_000_000_000


def test_a_timestamp_that_is_neither_is_refused(tmp_path: Path) -> None:
    row = ["yesterday", "1", "2", "0.5", "1.5", "10"]
    path = write_csv(tmp_path / "bad.csv", BAR_HEADER, [row])
    with pytest.raises(ValidationError, match="neither an ISO-8601 timestamp"):
        LOADER.discover(spec(bar_entry(path)))


def test_a_price_that_is_not_a_number_is_refused(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "bad.csv", BAR_HEADER, [["2024-03-04T09:35:00", "n/a", "2", "0.5", "1.5", "10"]]
    )
    ref = LOADER.discover(spec(bar_entry(path)))[0]
    with pytest.raises(ValidationError, match="is not a number"):
        list(LOADER.load(ref, ref.span))


def test_an_offset_in_the_file_beats_the_declared_zone(tmp_path: Path) -> None:
    """A timestamp that carries its own offset is already unambiguous."""
    path = write_csv(
        tmp_path / "aware.csv",
        BAR_HEADER,
        [["2024-03-04T14:35:00+00:00", "1", "2", "0.5", "1.5", "10"]],
    )
    ref = LOADER.discover(spec(bar_entry(path)))[0]
    assert list(LOADER.load(ref, ref.span))[0].ts_event == 1_709_562_900_000_000_000


def test_a_mapped_ts_init_is_the_availability_instant(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "delayed.csv",
        [*BAR_HEADER, "pub"],
        [["2024-03-04T09:35:00", "1", "2", "0.5", "1.5", "10", "2024-03-04T09:50:00"]],
    )
    entry = bar_entry(path)
    entry["columns"]["ts_init"] = "pub"
    ref = LOADER.discover(spec(entry))[0]
    bar = list(LOADER.load(ref, ref.span))[0]
    assert bar.ts_init - bar.ts_event == 15 * 60 * 1_000_000_000


def test_availability_before_the_event_is_refused(tmp_path: Path) -> None:
    """The invariant every loader owes: information cannot be public before it exists."""
    path = write_csv(
        tmp_path / "backwards.csv",
        [*BAR_HEADER, "pub"],
        [["2024-03-04T09:35:00", "1", "2", "0.5", "1.5", "10", "2024-03-04T09:20:00"]],
    )
    entry = bar_entry(path)
    entry["columns"]["ts_init"] = "pub"
    ref = LOADER.discover(spec(entry))[0]
    with pytest.raises(ValidationError, match="availability cannot precede"):
        list(LOADER.load(ref, ref.span))


def test_a_delayed_dataset_must_map_and_name_its_publication(bars: Path) -> None:
    with pytest.raises(ValidationError, match="maps no ts_init column"):
        LOADER.discover(spec(bar_entry(bars, publication="delayed")))
    entry = bar_entry(bars, publication="delayed")
    entry["columns"]["ts_init"] = "t"
    with pytest.raises(ValidationError, match="must come from a named rule"):
        LOADER.discover(spec(entry))


def test_an_unknown_publication_class_is_refused(bars: Path) -> None:
    with pytest.raises(ValidationError, match="not a publication class"):
        LOADER.discover(spec(bar_entry(bars, publication="eventually")))


def test_adjusted_prices_must_name_their_basis(bars: Path) -> None:
    with pytest.raises(ValidationError, match="not reproducible without naming it"):
        LOADER.discover(spec(bar_entry(bars, adjusted=True)))
    ref = LOADER.discover(spec(bar_entry(bars, adjusted=True, adjustment_basis="2024-03-05")))[0]
    assert ref.adjusted is True
    assert LOADER.manifest(ref).adjustment_basis == "2024-03-05"


def test_bars_need_a_bar_size_and_ticks_do_not(bars: Path) -> None:
    with pytest.raises(ValidationError, match="which have a bar size"):
        LOADER.discover(spec(bar_entry(bars, resolution=None)))
    entry = bar_entry(bars, type="trade", resolution="5m")
    with pytest.raises(ValidationError, match="which is not aggregated"):
        LOADER.discover(spec(entry))


def test_two_entries_for_one_dataset_are_refused(bars: Path) -> None:
    with pytest.raises(ValidationError, match="one dataset written twice"):
        LOADER.discover(spec(bar_entry(bars), bar_entry(bars)))


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="is not a file"):
        LOADER.discover(spec(bar_entry(tmp_path / "absent.csv")))


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "empty.csv", BAR_HEADER, [])
    with pytest.raises(ValidationError, match="holds no rows"):
        LOADER.discover(spec(bar_entry(path)))


def test_a_ref_nobody_discovered_is_refused() -> None:
    from kanso.data.loader import DatasetRef

    ref = DatasetRef(
        dataset_id="DEMO.XNAS-bar-5m-raw-20240305",
        instrument="DEMO.XNAS",
        type="bar",
        resolution="5m",
        span=(date(2024, 3, 4), date(2024, 3, 5)),
        adjusted=False,
        publication="realtime",
    )
    with pytest.raises(ValidationError, match="carries no file spec"):
        list(LOADER.load(ref, ref.span))


def test_parquet_is_read_through_the_same_map(tmp_path: Path) -> None:
    """The loader's other half of its name, over the identical column map."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "bars.parquet"
    pq.write_table(
        pa.table(
            {
                "t": ["2024-03-04T09:35:00", "2024-03-04T09:40:00"],
                "o": [100.0, 100.2],
                "h": [100.5, 100.7],
                "l": [99.8, 100.1],
                "c": [100.2, 100.6],
                "v": [1200, 900],
            }
        ),
        path,
    )
    ref = LOADER.discover(spec(bar_entry(path)))[0]
    bars_read = list(LOADER.load(ref, ref.span))
    assert len(bars_read) == 2
    assert str(bars_read[0].close) == "100.20"


def test_arrow_batches_carry_the_catalog_schema(bars: Path) -> None:
    ref = LOADER.discover(spec(bar_entry(bars)))[0]
    tables = LOADER.load_arrow(ref, ref.span)
    assert tables is not None
    assert sum(table.num_rows for table in tables) == 3


def test_the_column_map_of_a_type_names_its_own_fields() -> None:
    required, optional = columns_for("bar")
    assert required == ("ts_event", "open", "high", "low", "close", "volume")
    assert optional == ("ts_init",)
    required, optional = columns_for("corporate_action")
    assert required == ("ts_event", "kind", "ratio", "cash", "currency", "ex_date_ns")
    assert optional == ("ts_init", "instrument_id")


@pytest.mark.parametrize(
    ("column_type", "expected"),
    [
        (None, 1_709_580_900_000_000_000),
        ("UTC", 1_709_562_900_000_000_000),
    ],
)
def test_a_typed_parquet_column_is_a_datetime_already(
    tmp_path: Path, column_type: str | None, expected: int
) -> None:
    """A naive column takes the spec's zone; one carrying a zone keeps its own."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / f"typed{column_type}.parquet"
    pq.write_table(
        pa.table(
            {
                "t": pa.array([1_709_562_900_000_000], type=pa.timestamp("us", tz=column_type)),
                "o": [1.0],
                "h": [2.0],
                "l": [0.5],
                "c": [1.5],
                "v": [10],
            }
        ),
        path,
    )
    ref = LOADER.discover(spec(bar_entry(path)))[0]
    assert list(LOADER.load(ref, ref.span))[0].ts_event == expected


def test_parquet_numeric_timestamps_take_the_declared_unit(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "epoch.parquet"
    pq.write_table(
        pa.table({"t": [1_709_562_900], "o": [1.0], "h": [2.0], "l": [0.5], "c": [1.5], "v": [10]}),
        path,
    )
    with pytest.raises(ValidationError, match="does not say their unit"):
        LOADER.discover(spec(bar_entry(path)))
    ref = LOADER.discover(spec(bar_entry(path, timestamp_unit="s")))[0]
    assert list(LOADER.load(ref, ref.span))[0].ts_event == 1_709_562_900_000_000_000


def test_a_boolean_is_never_a_time(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = tmp_path / "bool.parquet"
    pq.write_table(
        pa.table({"t": [True], "o": [1.0], "h": [2.0], "l": [0.5], "c": [1.5], "v": [10]}),
        path,
    )
    with pytest.raises(ValidationError, match="holds a boolean, not a time"):
        LOADER.discover(spec(bar_entry(path)))


def test_a_file_entry_round_trips_through_the_ref_it_produces(bars: Path) -> None:
    """`load` is given a ref and nothing else, so the ref carries the whole entry."""
    from kanso.data.loaders.csv_parquet import FileSpec, _entry_of

    entry = bar_entry(bars, adjusted=True, adjustment_basis="2024-03-05", timestamp_unit="s")
    ref = LOADER.discover(spec(entry))[0]
    rebuilt, tz = _entry_of(ref)
    assert rebuilt == FileSpec.model_validate(entry)
    assert str(tz) == ZONE


def test_a_publication_rule_nobody_declared_is_refused(bars: Path) -> None:
    """Rules are keyed by data class and live in one module; a spec may only name one."""
    entry = bar_entry(bars, publication="delayed", publication_rule="my_vendor_tier")
    entry["columns"]["ts_init"] = "t"
    with pytest.raises(ValidationError, match="is not a declared publication rule"):
        LOADER.discover(spec(entry))


def test_a_declared_rule_reaches_the_manifest(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "delayed.csv",
        [*BAR_HEADER, "pub"],
        [["2024-03-04T09:35:00", "1", "2", "0.5", "1.5", "10", "2024-03-04T09:50:00"]],
    )
    entry = bar_entry(path, publication="delayed", publication_rule="delayed_quote")
    entry["columns"]["ts_init"] = "pub"
    ref = LOADER.discover(spec(entry))[0]
    manifest = LOADER.manifest(ref)
    assert manifest.publication == "delayed"
    assert manifest.publication_rule == "delayed_quote"
