"""Reading a registered custom type out of a file, by its own annotations alone.

The loader names no type. It asks the registry what the fields are and what they are
annotated with, so a type an extension registers is readable the moment it exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

from kanso.data.loaders.csv_parquet import CsvParquetLoader, columns_for
from kanso.data.types import register_custom_type
from kanso.errors import ValidationError

from .conftest import write_csv

LOADER = CsvParquetLoader()

SIGNAL = customdataclass(
    type(
        "KansoTestSignal",
        (Data,),
        {
            "__annotations__": {
                "instrument_id": InstrumentId,
                "label": str,
                "flag": bool,
                "count": int,
                "level": float,
                "at_ns": int,
            }
        },
    )
)
register_custom_type("kanso_test_signal", SIGNAL)

HEADER = ["t", "who", "label", "flag", "count", "level", "at"]
ROW = ["2024-03-04T09:35:00", "DEMO.XNAS", "wide", "yes", "3", "1.5", "2024-03-04T16:00:00"]


def signal_spec(path: Path, **over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": str(path),
        "instrument": "DEMO",
        "venue": "XNAS",
        "type": "kanso_test_signal",
        "columns": {
            "ts_event": "t",
            "instrument_id": "who",
            "label": "label",
            "flag": "flag",
            "count": "count",
            "level": "level",
            "at_ns": "at",
        },
    }
    entry.update(over)
    return {"loader": "csv_parquet", "timezone": "America/New_York", "files": [entry]}


def test_a_registered_type_names_its_own_columns() -> None:
    required, optional = columns_for("kanso_test_signal")
    assert required == ("ts_event", "label", "flag", "count", "level", "at_ns")
    assert optional == ("ts_init", "instrument_id")


def test_every_annotation_kind_is_read(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "signal.csv", HEADER, [ROW])
    ref = LOADER.discover(signal_spec(path))[0]
    point = list(LOADER.load(ref, ref.span))[0]
    assert str(point.instrument_id) == "DEMO.XNAS"
    assert point.label == "wide"
    assert point.flag is True
    assert point.count == 3
    assert point.level == 1.5
    assert point.at_ns == 1_709_586_000_000_000_000
    assert point.ts_event == point.ts_init == 1_709_562_900_000_000_000


def test_an_unmapped_instrument_comes_from_the_file_entry(tmp_path: Path) -> None:
    """The entry already names the instrument and its venue, so the column is optional."""
    path = write_csv(tmp_path / "signal.csv", HEADER, [ROW])
    spec = signal_spec(path)
    spec["files"][0]["columns"].pop("instrument_id")
    ref = LOADER.discover(spec)[0]
    point = list(LOADER.load(ref, ref.span))[0]
    assert str(point.instrument_id) == "DEMO.XNAS"


def test_an_instrument_id_that_names_no_venue_is_refused(tmp_path: Path) -> None:
    row = list(ROW)
    row[1] = "DEMO"
    path = write_csv(tmp_path / "signal.csv", HEADER, [row])
    ref = LOADER.discover(signal_spec(path))[0]
    with pytest.raises(ValidationError, match="is not a SYMBOL.VENUE instrument id"):
        list(LOADER.load(ref, ref.span))


def test_a_field_whose_value_is_not_its_type_is_refused(tmp_path: Path) -> None:
    row = list(ROW)
    row[4] = "many"
    path = write_csv(tmp_path / "signal.csv", HEADER, [row])
    ref = LOADER.discover(signal_spec(path))[0]
    with pytest.raises(ValidationError, match="is not a int"):
        list(LOADER.load(ref, ref.span))


def test_a_custom_type_needs_no_resolution(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "signal.csv", HEADER, [ROW])
    ref = LOADER.discover(signal_spec(path))[0]
    assert ref.resolution is None
    assert ref.dataset_id == "DEMO.XNAS-kanso_test_signal-none-raw-20240304"
