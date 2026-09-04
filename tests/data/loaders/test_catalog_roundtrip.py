"""Every type a loader yields survives the store, timestamps and all.

The point of these tests is not that parquet works. It is that the two timestamps stay
independent through a write and a read: the engine orders by `ts_init` alone, so a point
whose availability was stamped later than its event must come back that way or the
embargo it encodes is silently gone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

from kanso.data.loaders.csv_parquet import CsvParquetLoader
from kanso.data.loaders.synthetic import SyntheticLoader
from kanso.data.types import CorporateAction, resolve_type

from .conftest import write_csv

SYNTHETIC = SyntheticLoader()
FILES = CsvParquetLoader()

ACTION_HEADER = ["announced", "ex", "kind", "ratio", "cash", "ccy"]
ACTION_ROWS = [
    ["2024-02-20T16:05:00", "2024-03-04T09:30:00", "split", "4", "0", "USD"],
    ["2024-02-27T16:05:00", "2024-03-05T09:30:00", "dividend", "1", "0.24", "USD"],
]


def action_spec(path: Path) -> dict[str, Any]:
    return {
        "loader": "csv_parquet",
        "timezone": "America/New_York",
        "files": [
            {
                "path": str(path),
                "instrument": "DEMO",
                "venue": "XNAS",
                "type": "corporate_action",
                "columns": {
                    "ts_event": "announced",
                    "ex_date_ns": "ex",
                    "kind": "kind",
                    "ratio": "ratio",
                    "cash": "cash",
                    "currency": "ccy",
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("type_id", "reader"),
    [("bar", "bars"), ("quote", "quote_ticks"), ("trade", "trade_ticks")],
)
def test_a_market_type_round_trips_through_the_store(
    catalog: Any, synthetic_spec: dict[str, Any], type_id: str, reader: str
) -> None:
    ref = next(
        r
        for r in SYNTHETIC.discover({**synthetic_spec, "instruments": ["DEMO"]})
        if r.type == type_id
    )
    written = list(SYNTHETIC.load(ref, ref.span))
    catalog.write_data(written)
    read = getattr(catalog, reader)()
    assert len(read) == len(written)
    assert [(p.ts_event, p.ts_init) for p in read] == [(p.ts_event, p.ts_init) for p in written]
    assert [str(p) for p in read] == [str(p) for p in written]


def test_a_custom_type_is_written_and_read_back(catalog: Any, tmp_path: Path) -> None:
    """A file of corporate actions becomes points, and the store gives them back."""
    path = write_csv(tmp_path / "actions.csv", ACTION_HEADER, ACTION_ROWS)
    ref = FILES.discover(action_spec(path))[0]
    written = list(FILES.load(ref, ref.span))
    assert [type(point) for point in written] == [CorporateAction, CorporateAction]
    assert written[0].ratio == 4.0
    assert written[1].cash == 0.24
    assert str(written[0].instrument_id) == "DEMO.XNAS"

    catalog.write_data(written)
    read = catalog.custom_data(CorporateAction)
    assert len(read) == 2
    assert [point.data.kind for point in read] == ["split", "dividend"]
    assert [point.data.ratio for point in read] == [4.0, 1.0]


def test_an_announcement_is_public_when_it_is_made(catalog: Any, tmp_path: Path) -> None:
    """The ex-date is a field, not an availability: the invariant survives the store."""
    path = write_csv(tmp_path / "actions.csv", ACTION_HEADER, ACTION_ROWS)
    ref = FILES.discover(action_spec(path))[0]
    written = list(FILES.load(ref, ref.span))
    assert all(point.ts_init == point.ts_event for point in written)
    assert all(point.ex_date_ns > point.ts_event for point in written)
    catalog.write_data(written)
    read = catalog.custom_data(CorporateAction)
    assert [(p.data.ts_event, p.data.ts_init, p.data.ex_date_ns) for p in read] == [
        (p.ts_event, p.ts_init, p.ex_date_ns) for p in written
    ]


@pytest.mark.parametrize("type_id", ["bar", "quote", "trade"])
def test_the_arrow_path_carries_the_same_points(
    synthetic_spec: dict[str, Any], type_id: str
) -> None:
    """`load_arrow` is the same data in the catalog's own schema, not another format."""
    ref = next(
        r
        for r in SYNTHETIC.discover({**synthetic_spec, "instruments": ["DEMO"]})
        if r.type == type_id
    )
    points = list(SYNTHETIC.load(ref, ref.span))
    tables = SYNTHETIC.load_arrow(ref, ref.span)
    assert tables is not None
    data_cls = resolve_type(type_id)
    assert data_cls in (Bar, QuoteTick, TradeTick)
    back = [point for table in tables for point in ArrowSerializer.deserialize(data_cls, table)]
    assert [str(p) for p in back] == [str(p) for p in points]
    assert [(p.ts_event, p.ts_init) for p in back] == [(p.ts_event, p.ts_init) for p in points]


def test_the_arrow_path_carries_a_custom_type(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "actions.csv", ACTION_HEADER, ACTION_ROWS)
    ref = FILES.discover(action_spec(path))[0]
    tables = FILES.load_arrow(ref, ref.span)
    assert tables is not None
    back = [
        point for table in tables for point in ArrowSerializer.deserialize(CorporateAction, table)
    ]
    assert [point.kind for point in back] == ["split", "dividend"]
