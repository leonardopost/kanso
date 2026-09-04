"""`kanso migrate` applies what the database has not been given, and nothing twice."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.state import SCHEMA_VERSION, StateStore, migrations

from .conftest import at, payload


def test_init_leaves_nothing_pending(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "migrate", "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["applied"] == []
    assert (workspace / "state.db").is_file()


def test_migrate_applies_every_pending_migration(runner: CliRunner, workspace: Path) -> None:
    (workspace / "state.db").unlink()

    result = at(runner, workspace, "migrate", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["applied"] == [migration.name for migration in migrations()]
    assert document["schema_version"] == SCHEMA_VERSION
    assert (workspace / "state.db").is_file()


def test_migrate_is_idempotent(runner: CliRunner, workspace: Path) -> None:
    assert at(runner, workspace, "migrate").exit_code == Exit.OK

    result = at(runner, workspace, "migrate", "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["applied"] == []


def test_migrate_reports_what_it_applied(runner: CliRunner, workspace: Path) -> None:
    (workspace / "state.db").unlink()
    first = at(runner, workspace, "migrate")
    second = at(runner, workspace, "migrate")

    assert "0001_init.sql" in first.stdout
    assert "nothing pending" in second.stdout
    assert f"schema     {SCHEMA_VERSION}" in second.stdout


def test_the_migrated_database_carries_the_tables(runner: CliRunner, workspace: Path) -> None:
    assert at(runner, workspace, "migrate").exit_code == Exit.OK

    with StateStore(workspace / "state.db") as store:
        assert store.schema_version() == SCHEMA_VERSION
        assert store.pending() == []
        assert "cards" in store.tables()
