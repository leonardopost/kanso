from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from kanso.state import StateStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PREVIOUS_RELEASE = FIXTURES / "state_0_7_0.sql"
"""A workspace `state.db` the 0.7.0 demo wrote, as `sqlite3.Connection.iterdump()` emitted it.

The file's header records the command that printed it and why it is not a `.dump`.
"""
PREVIOUS_RELEASE_VERSION = 2
"""The schema version 0.7.0 stamped on it: the newest migration that release shipped."""


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[StateStore]:
    with StateStore(db_path) as opened:
        opened.migrate()
        yield opened


@pytest.fixture
def previous_release_db(tmp_path: Path) -> Path:
    """A `state.db` rebuilt from the previous release's dump, migrated by nothing yet."""
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(PREVIOUS_RELEASE.read_text(encoding="utf-8"))
    finally:
        conn.close()
    return path
