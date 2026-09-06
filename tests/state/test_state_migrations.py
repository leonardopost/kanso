"""Migrations, the stamped version and the connection settings the store guarantees."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kanso.errors import KansoError, PreconditionError
from kanso.state import BUSY_TIMEOUT_MS, SCHEMA_VERSION, TABLES, StateStore, migrations, usable
from kanso.state import store as store_module


def test_shipped_migrations_are_ordered_and_well_named() -> None:
    shipped = migrations()
    assert shipped, "the package ships at least one migration"
    versions = [m.version for m in shipped]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[0] == 1
    assert versions[-1] == SCHEMA_VERSION
    for migration in shipped:
        assert migration.name.startswith(f"{migration.version:04d}_")
        assert migration.name.endswith(".sql")
        assert migration.sql.strip().endswith(";")


def test_fresh_database_is_version_zero_with_everything_pending(db_path: Path) -> None:
    with StateStore(db_path) as store:
        assert store.schema_version() == 0
        assert store.pending() == [m.name for m in migrations()]
        assert store.tables() == []


def test_migrate_applies_everything_and_stamps_user_version(db_path: Path) -> None:
    with StateStore(db_path) as store:
        applied = store.migrate()
        assert applied == [m.name for m in migrations()]
        assert store.schema_version() == SCHEMA_VERSION
        assert store.pending() == []

    # PRAGMA user_version is authoritative: a plain sqlite3 connection reads the same number.
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()


def test_migrate_is_idempotent(store: StateStore) -> None:
    assert store.migrate() == []
    assert store.migrate() == []
    assert store.schema_version() == SCHEMA_VERSION


def test_every_named_table_exists(store: StateStore) -> None:
    assert store.tables() == sorted(TABLES)


def test_wal_and_busy_timeout_are_set(store: StateStore) -> None:
    journal = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(journal).lower() == "wal"
    assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5_000
    assert BUSY_TIMEOUT_MS >= 5_000
    assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_survives_reopen_and_a_second_store_sees_the_schema(db_path: Path) -> None:
    with StateStore(db_path) as first:
        first.migrate()
    with StateStore(db_path) as second:
        assert second.pending() == []
        assert second.schema_version() == SCHEMA_VERSION
        journal = second.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal).lower() == "wal"


def test_a_failed_migration_leaves_the_version_untouched(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = (store_module.Migration(9999, "9999_broken.sql", "SELECT nonexistent_fn();"),)
    monkeypatch.setattr(store_module, "migrations", lambda: broken)
    with StateStore(db_path) as store:
        with pytest.raises(KansoError, match="9999_broken.sql"):
            store.migrate()
        assert store.schema_version() == 0
        assert store.connection.in_transaction is False


def test_a_write_rolls_back_on_failure(store: StateStore) -> None:
    before = len(store.events())
    with pytest.raises(sqlite3.Error), store._write() as conn:
        conn.execute("INSERT INTO events (ts, kind, subject) VALUES ('t', 'k', 's')")
        conn.execute("INSERT INTO no_such_table (x) VALUES (1)")
    assert store.connection.in_transaction is False
    assert len(store.events()) == before


def test_a_closed_store_refuses_every_call(db_path: Path) -> None:
    store = StateStore(db_path)
    store.migrate()
    store.close()
    store.close()  # idempotent
    with pytest.raises(PreconditionError, match="closed"):
        store.schema_version()


def test_a_store_refuses_to_cross_a_process_boundary(store: StateStore) -> None:
    store._pid += 1
    with pytest.raises(PreconditionError, match="opened by process"):
        store.event("nope", "nope")


class _FakeFile:
    def __init__(self, name: str, text: str = "SELECT 1;") -> None:
        self.name = name
        self._text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text


class _FakeDir:
    def __init__(self, children: list[_FakeFile]) -> None:
        self._children = children

    def __truediv__(self, other: str) -> _FakeDir:
        return self

    def iterdir(self) -> list[_FakeFile]:
        return self._children


@pytest.fixture
def _uncached_migrations() -> Iterator[None]:
    migrations.cache_clear()
    yield
    migrations.cache_clear()


def _patch_files(monkeypatch: pytest.MonkeyPatch, children: list[_FakeFile]) -> None:
    def fake_files(_package: str) -> Any:
        return _FakeDir(children)

    monkeypatch.setattr(store_module.resources, "files", fake_files)


@pytest.mark.usefixtures("_uncached_migrations")
def test_a_misnamed_migration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_files(monkeypatch, [_FakeFile("init.sql"), _FakeFile("README.md")])
    with pytest.raises(KansoError, match="NNNN_name.sql"):
        migrations()


@pytest.mark.usefixtures("_uncached_migrations")
def test_two_migrations_sharing_a_version_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_files(monkeypatch, [_FakeFile("0001_a.sql"), _FakeFile("0001_b.sql")])
    with pytest.raises(KansoError, match="share version 1"):
        migrations()


@pytest.mark.usefixtures("_uncached_migrations")
def test_non_sql_files_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_files(
        monkeypatch, [_FakeFile("0002_b.sql"), _FakeFile("notes.txt"), _FakeFile("0001_a.sql")]
    )
    assert [m.name for m in migrations()] == ["0001_a.sql", "0002_b.sql"]


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def fetchone(self) -> tuple[object]:
        return (self._value,)


class _ScriptedConn:
    """A connection stub whose journal-mode pragmas follow a scripted sequence."""

    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = outcomes

    def execute(self, _sql: str) -> _Result:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Result(outcome)


def test_enabling_wal_retries_while_another_process_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(store_module.time, "sleep", slept.append)
    conn = _ScriptedConn(
        ["delete", sqlite3.OperationalError("database is locked"), "delete", "WAL"]
    )
    assert store_module._enable_wal(conn, 5.0) == "wal"  # type: ignore[arg-type]
    assert slept == [0.01]


def test_enabling_wal_gives_up_at_the_timeout() -> None:
    conn = _ScriptedConn(["delete", "delete"])
    assert store_module._enable_wal(conn, 0.0) == "delete"  # type: ignore[arg-type]


def test_a_database_that_cannot_run_in_wal_is_refused(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "_enable_wal", lambda _conn, _timeout: "delete")
    with pytest.raises(PreconditionError, match="WAL"):
        StateStore(db_path)


def test_a_migration_another_process_applied_first_is_not_reported_as_ours(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loser of a concurrent migrate: its own script fails because the winner already
    # created the tables, and by then the stamped version already covers the migration.
    contended = (store_module.Migration(2, "0002_contended.sql", "SELECT nonexistent_fn();"),)
    monkeypatch.setattr(store_module, "migrations", lambda: contended)
    observed = iter([0, 2])
    monkeypatch.setattr(StateStore, "schema_version", lambda _self: next(observed))
    with StateStore(db_path) as store:
        assert store.migrate() == []
        assert store.connection.in_transaction is False


# --- a database this package cannot correctly write, in either direction ------


def test_a_migrated_database_is_neither_behind_nor_ahead(db_path: Path) -> None:
    with StateStore(db_path) as store:
        store.migrate()

        assert store.pending() == []
        assert store.ahead_by() == 0
        usable(store, db_path)


def test_a_database_behind_the_package_is_refused_with_the_migration_to_run(
    db_path: Path,
) -> None:
    with StateStore(db_path) as store:
        with pytest.raises(PreconditionError, match="behind this kanso") as caught:
            usable(store, db_path)

        assert "kanso migrate" in (caught.value.remedy or "")


def test_a_database_ahead_of_the_package_is_refused_by_how_far(db_path: Path) -> None:
    """`pending` only looks upward, so a downgrade would otherwise read as up to date."""
    with StateStore(db_path) as store:
        store.migrate()
        newest = max(m.version for m in migrations())
        store.connection.execute(f"PRAGMA user_version = {newest + 3}")

        assert store.pending() == []
        assert store.ahead_by() == 3
        with pytest.raises(PreconditionError, match="written by a later kanso"):
            usable(store, db_path)


def test_the_daemon_entry_point_refuses_before_it_takes_the_lock(
    db_path: Path, tmp_path: Path
) -> None:
    """A service unit starts `serve`, and the supervisor never opens the store itself.

    Checking only where a store is opened for work would let a unit come up and restart
    lanes that each refuse, so the refusal has to happen here, before anything is spawned.
    """
    from kanso.research.daemon import main
    from kanso.workspace import init

    root = tmp_path / "ws"
    init(root)
    with StateStore(root / "state.db") as store:
        store.migrate()
        newest = max(m.version for m in migrations())
        store.connection.execute(f"PRAGMA user_version = {newest + 1}")

    with pytest.raises(PreconditionError, match="written by a later kanso"):
        main(["serve", str(root)])
