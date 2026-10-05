"""The data a screen needs: what the catalog holds, what an adapter can fetch, and the fetch.

**Missing data is fetched or built, never skipped.** Every leg is in one of three states:

* **held** — the catalog serves the leg's series on every day of the window its market opened;
* **fetchable** — it does not, and a registered adapter declares that it serves the leg's
  instrument at the leg's type: the plan names the adapter and its loader, and `fetch`
  backfills the missing days through that loader, exactly as `kanso data backfill` would;
* **unserved** — no registered adapter serves it, and the remedy is to build one: a data
  adapter written as a workspace extension against the protocol `docs/extensions.md`
  states, driven against the live source, then moved upstream.

**What an adapter declares, and nothing it is asked.** Three optional members of the
adapter protocol carry a screen's questions, so nothing here names a vendor: `serves`, the
leg types it can fetch for an instrument definition; `spec_for`, the loader and the spec
document that fetch is made with; and `timestamps`, what its points' `ts_init` is — an
exchange's own instant, a consolidated tape's, a vendor's receipt. An adapter that declares
none of them serves nothing here and is otherwise unchanged.

**A leg's instrument is defined before it is fetched.** A definition the store does not
hold is resolved the way `kanso data instruments resolve` resolves it — the manual entry, the
cache, then the workspace's reference adapter — asked without recording in `plan`, and
recorded in `fetch`. One no path resolves leaves the leg unserved, named.

**The fetch writes what a backfill writes.** One spec per series under
`screens/<id>/specs/`, then the backfill: chunked, each chunk's manifest its checkpoint,
empty answers recorded, successor datasets only — so an interrupted fetch resumes, a repeat
fetches nothing, and nothing a snapshot pins is edited. A snapshot is taken after any fetch
that wrote, so a run can be pinned to what it fetched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import yaml

from kanso import ext
from kanso.data import closures, commands, registry
from kanso.data.instruments import current_definitions, resolve_universe
from kanso.data.manifest import Manifest, contains, manifests
from kanso.errors import KansoError
from kanso.schemas.screen import Leg, Screen

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

State = Literal["held", "fetchable", "unserved"]

SPECS: Final = "specs"
"""The directory under a screen's own where the specs its fetches were made with are kept."""

UNKNOWN: Final = "unknown"
"""What a leg's timestamps are when nothing declared them: a file, or a generator."""


@dataclass(frozen=True)
class LegPlan:
    """One series a screen reads, and what getting it takes."""

    instrument: str
    type: str
    resolution: str | None
    legs: tuple[str, ...]
    state: State
    missing: tuple[tuple[date, date], ...] = ()
    adapter: str | None = None
    loader: str | None = None
    configured: bool | None = None
    defined: bool = True
    timestamps: str = UNKNOWN
    reason: str | None = None
    spec: dict[str, object] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str | None]:
        """The series: instrument, type and resolution, as the store files it."""
        return (self.instrument, self.type, self.resolution)

    def payload(self) -> dict[str, object]:
        return {
            "legs": list(self.legs),
            "instrument": self.instrument,
            "type": self.type,
            "resolution": self.resolution,
            "state": self.state,
            "missing": [[str(start), str(end)] for start, end in self.missing],
            "adapter": self.adapter,
            "loader": self.loader,
            "configured": self.configured,
            "defined": self.defined,
            "timestamps": self.timestamps,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Fetched:
    """What one fetch did for one series."""

    plan: LegPlan
    spec: Path
    requests: int
    rows: int

    def payload(self) -> dict[str, object]:
        return {
            "instrument": self.plan.instrument,
            "type": self.plan.type,
            "resolution": self.plan.resolution,
            "loader": self.plan.loader,
            "spec": str(self.spec),
            "requests": self.requests,
            "rows": self.rows,
        }


def plan(
    ws: Workspace, store: StateStore, screen: Screen, window: tuple[date, date]
) -> tuple[LegPlan, ...]:
    """Every series the screen reads, held, fetchable or unserved over `window`."""
    known = registry.adapters(ext.discover(ws.root, ws.config.extensions_paths))
    held = {
        (item.instrument, item.type, item.resolution): item for item in commands.series(ws, store)
    }
    defined = current_definitions(ws)
    datasets = manifests(ws)
    plans: list[LegPlan] = []
    for key, legs in _series(screen).items():
        instrument, kind, resolution = key
        definition = defined.get(instrument)
        reason = None
        if definition is None:
            definition, reason = _resolvable(ws, instrument, window[0])
        series = held.get(key)
        spans = [] if series is None else series.spans
        closed = closures.never if series is None else series.closed
        stamps = _held_timestamps(ws, known, datasets, key)
        if series is not None and contains(spans, window, closed):
            plans.append(
                LegPlan(*key, legs, "held", defined=instrument in defined, timestamps=stamps)
            )
            continue
        missing = tuple(commands.missing(spans, window, closed))
        server = None if definition is None else _server(ws, known, definition, kind, resolution)
        if server is None:
            plans.append(
                LegPlan(
                    *key,
                    legs,
                    "unserved",
                    missing=missing,
                    defined=instrument in defined,
                    timestamps=stamps,
                    reason=reason or _unserved(instrument, kind, resolution),
                )
            )
            continue
        adapter_id, adapter = server
        loader_id, spec = adapter.spec_for(ws, definition, kind, resolution, *window)
        plans.append(
            LegPlan(
                *key,
                legs,
                "fetchable",
                missing=missing,
                adapter=adapter_id,
                loader=loader_id,
                configured=bool(adapter.configured(ws)),
                defined=instrument in defined,
                timestamps=str(getattr(adapter, "timestamps", UNKNOWN)),
                spec=dict(spec),
            )
        )
    return tuple(plans)


def fetch(
    ws: Workspace,
    store: StateStore,
    screen: Screen,
    window: tuple[date, date],
    plans: Sequence[LegPlan],
) -> tuple[Fetched, ...]:
    """Resolve and backfill every fetchable series, then snapshot what was written."""
    wanted = [item for item in plans if item.state == "fetchable"]
    if not wanted:
        return ()
    undefined = sorted({item.instrument for item in wanted if not item.defined})
    if undefined:
        commands.resolve(ws, store, undefined, as_of=window[0])
    directory = ws.path("screens", screen.id, SPECS)
    directory.mkdir(parents=True, exist_ok=True)
    done: list[Fetched] = []
    for item in wanted:
        assert item.loader is not None
        path = directory / _spec_name(item)
        path.write_text(yaml.safe_dump(item.spec, sort_keys=False), encoding="utf-8")
        filled = commands.backfill(ws, store, item.loader, path, start=window[0], end=window[1])
        done.append(Fetched(item, path, filled.requests, filled.rows))
    if any(item.rows for item in done) or undefined:
        commands.freeze(ws, store)
    return tuple(done)


def _series(screen: Screen) -> dict[tuple[str, str, str | None], tuple[str, ...]]:
    """Each distinct series the screen reads, with the legs that read it."""
    found: dict[tuple[str, str, str | None], list[str]] = {}
    for name, leg in screen.legs.items():
        found.setdefault(_key(leg), []).append(name)
    return {key: tuple(names) for key, names in found.items()}


def _key(leg: Leg) -> tuple[str, str, str | None]:
    return (leg.instrument, leg.type, leg.resolution)


def _resolvable(ws: Workspace, instrument: str, as_of: date) -> tuple[Any, str | None]:
    """The definition the standard path would resolve, asked without recording anything."""
    try:
        return resolve_universe(ws, [instrument], as_of, record=False)[instrument], None
    except KansoError as error:
        return None, f"no definition of {instrument} resolves: {error.message}"


def _server(
    ws: Workspace,
    known: Mapping[str, registry.Adapter],
    definition: Any,
    kind: str,
    resolution: str | None,
) -> tuple[str, Any] | None:
    """The first adapter, by id, that declares it serves this series."""
    for adapter_id, adapter in sorted(known.items()):
        serves = getattr(adapter, "serves", None)
        if serves is None or getattr(adapter, "spec_for", None) is None:
            continue
        if kind in serves(ws, definition, resolution):
            return adapter_id, adapter
    return None


def _held_timestamps(
    ws: Workspace,
    known: Mapping[str, registry.Adapter],
    datasets: Mapping[str, Manifest],
    key: tuple[str, str, str | None],
) -> str:
    """What the held datasets' `ts_init` is, by the adapter whose loader wrote them."""
    owners: dict[str, str] = {}
    for adapter in known.values():
        stamps = str(getattr(adapter, "timestamps", UNKNOWN))
        for loader_id in adapter.loaders(ws):
            owners.setdefault(loader_id, stamps)
    kinds = {
        owners.get(manifest.source, UNKNOWN)
        for manifest in datasets.values()
        if manifest.filed_under == key
    }
    if not kinds:
        return UNKNOWN
    return kinds.pop() if len(kinds) == 1 else "mixed"


def _unserved(instrument: str, kind: str, resolution: str | None) -> str:
    grain = f" {resolution}" if resolution else ""
    return (
        f"no registered adapter serves {instrument} as {kind}{grain}; build one as a workspace "
        "extension (docs/extensions.md, a data adapter), then run the screen again"
    )


def _spec_name(item: LegPlan) -> str:
    grain = f"-{item.resolution}" if item.resolution else ""
    return f"{item.instrument}-{item.type}{grain}.yaml".replace("/", "_")
