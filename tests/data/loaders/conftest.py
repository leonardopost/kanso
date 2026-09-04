"""Fixtures for the loader suite: specs, files and a catalog to round-trip through."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile(
    "loaders",
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile("loaders")


@pytest.fixture
def synthetic_spec() -> dict[str, Any]:
    """Two weekday sessions of five-minute data for two instruments."""
    return {
        "loader": "synthetic",
        "model": "ou",
        "seed": 7,
        "instruments": ["DEMO", "OTHER"],
        "resolution": "5m",
        "types": ["bar", "quote", "trade"],
        "start": "2024-03-04",
        "end": "2024-03-05",
        "price_precision": 2,
    }


@pytest.fixture
def catalog(tmp_path: Path) -> Any:
    """A private `ParquetDataCatalog` under the test's own directory."""
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    return ParquetDataCatalog(str(tmp_path / "catalog"))


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    """A CSV file with `header` and `rows`, written where the test asked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


BAR_HEADER = ["t", "o", "h", "l", "c", "v"]
BAR_ROWS = [
    ["2024-03-04 09:35:00", "100.00", "100.50", "99.80", "100.20", "1200"],
    ["2024-03-04 09:40:00", "100.20", "100.70", "100.10", "100.60", "900"],
    ["2024-03-05 09:35:00", "100.60", "101.00", "100.40", "100.90", "1500"],
]


def bar_entry(path: Path, **over: Any) -> dict[str, Any]:
    """A `csv_parquet` file entry for the bar fixture."""
    entry: dict[str, Any] = {
        "path": str(path),
        "instrument": "DEMO",
        "venue": "XNAS",
        "type": "bar",
        "resolution": "5m",
        "columns": {
            "ts_event": "t",
            "open": "o",
            "high": "h",
            "low": "l",
            "close": "c",
            "volume": "v",
        },
    }
    entry.update(over)
    return entry
