"""`kanso monitor run`: one pass of the watch, and what a pass is allowed to do by itself.

The pass itself is proved in the monitor's own slice. What is asserted here is the command:
that a failing gate is a result rather than an error, that the object names the actions the
pass took, and that the pass is idempotent — running it twice does not escalate twice, which
is what makes it safe on a timer.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import HYP_ID, at, payload


def test_a_pass_judges_the_deployed_version_and_promotes_it(
    runner: CliRunner, deployed: Path
) -> None:
    result = at(runner, deployed, "monitor", "run", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    outcome = payload(result)
    assert outcome["actions"] == ["promotable"]
    assert len(outcome["escalations"]) == 1
    judged = next(
        one for one in outcome["outcomes"] if one["strategy"] == HYP_ID and one["stage"] == "paper"
    )
    assert [gate["id"] for gate in judged["gates"]] == ["paper_forward"]
    assert judged["gates"][0]["pass"] is True
    assert payload(at(runner, deployed, "strat", "show", f"{HYP_ID}@1", "--json"))["state"] == (
        "promotable"
    )


def test_the_pass_reports_each_stage_exposure_against_its_limits(
    runner: CliRunner, deployed: Path
) -> None:
    """Gross and net are the two limits only a whole-stage view can enforce."""
    result = at(runner, deployed, "monitor", "run", "--json")

    exposures = [one["exposure"] for one in payload(result)["outcomes"] if one["exposure"]]
    assert [one["stage"] for one in exposures] == ["paper", "live"]
    assert all(one["breached"] is False for one in exposures)
    assert exposures[0]["max_gross"] == 100_000, "a percentage of the stage capital, in money"


def test_a_second_pass_takes_no_action_and_escalates_nothing(
    runner: CliRunner, deployed: Path
) -> None:
    """An escalation repeated every few minutes is an inbox nobody reads."""
    assert at(runner, deployed, "monitor", "run").exit_code == Exit.OK

    result = at(runner, deployed, "monitor", "run", "--json")

    assert payload(result)["actions"] == []
    assert payload(result)["escalations"] == []
    assert payload(at(runner, deployed, "inbox", "--json"))["unread"] == 1


def test_the_pass_reads_as_the_stages_then_the_versions(runner: CliRunner, deployed: Path) -> None:
    result = at(runner, deployed, "monitor", "run")

    assert result.exit_code == Exit.OK
    assert "within limits" in result.stdout
    assert "paper_forward" in result.stdout
    assert "promotable" in result.stdout


def test_a_pass_over_a_workspace_with_nothing_deployed_is_still_a_pass(
    runner: CliRunner, registered: Path
) -> None:
    result = at(runner, registered, "monitor", "run", "--json")

    assert result.exit_code == Exit.OK
    outcome = payload(result)
    assert outcome["actions"] == []
    assert all(one["strategy"] is None for one in outcome["outcomes"])
