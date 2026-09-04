from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kanso.state import StateStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[StateStore]:
    with StateStore(db_path) as opened:
        opened.migrate()
        yield opened
