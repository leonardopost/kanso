"""`kanso research run`: the loop with no human in it, driven from the command line.

The driver itself is proved in its own slice; what is asserted here is the command around
it — that one invocation begins a run when there is none, that `--cards` counts what this
invocation proposed, that the object printed under `--json` carries the tally an operator
or an agent branches on, and that every model call it made is in the ledger.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.models import spend
from kanso.state import StateStore

from . import mocked
from .conftest import DRAFT, HYP_ID, at, lane, payload, write_hypothesis


def ledger_calls(root: Path) -> int:
    """How many model calls this workspace has recorded, over every day and lane."""
    with StateStore(root / "state.db") as store:
        return spend(store).calls


def test_run_begins_a_run_and_proposes_the_cards_it_was_asked_for(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 3, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    outcome = payload(result)
    assert outcome["proposed"] == 3
    assert outcome["keeps"] + outcome["discards"] + outcome["crashes"] == 3
    assert outcome["reason"] == "cards"
    assert outcome["ended"] is False
    # The run it began is still open, and its lane directory is still the interface.
    assert lane(mocked_ws).is_dir()


def test_the_three_scripted_answers_keep_discard_and_crash(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 3, "--json")

    outcome = payload(result)
    assert (outcome["keeps"], outcome["discards"], outcome["crashes"]) == (1, 1, 1)
    assert outcome["best_sha"] is not None
    assert outcome["best_metric"] is not None


def test_every_call_the_driver_made_is_in_the_ledger(runner: CliRunner, mocked_ws: Path) -> None:
    before = ledger_calls(mocked_ws)

    assert at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 2).exit_code == Exit.OK

    # One `propose` call per card, and nothing is proposed without being recorded.
    assert ledger_calls(mocked_ws) - before >= 2


def test_a_second_invocation_resumes_the_same_run(runner: CliRunner, mocked_ws: Path) -> None:
    first = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1, "--json"))

    second = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1, "--json"))

    assert second["run_id"] == first["run_id"]
    assert second["proposed"] == 1


def test_run_reads_as_the_tally_and_the_next_command(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 1)

    assert result.exit_code == Exit.OK
    assert "1 proposed" in result.stdout
    assert f"kanso research run {HYP_ID}" in result.stdout


def test_a_stall_ends_the_run_and_the_command_says_so(runner: CliRunner, mocked_ws: Path) -> None:
    # One non-keep is a stall, and the scripted cycle's second answer is a discard.
    mocked.tuned(mocked_ws, stall_k=1)

    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    outcome = payload(result)
    assert outcome["reason"] == "stalled"
    assert outcome["ended"] is True
    # Ending a run removes its lane directory and nothing else: the keep is still there.
    assert not lane(mocked_ws).exists()
    assert "Strategy.mode" in at(runner, mocked_ws, "research", "show", HYP_ID).stdout


def test_a_drift_check_falls_on_the_cadence_the_workspace_sets(
    runner: CliRunner, mocked_ws: Path
) -> None:
    mocked.tuned(mocked_ws, align_every=1)
    mocked.scripted(mocked_ws, align_check=[mocked.ALIGNED])

    outcome = payload(at(runner, mocked_ws, "research", "run", HYP_ID, "--cards", 2, "--json"))

    assert outcome["checks"] == 2
    assert outcome["drifts"] == 0


def test_running_an_unclassified_hypothesis_is_refused(runner: CliRunner, loaded: Path) -> None:
    path = write_hypothesis(loaded, mocked.SEED, base=DRAFT)
    assert at(runner, loaded, "hyp", "add", path).exit_code == Exit.OK
    mocked.scripted(loaded)

    result = at(runner, loaded, "research", "run", HYP_ID, "--cards", 1, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "classif" in payload(result)["error"]
