"""The built-in `Funding` type: registered, read from a file by its own annotations, stored.

A funding point is a perpetual's realised funding rate at the instant it settled, which is
also the instant it became public, so a file of them maps a timestamp and a rate and
nothing else: the loader's timestamp convention stamps `ts_init` equal to `ts_event`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from kanso.data.loaders.csv_parquet import CsvParquetLoader, columns_for
from kanso.data.types import Funding, custom_types, data_types, resolve_type, type_id_of
from kanso.errors import ValidationError

from .conftest import write_csv

FILES = CsvParquetLoader()
PERP = "BTCUSDT-PERP.SIM"

HEADER = ["instrument_id", "rate", "ts"]
ROWS = [
    [PERP, "0.0001", "2024-01-01T00:00:00"],
    [PERP, "-0.00005", "2024-01-01T08:00:00"],
    [PERP, "0.000125", "2024-01-01T16:00:00"],
]
"""The file contract's worked example, as `docs/workspace.md` shows it."""

HOUR_NS = 3_600 * 1_000_000_000
JAN1_NS = 1_704_067_200 * 1_000_000_000
"""2024-01-01T00:00:00Z."""


def funding_spec(path: Path, **columns: str) -> dict[str, Any]:
    return {
        "loader": "csv_parquet",
        "timezone": "UTC",
        "files": [
            {
                "path": str(path),
                "instrument": "BTCUSDT-PERP",
                "venue": "SIM",
                "type": "funding",
                "columns": {"instrument_id": "instrument_id", "rate": "rate", "ts_event": "ts"}
                | columns,
            }
        ],
    }


def loaded(path: Path, **columns: str) -> list[Any]:
    (ref,) = FILES.discover(funding_spec(path, **columns))
    return list(FILES.load(ref, ref.span))


def test_funding_is_a_shipped_custom_type() -> None:
    assert custom_types()["funding"] is Funding
    assert data_types()["funding"] is Funding
    assert resolve_type("funding") is Funding


def test_a_funding_point_reports_its_own_type_id() -> None:
    point = Funding(instrument_id=InstrumentId.from_str(PERP), rate=1e-4, ts_event=1, ts_init=1)
    assert type_id_of(point) == "funding"


instants = st.integers(min_value=0, max_value=2**62)
rates = st.floats(min_value=-0.05, max_value=0.05, allow_nan=False, allow_infinity=False)
symbols = st.from_regex(r"[A-Z]{2,6}USDT-PERP", fullmatch=True)


@given(symbol=symbols, rate=rates, ts=instants)
def test_a_funding_point_survives_every_encoding_the_catalog_uses(
    symbol: str, rate: float, ts: int
) -> None:
    """The dict, bytes and Arrow forms all give back the point that went in, rate and the
    one instant alike."""
    point = Funding(
        instrument_id=InstrumentId.from_str(f"{symbol}.SIM"), rate=rate, ts_event=ts, ts_init=ts
    )
    assert Funding.from_dict(point.to_dict()) == point
    assert Funding.from_bytes(point.to_bytes()) == point
    (back,) = ArrowSerializer.deserialize(Funding, ArrowSerializer.serialize(point))
    assert (str(back.instrument_id), back.rate, back.ts_event, back.ts_init) == (
        str(point.instrument_id),
        rate,
        ts,
        ts,
    )


def test_the_file_contract_is_a_timestamp_and_a_rate() -> None:
    """`instrument_id` is optional because the file entry names the instrument, and
    `ts_init` because the settlement is the availability."""
    assert columns_for("funding") == (("ts_event", "rate"), ("ts_init", "instrument_id"))


def test_a_file_of_realised_rates_loads_at_its_settlement_instants(tmp_path: Path) -> None:
    points = loaded(write_csv(tmp_path / "funding.csv", HEADER, ROWS))

    assert [type(point) for point in points] == [Funding] * 3
    assert [point.rate for point in points] == [0.0001, -0.00005, 0.000125]
    assert [str(point.instrument_id) for point in points] == [PERP] * 3
    assert [point.ts_event for point in points] == [JAN1_NS + h * HOUR_NS for h in (0, 8, 16)]
    assert all(point.ts_init == point.ts_event for point in points)


def test_a_file_without_an_instrument_column_is_filed_under_its_entry(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "funding.csv", ["rate", "ts"], [row[1:] for row in ROWS])
    (ref,) = FILES.discover(
        {
            "loader": "csv_parquet",
            "timezone": "UTC",
            "files": [
                {
                    "path": str(path),
                    "instrument": "BTCUSDT-PERP",
                    "venue": "SIM",
                    "type": "funding",
                    "columns": {"rate": "rate", "ts_event": "ts"},
                }
            ],
        }
    )
    assert [str(point.instrument_id) for point in FILES.load(ref, ref.span)] == [PERP] * 3


def test_a_file_that_maps_no_rate_is_refused(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "funding.csv", ["ts"], [[row[2]] for row in ROWS])
    spec = funding_spec(path)
    spec["files"][0]["columns"] = {"ts_event": "ts"}
    with pytest.raises(ValidationError, match="rate"):
        FILES.discover(spec)


def test_a_rate_published_before_it_settled_is_refused(tmp_path: Path) -> None:
    """A mapped availability earlier than the settlement is a prediction read as a payment."""
    rows = [[*row, "2023-12-31T23:59:00"] for row in ROWS]
    path = write_csv(tmp_path / "funding.csv", [*HEADER, "known"], rows)
    with pytest.raises(ValidationError, match="availability cannot precede the reference time"):
        loaded(path, ts_init="known")


def test_funding_is_written_to_the_store_and_read_back(catalog: Any, tmp_path: Path) -> None:
    written = loaded(write_csv(tmp_path / "funding.csv", HEADER, ROWS))

    catalog.write_data(written)
    read = catalog.custom_data(Funding)

    assert [type(point.data) for point in read] == [Funding] * 3
    assert [(p.data.rate, p.data.ts_event, p.data.ts_init) for p in read] == [
        (p.rate, p.ts_event, p.ts_init) for p in written
    ]


def test_the_arrow_path_carries_funding(tmp_path: Path) -> None:
    (ref,) = FILES.discover(funding_spec(write_csv(tmp_path / "funding.csv", HEADER, ROWS)))
    tables = FILES.load_arrow(ref, ref.span)
    assert tables is not None
    back = [point for table in tables for point in ArrowSerializer.deserialize(Funding, table)]
    assert [point.rate for point in back] == [0.0001, -0.00005, 0.000125]
