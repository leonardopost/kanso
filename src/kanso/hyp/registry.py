"""The hypothesis registry: what is registered, under which bytes, and in what state.

A hypothesis file is the operator's; the registry is kanso's record of it. Registering
stores the file's bytes as a blob and pins the hypothesis by their sha256, and records
the lifecycle status and the scope the pin was taken under. Nothing about status, pins or
the hypothesis's best card is ever written back into the file, which is what keeps the
file's bytes stable enough to be a content address.

Registering an id that is already registered re-pins it. The status survives when the
file still carries a classification, because re-pinning an edited but still classified
hypothesis is an edit, not a reclassification; a file whose classification has been
cleared returns to `draft`, since there is no longer a construct to research it as.
Either way it is refused outright while a run is active: the run is pinned to the bytes
that were registered when it began, and moving the pin under it would make the lane's
copy disagree with the run without anything having changed in the lane.

The scope a `best` is comparable under is the universe, the resolution and the data
requirements. A card's metric means nothing across a change to any of the three, so a
re-pin that changes one clears the hypothesis's `best` and says so in the event log.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from kanso.errors import PreconditionError
from kanso.hyp.scaffold import hypothesis_file
from kanso.hyp.validate import read_source, validate
from kanso.schemas import Hypothesis

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

Status = Literal[
    "draft", "classified", "researching", "candidate", "certified", "failed", "retired"
]
"""The lifecycle a hypothesis moves through; the state store checks the same set."""

DRAFT: Final[Status] = "draft"
CLASSIFIED: Final[Status] = "classified"
RETIRED: Final[Status] = "retired"

SCOPE: Final = ("universe", "resolution", "data_requirements")
"""What a `best` is comparable under; a change to any of them clears it."""

REGISTERED: Final = "registered"
REPINNED: Final = "repinned"
BEST_CLEARED: Final = "best_cleared"
"""The event kinds this module appends, all under the hypothesis id as subject."""

_COLUMNS: Final = (
    "hyp_id",
    "status",
    "hypothesis_sha",
    "pins",
    "construct_id",
    "objective_id",
    "best_sha",
    "best_metric",
    "best_run_id",
    "consecutive_cert_failures",
    "created_at",
    "updated_at",
)

_UPSERT: Final = f"""
INSERT INTO hypotheses ({", ".join(_COLUMNS)})
VALUES ({", ".join("?" * len(_COLUMNS))})
ON CONFLICT (hyp_id) DO UPDATE SET
    status = excluded.status,
    hypothesis_sha = excluded.hypothesis_sha,
    pins = excluded.pins,
    construct_id = excluded.construct_id,
    objective_id = excluded.objective_id,
    best_sha = excluded.best_sha,
    best_metric = excluded.best_metric,
    best_run_id = excluded.best_run_id,
    updated_at = excluded.updated_at
"""
"""One statement, so a registration is applied whole or not at all."""


@dataclass(frozen=True)
class Registration:
    """One row of the registry, together with what the workspace file says now."""

    hyp_id: str
    status: str
    hypothesis_sha: str | None
    pins: dict[str, Any]
    construct: str | None
    objective: str | None
    best_sha: str | None
    best_metric: float | None
    best_run_id: str | None
    cert_failures: int
    created_at: str
    updated_at: str
    path: str
    file_sha: str | None
    active_run: str | None

    @property
    def pinned(self) -> bool:
        """Whether the workspace file still is the bytes this registration pinned."""
        return self.file_sha is not None and self.file_sha == self.hypothesis_sha

    def payload(self) -> dict[str, Any]:
        """The record as one JSON object."""
        return {
            "id": self.hyp_id,
            "status": self.status,
            "hypothesis_sha": self.hypothesis_sha,
            "pins": self.pins,
            "construct": self.construct,
            "objective": self.objective,
            "best_sha": self.best_sha,
            "best_metric": self.best_metric,
            "best_run_id": self.best_run_id,
            "cert_failures": self.cert_failures,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": self.path,
            "file_sha": self.file_sha,
            "pinned": self.pinned,
            "active_run": self.active_run,
        }


def add(ws: Workspace, store: StateStore, path: Path) -> Hypothesis:
    """Validate the file at `path` and register or re-pin it.

    Raises a validation failure when the file is not admissible and a precondition
    failure when a run of that hypothesis is active.
    """
    source = read_source(path)
    hyp = validate(ws, path, source)
    pin(store, hyp, source)
    return hyp


def pin(store: StateStore, hyp: Hypothesis, source: bytes) -> str:
    """Record `hyp` under the sha256 of `source` and return that sha.

    A first registration is `classified` when the file already carries a valid construct,
    objective and constraints, and `draft` otherwise: an operator who already knows what a
    thesis is needs no model to say so, which is the same override that is open to them
    after a classification. A re-pin keeps the status while the file is still classified
    and returns it to `draft` otherwise, and clears the hypothesis's `best` when the
    universe, the resolution or the data requirements changed.
    """
    refuse_active_run(store, hyp.id, "re-pin")
    held = _row(store, hyp.id)
    sha = store.put_blob(source)
    scope = _scope(hyp)
    cleared = held is not None and _scope_of(held) != scope
    status: Status
    if hyp.construct is None:
        status = DRAFT
    elif held is None:
        status = CLASSIFIED
    else:
        status = cast("Status", held["status"])
    now = _now()
    values = (
        hyp.id,
        status,
        sha,
        json.dumps({"hypothesis_sha": sha, **scope}, sort_keys=True),
        hyp.construct.id if hyp.construct else None,
        hyp.objective.id if hyp.objective else None,
        None if cleared or held is None else held["best_sha"],
        None if cleared or held is None else held["best_metric"],
        None if cleared or held is None else held["best_run_id"],
        0,
        now if held is None else str(held["created_at"]),
        now,
    )
    store.connection.execute(_UPSERT, values)
    if cleared:
        store.event(BEST_CLEARED, hyp.id, {"reason": "scope changed", "scope": scope})
    store.event(
        REGISTERED if held is None else REPINNED,
        hyp.id,
        {"hypothesis_sha": sha, "status": status},
    )
    return sha


def show(
    ws: Workspace, store: StateStore, hyp_id: str | None = None
) -> Registration | list[Registration]:
    """One registration, or every one of them in id order when no id is given."""
    if hyp_id is None:
        return [_registration(ws, store, held) for held in _rows(store)]
    held = _row(store, hyp_id)
    if held is None:
        raise PreconditionError(
            f"{hyp_id!r} is not a registered hypothesis",
            remedy="run `kanso hyp add hypotheses/<id>/hypothesis.yaml` to register it",
        )
    return _registration(ws, store, held)


def retire(ws: Workspace, store: StateStore, hyp_id: str) -> None:
    """Retire a hypothesis. `ws` names the workspace whose registry this is."""
    if _row(store, hyp_id) is None:
        raise PreconditionError(f"{hyp_id!r} is not a registered hypothesis")
    refuse_active_run(store, hyp_id, "retire")
    set_status(store, hyp_id, RETIRED)


def set_status(store: StateStore, hyp_id: str, status: Status) -> None:
    """Move a registered hypothesis to `status` and record the move."""
    store.connection.execute(
        "UPDATE hypotheses SET status = ?, updated_at = ? WHERE hyp_id = ?",
        (status, _now(), hyp_id),
    )
    store.event(status, hyp_id, {})


def _row(store: StateStore, hyp_id: str) -> sqlite3.Row | None:
    """The registry row for an id, or `None` when the id is not registered."""
    found: sqlite3.Row | None = store.connection.execute(
        "SELECT * FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return found


def active_run(store: StateStore, hyp_id: str) -> str | None:
    """The id of this hypothesis's active run, when one is; a hypothesis has at most one."""
    found = store.connection.execute(
        "SELECT run_id FROM runs WHERE hyp_id = ? AND ended_at IS NULL", (hyp_id,)
    ).fetchone()
    return None if found is None else str(found["run_id"])


def refuse_active_run(store: StateStore, hyp_id: str, action: str) -> None:
    """Refuse an action that would move what an active run is pinned to."""
    run_id = active_run(store, hyp_id)
    if run_id is not None:
        raise PreconditionError(
            f"{hyp_id} has an active run ({run_id}), so it cannot {action}",
            remedy=f"end the run with `kanso research end {hyp_id}` first",
        )


def _rows(store: StateStore) -> list[sqlite3.Row]:
    return list(store.connection.execute("SELECT * FROM hypotheses ORDER BY hyp_id").fetchall())


def _registration(ws: Workspace, store: StateStore, held: sqlite3.Row) -> Registration:
    hyp_id = str(held["hyp_id"])
    path = hypothesis_file(ws, hyp_id)
    return Registration(
        hyp_id=hyp_id,
        status=str(held["status"]),
        hypothesis_sha=_optional(held["hypothesis_sha"]),
        pins=_pins(held),
        construct=_optional(held["construct_id"]),
        objective=_optional(held["objective_id"]),
        best_sha=_optional(held["best_sha"]),
        best_metric=None if held["best_metric"] is None else float(held["best_metric"]),
        best_run_id=_optional(held["best_run_id"]),
        cert_failures=int(held["consecutive_cert_failures"]),
        created_at=str(held["created_at"]),
        updated_at=str(held["updated_at"]),
        path=str(path.relative_to(ws.root)),
        file_sha=sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        active_run=active_run(store, hyp_id),
    )


def _pins(held: sqlite3.Row) -> dict[str, Any]:
    loaded: Any = json.loads(str(held["pins"]))
    return loaded if isinstance(loaded, dict) else {}


def _scope(hyp: Hypothesis) -> dict[str, Any]:
    """The three fields a metric is only comparable within, in a stable order."""
    return {
        "universe": sorted(hyp.universe),
        "resolution": hyp.resolution,
        "data_requirements": sorted(hyp.data_requirements),
    }


def _scope_of(held: sqlite3.Row) -> dict[str, Any]:
    pins = _pins(held)
    return {name: pins.get(name) for name in SCOPE}


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
