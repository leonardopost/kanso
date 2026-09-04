"""Snapshots: what a run is pinned to, and how one is chosen.

A snapshot is the list of datasets a run may read, each with the checksum of the bytes it
was written from, plus the checksum of the resolved instrument definitions. Its id is the
sha256 of those checksums sorted — the instrument checksum among them — so two workspaces
holding the same data reach the same id, and a snapshot id is a fact about data rather
than about a machine or a moment. Nothing about the id depends on when it was taken;
`created_at` is recorded beside it and is what "newest" means when several snapshots
would do.

Instruments are in the id because they change what a run means. A tick-size or lot-size
reassignment changes what a fill costs, so a card run against reassigned instruments is
not the card that was recorded. Pinning them freezes that too.

`reproducible` is false when any dataset in the snapshot carries vendor-adjusted prices.
Such a series is adjusted as of the date it was requested, so the same request repeated
after the next corporate action returns different numbers: the data cannot be fetched
again and the snapshot cannot promise what a snapshot promises. It may still be
researched against; certification refuses it.

Coverage is the question `covering` answers: for every instrument in the universe and
every required data type at the hypothesis's resolution, does the union of the snapshot's
dataset spans — whole UTC days, so a span that ends mid-day still covers that day —
contain the research window and the certification window end to end. A snapshot missing
one instrument's quotes for one day of certification does not cover, because a run pinned
to it would silently research a different universe than the one it names.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import Field, model_validator

from kanso.data.manifest import (
    Manifest,
    contains,
    manifests,
    snapshots_path,
)
from kanso.errors import PreconditionError, ValidationError
from kanso.schemas.base import NonEmpty, Sha256, Versioned
from kanso.schemas.hypothesis import Windows
from kanso.schemas.yamlio import load_yaml, write_yaml

if TYPE_CHECKING:  # pragma: no cover - kept out of the runtime import graph
    from kanso.workspace import Workspace

UNKNOWN_PUBLICATION: Final = "unknown"
"""The publication class a snapshot may hold but research may not be pinned to."""

_SNAPSHOT_ID: Final = re.compile(r"^[0-9a-f]{64}$")


class Snapshot(Versioned):
    """The datasets and instrument definitions one run is pinned to.

    `datasets` and `checksums` are parallel and sorted by dataset id. `snapshot_id` is
    derived from the checksums and never chosen.
    """

    snapshot_id: Sha256
    datasets: list[NonEmpty] = Field(default_factory=list)
    checksums: list[Sha256] = Field(default_factory=list)
    instruments_checksum: Sha256
    created_at: str
    reproducible: bool = True

    @property
    def checksum_of(self) -> dict[str, str]:
        """Each dataset's checksum, by dataset id."""
        return dict(zip(self.datasets, self.checksums, strict=True))

    @model_validator(mode="after")
    def _validate(self) -> Snapshot:
        if len(self.datasets) != len(self.checksums):
            raise ValueError(
                f"checksums: {len(self.checksums)} for {len(self.datasets)} datasets; the two "
                "lists are parallel"
            )
        derived = snapshot_id(self.checksums, self.instruments_checksum)
        if self.snapshot_id != derived:
            raise ValueError(
                f"snapshot_id: {self.snapshot_id} is not the id these checksums derive "
                f"({derived}); a snapshot id is derived, never chosen"
            )
        return self


def snapshot_id(checksums: Iterable[str], instruments_checksum: str) -> str:
    """`sha256` of the dataset checksums and the instrument checksum, sorted.

    Sorted because a snapshot is a set, not a sequence: the order datasets were written
    in is not part of what a run is pinned to. The instrument checksum is one of the
    values hashed, so re-resolving an instrument changes the id exactly as re-loading a
    dataset does.
    """
    digest = hashlib.sha256()
    for checksum in sorted([*checksums, instruments_checksum]):
        digest.update(checksum.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def freeze(
    ws: Workspace,
    *,
    instruments_checksum: str | None = None,
    datasets: Sequence[str] | None = None,
) -> Snapshot:
    """Record what the workspace holds now, and write the snapshot file.

    Every dataset the workspace holds is included unless `datasets` names a subset.
    `instruments_checksum` defaults to the checksum of the definitions in the store, which
    is what a run resolving its universe from the store would pin.
    """
    held = manifests(ws)
    chosen = sorted(held) if datasets is None else sorted(set(datasets))
    missing = [name for name in chosen if name not in held]
    if missing:
        raise PreconditionError(
            f"cannot freeze datasets this workspace does not hold: {', '.join(missing)}",
            remedy="run `kanso data show` to list the datasets it does hold",
        )
    if instruments_checksum is None:
        # Imported here rather than at module scope: `catalog` binds the engine and reads
        # this module's pinned set, and neither module needs the other at import time.
        from kanso.data.catalog import resolved_instruments_checksum

        instruments_checksum = resolved_instruments_checksum(ws)
    picked = [held[name] for name in chosen]
    snapshot = Snapshot(
        snapshot_id=snapshot_id([m.checksum for m in picked], instruments_checksum),
        datasets=chosen,
        checksums=[m.checksum for m in picked],
        instruments_checksum=instruments_checksum,
        created_at=datetime.now(tz=UTC).isoformat(),
        reproducible=not any(m.adjusted for m in picked),
    )
    write(ws, snapshot)
    return snapshot


def covering(
    ws: Workspace,
    universe: Sequence[str],
    types: Sequence[str],
    resolution: str | None,
    windows: Windows,
) -> Snapshot | None:
    """The newest snapshot covering the universe over research and certification.

    Newest is the latest `created_at`. A snapshot covers when, for every instrument and
    every required type at `resolution`, the union of the spans it pins contains both
    windows; the forward window is never loaded, so it is never required. A snapshot
    whose covering datasets include one with an unknown publication does not qualify:
    research may not be pinned to data whose availability nobody declared.
    """
    held = manifests(ws)
    required = tuple(
        (window.start, window.end) for window in (windows.research, windows.certification)
    )
    for snapshot in sorted(snapshots(ws), key=lambda s: s.created_at, reverse=True):
        picked = [held[name] for name in snapshot.datasets if name in held]
        if len(picked) != len(snapshot.datasets):
            continue
        if _covers(picked, universe, types, resolution, required):
            return snapshot
    return None


def _covers(
    picked: Sequence[Manifest],
    universe: Sequence[str],
    types: Sequence[str],
    resolution: str | None,
    windows: Sequence[tuple[date, date]],
) -> bool:
    """True when these manifests cover every instrument and type over every window."""
    for instrument in universe:
        for required in types:
            relied = [
                manifest
                for manifest in picked
                if manifest.instrument == instrument
                and manifest.type == required
                and _resolution_matches(manifest.resolution, resolution)
            ]
            if any(manifest.publication == UNKNOWN_PUBLICATION for manifest in relied):
                return False
            spans = [manifest.span for manifest in relied]
            if not all(contains(spans, window) for window in windows):
                return False
    return True


def _resolution_matches(held: str | None, wanted: str | None) -> bool:
    """True when a dataset's grain answers the hypothesis's.

    A dataset that declares no resolution is unaggregated — quotes, trades, a published
    series — and answers at any resolution. One that declares a bar size answers only the
    hypothesis asking for that size: aggregating a coarser series from a finer one is a
    load, not a coincidence, and a run pinned to the wrong grain is a run reading data it
    never asked for.
    """
    return held is None or held == wanted


def snapshots(ws: Workspace) -> list[Snapshot]:
    """Every snapshot the workspace holds."""
    directory = snapshots_path(ws)
    if not directory.is_dir():
        return []
    return [load_yaml(Snapshot, path) for path in sorted(directory.glob("*.yaml"))]


def pinned_datasets(ws: Workspace) -> frozenset[str]:
    """Every dataset a snapshot names, and which is therefore immutable.

    A snapshot is the promise that a pinned run can be replayed against the same bytes,
    so writing one freezes what it names. There is no separate act of pinning to consult:
    the promise is the file.
    """
    return frozenset(name for snapshot in snapshots(ws) for name in snapshot.datasets)


def snapshot_file(ws: Workspace, identifier: str) -> Path:
    """The file holding one snapshot."""
    if _SNAPSHOT_ID.match(identifier) is None:
        raise ValidationError(f"{identifier!r} is not a snapshot id")
    return snapshots_path(ws) / f"{identifier}.yaml"


def write(ws: Workspace, snapshot: Snapshot) -> Path:
    """Persist a snapshot. Writing the same one twice writes the same bytes."""
    path = snapshot_file(ws, snapshot.snapshot_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_yaml(snapshot, path)


def read(ws: Workspace, identifier: str) -> Snapshot:
    """The snapshot `identifier` names, or a precondition failure."""
    path = snapshot_file(ws, identifier)
    if not path.is_file():
        raise PreconditionError(
            f"no snapshot {identifier} in {snapshots_path(ws)}",
            remedy="run `kanso data snapshot` to take one",
        )
    return load_yaml(Snapshot, path)
