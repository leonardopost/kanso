"""Where a composed strategy lives, and the rows that index it.

A strategy is a directory: `strategies/<id>/strategy.yaml` lists every version in order,
and `strategies/<id>/impl/<version>/` holds the sources that version runs. The file is the
record an operator reads and an agent classifies against; the state rows are the index the
portfolio and the monitor query, and they carry the same facts. Nothing here decides what a
version is — that is the construct's — and nothing here deploys one, which is the
portfolio's: this module is the filing cabinet both of them open.

The id of a strategy is the id of the hypothesis whose sleeve it was built from, so a
construct classified onto a host names that host by an id an operator already knows.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from kanso.errors import PreconditionError
from kanso.schemas import StrategyFile, StrategyVersion, load_yaml, write_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "IMPL",
    "STRATEGIES",
    "STRATEGY_FILE",
    "appended",
    "impl_dir",
    "read",
    "record",
    "require",
    "strategies",
    "strategy_dir",
    "strategy_file",
    "write",
]

STRATEGIES: Final = "strategies"
"""The workspace directory every composed strategy lives under."""

STRATEGY_FILE: Final = "strategy.yaml"
IMPL: Final = "impl"
"""One subdirectory per version, holding the sources that version runs."""

_VERSION_COLUMNS: Final = (
    "strategy_id",
    "version",
    "state",
    "sleeve",
    "attached",
    "config",
    "pins",
    "expectation",
    "created_at",
)

_INSERT_VERSION: Final = (
    f"INSERT INTO strategy_versions ({', '.join(_VERSION_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_VERSION_COLUMNS))})"
)


def strategy_dir(ws: Workspace, strategy_id: str) -> Path:
    """The directory one strategy owns."""
    return ws.path(STRATEGIES, strategy_id)


def strategy_file(ws: Workspace, strategy_id: str) -> Path:
    """Where one strategy's versions are listed."""
    return strategy_dir(ws, strategy_id) / STRATEGY_FILE


def impl_dir(ws: Workspace, strategy_id: str, version: int) -> Path:
    """Where one version's generated implementation lives."""
    return strategy_dir(ws, strategy_id) / IMPL / str(version)


def read(ws: Workspace, strategy_id: str) -> StrategyFile | None:
    """One strategy's file, or `None` when this workspace has not composed it."""
    path = strategy_file(ws, strategy_id)
    return load_yaml(StrategyFile, path) if path.is_file() else None


def require(ws: Workspace, strategy_id: str) -> StrategyFile:
    """One strategy's file, refusing an id this workspace has never composed."""
    found = read(ws, strategy_id)
    if found is None:
        raise PreconditionError(
            f"{strategy_id!r} is not a composed strategy of this workspace",
            remedy="certify a sleeve hypothesis; composition writes the strategy",
        )
    return found


def strategies(ws: Workspace) -> list[StrategyFile]:
    """Every composed strategy of this workspace, in id order."""
    root = ws.path(STRATEGIES)
    if not root.is_dir():
        return []
    paths = [directory / STRATEGY_FILE for directory in sorted(root.iterdir())]
    return [load_yaml(StrategyFile, path) for path in paths if path.is_file()]


def appended(held: StrategyFile | None, strategy_id: str, version: StrategyVersion) -> StrategyFile:
    """The strategy file this version makes: a new one, or the held one plus it."""
    if held is None:
        return StrategyFile(id=strategy_id, versions=[version])
    return StrategyFile(id=held.id, versions=[*held.versions, version])


def write(ws: Workspace, strategy: StrategyFile) -> Path:
    """Write a strategy's file, creating its directory."""
    path = strategy_file(ws, strategy.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_yaml(strategy, path)


def record(store: StateStore, strategy_id: str, version: StrategyVersion) -> None:
    """Index one composed version: the strategy row, then the version's own.

    The row carries no stage, no capital and no join time: composition makes a version,
    and putting it on a stage is deployment's act and deployment's columns.
    """
    store.connection.execute(
        "INSERT INTO strategies (strategy_id, created_at) VALUES (?, ?)"
        " ON CONFLICT (strategy_id) DO NOTHING",
        (strategy_id, version.created_at.isoformat()),
    )
    store.connection.execute(
        _INSERT_VERSION,
        (
            strategy_id,
            version.version,
            version.state,
            _dump(version.sleeve.model_dump(mode="json")),
            _dump([ref.model_dump(mode="json", by_alias=True) for ref in version.attached]),
            _dump(version.config),
            _dump(version.pins.model_dump(mode="json")),
            _dump(version.expectation.model_dump(mode="json")),
            version.created_at.isoformat(),
        ),
    )


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True)
