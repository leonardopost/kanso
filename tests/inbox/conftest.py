"""Fixtures for the inbox slice: a scaffolded workspace and its state store.

The workspace is scaffolded rather than faked, because `escalations/inbox.md` arrives
from the template with the operator's own header already in it and the append path has to
leave that header alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kanso.inbox import inbox_file
from kanso.state import StateStore
from kanso.workspace import Workspace, init


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return init(tmp_path / "ws")


@pytest.fixture
def store(ws: Workspace) -> Iterator[StateStore]:
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
        yield opened


def entry_lines(ws: Workspace) -> list[str]:
    """The entries in the file, without the template's prose around them."""
    text = inbox_file(ws).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.startswith("- [")]
