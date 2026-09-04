"""The store: one `ParquetDataCatalog` for every point and every instrument definition.

kanso keeps no second store. NautilusTrader's `ParquetDataCatalog` holds the market data
and the resolved instrument definitions alike (verified against `nautilus_trader
1.231.0`: an `Instrument` written with `write_data` comes back from `instruments()`), so
there is no registry to keep consistent with the data and no second thing to snapshot.
Beside the engine's `data/` tree sit the files kanso owns — `manifests/`, `snapshots/`
and an adapter's `.cache/` — because what a dataset is, where it came from and when it
became public are kanso's questions, not the engine's.

Three disciplines govern every write here.

**Coverage is what was served, never what was asked.** A source may answer with less than
the requested range, a success status and no warning. The manifest records the span the
points actually cover, and the difference from the request comes back on `Written` for
the caller to surface: a manifest that claimed the request would claim coverage the
dataset lacks, and snapshots are pinned by coverage.

**A dataset a snapshot names is immutable.** A snapshot is a promise that a run can be
reproduced, so the moment one names a dataset that dataset stops being writable: an
overlapping write is refused, `--replace` does not lift the refusal, and the way forward
is a successor dataset that records what it follows in `supersedes`. Data no snapshot has
named yet is still refused an overlapping write, but that refusal is lifted by an explicit
replace, because a destructive default on a command that reads like an import is the
wrong default.

**Availability is checked at the door.** Every point must satisfy `ts_init >= ts_event`,
and a dataset declaring a delayed publication must carry timestamps a declared rule
derives — never `ts_event` copied into `ts_init`, never the moment of ingest. The engine
delivers by `ts_init`, so a point admitted with the wrong one is a leak of the future
into every card that reads it.

The checksum a manifest carries is taken over the bytes the write produced: the parquet
files that appeared under the engine's tree, each hashed and bound to its path within the
store. `nautilus_trader 1.231.0` writes those files deterministically, so the same points
written twice into two stores hash the same, which is what makes a snapshot id a fact
about data rather than about a machine.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nautilus_trader.model.data import CustomData
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from kanso.data import publication
from kanso.data.manifest import (
    DatasetRefLike,
    Manifest,
    as_publication,
    catalog_path,
    data_path,
    dataset_id,
    manifests,
    overlaps,
    remove_manifest,
    shortfall,
    write_manifest,
)
from kanso.data.snapshot import pinned_datasets
from kanso.errors import PreconditionError, ValidationError

if TYPE_CHECKING:  # pragma: no cover - kept out of the runtime import graph
    from kanso.workspace import Workspace

NANOS_PER_SECOND = 1_000_000_000
DAY_END_NANOS = 86_400 * NANOS_PER_SECOND - 1
"""The last nanosecond of a UTC day, so a dated window closes inclusively."""


@dataclass(frozen=True, slots=True)
class Written:
    """What one write did: the dataset it produced and what it cost.

    `shortfall` is the sentence a caller surfaces when the source served less than was
    asked for; `replaced` names the datasets an explicit replace removed to make room.
    """

    manifest: Manifest
    requested: tuple[date, date]
    files: tuple[str, ...]
    replaced: tuple[str, ...] = ()

    @property
    def served(self) -> tuple[date, date]:
        """The span the points actually cover."""
        return self.manifest.span

    @property
    def truncated(self) -> bool:
        """True when the source served less than the request."""
        return self.served != self.requested

    @property
    def shortfall(self) -> str | None:
        """One sentence naming the days asked for and not served, or `None`."""
        return shortfall(self.requested, self.served)


@dataclass(frozen=True, slots=True)
class Loaded:
    """A window of points, and the primer that makes its first instant answerable.

    `primer` is the last point published before the window opened. It is kept apart from
    `points` because it is an evaluation input, not a member of the window: it dates from
    before the window and exists so that "the value known at time t" has an answer at the
    window's first instant.
    """

    points: tuple[Any, ...]
    primer: Any | None = None

    def __len__(self) -> int:
        return len(self.points)


def open_catalog(ws: Workspace) -> ParquetDataCatalog:
    """The workspace's store, created on first use."""
    root = catalog_path(ws)
    root.mkdir(parents=True, exist_ok=True)
    return ParquetDataCatalog(root)


def day_start_ns(day: date) -> int:
    """Midnight UTC of `day`, in nanoseconds since the epoch."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return int(midnight.timestamp()) * NANOS_PER_SECOND


def window_ns(window: tuple[date, date]) -> tuple[int, int]:
    """A dated window as an inclusive nanosecond range covering whole UTC days."""
    return (day_start_ns(window[0]), day_start_ns(window[1]) + DAY_END_NANOS)


def day_of(ts_ns: int) -> date:
    """The UTC day a nanosecond timestamp falls in."""
    return datetime.fromtimestamp(ts_ns // NANOS_PER_SECOND, tz=UTC).date()


def served_span(points: Sequence[Any]) -> tuple[date, date]:
    """The whole UTC days the points' reference times cover, rounded outward.

    The span is economic, taken from `ts_event`, because that is the axis a research
    window names: availability is the separate axis `ts_init` carries, and it is what the
    engine orders by.
    """
    days = [day_of(point.ts_event) for point in points]
    return (min(days), max(days))


def write(
    ws: Workspace,
    points: Iterable[Any],
    *,
    ref: DatasetRefLike,
    source: str,
    replace: bool = False,
    as_of: date | None = None,
    adjustment_basis: str | None = None,
    supersedes: str | None = None,
) -> Written:
    """Write one dataset into the store and record what it is.

    `ref.span` is the span that was requested; the manifest records the span the points
    served and `Written.shortfall` states the difference. Raises `ValidationError` when a
    point's availability is impossible or a delayed dataset's timestamps did not come from
    a declared rule, and `PreconditionError` when the write would overwrite data a
    snapshot has pinned or would overlap held data without an explicit replace.
    """
    ordered = sorted(points, key=lambda point: point.ts_init)
    if not ordered:
        raise ValidationError(
            f"dataset for {ref.instrument} {ref.type}: no points were served, and an empty "
            "dataset would claim coverage it does not have",
            remedy="narrow the request, or check the source's history floor",
        )
    publication.check_availability(ordered)
    if ref.publication == "delayed":
        publication.check_delayed(ordered, ref.publication_rule)

    data_cls, identifier, instrument = _identify(ordered)
    if instrument is not None and instrument != ref.instrument:
        raise ValidationError(
            f"instrument: the dataset declares {ref.instrument!r} but its points carry "
            f"{instrument!r}"
        )

    served = served_span(ordered)
    dataset = dataset_id(ref.instrument, ref.type, ref.resolution, ref.adjusted, served[1])
    held = manifests(ws)
    if supersedes is not None and supersedes not in held:
        raise PreconditionError(
            f"supersedes: {supersedes!r} is not a dataset this workspace holds",
            remedy="name the dataset this one follows, or omit supersedes",
        )
    replaced = _clear(ws, held, dataset, ref, served, replace, data_cls, identifier)

    catalog = open_catalog(ws)
    before = _tree(data_path(ws))
    catalog.write_data(ordered)
    files = _new_files(before, _tree(data_path(ws)))
    if not files:
        raise PreconditionError(
            f"dataset {dataset!r} wrote no bytes: the store already holds files for this "
            "availability range",
            remedy="run `kanso data show` and load with --replace to rewrite the span",
        )

    manifest = Manifest(
        dataset_id=dataset,
        source=source,
        instrument=ref.instrument,
        type=ref.type,
        resolution=ref.resolution,
        span=served,
        adjusted=ref.adjusted,
        row_count=len(ordered),
        checksum=_checksum(data_path(ws), files),
        vendor=ref.vendor,
        vendor_dataset=ref.vendor_dataset,
        request_params=ref.request_params,
        publication=as_publication(ref.publication),
        publication_rule=ref.publication_rule,
        as_of=as_of,
        adjustment_basis=adjustment_basis,
        supersedes=supersedes,
    )
    write_manifest(ws, manifest)
    return Written(manifest=manifest, requested=ref.span, files=files, replaced=replaced)


def load_window(
    ws: Workspace,
    data_cls: type,
    identifier: str | None,
    window: tuple[date, date],
    *,
    rule: str | publication.PublicationRule | None = None,
) -> Loaded:
    """Read a window by availability, primed when the data class needs priming.

    The window is a range of `ts_init`, because that is the only order the engine has: a
    point is delivered when it became available and never at its reference time. For a
    series that changes only on publication, the last point published before the window
    opens comes back as `Loaded.primer` — outside the window, as an input to evaluation.
    """
    catalog = open_catalog(ws)
    identifiers = None if identifier is None else [identifier]
    start, end = window_ns(window)
    points = catalog.query(data_cls, identifiers=identifiers, start=start, end=end)
    primer = None
    if publication.primes(rule) and start > 0:
        earlier = catalog.query(data_cls, identifiers=identifiers, start=0, end=start - 1)
        primer = publication.last_before(earlier, start)
    return Loaded(points=tuple(points), primer=primer)


def resolved_instruments_checksum(ws: Workspace) -> str:
    """The checksum of the instrument definitions the store holds.

    A snapshot pins instruments as well as data: a tick-size or lot-size reassignment
    changes what a fill costs, so a run whose instruments moved is not the run that was
    recorded.

    The value is computed by `kanso.data.instruments.instruments_checksum`, which is the
    one canonicalisation of an instrument set in the package. Two canonicalisations would
    mean two snapshot ids for the same instruments, decided by which code path got there
    first, so this reads the store and delegates rather than hashing definitions itself.
    """
    from kanso.data.instruments import instruments_checksum

    return instruments_checksum(open_catalog(ws).instruments())


def _identify(points: Sequence[Any]) -> tuple[type, str | None, str | None]:
    """The one identity the points share, or a refusal.

    A dataset is one series, so a batch that mixes classes, catalog identifiers or
    instruments is refused rather than silently split into several.
    """
    seen = {identity(point) for point in points}
    if len(seen) > 1:
        rendered = ", ".join(sorted(f"{cls.__name__}/{name}" for cls, name, _ in seen))
        raise ValidationError(
            f"a dataset holds one series, but these points span {rendered}",
            remedy="write one dataset per instrument and type",
        )
    return seen.pop()


def identity(point: Any) -> tuple[type, str | None, str | None]:
    """A point's class, the identifier the store files it under, and its instrument.

    Mirrors how `nautilus_trader 1.231.0` groups a write: a bar is filed under its bar
    type, everything instrument-scoped under its instrument id. A market-wide series
    belongs to no instrument and is filed under the class alone, which the store allows
    and which is why both parts may be absent.
    """
    inner = point.data if isinstance(point, CustomData) else point
    bar_type = getattr(inner, "bar_type", None)
    if bar_type is not None:
        return type(inner), str(bar_type), str(bar_type.instrument_id)
    instrument_id = getattr(inner, "instrument_id", None)
    if instrument_id is None:
        return type(inner), None, None
    return type(inner), str(instrument_id), str(instrument_id)


def _clear(
    ws: Workspace,
    held: dict[str, Manifest],
    dataset: str,
    ref: DatasetRefLike,
    served: tuple[date, date],
    replace: bool,
    data_cls: type,
    identifier: str | None,
) -> tuple[str, ...]:
    """Enforce immutability, and make room when an explicit replace allows it.

    Returns the datasets a replace removed. A replaced dataset goes whole: a manifest
    describes a dataset, and one left holding part of its span would claim coverage it no
    longer has. What clashes is what the store files in the same place, so an unadjusted
    load over the span of an adjusted one clashes with it even though the two are
    different datasets to kanso.
    """
    filed_under = (ref.instrument, ref.type, ref.resolution)
    clashing = [
        manifest
        for manifest in held.values()
        if manifest.filed_under == filed_under
        and (manifest.dataset_id == dataset or overlaps(manifest.span, served))
    ]
    if not clashing:
        return ()
    pinned = pinned_datasets(ws)
    frozen = sorted(m.dataset_id for m in clashing if m.dataset_id in pinned)
    if frozen:
        raise PreconditionError(
            f"{', '.join(frozen)} {'is' if len(frozen) == 1 else 'are'} named by a snapshot and "
            "cannot be rewritten",
            remedy="write a successor dataset recording supersedes=<dataset_id>",
        )
    if not replace:
        names = ", ".join(sorted(m.dataset_id for m in clashing))
        raise PreconditionError(
            f"{served[0]}..{served[1]} overlaps the held dataset(s) {names}",
            remedy="pass --replace to delete and rewrite the overlapped span",
        )
    catalog = open_catalog(ws)
    for manifest in clashing:
        _delete(catalog, data_cls, identifier, manifest.span)
        remove_manifest(ws, manifest.dataset_id)
    return tuple(sorted(m.dataset_id for m in clashing))


def _delete(
    catalog: ParquetDataCatalog, data_cls: type, identifier: str | None, span: tuple[date, date]
) -> None:
    """Remove the files holding a dataset's span.

    The engine addresses its files by availability interval and keeps those intervals
    disjoint, so one file belongs to one dataset; the files removed are the ones whose
    interval meets the dataset's served span.
    """
    start, end = window_ns(span)
    intervals = [
        interval
        for interval in catalog.get_intervals(data_cls, identifier)
        if interval[0] <= end and start <= interval[1]
    ]

    for interval in intervals:
        catalog.delete_data_range(data_cls, identifier, interval[0], interval[1])


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under `root`, by path relative to it, with its size and modification time."""
    if not root.is_dir():
        return {}
    found: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            stat = path.stat()
            found[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return found


def _new_files(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]
) -> tuple[str, ...]:
    """The files a write created or changed."""
    return tuple(sorted(name for name, stamp in after.items() if before.get(name) != stamp))


def _checksum(root: Path, files: Sequence[str]) -> str:
    """A digest over the written bytes, each hashed under its path within the store."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()
