"""`kanso monitor run`: one pass of the watch, and what a pass is allowed to do by itself.

The pass itself is proved in the monitor's own slice. What is asserted here is the command:
that a failing gate is a result rather than an error, that the object names the actions the
pass took, and that the pass is idempotent — running it twice does not escalate twice, which
is what makes it safe on a timer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from kanso.criteria.run import CardRun
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
    assert [gate["id"] for gate in judged["gates"]] == ["paper_forward", "forward_holdout"]
    assert all(gate["pass"] is True for gate in judged["gates"])
    forward = judged["gates"][1]["evidence"]
    assert forward["judged"] is True, "the four forward weeks resolve the certified value"
    assert forward["periods"] >= forward["required_periods"]
    assert forward["realised"] > forward["bar"] == 0.0
    assert forward["window"][0] == forward["forward_start"] == "2024-06-03"
    assert payload(at(runner, deployed, "strat", "show", f"{HYP_ID}@1", "--json"))["state"] == (
        "promotable"
    )


def test_a_pass_shows_the_forward_look_on_forward_holdout(
    runner: CliRunner, deployed: Path
) -> None:
    """The paper stage's window was the lineage's first look at the sleeve's forward window, and
    a replay of the best card over those days the second: `forward_holdout`'s evidence says
    which look the version is, how many the lineage has taken, and who the lineage is."""
    assert at(runner, deployed, "replay", "run", "--hyp", HYP_ID).exit_code == Exit.OK

    result = at(runner, deployed, "monitor", "run", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    judged = next(one for one in payload(result)["outcomes"] if one["strategy"] == HYP_ID)
    forward = next(gate for gate in judged["gates"] if gate["id"] == "forward_holdout")
    assert (forward["evidence"]["look"], forward["evidence"]["looks"]) == (1, 2)
    assert forward["evidence"]["lineage"] == [HYP_ID]
    assert judged["actions"] == ["promotable"], "the count moved no verdict"


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
    assert "paper_forward" in result.stdout and "forward_holdout" in result.stdout
    assert "promotable" in result.stdout


def test_a_version_whose_windows_kept_a_period_target_s_record_prints_and_carries_it(
    runner: CliRunner, deployed: Path
) -> None:
    """The demo declares no period target, so its judgement carries no `period_stops` and
    prints no `stops` line. A window recorded under one — the deployed version's paper stage
    closing a window it replayed nothing in, as an idle stage window records it, with the
    record of two weeks, one stopped — joins the version's windows, and the pass carries the
    joined record on the version's outcome under `--json` and prints its `stops` line."""
    from dataclasses import replace

    from kanso.nautilus.node import Realised
    from kanso.portfolio import records as stage_records
    from kanso.schemas import PeriodStops
    from kanso.state import StateStore

    def version(result: Any) -> dict[str, Any]:
        return next(
            one
            for one in payload(result)["outcomes"]
            if one["strategy"] == HYP_ID and one["stage"] == "paper"
        )

    def idle(run: CardRun, **changes: Any) -> CardRun:
        return replace(
            run,
            period_ends_ns=(),
            returns=(),
            equity=(),
            trades=(),
            fills=(),
            held=(),
            cushion=(),
            carry=(),
            worst_ratio=(),
            funding=(),
            **changes,
        )

    assert "period_stops" not in version(at(runner, deployed, "monitor", "run", "--json"))
    assert "target periods" not in at(runner, deployed, "monitor", "run").stdout
    stop = {
        "opening_ns": 100,
        "anchor": 100_000.0,
        "threshold": 500.0,
        "hit_ns": 150,
        "segment": "rth",
        "equity": 100_600.0,
    }
    weeks = PeriodStops.model_validate({"openings": [100, 200], "stops": [stop]})
    with StateStore(deployed / "state.db") as store:
        last = stage_records.stage_results(store, strategy_id=HYP_ID, version=1, stage="paper")[-1]
        quiet = Realised(
            strategy_id=HYP_ID,
            version=1,
            capital=last.capital,
            run=idle(last.run, period_stops=weeks),
            positions=(),
            benchmark=None if last.benchmark is None else idle(last.benchmark),
        )
        stage_records.record_stage_run(store, "paper", "under-a-target", [quiet])

    judged = version(at(runner, deployed, "monitor", "run", "--json"))
    printed = at(runner, deployed, "monitor", "run")

    assert judged["period_stops"] == weeks.model_dump(mode="json")
    assert printed.exit_code == Exit.OK
    assert any(
        line.strip() == "stops 1 of 2 target periods" for line in printed.stdout.splitlines()
    )


def test_a_pass_over_a_workspace_with_nothing_deployed_is_still_a_pass(
    runner: CliRunner, registered: Path
) -> None:
    result = at(runner, registered, "monitor", "run", "--json")

    assert result.exit_code == Exit.OK
    outcome = payload(result)
    assert outcome["actions"] == []
    assert all(one["strategy"] is None for one in outcome["outcomes"])
