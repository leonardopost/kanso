"""`kanso portfolio`, `kanso promote`, `kanso demote`: the deployment surface from the shell.

The deployment machinery has its own slice; what these assert is the command around it. Two
things only the command line can be asked about are here: that every refusal arrives with
the exit code the contract gives it — 2 for a precondition, 4 for a missing approval — and
that a promotion without `--as` changes nothing at all, since the approval is the whole of
what stands between a workspace and real money.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import (
    HYP_ID,
    a_client,
    at,
    payload,
    portfolio_document,
    reconfigure,
    write_hypothesis,
)


@pytest.fixture
def funded(runner: CliRunner, deployed: Path) -> Path:
    """That workspace with the live stage funded, which is what a promotion needs."""
    reconfigure(deployed, "live", capital=100_000)
    return deployed


def restate(root: Path, state: str) -> None:
    """Rewrite the composed version's state by hand, as an operator editing the file would."""
    path = root / "strategies" / HYP_ID / "strategy.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["versions"][0]["state"] = state
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def stage(runner: CliRunner, root: Path, name: str) -> dict[str, object]:
    """One stage of `portfolio show`, as the JSON object reports it."""
    document = payload(at(runner, root, "portfolio", "show", "--json"))
    stages = document["stages"]
    assert isinstance(stages, list)
    return next(one for one in stages if one["stage"] == name)


# -- show --------------------------------------------------------------------------


def test_show_reports_both_stages_their_clocks_and_what_they_made(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "portfolio", "show", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert [one["stage"] for one in document["stages"]] == ["paper", "live"]
    paper = document["stages"][0]
    assert paper["live"] is True, "the paper node has consumed everything the catalog holds"
    assert paper["clock"] == "2024-06-28"
    assert paper["strategies"][0]["strategy"] == HYP_ID
    assert paper["strategies"][0]["capital"] > 0
    assert paper["strategies"][0]["windows"] == 1
    assert document["limits"]["per_strategy_max_pct"] == 40


def test_show_reads_as_two_stages_and_the_limits(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "portfolio", "show")

    assert result.exit_code == Exit.OK
    assert "paper" in result.stdout
    assert "live" in result.stdout
    assert "limits" in result.stdout


def test_show_writes_nothing(runner: CliRunner, deployed: Path) -> None:
    before = portfolio_document(deployed)

    at(runner, deployed, "portfolio", "show")

    assert portfolio_document(deployed) == before


def by_hand(root: Path, name: str, entry: dict[str, Any]) -> None:
    """Add a strategy to a stage of `portfolio.yaml` with an editor, which is legal."""
    document = portfolio_document(root)
    document["stages"][name]["capital"] = 100_000
    document["stages"][name]["strategies"] = [entry]
    (root / "portfolio.yaml").write_text(yaml.safe_dump(document, sort_keys=False))


def test_show_marks_a_hand_added_entry_rather_than_printing_it_as_deployed(
    runner: CliRunner, deployed: Path
) -> None:
    """The file can name a stage that will never exist; `show` says so, `deploy` proves it."""
    by_hand(
        deployed,
        "live",
        {"id": HYP_ID, "version": 1, "capital": 40_000, "joined_at": "2024-06-03T00:00:00Z"},
    )

    result = at(runner, deployed, "portfolio", "show")
    live = stage(runner, deployed, "live")

    assert result.exit_code == Exit.OK
    assert "not deployed · in portfolio.yaml only" in result.stdout
    assert live["strategies"][0]["recorded"] is False
    assert live["allocated"] == 0, "an entry no deployment wrote holds none of the capital"
    assert live["live"] is False
    admitted = payload(at(runner, deployed, "portfolio", "deploy", "--stage", "live", "--json"))
    assert admitted["admitted"] == [], "which is what deploy says about the same entry"


def test_show_reports_a_deployed_version_as_recorded(runner: CliRunner, deployed: Path) -> None:
    paper = stage(runner, deployed, "paper")

    assert paper["strategies"][0]["recorded"] is True
    assert "not deployed" not in at(runner, deployed, "portfolio", "show").stdout


# -- deploy ------------------------------------------------------------------------


def test_deploy_admits_funds_and_runs_the_stage(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert document["stage"] == "paper"
    assert [one["strategy"] for one in document["admitted"]] == [HYP_ID]
    assert document["capital"] > 0
    assert document["session"], "a stage that admitted something ran a node"


def test_deploy_reads_as_what_was_admitted_and_what_ran(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper")

    assert result.exit_code == Exit.OK
    assert "deployed" in result.stdout
    assert f"{HYP_ID}@1" in result.stdout


def test_a_stage_with_nothing_to_admit_runs_no_node(runner: CliRunner, deployed: Path) -> None:
    """A deployment is not a failure when there is nothing composed left to deploy."""
    assert at(runner, deployed, "strat", "retire", HYP_ID).exit_code == Exit.OK

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["admitted"] == []
    assert payload(result)["session"] is None
    assert "no node ran" in at(runner, deployed, "portfolio", "deploy", "--stage", "paper").stdout


def test_a_stage_that_is_not_a_stage_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "portfolio", "deploy", "--stage", "shadow", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "is not a deployment stage" in payload(result)["error"]


def test_a_halted_stage_is_refused(runner: CliRunner, deployed: Path) -> None:
    reconfigure(deployed, "paper", kill_switch=True)

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "kill_switch" in payload(result)["error"]


def test_an_engine_pin_that_has_moved_is_refused(runner: CliRunner, deployed: Path) -> None:
    path = deployed / "strategies" / HYP_ID / "strategy.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["versions"][0]["pins"]["nautilus_version"] = "0.0.1"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "0.0.1" in payload(result)["error"]
    assert "cert run" in str(payload(result)["remedy"])


def test_an_edited_implementation_is_refused_rather_than_deployed(
    runner: CliRunner, deployed: Path
) -> None:
    """A stage runs the certified bytes or nothing: the digest is checked before the node."""
    source = next((deployed / "strategies" / HYP_ID / "impl" / "1").glob("kanso_impl_*.py"))
    source.write_bytes(source.read_bytes() + b"\nEDITED = True\n")

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert source.name in str(payload(result)["error"])
    assert "is not the sleeve that was certified" in str(payload(result)["error"])
    assert "certificates" in str(payload(result)["remedy"])


def test_a_stage_with_no_data_in_the_forward_window_is_refused(
    runner: CliRunner, deployed: Path
) -> None:
    directory = deployed / "hypotheses" / HYP_ID
    held = yaml.safe_load((directory / "hypothesis.yaml").read_text(encoding="utf-8"))
    moved = {**held["windows"], "forward": {"start": "2030-01-01"}}
    path = write_hypothesis(
        deployed,
        (directory / "strategy.py").read_text(encoding="utf-8"),
        base=held,
        windows=moved,
    )
    assert at(runner, deployed, "hyp", "add", path).exit_code == Exit.OK

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "2030-01-01" in payload(result)["error"]


@pytest.mark.usefixtures("_leave_the_interpreter_as_found")
def test_real_capital_without_an_approval_is_exit_four(runner: CliRunner, funded: Path) -> None:
    """The record, not the file, is what lets a version onto real money.

    The version is moved onto the live stage by editing the files, which is the act the
    approval record exists to defeat: the file says the version is live and nothing an
    operator ever did says it may be.
    """
    reconfigure(funded, "live", exec=a_client(funded, "house", capital="real", clock="replay"))
    restate(funded, "live")

    result = at(runner, funded, "portfolio", "deploy", "--stage", "live", "--json")

    assert result.exit_code == Exit.APPROVAL
    assert "operator approval" in payload(result)["error"]


@pytest.mark.usefixtures("_leave_the_interpreter_as_found")
def test_real_capital_off_the_live_stage_is_exit_four(runner: CliRunner, deployed: Path) -> None:
    reconfigure(
        deployed, "paper", exec=a_client(deployed, "housetwo", capital="real", clock="replay")
    )

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.APPROVAL
    assert "only on the live stage" in payload(result)["error"]


@pytest.mark.usefixtures("_leave_the_interpreter_as_found")
def test_a_wall_clock_client_fed_a_replay_is_refused(runner: CliRunner, deployed: Path) -> None:
    reconfigure(
        deployed, "paper", exec=a_client(deployed, "broker", capital="broker_paper", clock="wall")
    )

    result = at(runner, deployed, "portfolio", "deploy", "--stage", "paper", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "replay" in payload(result)["error"]


# -- promote and demote ------------------------------------------------------------


def promotable(runner: CliRunner, root: Path) -> Path:
    """The paper version moved to `promotable`, which is what a monitor pass does."""
    outcome = payload(at(runner, root, "monitor", "run", "--json"))
    assert "promotable" in outcome["actions"], outcome
    return root


def test_a_promotion_without_a_name_is_exit_four_and_changes_nothing(
    runner: CliRunner, funded: Path
) -> None:
    promotable(runner, funded)
    before = portfolio_document(funded)

    result = at(runner, funded, "promote", HYP_ID, "--live", "--json")

    assert result.exit_code == Exit.APPROVAL
    assert "named operator act" in payload(result)["error"]
    assert portfolio_document(funded) == before, "nothing moved"
    assert payload(at(runner, funded, "strat", "show", f"{HYP_ID}@1", "--json"))["state"] != "live"


def test_a_promotion_that_names_no_stage_is_refused(runner: CliRunner, funded: Path) -> None:
    promotable(runner, funded)

    result = at(runner, funded, "promote", HYP_ID, "--as", "Ada", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "--live" in str(payload(result)["remedy"])


def test_a_named_promotion_records_the_approval_and_moves_the_version(
    runner: CliRunner, funded: Path
) -> None:
    promotable(runner, funded)

    result = at(runner, funded, "promote", HYP_ID, "--live", "--as", "Ada Lovelace", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    promotion = payload(result)
    assert promotion["operator"] == "Ada Lovelace"
    assert promotion["state"] == "live"
    assert [one["stage"] for one in promotion["deployments"]] == ["live", "paper"]
    live = stage(runner, funded, "live")
    assert [one["strategy"] for one in live["strategies"]] == [HYP_ID]
    assert stage(runner, funded, "paper")["strategies"] == []


def test_a_version_that_is_not_promotable_is_refused(runner: CliRunner, funded: Path) -> None:
    result = at(runner, funded, "promote", HYP_ID, "--live", "--as", "Ada", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no promotable version" in payload(result)["error"]


def test_demote_returns_the_version_to_the_paper_stage(runner: CliRunner, funded: Path) -> None:
    promotable(runner, funded)
    assert at(runner, funded, "promote", HYP_ID, "--live", "--as", "Ada").exit_code == Exit.OK

    result = at(runner, funded, "demote", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    demotion = payload(result)
    assert demotion["state"] == "paper"
    assert demotion["halted"] == []
    assert stage(runner, funded, "live")["strategies"] == []
    assert [one["strategy"] for one in stage(runner, funded, "paper")["strategies"]] == [HYP_ID]


def test_demote_leaves_a_halted_stage_halted_rather_than_deadlocking(
    runner: CliRunner, funded: Path
) -> None:
    """The kill switch and the demotion it triggers must not each wait for the other."""
    promotable(runner, funded)
    assert at(runner, funded, "promote", HYP_ID, "--live", "--as", "Ada").exit_code == Exit.OK
    reconfigure(funded, "live", kill_switch=True)

    result = at(runner, funded, "demote", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    demotion = payload(result)
    assert demotion["halted"] == ["live"]
    assert [one["stage"] for one in demotion["deployments"]] == ["paper"]
    unread = payload(at(runner, funded, "inbox", "--json"))["entries"]
    assert any(entry["kind"] == "demoted" for entry in unread)
    assert portfolio_document(funded)["stages"]["live"]["kill_switch"] is True


def test_demoting_a_version_that_is_not_live_is_refused(runner: CliRunner, funded: Path) -> None:
    result = at(runner, funded, "demote", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no live version" in payload(result)["error"]
