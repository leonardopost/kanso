"""Every migration after the previous release applies over a workspace that release wrote.

`tests/state/fixtures/state_0_7_0.sql` is a `state.db` the 0.7.0 demo built, with rows in
hypotheses, runs, cards and events, stamped with the newest migration 0.7.0 shipped. A
fresh database proves a migration's SQL parses; only a populated earlier one proves it
runs over rows written under the old shape, which is what a release has to know.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from kanso.state import SCHEMA_VERSION, TABLES, StateStore, migrations, usable

from .conftest import PREVIOUS_RELEASE, PREVIOUS_RELEASE_VERSION

POPULATED = ("hypotheses", "runs", "cards", "events")
"""The tables the fixture must hold rows in, or it proves nothing a fresh file does not."""


def counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in POPULATED
        }
    finally:
        conn.close()


def test_the_fixture_is_a_populated_workspace_the_previous_release_wrote(
    previous_release_db: Path,
) -> None:
    assert "kanso 0.7.0" in PREVIOUS_RELEASE.read_text(encoding="utf-8").splitlines()[0]
    conn = sqlite3.connect(previous_release_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PREVIOUS_RELEASE_VERSION
        criteria = conn.execute("SELECT criteria_version FROM runs").fetchall()
        assert criteria and all(str(row[0]).startswith("0.7.0") for row in criteria)
    finally:
        conn.close()
    assert all(count > 0 for count in counts(previous_release_db).values())


def test_every_later_migration_applies_over_the_previous_release_and_keeps_its_rows(
    previous_release_db: Path,
) -> None:
    before = counts(previous_release_db)
    later = [m.name for m in migrations() if m.version > PREVIOUS_RELEASE_VERSION]

    with StateStore(previous_release_db) as store:
        assert store.schema_version() == PREVIOUS_RELEASE_VERSION
        assert store.pending() == later
        assert store.migrate() == later
        assert store.schema_version() == SCHEMA_VERSION
        assert store.pending() == []
        assert store.tables() == sorted(TABLES)
        usable(store, previous_release_db)
        # The migrated database is one this package writes to, and its history is intact.
        events = len(store.events())
        store.event("migrated_over", "state_0_7_0")
        assert len(store.events()) == events + 1

    after = counts(previous_release_db)
    assert after == {**before, "events": before["events"] + 1}
