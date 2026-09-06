"""`kanso data instruments`: resolution into the catalog, and what a refresh is refused for."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml
from typer.testing import CliRunner

from kanso.data.catalog import day_start_ns
from kanso.errors import Exit

from .conftest import FIRST, HYP_ID, INSTRUMENT, at, payload, write_instruments, write_spec

RESOLVE = ("data", "instruments", "resolve", "--as-of", str(FIRST))


def correct(root: Path, **override: str) -> None:
    """Edit the operator's own `override` of the one instrument, as a correction is made."""
    path = root / "instruments.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document[INSTRUMENT]["override"].update(override)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def shown(runner: CliRunner, root: Path) -> dict[str, object]:
    """The one definition `show` renders for the instrument."""
    result = at(runner, root, "data", "instruments", "show", INSTRUMENT, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    [held] = payload(result)["instruments"]
    assert isinstance(held, dict)
    return held


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


def test_a_manual_universe_resolves_under_an_unconfigured_reference_adapter(
    runner: CliRunner, workspace: Path
) -> None:
    """Naming a vendor is not the same as needing one.

    Every entry here is `manual`, so the file answers the whole universe and nothing is
    left for a reference adapter to resolve. Building that adapter is what resolves its
    credential, so building it before anything is known to be unresolved makes a key the
    resolution never uses into a requirement of it — and this is the ordinary shape of a
    workspace, the demo's and every file-export workspace's included.
    """
    write_instruments(workspace)
    config = workspace / "kanso.toml"
    text = config.read_text(encoding="utf-8")
    table = "[data]\n"
    named = (
        text.replace(table, table + 'reference = "massive"\n', 1)
        if table in text
        else text + "\n" + table + 'reference = "massive"\n'
    )
    config.write_text(named, encoding="utf-8")

    result = at(runner, workspace, "data", "instruments", "resolve", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert [item["id"] for item in payload(result)["instruments"]] == [INSTRUMENT]


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


# --- the store as the registry of record --------------------------------------


def test_a_same_dated_refresh_replaces_the_stored_definition(
    runner: CliRunner, workspace: Path
) -> None:
    """The definition the command reports is the one the store holds afterwards."""
    write_instruments(workspace)
    assert at(runner, workspace, *RESOLVE).exit_code == Exit.OK
    correct(workspace, price_increment="0.05")

    plain = at(runner, workspace, *RESOLVE, "--json")
    assert plain.exit_code == Exit.PRECONDITION
    assert "--refresh" in payload(plain)["remedy"]
    assert shown(runner, workspace)["definition"]["price_increment"] == "0.01"

    refreshed = at(runner, workspace, *RESOLVE, "--refresh", "--json")
    assert refreshed.exit_code == Exit.OK, refreshed.stdout
    assert "skipping" not in refreshed.stdout
    [reported] = payload(refreshed)["instruments"]
    assert reported["definition"]["price_increment"] == "0.05"
    held = shown(runner, workspace)
    assert held["checksum"] == reported["checksum"]
    assert held["definition"]["price_increment"] == "0.05"


def test_show_renders_the_one_definition_a_run_would_use(
    runner: CliRunner, workspace: Path
) -> None:
    """Two dated definitions are held; one is listed, and it is the newest-dated."""
    write_instruments(workspace)
    assert at(runner, workspace, *RESOLVE).exit_code == Exit.OK
    correct(workspace, price_increment="0.05")
    later = "2024-01-03"
    assert at(runner, workspace, *RESOLVE[:-1], later).exit_code == Exit.OK

    listed = at(runner, workspace, "data", "instruments", "show")
    assert listed.exit_code == Exit.OK
    assert listed.stdout.count(INSTRUMENT) == 1
    held = shown(runner, workspace)
    assert held["definition"]["price_increment"] == "0.05"
    assert held["definition"]["ts_init"] == day_start_ns(date.fromisoformat(later))


def test_a_snapshot_refuses_an_empty_instrument_store_over_instrument_data(
    runner: CliRunner, workspace: Path
) -> None:
    """A run reads its definitions from the store, so a snapshot pinning none is refused."""
    write_instruments(workspace)
    spec = write_spec(workspace)
    loaded = at(runner, workspace, "data", "load", "--loader", "synthetic", "--spec", spec)
    assert loaded.exit_code == Exit.OK, loaded.stdout

    result = at(runner, workspace, "data", "snapshot", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert INSTRUMENT in payload(result)["error"]
    assert "kanso data instruments resolve" in payload(result)["remedy"]
    assert at(runner, workspace, *RESOLVE).exit_code == Exit.OK
    assert at(runner, workspace, "data", "snapshot").exit_code == Exit.OK


def test_begin_refuses_a_store_that_moved_since_the_snapshot_until_one_is_taken(
    runner: CliRunner, registered: Path
) -> None:
    """A run is pinned to the definitions its snapshot pins, not to whatever the store holds."""
    correct(registered, isin="US0378331005")
    assert at(runner, registered, *RESOLVE, "--refresh").exit_code == Exit.OK

    refused = at(runner, registered, "research", "begin", HYP_ID, "--json")

    assert refused.exit_code == Exit.PRECONDITION
    error = payload(refused)
    assert "pins instruments" in error["error"]
    assert "kanso data snapshot" in error["remedy"]
    assert at(runner, registered, "data", "snapshot").exit_code == Exit.OK
    begun = at(runner, registered, "research", "begin", HYP_ID, "--json")
    assert begun.exit_code == Exit.OK, begun.stdout
