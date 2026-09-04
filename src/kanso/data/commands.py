"""What the `kanso data` commands do, one level below the command line.

Every function here takes a workspace and arguments, does the work, and returns a plain
record; nothing here prints and nothing here parses an argument vector. The command layer
renders these records as one JSON object or as a few terse lines.

Three verbs write data and they share one mechanism. `load` writes exactly the range its
spec names. `backfill` walks history from the source's floor forward to what is already
held, and `sync` walks from each dataset's served end towards now; both also close the
gaps inside an existing span that `show` reports. Both are chunked, and the manifest each
chunk writes is its checkpoint: an interrupted run resumes at the first chunk with no
manifest, and a repeated run finds nothing missing and fetches nothing. A chunk a source
serves nothing for leaves no manifest, so it is recorded in the event log instead and is
not asked for twice.

A dataset a snapshot pins is immutable, which the catalog enforces at the write. Backfill
and sync never rewrite one: a backfilled chunk covers days no held dataset covers, and a
sync writes a successor dataset recording `supersedes`.

Coverage is what was served. Every span here is the span a loader's points actually
covered, never the span that was asked for, and the difference is reported rather than
smoothed over.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import yaml

from kanso import ext
from kanso.data import catalog
from kanso.data import instruments as reference
from kanso.data import snapshot as snapshots
from kanso.data.loader import DatasetRef, Loader, get_loader, loaders
from kanso.data.manifest import Manifest, data_path, manifests, merge
from kanso.data.snapshot import Snapshot
from kanso.errors import PreconditionError, ValidationError
from kanso.schemas import InstrumentsFile, load_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

CHUNK_DAYS: Final = 30
"""Days per fetched chunk. Small enough that an interrupt loses little work, large enough
that a decade of history is hundreds of requests rather than thousands."""

DAY: Final = timedelta(days=1)

CACHE_NAME: Final = "instruments.yaml"

EMPTY_CHUNK: Final = "data_chunk_empty"
"""The event kind recording a chunk a source served nothing for, so it is asked once."""

LOADED: Final = "data_loaded"
BACKFILLED: Final = "data_backfilled"
SYNCED: Final = "data_synced"
SNAPSHOT: Final = "data_snapshot"
RESOLVED: Final = "instruments_resolved"
"""The event kinds these commands append."""


# --- what the workspace holds -------------------------------------------------


@dataclass(frozen=True, slots=True)
class Series:
    """Every dataset the store holds for one instrument, type and resolution.

    A series is what coverage is asked of: `research begin` pins a snapshot when the union
    of a series' spans contains its windows, so the union and the holes in it are the two
    facts `data show` exists to report.
    """

    instrument: str
    type: str
    resolution: str | None
    datasets: tuple[Manifest, ...]

    @property
    def spans(self) -> list[tuple[date, date]]:
        """The served spans, merged into the fewest that cover the same days."""
        return merge([manifest.span for manifest in self.datasets])

    @property
    def gaps(self) -> list[tuple[date, date]]:
        """The days between the first and the last served day that nothing serves."""
        spans = self.spans
        return [
            (left[1] + DAY, right[0] - DAY) for left, right in zip(spans, spans[1:], strict=False)
        ]

    @property
    def rows(self) -> int:
        return sum(manifest.row_count for manifest in self.datasets)

    def payload(self) -> dict[str, Any]:
        """The series as one JSON object."""
        return {
            "instrument": self.instrument,
            "type": self.type,
            "resolution": self.resolution,
            "rows": self.rows,
            "spans": [[str(start), str(end)] for start, end in self.spans],
            "gaps": [[str(start), str(end)] for start, end in self.gaps],
            "datasets": [_dataset_payload(manifest) for manifest in self.datasets],
        }


def series(ws: Workspace) -> list[Series]:
    """Every series the store holds, in instrument, type and resolution order."""
    grouped: dict[tuple[str, str, str | None], list[Manifest]] = {}
    for manifest in manifests(ws).values():
        grouped.setdefault(manifest.filed_under, []).append(manifest)
    return [
        Series(
            instrument=key[0],
            type=key[1],
            resolution=key[2],
            datasets=tuple(sorted(found, key=lambda m: (m.span, m.dataset_id))),
        )
        for key, found in sorted(
            grouped.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
        )
    ]


def _dataset_payload(manifest: Manifest) -> dict[str, Any]:
    """One dataset as the object `--json` prints, with the empty fields left out."""
    out: dict[str, Any] = {
        "dataset_id": manifest.dataset_id,
        "source": manifest.source,
        "span": [str(manifest.start), str(manifest.end)],
        "rows": manifest.row_count,
        "adjusted": manifest.adjusted,
        "publication": manifest.publication,
        "checksum": manifest.checksum,
    }
    for name in ("vendor", "vendor_dataset", "publication_rule", "adjustment_basis", "supersedes"):
        value = getattr(manifest, name)
        if value is not None:
            out[name] = value
    if manifest.as_of is not None:
        out["as_of"] = str(manifest.as_of)
    return out


# --- specs and loaders --------------------------------------------------------


def read_spec(path: Path) -> dict[str, object]:
    """The loader spec at `path`, refused when it is not a YAML mapping."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"{path}: cannot be read: {exc}") from None
    try:
        document: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"{path}: not valid YAML: {exc}") from None
    if not isinstance(document, dict):
        raise ValidationError(
            f"{path}: a loader spec is a mapping, not {type(document).__name__}",
            remedy="write the spec as `loader: <id>` and the keys that loader documents",
        )
    return document


def loader_for(ws: Workspace, loader_id: str) -> Loader:
    """The loader `loader_id` names, including one a workspace extension provides."""
    return get_loader(loader_id, ext.discover(ws.root, ws.config.extensions_paths))


def _declared(spec: Mapping[str, object], loader_id: str, path: Path) -> None:
    """Refuse a spec that names a different loader than the command did."""
    named = spec.get("loader")
    if named is not None and str(named) != loader_id:
        raise ValidationError(
            f"{path}: the spec names loader {named!r} but --loader says {loader_id!r}",
            remedy="drop --loader, or point it at the spec that belongs to it",
        )


# --- load ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Load:
    """What one `data load` wrote."""

    loader: str
    spec: Path
    written: tuple[catalog.Written, ...]

    @property
    def rows(self) -> int:
        return sum(item.manifest.row_count for item in self.written)

    def payload(self) -> dict[str, Any]:
        return {
            "loader": self.loader,
            "spec": str(self.spec),
            "rows": self.rows,
            "datasets": [
                {
                    **_dataset_payload(item.manifest),
                    "requested": [str(item.requested[0]), str(item.requested[1])],
                    "truncated": item.truncated,
                    "shortfall": item.shortfall,
                    "replaced": list(item.replaced),
                }
                for item in self.written
            ],
        }


def load(ws: Workspace, store: StateStore, loader_id: str, spec: Path, *, replace: bool) -> Load:
    """Run a loader over the range its spec names and write what it serves.

    Writes exactly the datasets the spec discovers, over exactly their spans. An
    overlapping write into unpinned data needs `replace`, and one into a dataset a
    snapshot pins is refused outright.
    """
    document = read_spec(spec)
    _declared(document, loader_id, spec)
    loader = loader_for(ws, loader_id)
    written: list[catalog.Written] = []
    for ref in loader.discover(document):
        points = list(loader.load(ref, ref.span))
        written.append(catalog.write(ws, points, ref=ref, source=loader_id, replace=replace))
    result = Load(loader=loader_id, spec=spec, written=tuple(written))
    store.event(
        LOADED,
        loader_id,
        {"spec": str(spec), "datasets": [item.manifest.dataset_id for item in written]},
    )
    return result


# --- snapshot -----------------------------------------------------------------


def freeze(ws: Workspace, store: StateStore) -> Snapshot:
    """Freeze what the workspace holds now and record the snapshot in state."""
    frozen = snapshots.freeze(ws)
    store.connection.execute(
        "INSERT OR REPLACE INTO snapshots"
        " (snapshot_id, datasets, instruments_checksum, reproducible, path, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            frozen.snapshot_id,
            json.dumps(list(frozen.datasets)),
            frozen.instruments_checksum,
            int(frozen.reproducible),
            str(snapshots.snapshot_file(ws, frozen.snapshot_id)),
            frozen.created_at,
        ),
    )
    store.event(
        SNAPSHOT,
        frozen.snapshot_id,
        {"datasets": len(frozen.datasets), "reproducible": frozen.reproducible},
    )
    return frozen


# --- chunking, spans and the arithmetic backfill and sync share ---------------


@dataclass(frozen=True, slots=True)
class Chunk:
    """One fetched range: what a request asks for and what a checkpoint records."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def payload(self) -> dict[str, Any]:
        return {"start": str(self.start), "end": str(self.end), "days": self.days}


def chunked(span: tuple[date, date], size: int = CHUNK_DAYS) -> list[Chunk]:
    """`span` cut into consecutive chunks of at most `size` days, in order."""
    out: list[Chunk] = []
    cursor = span[0]
    while cursor <= span[1]:
        end = min(cursor + timedelta(days=size - 1), span[1])
        out.append(Chunk(cursor, end))
        cursor = end + DAY
    return out


def missing(held: Sequence[tuple[date, date]], want: tuple[date, date]) -> list[tuple[date, date]]:
    """The days of `want` that no span in `held` covers, in order."""
    out: list[tuple[date, date]] = []
    cursor = want[0]
    for start, end in merge(list(held)):
        if end < cursor:
            continue
        if start > want[1]:
            break
        if start > cursor:
            out.append((cursor, min(start - DAY, want[1])))
        cursor = max(cursor, end + DAY)
    if cursor <= want[1]:
        out.append((cursor, want[1]))
    return out


def _clip(span: tuple[date, date], bound: tuple[date, date]) -> tuple[date, date] | None:
    start, end = max(span[0], bound[0]), min(span[1], bound[1])
    return (start, end) if start <= end else None


def bytes_per_row(ws: Workspace) -> float | None:
    """What a row of held data costs on disk, or `None` when nothing is held.

    An estimate needs a rate and nothing declares one, so it is measured from what this
    workspace already wrote rather than invented.
    """
    rows = sum(manifest.row_count for manifest in manifests(ws).values())
    if not rows:
        return None
    root = data_path(ws)
    written = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    return written / rows


def _rows_per_day(datasets: Iterable[Manifest]) -> float | None:
    """The rate the held datasets of a series served at, or `None` when none are held."""
    rows = 0
    days = 0
    for manifest in datasets:
        rows += manifest.row_count
        days += (manifest.end - manifest.start).days + 1
    return rows / days if days else None


# --- backfill and sync --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fetch:
    """One chunk of one series: what was planned, and what came of it."""

    instrument: str
    type: str
    resolution: str | None
    chunk: Chunk
    rows: int = 0
    dataset_id: str | None = None
    outcome: str = "planned"
    est_rows: int | None = None
    est_bytes: int | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "instrument": self.instrument,
            "type": self.type,
            "resolution": self.resolution,
            **self.chunk.payload(),
            "outcome": self.outcome,
        }
        if self.outcome == "planned":
            out["est_rows"] = self.est_rows
            out["est_bytes"] = self.est_bytes
        else:
            out["rows"] = self.rows
            out["dataset_id"] = self.dataset_id
        return out


@dataclass(frozen=True, slots=True)
class Backfill:
    """What one `data backfill` planned or did."""

    loader: str
    spec: Path
    dry_run: bool
    fetches: tuple[Fetch, ...]
    clamps: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def requests(self) -> int:
        return len(self.fetches)

    @property
    def rows(self) -> int:
        return sum(item.rows for item in self.fetches)

    @property
    def est_bytes(self) -> int | None:
        known = [item.est_bytes for item in self.fetches if item.est_bytes is not None]
        return sum(known) if known else None

    def payload(self) -> dict[str, Any]:
        return {
            "loader": self.loader,
            "spec": str(self.spec),
            "dry_run": self.dry_run,
            "requests": self.requests,
            "rows": self.rows,
            "est_bytes": self.est_bytes,
            "clamps": list(self.clamps),
            "notes": list(self.notes),
            "chunks": [item.payload() for item in self.fetches],
        }


def backfill(
    ws: Workspace,
    store: StateStore,
    loader_id: str,
    spec: Path,
    *,
    start: date | None = None,
    end: date | None = None,
    dry_run: bool = False,
) -> Backfill:
    """Fill history for the spec's universe and types, and close the gaps inside it.

    The range runs from the source's history floor, or from `start` clamped up to that
    floor, to the earliest day already held, or to `end`. Reaching the floor is a normal
    outcome and is reported, never an error. Every chunk that writes leaves a manifest, so
    an interrupted backfill resumes at the first chunk with none and a repeated one finds
    nothing missing.
    """
    document = read_spec(spec)
    _declared(document, loader_id, spec)
    loader = loader_for(ws, loader_id)
    held = manifests(ws)
    fetches: list[Fetch] = []
    clamps: list[str] = []
    notes: list[str] = []
    rate = bytes_per_row(ws)

    for ref in loader.discover(document):
        floor = ref.span[0]
        mine = [
            m for m in held.values() if m.filed_under == (ref.instrument, ref.type, ref.resolution)
        ]
        spans = [m.span for m in mine]
        wanted_start = floor if start is None else max(start, floor)
        if start is not None and start < floor:
            clamps.append(
                f"{ref.instrument} {ref.type}: {start} is before the source's history floor "
                f"{floor}, so the backfill starts there"
            )
        wanted_end = (
            end if end is not None else (min(s[0] for s in spans) - DAY if spans else ref.span[1])
        )
        window = _clip((wanted_start, wanted_end), ref.span) if wanted_start <= wanted_end else None
        targets: list[tuple[date, date]] = []
        if window is not None:
            targets += missing(spans, window)
        for gap in Series(ref.instrument, ref.type, ref.resolution, tuple(mine)).gaps:
            clipped = _clip(gap, ref.span)
            if clipped is not None:
                targets.append(clipped)
        if not targets:
            notes.append(f"{ref.instrument} {ref.type}: nothing missing before {wanted_end}")
            continue
        for target in sorted(targets):
            for chunk in chunked(target):
                fetches.append(
                    _fetch(ws, store, loader, ref, chunk, dry_run=dry_run, rate=rate, held=mine)
                )

    result = Backfill(
        loader=loader_id,
        spec=spec,
        dry_run=dry_run,
        fetches=tuple(fetches),
        clamps=tuple(clamps),
        notes=tuple(notes),
    )
    if not dry_run:
        store.event(
            BACKFILLED, loader_id, {"spec": str(spec), "chunks": len(fetches), "rows": result.rows}
        )
    return result


def _fetch(
    ws: Workspace,
    store: StateStore,
    loader: Loader,
    ref: DatasetRef,
    chunk: Chunk,
    *,
    dry_run: bool,
    rate: float | None,
    held: Sequence[Manifest],
) -> Fetch:
    """Fetch and write one chunk, or describe what fetching it would cost."""
    where = _checkpoint_subject(ref)
    if dry_run:
        per_day = _rows_per_day(held)
        rows = None if per_day is None else int(round(per_day * chunk.days))
        return Fetch(
            instrument=ref.instrument,
            type=ref.type,
            resolution=ref.resolution,
            chunk=chunk,
            est_rows=rows,
            est_bytes=None if rows is None or rate is None else int(round(rows * rate)),
        )
    if _already_empty(store, where, chunk):
        return Fetch(ref.instrument, ref.type, ref.resolution, chunk, outcome="empty")
    points = list(loader.load(_over(ref, (chunk.start, chunk.end)), (chunk.start, chunk.end)))
    if not points:
        store.event(EMPTY_CHUNK, where, {"start": str(chunk.start), "end": str(chunk.end)})
        return Fetch(ref.instrument, ref.type, ref.resolution, chunk, outcome="empty")
    written = catalog.write(ws, points, ref=_over(ref, (chunk.start, chunk.end)), source=loader.id)
    return Fetch(
        instrument=ref.instrument,
        type=ref.type,
        resolution=ref.resolution,
        chunk=chunk,
        rows=written.manifest.row_count,
        dataset_id=written.manifest.dataset_id,
        outcome="written",
    )


def _checkpoint_subject(ref: DatasetRef) -> str:
    """What an empty-chunk checkpoint is filed under: the series, never the dataset."""
    return f"{ref.instrument}|{ref.type}|{ref.resolution or '-'}"


def _already_empty(store: StateStore, subject: str, chunk: Chunk) -> bool:
    """Whether this chunk was already asked for and served nothing."""
    wanted = {"start": str(chunk.start), "end": str(chunk.end)}
    return any(event.detail == wanted for event in store.events(kind=EMPTY_CHUNK, subject=subject))


def _over(ref: DatasetRef, span: tuple[date, date]) -> DatasetRef:
    """The same dataset asked for over another span."""
    return DatasetRef(
        dataset_id=ref.dataset_id,
        instrument=ref.instrument,
        type=ref.type,
        resolution=ref.resolution,
        span=span,
        adjusted=ref.adjusted,
        publication=ref.publication,
        publication_rule=ref.publication_rule,
        vendor=ref.vendor,
        vendor_dataset=ref.vendor_dataset,
        request_params=ref.request_params,
    )


def ref_of(manifest: Manifest, span: tuple[date, date]) -> DatasetRef:
    """The dataset a manifest describes, asked for over `span`.

    A manifest records everything a loader needs to serve the same dataset again — the
    instrument, the type, the resolution and the request parameters, with credentials
    already removed — so a successor is fetched without the spec that first wrote it.
    """
    return DatasetRef(
        dataset_id=manifest.dataset_id,
        instrument=manifest.instrument,
        type=manifest.type,
        resolution=manifest.resolution,
        span=span,
        adjusted=manifest.adjusted,
        publication=manifest.publication,
        publication_rule=manifest.publication_rule,
        vendor=manifest.vendor,
        vendor_dataset=manifest.vendor_dataset,
        request_params=manifest.request_params,
    )


@dataclass(frozen=True, slots=True)
class Sync:
    """What one `data sync` did, dataset by dataset."""

    to: date
    fetches: tuple[Fetch, ...]
    notes: tuple[str, ...]

    @property
    def rows(self) -> int:
        return sum(item.rows for item in self.fetches)

    def payload(self) -> dict[str, Any]:
        return {
            "to": str(self.to),
            "rows": self.rows,
            "requests": len(self.fetches),
            "notes": list(self.notes),
            "chunks": [item.payload() for item in self.fetches],
        }


def sync(
    ws: Workspace,
    store: StateStore,
    *,
    loader_id: str | None = None,
    dataset: str | None = None,
    to: date | None = None,
) -> Sync:
    """Extend each held dataset from its served end towards `to`, as a successor.

    The successor records `supersedes`, so a dataset a pinned snapshot references is never
    mutated: the run that pinned it keeps exactly the bytes it ran on.
    """
    horizon = to or datetime.now(tz=UTC).date()
    held = manifests(ws)
    if dataset is not None and dataset not in held:
        raise PreconditionError(
            f"{dataset!r} is not a dataset this workspace holds",
            remedy="run `kanso data show` to list the datasets it does hold",
        )
    chosen = [
        manifest
        for manifest in sorted(held.values(), key=lambda m: m.dataset_id)
        if (dataset is None or manifest.dataset_id == dataset)
        and (loader_id is None or manifest.source == loader_id)
    ]
    fetches: list[Fetch] = []
    notes: list[str] = []
    for manifest in chosen:
        window = (manifest.end + DAY, horizon)
        if window[0] > window[1]:
            notes.append(f"{manifest.dataset_id}: served to {manifest.end}, at or past {horizon}")
            continue
        loader = loader_for(ws, manifest.source)
        latest = manifest
        mine: list[Fetch] = []
        for chunk in chunked(window):
            fetched = _extend(ws, store, loader, latest, chunk)
            mine.append(fetched)
            if fetched.outcome == "written" and fetched.dataset_id is not None:
                # Each successor supersedes the one before it, so a multi-chunk sync is a
                # chain of datasets rather than one dataset rewritten several times.
                latest = manifests(ws)[fetched.dataset_id]
        fetches += mine
        if all(item.outcome == "empty" for item in mine):
            notes.append(
                f"{manifest.dataset_id}: {manifest.source} served nothing after {manifest.end}"
            )
    result = Sync(to=horizon, fetches=tuple(fetches), notes=tuple(notes))
    store.event(SYNCED, loader_id or "all", {"to": str(horizon), "rows": result.rows})
    return result


def _extend(
    ws: Workspace, store: StateStore, loader: Loader, manifest: Manifest, chunk: Chunk
) -> Fetch:
    """Fetch one chunk beyond a dataset's served end and write it as its successor."""
    window = (chunk.start, chunk.end)
    ref = ref_of(manifest, window)
    subject = _checkpoint_subject(ref)
    if _already_empty(store, subject, chunk):
        return Fetch(
            manifest.instrument, manifest.type, manifest.resolution, chunk, outcome="empty"
        )
    points = list(loader.load(ref, window))
    if not points:
        store.event(EMPTY_CHUNK, subject, {"start": str(chunk.start), "end": str(chunk.end)})
        return Fetch(
            manifest.instrument, manifest.type, manifest.resolution, chunk, outcome="empty"
        )
    written = catalog.write(
        ws, points, ref=ref, source=manifest.source, supersedes=manifest.dataset_id
    )
    return Fetch(
        instrument=manifest.instrument,
        type=manifest.type,
        resolution=manifest.resolution,
        chunk=chunk,
        rows=written.manifest.row_count,
        dataset_id=written.manifest.dataset_id,
        outcome="written",
    )


# --- instruments --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolved:
    """One resolved instrument, as the operator asked for it."""

    id: str
    definition: object

    @property
    def fields(self) -> dict[str, Any]:
        """The engine's own canonical field map for this definition."""
        return engine_fields(self.definition)

    def payload(self) -> dict[str, Any]:
        """The instrument as one object: the id asked for, its address, its definition.

        The engine's field map is nested rather than spread, because it carries an `id` of
        its own — the qualified one — and an operator may have asked by a shorter name.
        """
        return {
            "id": self.id,
            "checksum": reference.definition_checksum(self.definition),
            "definition": self.fields,
        }


def engine_fields(definition: object) -> dict[str, Any]:
    """A Nautilus instrument as its own canonical field map.

    NautilusTrader facts (`nautilus_trader 1.231.0`): every instrument class carries a
    `to_dict` taking the instrument, which is the definition's canonical serialisation and
    the same map the content address is taken over.
    """
    engine: Any = definition
    dumped: dict[str, Any] = type(engine).to_dict(engine)
    return dumped


def cache(ws: Workspace) -> InstrumentsFile:
    """The workspace's instrument cache, empty when the file is absent."""
    path = ws.path(CACHE_NAME)
    return load_yaml(InstrumentsFile, path) if path.is_file() else InstrumentsFile({})


def resolve(
    ws: Workspace,
    store: StateStore,
    ids: Sequence[str],
    *,
    as_of: date | None = None,
    refresh: bool = False,
) -> tuple[date, list[Resolved]]:
    """Resolve ids into the catalog's instrument store and the cache.

    With no ids, every id the cache names. `refresh` re-resolves rather than answering
    from the cache and is refused while a run is active or while a deployed version
    depends on a snapshot that pins one of these instruments: both would move the
    definitions under something already recorded.
    """
    when = as_of or datetime.now(tz=UTC).date()
    wanted = list(ids) if ids else sorted(cache(ws).root)
    if not wanted:
        raise PreconditionError(
            f"{ws.path(CACHE_NAME)} names no instrument and none was given",
            remedy="name the ids to resolve, or add them to instruments.yaml",
        )
    if refresh:
        _refuse_refresh(ws, store, wanted)
    resolved = reference.resolve_universe(ws, wanted, when, refresh=refresh)
    store.event(RESOLVED, ",".join(wanted), {"as_of": str(when), "refresh": refresh})
    return when, [Resolved(name, resolved[name]) for name in wanted]


def _refuse_refresh(ws: Workspace, store: StateStore, ids: Sequence[str]) -> None:
    """Refuse a refresh that would move definitions a run or a deployment depends on."""
    active = store.connection.execute(
        "SELECT hyp_id, run_id FROM runs WHERE ended_at IS NULL ORDER BY run_id"
    ).fetchall()
    blocking = [f"{row['hyp_id']} has an active run ({row['run_id']})" for row in active]
    held = manifests(ws)
    for row in store.connection.execute(
        "SELECT strategy_id, version, stage, pins FROM strategy_versions"
        " WHERE stage IS NOT NULL ORDER BY strategy_id, version"
    ).fetchall():
        pins = json.loads(str(row["pins"]))
        snapshot_id = pins.get("snapshot_id")
        if snapshot_id is None:
            continue
        pinned = snapshots.read(ws, str(snapshot_id))
        names = {held[name].instrument for name in pinned.datasets if name in held}
        if names & set(ids):
            blocking.append(
                f"{row['strategy_id']}@{row['version']} is deployed to {row['stage']} on "
                f"snapshot {snapshot_id}"
            )
    if blocking:
        raise PreconditionError(
            "--refresh is refused: " + "; ".join(blocking),
            remedy="end the run, or demote the version, before re-resolving",
        )


def held_instruments(ws: Workspace, instrument_id: str | None = None) -> list[Resolved]:
    """The definitions the catalog holds, newest resolution per id, optionally one.

    The catalog is the registry of record, so this reads it rather than the cache; the
    cache's own entries are what `doctor` reports on.
    """
    found = [
        Resolved(str(engine_fields(definition)["id"]), definition)
        for definition in reference.read_store(ws).values()
    ]
    picked = sorted(found, key=lambda item: item.id)
    if instrument_id is None:
        return picked
    matching = [
        item
        for item in picked
        if item.id == instrument_id or item.id.split(".")[0] == instrument_id
    ]
    if not matching:
        raise PreconditionError(
            f"{instrument_id!r} is not an instrument this catalog holds",
            remedy="run `kanso data instruments resolve` first, or name one `show` lists",
        )
    return matching


# --- adapters -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Registered:
    """One registered source of data or reference definitions."""

    id: str
    kind: str
    provider: str
    credentials: tuple[str, ...]
    capabilities: tuple[str, ...]
    quota: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider": self.provider,
            "credentials": list(self.credentials),
            "credentials_resolve": not self.credentials,
            "capabilities": list(self.capabilities),
            "quota": self.quota,
        }


def adapters(ws: Workspace) -> tuple[list[Registered], list[str]]:
    """Everything registered here, and what is missing, without touching the network.

    No vendor adapter ships in this build, so what is registered is the package's own
    loaders and the manual instrument provider. None of them takes a credential and none
    of them reaches a network, which is exactly the property that keeps the suite, the
    demo and `doctor` green with every vendor credential unset.
    """
    found = [
        Registered(
            id=loader_id,
            kind="data",
            provider="builtin" if loader_id in {"synthetic", "csv_parquet"} else "extension",
            credentials=(),
            capabilities=("discover", "load", "load_arrow", "manifest"),
        )
        for loader_id in sorted(loaders(ext.discover(ws.root, ws.config.extensions_paths)))
    ]
    found.append(
        Registered(
            id=reference.ManualProvider.id,
            kind="reference",
            provider="builtin",
            credentials=(),
            capabilities=("resolve", "sources"),
        )
    )
    notes = ["no vendor adapter is configured: this build ships none"]
    configured = sorted(ws.config.adapters)
    if configured:
        notes.append(f"kanso.toml configures {', '.join(configured)}, which nothing here provides")
    return found, notes


def checked_adapters() -> list[str]:
    """What `--check` had to check: nothing, because nothing configured reaches a network."""
    return ["--check had no configured adapter to reach, and made no network call"]


def dataset_ids(found: Iterable[Series]) -> Iterator[str]:
    """Every dataset id of every series, in series order."""
    for item in found:
        for manifest in item.datasets:
            yield manifest.dataset_id
