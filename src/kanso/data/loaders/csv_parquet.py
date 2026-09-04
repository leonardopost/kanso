"""`csv_parquet`: files the operator already has, mapped onto engine data types.

A file is not a data source; it is bytes plus somebody's undocumented conventions. This
loader refuses to guess at any of them. A spec must say, per file, **which column is
which field** and, once for the whole spec, **which IANA time zone its naive timestamps
are in**. Those are the two facts a file cannot supply about itself and the two whose
wrong guess is invisible: a column map inferred from headers silently swaps bid and ask
the day a vendor renames a column, and a time zone assumed to be UTC moves a whole
session by hours and turns yesterday's close into today's open. Both refusals are
validation failures naming what is missing.

Anything a file can supply about itself, it supplies. The served span is measured from
the rows rather than declared, so a file that stops early is recorded as stopping early.
Numeric timestamps are read only when the spec declares their unit, because seconds,
milliseconds and nanoseconds are indistinguishable in a column of integers for any epoch
a workspace cares about.

Availability follows the same rule as every other loader with no adapter behind it: a
file is `realtime` and `ts_init == ts_event` unless the spec maps a `ts_init` column. A
spec that declares `publication: delayed` must map one and must name the publication
rule those timestamps came from, because a delayed dataset whose availability equals its
event time is a look-ahead bug wearing a manifest.

Every type the workspace knows can be read, not just the three market-data ones: a
registered custom type's fields are taken from its own annotations, so a `CorporateAction`
file and an extension's own type are mapped by exactly the same rule and this module
names neither. An integer field whose name ends in `_ns` is read as a timestamp rather
than as a number, in the spec's own time zone and by the spec's own unit rule, because
that is the engine's own spelling for an instant on a data class and no operator should
have to write nanoseconds into a CSV by hand.

Parquet is read with `pyarrow`, imported where it is used. kanso declares no dependency
on it; it arrives with NautilusTrader, whose catalog is a parquet store, and a loader
whose name is `csv_parquet` cannot honour half its name.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar, Final, Literal
from zoneinfo import ZoneInfo

from nautilus_trader.model.identifiers import InstrumentId
from pydantic import Field, model_validator

from kanso.data.loader import (
    DatasetRef,
    arrow_batches,
    checked,
    manifest_for,
    to_ns,
    utc_day,
)
from kanso.data.loaders.points import (
    aggressor,
    bar_type,
    instrument_id,
    make_bar,
    make_quote,
    make_trade,
    zone,
)
from kanso.data.manifest import PUBLICATIONS, Manifest, dataset_id
from kanso.data.publication import resolve as resolve_rule
from kanso.data.types import resolve_type
from kanso.errors import ValidationError
from kanso.schemas.base import KansoModel, NonEmpty
from kanso.schemas.duration import Duration

TIMESTAMP_FIELDS: Final = ("ts_event", "ts_init")
"""The two fields every type has and no type declares."""

REQUIRED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "bar": ("ts_event", "open", "high", "low", "close", "volume"),
    "quote": ("ts_event", "bid_price", "ask_price", "bid_size", "ask_size"),
    "trade": ("ts_event", "price", "size"),
}
"""What a market-data file must map. A custom type's requirement is its own annotations."""

OPTIONAL_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "bar": ("ts_init",),
    "quote": ("ts_init",),
    "trade": ("ts_init", "aggressor_side", "trade_id"),
}

EPOCH_UNITS: Final[dict[str, int]] = {
    "s": 1_000_000_000,
    "ms": 1_000_000,
    "us": 1_000,
    "ns": 1,
}
"""Nanoseconds per unit of a numeric timestamp column."""

PARQUET_SUFFIXES: Final = (".parquet", ".pq")

Builder = Callable[["FileSpec", Mapping[str, object], Mapping[str, str], int, int], object]
"""How one row of a mapped file becomes one data point."""


class FileSpec(KansoModel):
    """One file, the dataset it holds, and the map from its columns to that type."""

    path: NonEmpty
    instrument: NonEmpty
    venue: NonEmpty
    type: NonEmpty
    resolution: Duration | None = None
    columns: dict[str, NonEmpty] = Field(default_factory=dict)
    timestamp_unit: Literal["s", "ms", "us", "ns"] | None = None
    price_precision: int = Field(default=2, ge=0, le=9)
    size_precision: int = Field(default=0, ge=0, le=9)
    adjusted: bool = False
    adjustment_basis: str | None = None
    publication: str = "realtime"
    publication_rule: str | None = None

    @model_validator(mode="after")
    def _validate(self) -> FileSpec:
        if not self.columns:
            raise ValueError(
                f"files.columns: {self.path} maps no columns; name the column holding each "
                f"field of {self.type!r} — a column map is never inferred from headers"
            )
        if self.publication not in PUBLICATIONS:
            raise ValueError(
                f"files.publication: {self.publication!r} is not a publication class; "
                f"expected one of {', '.join(PUBLICATIONS)}"
            )
        if self.publication == "delayed":
            if "ts_init" not in self.columns:
                raise ValueError(
                    f"files.columns: {self.path} declares publication 'delayed' but maps no "
                    "ts_init column, so every point would claim to be available at the instant "
                    "it happened"
                )
            if not self.publication_rule:
                raise ValueError(
                    f"files.publication_rule: {self.path} declares publication 'delayed', whose "
                    "availability timestamps must come from a named rule"
                )
            resolve_rule(self.publication_rule)
        if self.adjusted and not self.adjustment_basis:
            raise ValueError(
                f"files.adjustment_basis: {self.path} declares adjusted prices, which are "
                "adjusted as of a date and are not reproducible without naming it"
            )
        if self.type == "bar" and self.resolution is None:
            raise ValueError(f"files.resolution: {self.path} holds bars, which have a bar size")
        if self.type != "bar" and self.resolution is not None:
            raise ValueError(
                f"files.resolution: {self.path} holds {self.type!r}, which is not aggregated"
            )
        return self


class CsvParquetSpec(KansoModel):
    """A `csv_parquet` spec: one time zone, and one entry per file."""

    loader: Literal["csv_parquet"] = "csv_parquet"
    timezone: str | None = None
    files: list[FileSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> CsvParquetSpec:
        if not self.timezone:
            raise ValueError(
                "timezone: name the IANA time zone this spec's naive timestamps are in "
                "(for example 'America/New_York' or 'UTC'); a file does not say which it "
                "used and assuming one moves every session by hours"
            )
        zone(self.timezone)
        seen = {(f.instrument, f.type, f.resolution) for f in self.files}
        if len(seen) != len(self.files):
            raise ValueError(
                "files: two entries name the same instrument, type and resolution, which is "
                "one dataset written twice"
            )
        return self


@dataclass(frozen=True)
class CsvParquetLoader:
    """The reference file loader. Stateless: the spec and the files are the whole input."""

    id: ClassVar[str] = "csv_parquet"

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """One dataset per file entry, spanning the days its rows actually hold."""
        parsed = CsvParquetSpec.model_validate(dict(spec))
        return [_ref(entry, str(parsed.timezone)) for entry in parsed.files]

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The file's points whose event day falls in `window`."""
        entry, tz = _entry_of(ref)
        return checked(_points(entry, tz, window), f"{self.id} file {entry.path}")

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as catalog-schema Arrow tables."""
        return arrow_batches(self.load(ref, window), resolve_type(ref.type))

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the file served over the whole dataset span."""
        entry, _ = _entry_of(ref)
        return manifest_for(ref, self.id, self.load(ref, ref.span), entry.adjustment_basis)


def columns_for(type_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The required and optional field names of a type's column map.

    Market-data types have fixed field sets; a custom type's fields are read from its own
    annotations, so registering a type is all it takes to make it loadable from a file.
    `instrument_id` is optional for a custom type because the file entry already names the
    instrument and its venue.
    """
    if type_id in REQUIRED_COLUMNS:
        return REQUIRED_COLUMNS[type_id], OPTIONAL_COLUMNS[type_id]
    fields = _custom_fields(resolve_type(type_id))
    required = ("ts_event", *(name for name in fields if name != "instrument_id"))
    optional = ("ts_init", *(name for name in fields if name == "instrument_id"))
    return required, optional


def _custom_fields(data_cls: type) -> dict[str, object]:
    """A custom type's own fields, in declaration order, without the two timestamps."""
    annotations = dict(getattr(data_cls, "__annotations__", {}))
    return {
        name: kind
        for name, kind in annotations.items()
        if name not in TIMESTAMP_FIELDS and not name.startswith("_")
    }


def _ref(entry: FileSpec, timezone: str) -> DatasetRef:
    span = _span(entry, timezone)
    instrument = str(instrument_id(entry.instrument, entry.venue))
    return DatasetRef(
        dataset_id=dataset_id(instrument, entry.type, entry.resolution, entry.adjusted, span[1]),
        instrument=instrument,
        type=entry.type,
        resolution=entry.resolution,
        span=span,
        adjusted=entry.adjusted,
        publication=entry.publication,
        publication_rule=entry.publication_rule,
        request_params={"path": entry.path, "timezone": timezone, **_encoded(entry)},
    )


def _encoded(entry: FileSpec) -> dict[str, str]:
    """The file entry as the string map a ref carries, so `load` needs only the ref."""
    dumped = entry.model_dump(mode="json")
    encoded: dict[str, str] = {}
    for key, value in dumped.items():
        if key == "columns":
            encoded[key] = ";".join(f"{field}={column}" for field, column in value.items())
        elif value is None:
            encoded[key] = ""
        else:
            encoded[key] = str(value)
    return encoded


def _entry_of(ref: DatasetRef) -> tuple[FileSpec, ZoneInfo]:
    """The file entry a ref carries, and the zone its naive timestamps are in."""
    params = ref.request_params
    if not params or "timezone" not in params:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} carries no file spec; refs come from "
            "CsvParquetLoader.discover and are not built by hand"
        )
    fields: dict[str, object] = {}
    for key, raw in params.items():
        if key == "timezone":
            continue
        if key == "columns":
            fields[key] = dict(pair.split("=", 1) for pair in raw.split(";") if pair)
        elif raw == "":
            fields[key] = None
        elif key in {"adjusted"}:
            fields[key] = raw == "True"
        else:
            fields[key] = raw
    return FileSpec.model_validate(fields), zone(params["timezone"])


def _span(entry: FileSpec, timezone: str) -> tuple[date, date]:
    """The span the file's rows cover, measured rather than declared."""
    tz = zone(timezone)
    column = _require_columns(entry)["ts_event"]
    days = [
        utc_day(_timestamp(_cell(row, column, entry), tz, entry, "ts_event"))
        for row in _rows(entry)
    ]
    if not days:
        raise ValidationError(
            f"files.path: {entry.path} holds no rows, so there is no dataset in it",
            remedy="remove the entry, or point it at the file that has the data",
        )
    return (min(days), max(days))


def _require_columns(entry: FileSpec) -> dict[str, str]:
    """The entry's column map, checked against the type it claims to hold."""
    required, optional = columns_for(entry.type)
    missing = [name for name in required if name not in entry.columns]
    if missing:
        raise ValidationError(
            f"files.columns: {entry.path} maps no column for {', '.join(missing)}; a "
            f"{entry.type!r} file must map {', '.join(required)}"
            + (f" and may map {', '.join(optional)}" if optional else "")
        )
    unknown = [name for name in entry.columns if name not in (*required, *optional)]
    if unknown:
        raise ValidationError(
            f"files.columns: {entry.path} maps {', '.join(sorted(unknown))}, which "
            f"{entry.type!r} has no field for; its fields are {', '.join((*required, *optional))}"
        )
    return dict(entry.columns)


def _points(entry: FileSpec, tz: ZoneInfo, window: tuple[date, date]) -> Iterator[object]:
    columns = _require_columns(entry)
    build = _BUILDERS[entry.type] if entry.type in _BUILDERS else _custom_builder(entry, str(tz))
    for row in _rows(entry):
        ts_event = _timestamp(_cell(row, columns["ts_event"], entry), tz, entry, "ts_event")
        if not window[0] <= utc_day(ts_event) <= window[1]:
            continue
        ts_init = ts_event
        if "ts_init" in columns:
            ts_init = _timestamp(_cell(row, columns["ts_init"], entry), tz, entry, "ts_init")
        yield build(entry, row, columns, ts_event, ts_init)


def _rows(entry: FileSpec) -> Iterator[Mapping[str, object]]:
    """The file's rows as field maps, whichever of the two formats it is in."""
    path = Path(entry.path)
    if not path.is_file():
        raise ValidationError(
            f"files.path: {entry.path} is not a file",
            remedy="paths are resolved from the working directory; give an absolute one if unsure",
        )
    if path.suffix.lower() in PARQUET_SUFFIXES:
        yield from _parquet_rows(path)
        return
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def _parquet_rows(path: Path) -> Iterator[Mapping[str, object]]:
    # pyarrow is NautilusTrader's own dependency, present wherever the catalog is; kanso
    # declares none on it and reaches it only here, to honour this loader's name.
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    table = pq.read_table(path)
    yield from table.to_pylist()


def _cell(row: Mapping[str, object], column: str, entry: FileSpec) -> object:
    if column not in row:
        raise ValidationError(
            f"files.columns: {entry.path} has no column {column!r}; its columns are "
            f"{', '.join(str(name) for name in row)}"
        )
    return row[column]


def _timestamp(value: object, tz: ZoneInfo, entry: FileSpec, field: str) -> int:
    """One cell as UTC nanoseconds, refusing every ambiguity rather than resolving it."""
    if isinstance(value, datetime):
        return to_ns(value if value.tzinfo is not None else value.replace(tzinfo=tz))
    if isinstance(value, bool):
        raise ValidationError(f"files.columns.{field}: {entry.path} holds a boolean, not a time")
    if isinstance(value, int | float):
        return _epoch(float(value), entry, field)
    text = str(value).strip()
    if _numeric(text):
        return _epoch(float(text), entry, field)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"files.columns.{field}: {text!r} in {entry.path} is neither an ISO-8601 timestamp "
            "nor a number of epoch units"
        ) from None
    return to_ns(moment if moment.tzinfo is not None else moment.replace(tzinfo=tz))


def _epoch(value: float, entry: FileSpec, field: str) -> int:
    if entry.timestamp_unit is None:
        raise ValidationError(
            f"files.timestamp_unit: {entry.path} holds numeric timestamps in column "
            f"{entry.columns[field]!r} but the spec does not say their unit; seconds, "
            "milliseconds, microseconds and nanoseconds are indistinguishable in a column "
            "of numbers",
            remedy="add `timestamp_unit: s | ms | us | ns` to the file entry",
        )
    return int(round(value * EPOCH_UNITS[entry.timestamp_unit]))


def _numeric(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _ticks(value: object, precision: int, entry: FileSpec, field: str) -> int:
    """A cell as a whole number of increments at the declared precision."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError(
            f"files.columns.{field}: {value!r} in {entry.path} is not a number"
        ) from None
    return int(math.floor(number * 10**precision + 0.5))


def _build_bar(
    entry: FileSpec,
    row: Mapping[str, object],
    columns: Mapping[str, str],
    ts_event: int,
    ts_init: int,
) -> object:
    ohlc = tuple(
        _ticks(_cell(row, columns[field], entry), entry.price_precision, entry, field)
        for field in ("open", "high", "low", "close")
    )
    return make_bar(
        bar_type(instrument_id(entry.instrument, entry.venue), str(entry.resolution)),
        (ohlc[0], ohlc[1], ohlc[2], ohlc[3]),
        _ticks(_cell(row, columns["volume"], entry), entry.size_precision, entry, "volume"),
        entry.price_precision,
        entry.size_precision,
        ts_event,
        ts_init,
    )


def _build_quote(
    entry: FileSpec,
    row: Mapping[str, object],
    columns: Mapping[str, str],
    ts_event: int,
    ts_init: int,
) -> object:
    return make_quote(
        instrument_id(entry.instrument, entry.venue),
        _ticks(_cell(row, columns["bid_price"], entry), entry.price_precision, entry, "bid_price"),
        _ticks(_cell(row, columns["ask_price"], entry), entry.price_precision, entry, "ask_price"),
        _ticks(_cell(row, columns["bid_size"], entry), entry.size_precision, entry, "bid_size"),
        _ticks(_cell(row, columns["ask_size"], entry), entry.size_precision, entry, "ask_size"),
        entry.price_precision,
        entry.size_precision,
        ts_event,
        ts_init,
    )


def _build_trade(
    entry: FileSpec,
    row: Mapping[str, object],
    columns: Mapping[str, str],
    ts_event: int,
    ts_init: int,
) -> object:
    side = "buyer"
    if "aggressor_side" in columns:
        side = str(_cell(row, columns["aggressor_side"], entry))
    trade_id = f"{entry.instrument}-{ts_event}"
    if "trade_id" in columns:
        trade_id = str(_cell(row, columns["trade_id"], entry))
    return make_trade(
        instrument_id(entry.instrument, entry.venue),
        _ticks(_cell(row, columns["price"], entry), entry.price_precision, entry, "price"),
        _ticks(_cell(row, columns["size"], entry), entry.size_precision, entry, "size"),
        aggressor(side),
        trade_id,
        entry.price_precision,
        entry.size_precision,
        ts_event,
        ts_init,
    )


_BUILDERS: Final[dict[str, Builder]] = {
    "bar": _build_bar,
    "quote": _build_quote,
    "trade": _build_trade,
}


def _custom_builder(entry: FileSpec, timezone: str) -> Builder:
    """A builder for any registered custom type, driven by its own annotations."""
    data_cls = resolve_type(entry.type)
    fields = _custom_fields(data_cls)

    tz = zone(timezone)

    def build(
        spec: FileSpec,
        row: Mapping[str, object],
        columns: Mapping[str, str],
        ts_event: int,
        ts_init: int,
    ) -> object:
        values: dict[str, object] = {}
        for name, kind in fields.items():
            if name not in columns:
                values[name] = instrument_id(spec.instrument, spec.venue)
                continue
            cell = _cell(row, columns[name], spec)
            if _is_instant(name, kind):
                values[name] = _timestamp(cell, tz, spec, name)
            else:
                values[name] = _coerce(cell, kind, spec, name)
        return data_cls(ts_event=ts_event, ts_init=ts_init, **values)

    return build


def _is_instant(name: str, kind: object) -> bool:
    """True when a custom field is an instant: the engine spells those `<name>_ns`."""
    return kind is int and name.endswith("_ns")


def _coerce(value: object, kind: object, entry: FileSpec, field: str) -> object:
    """One cell as the type a custom class annotated its field with."""
    text = str(value).strip()
    try:
        if kind is InstrumentId:
            symbol, _, venue = text.rpartition(".")
            if not symbol or not venue:
                raise ValidationError(
                    f"files.columns.{field}: {text!r} in {entry.path} is not a "
                    "SYMBOL.VENUE instrument id"
                )
            return instrument_id(symbol, venue)
        if kind is float:
            return float(text)
        if kind is int:
            return int(text)
        if kind is bool:
            return text.lower() in {"1", "true", "yes", "y"}
        return text
    except ValueError:
        raise ValidationError(
            f"files.columns.{field}: {text!r} in {entry.path} is not a "
            f"{getattr(kind, '__name__', kind)}"
        ) from None
