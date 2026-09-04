"""`kanso data instruments`: resolution into the catalog, and what a refresh is refused for."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import FIRST, INSTRUMENT, at, payload, write_instruments


def test_resolve_writes_the_definition_into_the_catalog(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)

    result = at(
        runner, workspace, "data", "instruments", "resolve", "--as-of", str(FIRST), "--json"
    )

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["as_of"] == str(FIRST)
    assert document["refresh"] is False
    [instrument] = document["instruments"]
    assert instrument["id"] == INSTRUMENT
    assert len(instrument["checksum"]) == 64
    held = payload(at(runner, workspace, "data", "instruments", "show", "--json"))
    assert [item["id"] for item in held["instruments"]] == [INSTRUMENT]


def test_resolve_with_no_ids_takes_the_cache_s_own(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)

    document = payload(at(runner, workspace, "data", "instruments", "resolve", "--json"))

    assert [item["id"] for item in document["instruments"]] == [INSTRUMENT]


def test_resolve_names_the_ids_it_was_given(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)

    result = at(runner, workspace, "data", "instruments", "resolve", INSTRUMENT)

    assert result.exit_code == Exit.OK
    assert INSTRUMENT in result.stdout


def test_an_unknown_id_names_itself_and_the_reason(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)

    result = at(runner, workspace, "data", "instruments", "resolve", "NOPE.SIM", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "NOPE.SIM" in payload(result)["error"]


def test_a_workspace_naming_no_instrument_has_nothing_to_resolve(
    runner: CliRunner, workspace: Path
) -> None:
    (workspace / "instruments.yaml").write_text("{}\n", encoding="utf-8")

    result = at(runner, workspace, "data", "instruments", "resolve", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "instruments.yaml" in payload(result)["remedy"]


def test_a_malformed_as_of_is_a_validation_failure(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)

    result = at(runner, workspace, "data", "instruments", "resolve", "--as-of", "soon", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "--as-of" in payload(result)["error"]


def test_refresh_resolves_again_rather_than_from_the_cache(
    runner: CliRunner, workspace: Path
) -> None:
    """Both calls name the same date, because the claim is about the cache.

    A definition is stamped with the date it was resolved as of, so two unpinned calls
    either side of UTC midnight legitimately differ — which is a fact about the calendar
    rather than about the cache this test is here to check.
    """
    write_instruments(workspace)
    resolve = ("data", "instruments", "resolve", "--as-of", str(FIRST), "--json")
    first = payload(at(runner, workspace, *resolve))

    second = payload(at(runner, workspace, *resolve, "--refresh"))

    assert second["refresh"] is True
    assert second["instruments"] == first["instruments"]


def test_refresh_is_refused_while_a_run_is_active(runner: CliRunner, registered: Path) -> None:
    assert at(runner, registered, "research", "begin", "demo_mr").exit_code == Exit.OK

    result = at(runner, registered, "data", "instruments", "resolve", "--refresh", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "active run" in payload(result)["error"]
    assert at(runner, registered, "data", "instruments", "resolve", "--json").exit_code == Exit.OK


def test_refresh_is_refused_while_a_deployed_version_depends_on_the_snapshot(
    runner: CliRunner, loaded: Path
) -> None:
    """A deployed version's snapshot pins these definitions, and a refresh would move them."""
    from kanso.state import StateStore

    snapshot = payload(at(runner, loaded, "data", "snapshot", "--json"))
    with StateStore(loaded / "state.db") as store:
        store.connection.execute(
            "INSERT INTO strategies (strategy_id, created_at) VALUES ('demo', '2024-01-01')"
        )
        store.connection.execute(
            "INSERT INTO strategy_versions (strategy_id, version, state, stage, pins, created_at)"
            " VALUES ('demo', 1, 'paper', 'paper', ?, '2024-01-01')",
            (json.dumps({"snapshot_id": snapshot["snapshot_id"]}),),
        )

    result = at(runner, loaded, "data", "instruments", "resolve", "--refresh", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "deployed to paper" in payload(result)["error"]


def test_a_deployed_version_pinned_to_nothing_blocks_no_refresh(
    runner: CliRunner, loaded: Path
) -> None:
    from kanso.state import StateStore

    with StateStore(loaded / "state.db") as store:
        store.connection.execute(
            "INSERT INTO strategies (strategy_id, created_at) VALUES ('demo', '2024-01-01')"
        )
        store.connection.execute(
            "INSERT INTO strategy_versions (strategy_id, version, state, stage, pins, created_at)"
            " VALUES ('demo', 1, 'paper', 'paper', '{}', '2024-01-01')"
        )

    result = at(runner, loaded, "data", "instruments", "resolve", "--refresh", "--json")

    assert result.exit_code == Exit.OK


def test_show_one_instrument_prints_its_canonical_fields(
    runner: CliRunner, workspace: Path
) -> None:
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK

    result = at(runner, workspace, "data", "instruments", "show", INSTRUMENT)

    assert result.exit_code == Exit.OK
    assert "price_increment" in result.stdout
    document = payload(at(runner, workspace, "data", "instruments", "show", INSTRUMENT, "--json"))
    [instrument] = document["instruments"]
    assert instrument["id"] == INSTRUMENT
    assert instrument["definition"]["type"] == "Equity"
    assert instrument["definition"]["id"] == INSTRUMENT


def test_show_accepts_the_bare_symbol(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK

    result = at(runner, workspace, "data", "instruments", "show", "DEMO", "--json")

    assert result.exit_code == Exit.OK
    assert [item["id"] for item in payload(result)["instruments"]] == [INSTRUMENT]


def test_show_of_an_instrument_the_catalog_lacks_is_a_precondition_failure(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "data", "instruments", "show", "NOPE.SIM", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "resolve" in payload(result)["remedy"]


def test_a_catalog_holding_nothing_lists_nothing(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "data", "instruments", "show")

    assert result.exit_code == Exit.OK
    assert "none resolved" in result.stdout


def test_resolution_is_recorded_in_the_event_log(runner: CliRunner, workspace: Path) -> None:
    from kanso.state import StateStore

    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK

    with StateStore(workspace / "state.db") as store:
        assert store.events(kind="instruments_resolved")
