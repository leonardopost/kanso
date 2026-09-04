"""A scaffolded workspace and its state store: everything a monitor pass reads.

A pass touches no catalog and runs no engine, so the fixture is `kanso init` and a migrated
database. What a stage node would have written is written by `tests.monitor.builders`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kanso.state import StateStore
from kanso.workspace import Workspace, init


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    """A freshly scaffolded workspace, with the template portfolio and no deployments."""
    return init(tmp_path / "ws")


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    """A migrated state store for that workspace."""
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened
