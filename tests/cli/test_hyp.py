"""`kanso hyp`: scaffolding, validating, registering, listing and retiring a hypothesis."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import HYP_ID, INSTRUMENT, at, payload, write_hypothesis


def test_new_renders_the_three_scoped_files(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "hyp", "new", "trial_one", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["id"] == "trial_one"
    assert document["files"] == ["hypothesis.yaml", "program.md", "strategy.py"]
    assert Path(document["dir"]) == workspace / "hypotheses" / "trial_one"


def test_new_points_the_operator_at_the_next_command(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "hyp", "new", "trial_one")

    assert result.exit_code == Exit.OK
    assert "kanso hyp add" in result.stdout


def test_new_refuses_an_id_that_is_not_one(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "hyp", "new", "Not An Id", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "a-z" in payload(result)["remedy"]


def test_new_refuses_to_write_over_an_existing_hypothesis(
    runner: CliRunner, workspace: Path
) -> None:
    assert at(runner, workspace, "hyp", "new", "trial_one").exit_code == Exit.OK

    result = at(runner, workspace, "hyp", "new", "trial_one", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "scaffolded once" in payload(result)["error"]


def test_validate_reports_the_hypothesis_and_changes_nothing(
    runner: CliRunner, loaded: Path
) -> None:
    from kanso.state import StateStore

    path = write_hypothesis(loaded)

    result = at(runner, loaded, "hyp", "validate", path, "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["id"] == HYP_ID
    assert document["universe"] == [INSTRUMENT]
    assert document["construct"] == "sleeve"
    assert document["objective"] == "net_edge_bps"
    assert document["constraints"] == ["strategy_integrity", "min_trades"]
    with StateStore(loaded / "state.db") as store:
        assert store.connection.execute("SELECT count(*) FROM hypotheses").fetchone()[0] == 0


def test_a_data_type_a_vendor_adapter_registers_is_admissible(
    runner: CliRunner, loaded: Path
) -> None:
    """A type introduced by an adapter is known to validation before anything has loaded it.

    Registration happens when the adapter's loaders are listed, which costs no credential,
    so a hypothesis may require a vendor's own type in a workspace holding none.
    """
    path = write_hypothesis(loaded, data_requirements=["bar", "financial_statement"])

    result = at(runner, loaded, "hyp", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout


def test_validate_refuses_a_hypothesis_whose_universe_does_not_resolve(
    runner: CliRunner, loaded: Path
) -> None:
    path = write_hypothesis(loaded, universe=["GONE.SIM"])

    result = at(runner, loaded, "hyp", "validate", path, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "GONE.SIM" in payload(result)["error"]


def test_add_registers_the_file_under_the_sha_of_its_bytes(runner: CliRunner, loaded: Path) -> None:
    from hashlib import sha256

    path = write_hypothesis(loaded)

    result = at(runner, loaded, "hyp", "add", path, "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    # The fixture file carries a classification, and a file that states one registers as
    # classified: the operator has said what the thesis is, so no model need repeat it.
    assert document["status"] == "classified"
    assert document["hypothesis_sha"] == sha256(path.read_bytes()).hexdigest()
    assert document["pinned"] is True


def test_add_of_an_edited_file_re_pins_it(runner: CliRunner, loaded: Path) -> None:
    path = write_hypothesis(loaded)
    first = payload(at(runner, loaded, "hyp", "add", path, "--json"))
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["title"] = "Demo: the same idea, better said"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    second = payload(at(runner, loaded, "hyp", "add", path, "--json"))

    assert second["hypothesis_sha"] != first["hypothesis_sha"]
    assert second["status"] == "classified"


def test_add_is_refused_while_a_run_is_active(runner: CliRunner, registered: Path) -> None:
    """A run is pinned to the bytes it began with, so the pin does not move under it."""
    assert at(runner, registered, "research", "begin", HYP_ID).exit_code == Exit.OK

    result = at(
        runner,
        registered,
        "hyp",
        "add",
        registered / "hypotheses" / HYP_ID / "hypothesis.yaml",
        "--json",
    )

    assert result.exit_code == Exit.PRECONDITION
    assert "active run" in payload(result)["error"]


def test_show_lists_every_registration(runner: CliRunner, registered: Path) -> None:
    result = at(runner, registered, "hyp", "show", "--json")

    assert result.exit_code == Exit.OK
    [entry] = payload(result)["hypotheses"]
    assert entry["id"] == HYP_ID
    assert entry["status"] == "classified"


def test_show_of_one_reports_its_pins_and_its_best(runner: CliRunner, registered: Path) -> None:
    result = at(runner, registered, "hyp", "show", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "classified" in result.stdout
    assert "no keep yet" in result.stdout
    assert "none active" in result.stdout


def test_show_of_an_unregistered_id_is_a_precondition_failure(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "hyp", "show", "nobody", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "kanso hyp add" in payload(result)["remedy"]


def test_an_empty_registry_lists_nothing(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "hyp", "show")

    assert result.exit_code == Exit.OK
    assert "none registered" in result.stdout


def test_retire_ends_a_hypothesis_and_leaves_its_records(
    runner: CliRunner, registered: Path
) -> None:
    result = at(runner, registered, "hyp", "retire", HYP_ID, "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["status"] == "retired"
    assert (registered / "hypotheses" / HYP_ID / "hypothesis.yaml").is_file()


def test_retire_of_an_unregistered_id_is_a_precondition_failure(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "hyp", "retire", "nobody", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "not a registered hypothesis" in payload(result)["error"]


def test_a_registration_shows_the_best_once_a_card_has_kept(
    runner: CliRunner, registered: Path
) -> None:
    from .conftest import REVERTING, edit

    assert at(runner, registered, "research", "begin", HYP_ID).exit_code == Exit.OK
    edit(registered, REVERTING)
    assert at(runner, registered, "research", "card", HYP_ID, "--desc", "fade").exit_code == Exit.OK

    result = at(runner, registered, "hyp", "show", HYP_ID, "--json")

    document = payload(result)
    assert document["best_sha"] is not None
    assert document["best_metric"] > 0
    assert f"{document['best_sha'][:7]} at " in at(runner, registered, "hyp", "show", HYP_ID).stdout
