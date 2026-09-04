"""The loader interface: how data of any origin becomes points in the catalog.

A **loader** turns a spec the operator wrote into datasets and datasets into engine data
points. It answers three questions and one optional fourth:

* `discover(spec)` — which datasets does this spec name? One `DatasetRef` per
  instrument, type and resolution, each with the span it will serve.
* `load(ref, window)` — the points of that dataset whose economic reference time falls
  in `window`, in non-decreasing availability order.
* `manifest(ref)` — what was actually served: the span, the row count and a checksum over
  the points. It describes the source, not the store: the manifest the catalog persists is
  the catalog's own, over the bytes it wrote.
* `load_arrow(ref, window)` — the same points already serialised into the catalog's own
  Arrow schema, or `None` when the loader has no such path. The catalog writer prefers
  it, because writing tables costs far less than writing one Python object per row.

Two rules bind every loader, including the ones a vendor adapter or a workspace
extension provides.

**Availability.** `ts_init` is the instant the information became public and `ts_event`
its economic reference time, and `ts_init >= ts_event` always. `checked` applies that
invariant — the one `kanso.data.publication` states — on the way out of a loader and
names the dataset or file the offending point came from, so a loader that computes an
availability timestamp wrongly fails at its own boundary rather than corrupting a card
months later. A loader with no publication knowledge — a file, a generator — produces
`realtime` data, where the two timestamps coincide: publication is declared by the
adapter that produced the data, and with no adapter there is nothing to declare.

**Coverage is what was served.** A dataset's span is the span its points actually cover,
never the span the spec asked for, because a source may answer a range it cannot fill
with a success status and no warning, and snapshots are pinned by coverage. `manifest`
therefore measures the stream rather than repeating the request, and a dataset that
served nothing has no manifest at all.

Spans and windows are stated in **UTC days of `ts_event`**, the same basis the operator
writes a spec in: a request for `2024-01-02` to `2024-12-31` is a request for the data
whose economic reference time falls in those days, whenever it was published.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
The engine has no public Arrow write path — `write_data` takes Python `Data` objects
and its table conversion is private — but it does publish the schemas:
`nautilus_trader.serialization.arrow.serializer` exposes `list_schemas`, `get_schema`
and `ArrowSerializer.serialize_batch`, which returns a `pyarrow.Table` in the catalog's
own schema for any registered class. `arrow_batches` is built on exactly that, so the
Arrow path never invents a layout the catalog would have to be taught. Every `Data`
carries `ts_event` and `ts_init` as nanosecond integers, and the engine orders, filters
and clocks by `ts_init` alone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, ClassVar, Final, Protocol, cast, runtime_checkable

from kanso.data.manifest import Manifest, Publication, dataset_id
from kanso.data.publication import check_availability
from kanso.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kanso.ext import Extension

__all__ = [
    "ARROW_BATCH",
    "DatasetRef",
    "Loader",
    "arrow_batches",
    "checked",
    "get_loader",
    "loaders",
    "manifest_for",
    "register_custom_type",
    "to_ns",
    "utc_day",
]

ARROW_BATCH: Final = 10_000
"""Points per Arrow table on the `load_arrow` path: large enough that per-table overhead
disappears, small enough that a decade of ticks never has to fit in memory at once."""

NS_PER_DAY: Final = 86_400_000_000_000
_EPOCH_DAY: Final = date(1970, 1, 1).toordinal()
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class DatasetRef:
    """One dataset a loader can serve: one instrument, one type, one resolution.

    `span` is the span the loader expects to serve, which is what `dataset_id` is
    derived from; `resolution` is `None` for an unaggregated type. `publication` says
    how the points became public — `realtime`, `delayed` or `unknown` — and a `delayed`
    dataset must name the `publication_rule` its availability timestamps came from. The
    vendor fields are empty for everything but a vendor adapter's own datasets, and
    `request_params` never carries a credential.
    """

    dataset_id: str
    instrument: str
    type: str
    resolution: str | None
    span: tuple[date, date]
    adjusted: bool
    publication: str
    publication_rule: str | None = None
    vendor: str | None = None
    vendor_dataset: str | None = None
    request_params: dict[str, str] | None = field(default=None)

    def window(self, window: tuple[date, date]) -> tuple[date, date] | None:
        """`window` clipped to this dataset's span, or `None` when they do not meet."""
        start = max(window[0], self.span[0])
        end = min(window[1], self.span[1])
        return (start, end) if start <= end else None


@runtime_checkable
class Loader(Protocol):
    """What every source of catalog data looks like, whoever wrote it.

    A loader is stateless: two calls with the same spec produce the same datasets, and
    two calls with the same ref and window produce the same points. That is what makes a
    snapshot reproducible, so it is a requirement rather than a convention.
    """

    id: ClassVar[str]

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        """The datasets `spec` names, in a stable order."""
        ...

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        """The dataset's points over `window`, non-decreasing in `ts_init`."""
        ...

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> Iterator[object] | None:
        """The same points as Arrow tables in the catalog's schema, or `None`."""
        ...

    def manifest(self, ref: DatasetRef) -> Manifest:
        """What the dataset actually served, measured from the points, not the request."""
        ...


def loaders(extensions: Sequence[Extension] = ()) -> dict[str, Loader]:
    """Every loader available here: the built-in ones, then the extensions' own.

    A built-in id wins a clash. An extension may add loaders but may not replace the
    reference ones, because the fixture every test and the demo run on must be the
    package's own; `kanso ext show` reports the shadowing it refused.

    An extension provides loaders by declaring their ids in `PROVIDES["loaders"]` and
    exposing a module-level `LOADERS` mapping of id to loader. Only declared ids are
    taken, so the declaration `doctor` reads is the truth.
    """
    from kanso.data.loaders import BUILTIN_LOADERS

    found: dict[str, Loader] = dict(BUILTIN_LOADERS)
    for extension in extensions:
        for loader_id, loader in _extension_loaders(extension).items():
            found.setdefault(loader_id, loader)
    return found


def get_loader(loader_id: str, extensions: Sequence[Extension] = ()) -> Loader:
    """The loader `loader_id` names.

    Refuses (validation) an unknown id, naming the ones that exist: a spec naming a
    loader nobody provides is either a typo or an extension that failed to import, and
    both are the operator's to fix.
    """
    known = loaders(extensions)
    found = known.get(loader_id)
    if found is None:
        raise ValidationError(
            f"loader: {loader_id!r} is not a known loader; known loaders are "
            f"{', '.join(sorted(known))}",
            remedy=(
                "check the spec's `loader` key, or run `kanso ext show` to see whether the "
                "extension providing it imported cleanly"
            ),
        )
    return found


def checked(points: Iterable[object], where: str) -> Iterator[object]:
    """Yield each point, refusing one whose information predates its own availability.

    The invariant is `kanso.data.publication`'s and is stated once there; this is where a
    loader meets it, and all it adds is which dataset or file the point came from and
    what to do about it. Catching the failure at the loader boundary matters because the
    engine delivers a point at its `ts_init`: once such a point is in the catalog, no
    later check can undo a card that traded on it.
    """
    for point in points:
        _stamp(point, "ts_event", where)
        _stamp(point, "ts_init", where)
        try:
            check_availability((point,))
        except ValidationError as exc:
            raise ValidationError(
                f"{where}: {exc.message}",
                remedy=(
                    "leave ts_init unset for real-time data, where it equals ts_event, or "
                    "derive it from the publication rule of the data class"
                ),
            ) from None
        yield point


def manifest_for(
    ref: DatasetRef,
    source: str,
    points: Iterable[object],
    adjustment_basis: str | None = None,
) -> Manifest:
    """The manifest of the dataset `points` are, measured rather than assumed.

    The span, the row count and the checksum all come from the stream, so a source that
    served less than the spec asked for is recorded as having served less. A stream with
    no points is refused: an empty dataset covers no days, and a manifest claiming
    otherwise would let `research begin` pin a snapshot with nothing in it.

    `adjustment_basis` is the date an adjusted series was adjusted as of, which only the
    loader knows and which such a series may not be recorded without.

    The checksum here is over the **points**, in the engine's own canonical dict form. It
    answers "what did this source serve", which is a question a loader can answer with no
    workspace at all — before a `load`, on a dry run, or to compare two sources. The
    manifest the store persists carries a different checksum, over the bytes the write
    produced, because that one answers "what is held". Neither substitutes for the other
    and the store's is the one a snapshot pins.
    """
    digest = hashlib.sha256()
    rows = 0
    first: date | None = None
    last: date | None = None
    for point in points:
        day = utc_day(_stamp(point, "ts_event", ref.dataset_id))
        first = day if first is None else min(first, day)
        last = day if last is None else max(last, day)
        digest.update(_canonical(point))
        rows += 1
    if first is None or last is None:
        raise ValidationError(
            f"dataset {ref.dataset_id!r} served no points over "
            f"{ref.span[0]}..{ref.span[1]}; an empty dataset covers no days and has nothing "
            "to record",
            remedy="widen the spec's range, or check that the source holds this series at all",
        )
    return Manifest(
        dataset_id=dataset_id(ref.instrument, ref.type, ref.resolution, ref.adjusted, last),
        source=source,
        instrument=ref.instrument,
        type=ref.type,
        resolution=ref.resolution,
        span=(first, last),
        adjusted=ref.adjusted,
        row_count=rows,
        checksum=digest.hexdigest(),
        vendor=ref.vendor,
        vendor_dataset=ref.vendor_dataset,
        request_params=ref.request_params,
        publication=cast("Publication", ref.publication),
        publication_rule=ref.publication_rule,
        adjustment_basis=adjustment_basis,
    )


def arrow_batches(
    points: Iterable[object], data_cls: type, batch_size: int = ARROW_BATCH
) -> Iterator[object] | None:
    """The points as catalog-schema Arrow tables, or `None` for an unregistered class.

    The schema is the engine's own, so a table produced here is one the catalog already
    knows how to read back; nothing about the layout is invented in kanso.
    """
    from nautilus_trader.serialization.arrow.serializer import list_schemas

    if data_cls not in list_schemas():
        return None
    return _batches(points, data_cls, batch_size)


def to_ns(moment: datetime) -> int:
    """A moment as UTC nanoseconds, by integer arithmetic so it is exact.

    A naive datetime is refused: the timezone is the whole point of an availability
    timestamp, and guessing one is how an hour of look-ahead gets in.
    """
    if moment.tzinfo is None:
        raise ValidationError(
            f"timestamp: {moment.isoformat()} carries no timezone, so the instant it names "
            "is unknown"
        )
    delta = moment.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def utc_day(ts_ns: int) -> date:
    """The UTC day a nanosecond timestamp falls in."""
    return date.fromordinal(_EPOCH_DAY + ts_ns // NS_PER_DAY)


def register_custom_type(type_id: str, py_class: type, arrow_schema: object = None) -> None:
    """Register a data type under `type_id`; see `kanso.data.types`.

    Re-exported here because a loader and the type it yields are written together.
    """
    from kanso.data.types import register_custom_type as register

    register(type_id, py_class, arrow_schema)


def _batches(points: Iterable[object], data_cls: type, batch_size: int) -> Iterator[object]:
    from nautilus_trader.serialization.arrow.serializer import ArrowSerializer

    batch: list[object] = []
    for point in points:
        batch.append(point)
        if len(batch) >= batch_size:
            yield ArrowSerializer.serialize_batch(batch, data_cls)
            batch = []
    if batch:
        yield ArrowSerializer.serialize_batch(batch, data_cls)


def _extension_loaders(extension: Extension) -> dict[str, Loader]:
    """The loaders an extension both declares and exposes; anything else is ignored."""
    declared = extension.provides.get("loaders", ())
    table = getattr(extension.module, "LOADERS", None)
    if not declared or not isinstance(table, Mapping):
        return {}
    return {
        loader_id: loader
        for loader_id in declared
        if isinstance(loader := table.get(loader_id), Loader)
    }


def _stamp(point: object, name: str, where: str) -> int:
    value = getattr(point, name, None)
    if not isinstance(value, int):
        raise ValidationError(
            f"{where}: {type(point).__name__} has no integer {name}, so it is not an engine "
            "data point"
        )
    return value


def _canonical(point: object) -> bytes:
    """One point as the bytes its checksum covers.

    The engine's own `to_dict` is the canonical form: it is what the catalog writes, it
    includes both timestamps, and it renders every price and quantity as the exact
    decimal string the engine round-trips, so a checksum computed here means the same
    thing on both hosts.
    """
    to_dict = getattr(type(point), "to_dict", None)
    if to_dict is None:
        raise ValidationError(
            f"{type(point).__name__} has no to_dict, so it has no canonical form to checksum"
        )
    return json.dumps(to_dict(point), sort_keys=True, separators=(",", ":"), default=str).encode()
