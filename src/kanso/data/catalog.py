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
is a successor dataset that records what it follows in `supersedes`. A successor written
over the span of the one pinned dataset it names takes its place — the files and the
manifest go, as a replace's do — and every snapshot naming the old dataset stops
supporting a certification, since the workspace no longer describes what it pinned; that
is the one way a pinned mistake is corrected, and it says so in the refusal. Data no snapshot has
named yet is still refused an overlapping write, but that refusal is lifted by an explicit
replace, because a destructive default on a command that reads like an import is the
wrong default.

**Availability is checked at the door.** Every point must satisfy `ts_init >= ts_event`,
and a dataset declaring a delayed publication must carry timestamps a declared rule
derives — never `ts_event` copied into `ts_init`, never the moment of ingest. The engine
delivers by `ts_init`, so a point admitted with the wrong one is a leak of the future
into every card that reads it.

The checksum a manifest carries is taken over the bytes the write produced: the parquet
files that appeared in the directory the engine files the series in, each hashed and bound
to its path within the store. `nautilus_trader 1.231.0` writes those files
deterministically, so the same points written twice into two stores hash the same, which
is what makes a snapshot id a fact about data rather than about a machine.

**A dataset too large to hold is written in batches.** By default a write gathers every
point and sorts it before writing any, which is right for a series of bars and fatal for a
liquid instrument's day of book changes: the engine's `write_data` holds about 204 bytes a
book delta and a 561-byte-a-delta transient on top while it converts them, so a day of about
8.5 million changes — a liquid perpetual's, ten levels deep — would peak near 6.5 GB written
whole. Asked for `batch`, the write takes the points as they come, in `ts_init` order, and
hands the engine at most `batch` of them at a time, cutting only between two instants so no
instant is split across two files. Each batch is checked as a whole write is — availability,
a delayed dataset's rule, one series — and each becomes one file whose interval is disjoint
from the last, so the store reads the batches back as one series; the clash with held data
is checked once, over the span requested, before the first file. The manifest is one, over
every file the write produced, and a failure in any batch removes every file the write had
already produced and records nothing, so a dataset is written whole or not at all.

**A write's cost grows with its own series, never with the store.** It opens no other
series' manifest. It reads its series' manifests — filed under the same instrument, type and
resolution, adjusted or not — that end on or after the day the span it writes begins, found
by their names, and the one `supersedes` names, by its own file. Those are every manifest
it can clash with, and more: a name carries a dataset's end and not its start, so a span
written ahead of what the series holds reads every later dataset of it. It finds the files it
produced by listing the one directory the engine files the series in, and sets aside only
that directory's files. `nautilus_trader 1.231.0` files a series under
`data/<class_to_filename(class)>/<urisafe_identifier(identifier)>`, and a series of no
instrument in the class's own directory.
Measured on a workspace of 13,293 manifests and 18,465 files, reading every manifest took
8.9 s and walking the whole tree 0.68 s, twice — about ten seconds a dataset, which a
backfill paid on every chunk.

**A replace removes whole files of its own series, and kanso removes them.** A file's name
is the availability interval it holds, first and last `ts_init`, and a dataset's span is
economic, the days of its `ts_event`, so a span does not say which files are a dataset's: a
delayed dataset's file runs into the next one's span. The checksum its manifest recorded
does. The files that go for a clashing dataset are the run of the series' files, in name
order and named as the engine names them, whose checksum is that one, and a dataset no run
matches — its files altered since it was written — is refused before anything is removed.
The engine's `delete_data_range` is not asked: given no identifier it runs on each
instrument's directory of the class and never on the class's own, so it would remove every
instrument's files over the span and none of a series of no instrument, whose replace would
then be refused as having written no bytes.

**A replaced dataset is kept until its replacement is written.** A replace or a supersede
removes the held dataset before the first new file is written, because the engine keeps its
files' intervals disjoint; a batched write has by then read one batch of a stream that may
still fail. So the files a removal could touch are first linked aside, under
`catalog/.replaced/`, and a write that fails — a refused point in a later batch, a loader
that raises, an interrupt — puts them and their manifests back as they were before it
removes the aside. Only a write that records its manifest lets them go.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from nautilus_trader.model.data import CustomData
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from nautilus_trader.persistence.funcs import class_to_filename, urisafe_identifier

from kanso.data import publication
from kanso.data.manifest import (
    CATALOG_DIR,
    DATA_DIR,
    DatasetRefLike,
    Manifest,
    as_publication,
    catalog_path,
    data_path,
    dataset_id,
    holds,
    overlaps,
    remove_manifest,
    series_manifests,
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

ASIDE_DIR: Final = ".replaced"
"""Where a write keeps what a replace removed, beside the engine's tree, until it is done."""

ENGINE_FILE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{9}Z_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{9}Z\.parquet"
)
"""The name the engine gives a file it writes: the first and the last `ts_init` it holds, to
the nanosecond. A file named otherwise is not the engine's, and `filter_files` keeps a name
it reads no interval off as meeting every span, so a replace looks only at names of this
form."""

WRITE_BATCH: Final = 250_000
"""Points per `write_data` call on a batched write: about 0.2 GB of engine objects and
conversion at a book delta's measured 204 + 561 bytes, whatever the day holds."""


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
    batch: int | None = None,
) -> Written:
    """Write one dataset into the store and record what it is.

    `ref.span` is the span that was requested; the manifest records the span the points
    served and `Written.shortfall` states the difference. Raises `ValidationError` when a
    point's availability is impossible or a delayed dataset's timestamps did not come from
    a declared rule, and `PreconditionError` when the write would overwrite data a
    snapshot has pinned or would overlap held data without an explicit replace.

    With `batch`, the points are taken in the `ts_init` order they arrive in and written
    at most `batch` at a time, each batch cut between two instants; the module docstring
    says what that changes and what it does not.
    """
    if batch is not None:
        return _write_batched(
            ws,
            points,
            ref=ref,
            source=source,
            replace=replace,
            as_of=as_of,
            adjustment_basis=adjustment_basis,
            supersedes=supersedes,
            batch=batch,
        )
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
    held = _held(ws, ref, served, supersedes)
    replaced = _clear(ws, held, dataset, ref, served, replace, data_cls, identifier, supersedes)

    root = data_path(ws)
    home = _filed_in(root, data_cls, identifier)
    before = _listing(root, home)
    try:
        open_catalog(ws).write_data(ordered)
        files = _new_files(before, _listing(root, home))
        if not files:
            raise PreconditionError(
                f"dataset {dataset!r} wrote no bytes: the store already holds files for this "
                "availability range",
                remedy="run `kanso data show` and load with --replace to rewrite the span",
            )
    except BaseException:
        _undo(ws, home, before, [held[name] for name in replaced])
        raise
    _let_go(ws)

    return _record(
        ws,
        ref,
        source,
        dataset=dataset,
        served=served,
        rows=len(ordered),
        files=files,
        replaced=replaced,
        as_of=as_of,
        adjustment_basis=adjustment_basis,
        supersedes=supersedes,
    )


def _record(
    ws: Workspace,
    ref: DatasetRefLike,
    source: str,
    *,
    dataset: str,
    served: tuple[date, date],
    rows: int,
    files: tuple[str, ...],
    replaced: tuple[str, ...],
    as_of: date | None,
    adjustment_basis: str | None,
    supersedes: str | None,
) -> Written:
    """The manifest of what a write produced, recorded, and the write's own account of it."""
    manifest = Manifest(
        dataset_id=dataset,
        source=source,
        instrument=ref.instrument,
        type=ref.type,
        resolution=ref.resolution,
        span=served,
        adjusted=ref.adjusted,
        row_count=rows,
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


def _write_batched(
    ws: Workspace,
    points: Iterable[Any],
    *,
    ref: DatasetRefLike,
    source: str,
    replace: bool,
    as_of: date | None,
    adjustment_basis: str | None,
    supersedes: str | None,
    batch: int,
) -> Written:
    """`write` a batch at a time: the same checks per batch, one manifest over every file."""
    batches = _cut(points, batch)
    first = next(batches, None)
    if first is None:
        raise ValidationError(
            f"dataset for {ref.instrument} {ref.type}: no points were served, and an empty "
            "dataset would claim coverage it does not have",
            remedy="narrow the request, or check the source's history floor",
        )
    series = _checked(first, ref)
    held = _held(ws, ref, ref.span, supersedes)
    requested = dataset_id(ref.instrument, ref.type, ref.resolution, ref.adjusted, ref.span[1])
    replaced = _clear(ws, held, requested, ref, ref.span, replace, *series[:2], supersedes)

    catalog = open_catalog(ws)
    root = data_path(ws)
    home = _filed_in(root, *series[:2])
    before = _listing(root, home)
    rows = 0
    first_day, last_day = served_span(first)
    current: list[Any] | None = first
    del first
    try:
        while current is not None:
            found = _checked(current, ref)
            if found != series:
                raise ValidationError(
                    f"a dataset holds one series, but these points span "
                    f"{series[0].__name__}/{series[1]} and {found[0].__name__}/{found[1]}",
                    remedy="write one dataset per instrument and type",
                )
            catalog.write_data(current)
            rows += len(current)
            span = served_span(current)
            first_day, last_day = min(first_day, span[0]), max(last_day, span[1])
            current = next(batches, None)
        files = _new_files(before, _listing(root, home))
        if not files:
            raise PreconditionError(
                f"dataset for {ref.instrument} {ref.type} wrote no bytes: the store already "
                "holds files for this availability range",
                remedy="run `kanso data show` and load with --replace to rewrite the span",
            )
    except BaseException:
        _undo(ws, home, before, [held[name] for name in replaced])
        raise
    _let_go(ws)
    served = (first_day, last_day)
    return _record(
        ws,
        ref,
        source,
        dataset=dataset_id(ref.instrument, ref.type, ref.resolution, ref.adjusted, served[1]),
        served=served,
        rows=rows,
        files=files,
        replaced=replaced,
        as_of=as_of,
        adjustment_basis=adjustment_basis,
        supersedes=supersedes,
    )


def _cut(points: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """The points in batches of at least `size`, each closed at the next change of instant.

    A batch runs past `size` only for as long as the instant does, so no instant is split
    across two files. Points that go back in `ts_init` are refused: a batched write keeps
    nothing back to sort, and the engine files each batch under its own interval.
    """
    current: list[Any] = []
    last = None
    for point in points:
        if last is not None and point.ts_init < last:
            raise ValidationError(
                f"a {type(point).__name__} at ts_init {point.ts_init} follows one at {last}: a "
                "dataset written a batch at a time must arrive in availability order",
                remedy="the loader promises non-decreasing ts_init; report it",
            )
        if len(current) >= size and point.ts_init != last:
            yield current
            current = []
        current.append(point)
        last = point.ts_init
    if current:
        yield current


def _checked(points: Sequence[Any], ref: DatasetRefLike) -> tuple[type, str | None, str | None]:
    """One batch checked as a whole write checks its points, and the series it is."""
    publication.check_availability(points)
    if ref.publication == "delayed":
        publication.check_delayed(points, ref.publication_rule)
    found = _identify(points)
    if found[2] is not None and found[2] != ref.instrument:
        raise ValidationError(
            f"instrument: the dataset declares {ref.instrument!r} but its points carry {found[2]!r}"
        )
    return found


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


def _held(
    ws: Workspace, ref: DatasetRefLike, span: tuple[date, date], supersedes: str | None
) -> dict[str, Manifest]:
    """The held datasets of the series a write over `span` files under that end on or after
    the span's first day, keyed by id: every one it can clash with, and no other series'.

    What clashes is what `_clear` says: a dataset of the same series that overlaps the span
    or has the id of the one written. A dataset that ends before the span begins does
    neither, so it is not read. One that ends later is, whether or not it begins in time to
    overlap, because its name carries its end and not its start. The dataset `supersedes`
    names is read by its own file, of whatever series, and refused unless held.
    """
    if supersedes is not None and not holds(ws, supersedes):
        raise PreconditionError(
            f"supersedes: {supersedes!r} is not a dataset this workspace holds",
            remedy="name the dataset this one follows, or omit supersedes",
        )
    return series_manifests(ws, (ref.instrument, ref.type, ref.resolution), ending_from=span[0])


def _clear(
    ws: Workspace,
    held: dict[str, Manifest],
    dataset: str,
    ref: DatasetRefLike,
    served: tuple[date, date],
    replace: bool,
    data_cls: type,
    identifier: str | None,
    supersedes: str | None = None,
) -> tuple[str, ...]:
    """Enforce immutability, and make room when an explicit replace or supersede allows it.

    Returns the datasets a replace or a supersede removed. A removed dataset goes whole: a
    manifest describes a dataset, and one left holding part of its span would claim
    coverage it no longer has. What clashes is what the store files in the same place, so
    an unadjusted load over the span of an adjusted one clashes with it even though the
    two are different datasets to kanso. A pinned dataset goes only when it is the one the
    successor names in `supersedes`; any other pinned clash is refused as it always was.
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
    frozen = sorted(
        m.dataset_id for m in clashing if m.dataset_id in pinned and m.dataset_id != supersedes
    )
    if frozen:
        raise PreconditionError(
            f"{', '.join(frozen)} {'is' if len(frozen) == 1 else 'are'} named by a snapshot and "
            "cannot be rewritten",
            remedy=f"load it again with --supersedes {frozen[0]} to put this dataset in its "
            "place; every snapshot naming the old one then stops supporting a certification",
        )
    others = [m for m in clashing if m.dataset_id != supersedes]
    if others and not replace:
        names = ", ".join(sorted(m.dataset_id for m in others))
        raise PreconditionError(
            f"{served[0]}..{served[1]} overlaps the held dataset(s) {names}",
            remedy="pass --replace to delete and rewrite the overlapped span",
        )
    root = data_path(ws)
    home = _filed_in(root, data_cls, identifier)
    owned = _owned(open_catalog(ws), root, home, data_cls, clashing)
    _set_aside(ws, home)
    for name in sorted(owned):
        (root / name).unlink()
    for manifest in clashing:
        remove_manifest(ws, manifest.dataset_id)
    return tuple(sorted(m.dataset_id for m in clashing))


def _set_aside(ws: Workspace, home: Path) -> None:
    """Link every file in `home`, a series' directory, under `catalog/.replaced/`.

    Those are all the files a removal of the series can touch, and a link costs no bytes:
    the removal unlinks the store's name and the aside one keeps the file, until `_let_go`
    drops it or `_undo` links it back.
    """
    aside = catalog_path(ws) / ASIDE_DIR
    shutil.rmtree(aside, ignore_errors=True)
    root = data_path(ws)
    for path in home.glob("*.parquet"):
        kept = aside / path.relative_to(root)
        kept.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, kept)


def _undo(
    ws: Workspace, home: Path, before: dict[str, tuple[int, int]], removed: Sequence[Manifest]
) -> None:
    """A failed write taken back: the files it produced in `home` removed, and what its
    replace removed put back, files and manifests, as they were before it began."""
    root = data_path(ws)
    for name in set(_listing(root, home)) - set(before):
        (root / name).unlink(missing_ok=True)
    aside = catalog_path(ws) / ASIDE_DIR
    if removed and aside.is_dir():
        for kept in aside.rglob("*"):
            home = root / kept.relative_to(aside)
            if kept.is_file() and not home.exists():
                home.parent.mkdir(parents=True, exist_ok=True)
                os.link(kept, home)
    for manifest in removed:
        write_manifest(ws, manifest)
    _let_go(ws)


def _let_go(ws: Workspace) -> None:
    """What a write set aside, dropped once the write is recorded or undone."""
    shutil.rmtree(catalog_path(ws) / ASIDE_DIR, ignore_errors=True)


def _owned(
    catalog: ParquetDataCatalog,
    root: Path,
    home: Path,
    data_cls: type,
    datasets: Sequence[Manifest],
) -> set[str]:
    """The files of `home`, a series' directory, that hold `datasets`, by path within `root`.

    A file's name is the availability interval it holds, its first and last `ts_init`, and a
    dataset's span is economic, the days of its `ts_event`, so a span does not say which
    files are a dataset's: a delayed dataset's last points are published after the next
    one's first day has begun, and its file meets that day. The checksum a dataset's manifest
    recorded over the files its write produced does. The engine keeps a series' intervals
    disjoint and names that sort in time, and the files of one write follow one another, so
    a dataset's files are the run of the directory's engine-named files, in name order, whose
    checksum is the one it recorded, and the run begins at or after the first instant of its
    span, since no point is published before its reference time. Each file is hashed once.

    Refused, before anything is removed, when no run matches a dataset — its files altered,
    removed or rewritten since it was recorded, or interleaved with another write's —
    because a removal chosen any other way can reach another dataset's files and leave its
    manifest claiming them.
    """
    names = sorted(
        path.relative_to(root).as_posix()
        for path in home.glob("*.parquet")
        if ENGINE_FILE.fullmatch(path.name)
    )
    hashed: dict[str, bytes] = {}
    owned: set[str] = set()
    for manifest in datasets:
        first = day_start_ns(manifest.start)
        sooner = set(catalog.filter_files(data_cls, names, None, None, first - 1))
        run = _run(root, [name for name in names if name not in sooner], manifest, hashed)
        if run is None:
            filed = f"{CATALOG_DIR}/{DATA_DIR}/{home.relative_to(root).as_posix()}"
            raise PreconditionError(
                f"{manifest.dataset_id}: no run of the files in {filed} hashes to the checksum "
                "its manifest recorded, so which of them are its own is not known and none is "
                "removed",
                remedy=f"inspect {filed}: put back the files {manifest.dataset_id} was written "
                "with, or remove its manifest and its files by hand",
            )
        owned.update(run)
    return owned


def _run(
    root: Path, names: Sequence[str], manifest: Manifest, hashed: dict[str, bytes]
) -> Sequence[str] | None:
    """The first run of `names`, in order, whose checksum is `manifest`'s, or `None`.

    Runs are tried as their last file is reached, so the search reads no file past the
    dataset's own last; `hashed` keeps each file's digest for the next dataset's search.
    """
    runs: list[Any] = []
    for last, name in enumerate(names):
        if name not in hashed:
            hashed[name] = hashlib.sha256((root / name).read_bytes()).digest()
        runs.append(hashlib.sha256())
        for begin, run in enumerate(runs):
            _bind(run, name, hashed[name])
            if run.hexdigest() == manifest.checksum:
                return names[begin : last + 1]
    return None


def _filed_in(root: Path, data_cls: type, identifier: str | None) -> Path:
    """The directory under the engine's tree `root` that a series is filed in.

    Its class's directory, then its identifier's — a bar type or an instrument id, as
    `identity` gives it — or the class's own directory for a series of no instrument.
    """
    home = root / class_to_filename(data_cls)
    return home if identifier is None else home / urisafe_identifier(identifier)


def _listing(root: Path, home: Path) -> dict[str, tuple[int, int]]:
    """Every file in `home`, by path relative to `root`, with its size and modification time.

    `home` is a series' directory, and a write of the series creates or changes files there
    and nowhere else, so this is all a write is compared against.
    """
    if not home.is_dir():
        return {}
    found: dict[str, tuple[int, int]] = {}
    for path in home.iterdir():
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
        _bind(digest, name, hashlib.sha256((root / name).read_bytes()).digest())
    return digest.hexdigest()


def _bind(digest: Any, name: str, content: bytes) -> None:
    """One file folded into a checksum: its path within the store, then its bytes' digest."""
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(content)
