"""`kanso replay`: running a target on either code path, comparing them, and reading a session.

The replay machinery is proved in its own slice. What is asserted here is the command — how
a target is named, what the range defaults to, that a session is written where the object
says it is, and that `parity` reports agreement rather than merely not raising.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.cli.replay import CAUSE_WIDTH, _cause
from kanso.errors import Exit
from kanso.replay.parity import Parity
from kanso.replay.record import Intent

from .conftest import HYP_ID, at, payload


def test_a_strategy_replays_on_the_live_code_path_by_default(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "replay", "run", "--strategy", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    session = payload(result)
    assert session["mode"] == "node"
    assert session["target"] == f"{HYP_ID}@1"
    assert session["exec"] == "sandbox", "a replay never reaches a broker"
    assert session["from"] == "2024-06-03", "the range opens at the forward window"
    assert session["to"] == "2024-06-28", "and closes where the catalog does"
    assert session["released"] > 0
    assert Path(session["path"]).is_dir()


def test_the_research_code_path_is_asked_for_by_name(runner: CliRunner, deployed: Path) -> None:
    result = at(
        runner, deployed, "replay", "run", "--strategy", HYP_ID, "--mode", "engine", "--json"
    )

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["mode"] == "engine"


def test_a_range_may_be_given_explicitly(runner: CliRunner, deployed: Path) -> None:
    result = at(
        runner,
        deployed,
        "replay",
        "run",
        "--strategy",
        HYP_ID,
        "--from",
        "2024-06-10",
        "--to",
        "2024-06-20",
        "--json",
    )

    session = payload(result)
    assert (session["from"], session["to"]) == ("2024-06-10", "2024-06-20")


def test_a_hypothesis_replays_its_best_card(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "replay", "run", "--hyp", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["target"].startswith(f"{HYP_ID}@")


def test_naming_neither_target_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "replay", "run", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "exactly one" in payload(result)["error"]


def test_naming_both_targets_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "replay", "run", "--strategy", HYP_ID, "--hyp", HYP_ID, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "exactly one" in payload(result)["error"]


def test_a_mode_that_is_not_a_code_path_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "replay", "run", "--strategy", HYP_ID, "--mode", "live", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "not a code path" in payload(result)["error"]


def test_a_date_that_is_not_a_date_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(
        runner, deployed, "replay", "run", "--strategy", HYP_ID, "--from", "last week", "--json"
    )

    assert result.exit_code == Exit.VALIDATION
    assert "is not a date" in payload(result)["error"]


def test_a_version_the_strategy_does_not_have_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "replay", "run", "--strategy", f"{HYP_ID}@7", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "has no version 7" in payload(result)["error"]


def test_parity_runs_both_paths_and_reports_that_they_agree(
    runner: CliRunner, deployed: Path
) -> None:
    """The claim the whole design rests on, asked for in one command."""
    result = at(runner, deployed, "replay", "parity", "--strategy", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    parity = payload(result)
    assert parity["identical"] is True
    assert parity["divergence"] is None
    assert parity["compared"] > 0, "two silent paths agree about nothing"
    assert parity["node_intents"] == parity["engine_intents"]
    assert parity["max_ts_delta_ns"] == 0
    assert parity["node"] != parity["engine"]


def test_parity_reads_as_the_verdict_and_the_two_sessions(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "replay", "parity", "--strategy", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "identical" in result.stdout
    assert "node" in result.stdout
    assert "engine" in result.stdout


def parted(node: float, engine: float) -> Parity:
    """A comparison of two intent sequences that differ only in quantity."""

    def order(qty: float) -> Intent:
        return Intent(
            ts_event=1_000, instrument="DEMO.XNAS", side="BUY", qty=qty, order_type="MARKET"
        )

    return Parity(
        node="n",
        engine="e",
        ts_ns=0,
        node_orders=(order(node),),
        engine_orders=(order(engine),),
        max_ts_delta_ns=0,
    ).at(0)


def test_a_divergence_with_a_known_cause_is_explained_under_the_parity_line() -> None:
    """The two paths agree over every fixture in this workspace, so nothing a command can
    be run here would ever render the explanation — and it shipped unrendered by any test.
    The cause is wrapped rather than printed as one long line, which is what makes it
    readable beside a label column."""
    lines = _cause(parted(node=739.0, engine=976.0))

    assert lines
    assert "filled only in part" in " ".join(line.strip() for line in lines)
    assert max(len(line) for line in lines) <= CAUSE_WIDTH + len(lines[0]) - len(lines[0].lstrip())


def test_a_divergence_of_a_shape_nobody_can_name_is_explained_with_nothing() -> None:
    """A reader looking at an unexplained divergence sees no explanation, not a hedge."""
    assert _cause(parted(node=976.0, engine=739.0)) == ()


def test_show_lists_the_sessions_the_workspace_holds(runner: CliRunner, deployed: Path) -> None:
    made = payload(at(runner, deployed, "replay", "run", "--strategy", HYP_ID, "--json"))

    result = at(runner, deployed, "replay", "show", "--json")

    assert result.exit_code == Exit.OK
    identifiers = [one["session_id"] for one in payload(result)["sessions"]]
    assert made["session_id"] in identifiers
    assert len(identifiers) > 1, "certification and deployment left sessions of their own"


def test_show_of_one_session_is_that_session(runner: CliRunner, deployed: Path) -> None:
    made = payload(at(runner, deployed, "replay", "run", "--strategy", HYP_ID, "--json"))

    result = at(runner, deployed, "replay", "show", made["session_id"], "--json")

    assert result.exit_code == Exit.OK
    assert payload(result)["session_id"] == made["session_id"]
    assert payload(result)["intents"] == made["intents"]


def test_a_session_this_workspace_never_ran_is_refused(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "replay", "show", "nothing-like-this", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no session" in payload(result)["error"]
