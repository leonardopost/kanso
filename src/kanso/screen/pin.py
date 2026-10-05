"""The snapshot a screen is pinned to: the newest that covers every series it reads.

A screen's legs are of many types and grains, so the question `research begin` asks of one
universe at one resolution is asked here of each series: does the union of the spans a
snapshot pins for it contain every day of the window its market opened, read on the
closures the store's definition of its instrument is filed under. A dataset of unknown
publication covers nothing, as it covers nothing for a run: availability nobody declared is
availability nobody can rely on. Among the covering snapshots the newest whose instrument
checksum is the store's own is the one a screen is pinned to; when one covers and none
matches, the definitions moved since it was taken, and that is refused by name.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from kanso.data import closures
from kanso.data.manifest import Manifest, contains, manifests
from kanso.data.snapshot import UNKNOWN_PUBLICATION, Snapshot, instrument_drift, snapshots
from kanso.errors import PreconditionError
from kanso.schemas.screen import Screen

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.workspace import Workspace


def covering(
    ws: Workspace, screen: Screen, window: tuple[date, date], defined: dict[str, object]
) -> Snapshot:
    """The snapshot a screen of `window` is pinned to, refused by name when none covers."""
    wanted: list[tuple[str, str, str | None]] = sorted(
        {(leg.instrument, str(leg.type), leg.resolution) for leg in screen.legs.values()}
    )
    closed = closures.by_instrument(defined.values())
    held = manifests(ws)
    candidates: list[Snapshot] = []
    for snapshot in sorted(snapshots(ws), key=lambda item: item.created_at, reverse=True):
        picked = [held[name] for name in snapshot.datasets if name in held]
        if len(picked) == len(snapshot.datasets) and _covers(picked, wanted, window, closed):
            candidates.append(snapshot)
    if not candidates:
        series = ", ".join(_spelled(key) for key in wanted)
        raise PreconditionError(
            f"no snapshot covers {series} over {window[0]}..{window[1]}",
            remedy="run `kanso screen validate` to see what is missing, load it, then "
            "`kanso data snapshot`",
        )
    drifts = [instrument_drift(ws, snapshot) for snapshot in candidates]
    for snapshot, drift in zip(candidates, drifts, strict=True):
        if drift is None:
            return snapshot
    moved = drifts[0]
    assert moved is not None
    raise PreconditionError(
        f"snapshot {moved.snapshot_id} pins instruments {moved.pinned}, and the store now "
        f"holds {moved.held}; a screen reads the definitions its snapshot pins",
        remedy="run `kanso data snapshot` to pin the definitions the store holds now",
    )


def _covers(
    picked: list[Manifest],
    wanted: list[tuple[str, str, str | None]],
    window: tuple[date, date],
    closed: dict[str, closures.Closed],
) -> bool:
    for key in wanted:
        relied = [manifest for manifest in picked if manifest.filed_under == key]
        if any(manifest.publication == UNKNOWN_PUBLICATION for manifest in relied):
            return False
        spans = [manifest.span for manifest in relied]
        if not contains(spans, window, closed.get(key[0], closures.never)):
            return False
    return True


def _spelled(key: tuple[str, str, str | None]) -> str:
    instrument, kind, resolution = key
    return f"{instrument} {kind}" + (f" {resolution}" if resolution else "")
