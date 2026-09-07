"""`kanso cert`: planning, running and showing a certification through the commands typed.

The register is the mock on every tier, so the plan here is a real router call over a real
prompt and no socket is opened. The subjects are real cards of a real run: certification
judges the bytes a card recorded, so a card inserted by hand would prove nothing about the
command that certifies one.

What is asserted here is the command surface — the arguments, the exit codes, the one JSON
object, the lines a person reads — and the two things only the command line can be asked
about: that a failing verdict is a result rather than an error, and that the refusals to
certify twice arrive as exit 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.research import records
from kanso.state import StateStore
from tests.params import pairs

from . import mocked
from .conftest import DRAFT, HYP_ID, at, payload, write_hypothesis


def begin(runner: CliRunner, root: Path) -> dict[str, Any]:
    """Open the run whose baseline card is the flat, non-trading strategy."""
    result = at(runner, root, "research", "begin", HYP_ID, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def keep(runner: CliRunner, root: Path) -> dict[str, Any]:
    """One driven card: the scripted `revert` proposal, which keeps and becomes `best`."""
    result = at(runner, root, "research", "run", HYP_ID, "--cards", 1, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


@pytest.fixture
def researched(runner: CliRunner, mocked_ws: Path) -> Path:
    """A workspace whose hypothesis has a baseline card and a `best` worth certifying."""
    begin(runner, mocked_ws)
    document = keep(runner, mocked_ws)
    assert document["best_sha"], "the first driven card keeps, or nothing here has a subject"
    return mocked_ws


def plan_document(root: Path) -> dict[str, Any]:
    """The pinned plan as it stands on disk."""
    text = (root / "certificates" / HYP_ID / "plan.yaml").read_text(encoding="utf-8")
    document: dict[str, Any] = yaml.safe_load(text)
    return document


def certificates(root: Path) -> list[Path]:
    """Every certificate file written for the demo hypothesis."""
    directory = root / "certificates" / HYP_ID
    return sorted(path for path in directory.glob("*.yaml") if path.name != "plan.yaml")


def calls(root: Path, task: str) -> int:
    """How many ledgered calls one task class has made, which is what a plan costs."""
    with StateStore(root / "state.db") as store:
        counted = store.connection.execute(
            "SELECT COUNT(*) AS n FROM spend WHERE task_class = ?", (task,)
        ).fetchone()
    return int(counted["n"])


def baseline_sha(root: Path) -> str:
    """The sha of the run's first card — the flat strategy, which never trades."""
    with StateStore(root / "state.db") as store:
        return records.cards_of(store, HYP_ID)[0].strategy_sha


# -- cert plan ----------------------------------------------------------------------


def test_a_plan_is_written_pinned_and_reported(runner: CliRunner, researched: Path) -> None:
    result = at(runner, researched, "cert", "plan", HYP_ID, "--json")

    document = payload(result)
    assert result.exit_code == Exit.OK
    assert document["plan_version"] == 1
    assert [gate["id"] for gate in document["gates"]] == [
        gate["id"] for gate in mocked.PLAN["gates"]
    ]
    assert document["path"] == str(researched / "certificates" / HYP_ID / "plan.yaml")
    assert plan_document(researched)["hyp_id"] == HYP_ID


def test_the_plan_reads_as_the_gates_the_stages_and_the_reasons(
    runner: CliRunner, researched: Path
) -> None:
    result = at(runner, researched, "cert", "plan", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "version 1" in result.stdout
    assert "embargoed_window" in result.stdout and "min_fraction=0.0" in result.stdout
    assert "live_drift" in result.stdout and "no params" in result.stdout
    assert "bootstrap" in result.stdout, "an exclusion is part of the plan"
    assert f"kanso cert run {HYP_ID}" in result.stdout


def sized(min_duration: str) -> dict[str, Any]:
    """The fixture's plan with its paper window set, which is the operator's real choice."""
    gates = [
        dict(gate, params=pairs({"min_duration": min_duration, "horizon_mult": 20.0}))
        if gate["id"] == "paper_forward"
        else gate
        for gate in mocked.PLAN["gates"]
    ]
    return {**mocked.PLAN, "gates": gates}


def test_a_paper_window_too_short_for_its_own_band_is_warned_about(
    runner: CliRunner, researched: Path
) -> None:
    """The fixture certifies over 61 days and its plan judges paper over five: a warning."""
    result = at(runner, researched, "cert", "plan", HYP_ID)
    document = payload(at(runner, researched, "cert", "plan", HYP_ID, "--json"))

    assert result.exit_code == Exit.OK
    assert "warning" in result.stdout
    assert "paper window 5d against a 61d certification window" in result.stdout
    (warning,) = document["warnings"]
    assert "noisier than the band" in warning
    assert plan_document(researched)["plan_version"] == 1, "a warning pins the plan all the same"


def test_a_paper_window_the_certification_window_can_judge_is_not(
    runner: CliRunner, researched: Path
) -> None:
    mocked.scripted(researched, certify_plan=[sized("5d"), sized("30d")])
    assert at(runner, researched, "cert", "plan", HYP_ID, "--json").exit_code == Exit.OK

    result = at(runner, researched, "cert", "plan", HYP_ID, "--replan", "--json")

    assert payload(result)["warnings"] == []
    assert "warning" not in at(runner, researched, "cert", "plan", HYP_ID).stdout


def test_reading_a_pinned_plan_costs_nothing(runner: CliRunner, researched: Path) -> None:
    assert at(runner, researched, "cert", "plan", HYP_ID, "--json").exit_code == Exit.OK

    again = at(runner, researched, "cert", "plan", HYP_ID, "--json")

    assert payload(again)["plan_version"] == 1
    assert calls(researched, "certify_plan") == 1, "a pinned plan is read, not asked for again"


def test_replan_mints_the_next_version_and_pays_again(runner: CliRunner, researched: Path) -> None:
    at(runner, researched, "cert", "plan", HYP_ID, "--json")

    result = at(runner, researched, "cert", "plan", HYP_ID, "--replan", "--json")

    assert payload(result)["plan_version"] == 2
    assert plan_document(researched)["plan_version"] == 2
    assert calls(researched, "certify_plan") == 2


def test_a_hypothesis_that_was_never_classified_cannot_be_planned(
    runner: CliRunner, mocked_ws: Path
) -> None:
    path = write_hypothesis(mocked_ws, base=DRAFT, id="draft_one")
    assert at(runner, mocked_ws, "hyp", "add", path).exit_code == Exit.OK

    result = at(runner, mocked_ws, "cert", "plan", "draft_one", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "not classified" in payload(result)["error"]


def test_a_plan_naming_a_gate_this_version_cannot_run_is_refused(
    runner: CliRunner, researched: Path
) -> None:
    unrunnable: dict[str, Any] = {
        "gates": [
            {**gate, "id": "parity_replay", "params": pairs({"ts_ns": 0})}
            if gate["id"] == "embargoed_window"
            else gate
            for gate in mocked.PLAN["gates"]
        ],
        "excluded": [],
    }
    mocked.scripted(researched, certify_plan=[unrunnable, unrunnable])
    # `parity_replay` needs replay sessions, so this version neither offers it to the
    # planner nor accepts it from one; the plan is complained about and asked for again.

    result = at(runner, researched, "cert", "plan", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "in 2 attempts" in payload(result)["error"], "one retry with the complaints, then out"
    assert not (researched / "certificates" / HYP_ID / "plan.yaml").exists()
    assert calls(researched, "certify_plan") == 2, "both attempts were generated and billed"


# -- cert run -----------------------------------------------------------------------


def test_running_the_plan_writes_the_certificate_and_the_bytes_beside_it(
    runner: CliRunner, researched: Path
) -> None:
    result = at(runner, researched, "cert", "run", HYP_ID, "--json")

    document = payload(result)
    assert result.exit_code == Exit.OK, document
    assert document["verdict"] in ("pass", "fail")
    assert Path(document["path"]).is_file()
    assert Path(document["source"]).read_bytes(), "the certified bytes travel beside it"
    assert certificates(researched) == [Path(document["path"])]
    assert document["plan_version"] == 1, "a run with no plan plans first"


def test_the_certificate_reads_as_the_verdict_then_the_gates(
    runner: CliRunner, researched: Path
) -> None:
    result = at(runner, researched, "cert", "run", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "embargoed_window" in result.stdout
    assert "publication_lag" in result.stdout
    assert "1 skipped" in result.stdout and "nothing is deployed" in result.stdout
    assert "engine" in result.stdout and "snapshot" in result.stdout


def test_a_failing_verdict_is_a_result_and_not_an_error(
    runner: CliRunner, researched: Path
) -> None:
    flat = baseline_sha(researched)

    result = at(runner, researched, "cert", "run", HYP_ID, "--sha", flat[:7], "--json")

    document = payload(result)
    assert result.exit_code == Exit.OK, "a strategy that fails its gates is evidence, not a fault"
    assert document["verdict"] == "fail"
    assert document["status"] == "researching", "a fail short of the allowance returns to research"
    failed = [gate["id"] for gate in document["gates"] if not gate["pass"]]
    assert failed == ["embargoed_window"], "bytes that never trade earn nothing out of sample"


def test_a_fail_points_at_more_research_rather_than_at_a_replan(
    runner: CliRunner, researched: Path
) -> None:
    result = at(runner, researched, "cert", "run", HYP_ID, "--sha", baseline_sha(researched)[:7])

    assert result.exit_code == Exit.OK
    assert "fail" in result.stdout
    assert f"kanso research run {HYP_ID}" in result.stdout
    assert "--replan" not in result.stdout, "a plan rewritten to pass is not a proof"


def test_certifying_the_same_subject_again_is_refused(runner: CliRunner, researched: Path) -> None:
    assert at(runner, researched, "cert", "run", HYP_ID, "--json").exit_code == Exit.OK

    result = at(runner, researched, "cert", "run", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "immutable" in payload(result)["error"]
    assert len(certificates(researched)) == 1


def test_a_sha_that_names_no_card_of_this_hypothesis_is_refused(
    runner: CliRunner, researched: Path
) -> None:
    result = at(runner, researched, "cert", "run", HYP_ID, "--sha", "deadbee", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "no card" in payload(result)["error"]


def test_a_sha_that_is_not_a_sha_is_refused(runner: CliRunner, researched: Path) -> None:
    result = at(runner, researched, "cert", "run", HYP_ID, "--sha", "the-best-one", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "prefix" in payload(result)["error"]


def test_a_hypothesis_with_no_card_has_nothing_to_certify(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "cert", "run", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no best card" in payload(result)["error"]


# -- cert show ----------------------------------------------------------------------


def test_show_says_so_before_the_first_certification(runner: CliRunner, researched: Path) -> None:
    result = at(runner, researched, "cert", "show", HYP_ID, "--json")

    assert result.exit_code == Exit.OK
    assert payload(result) == {"id": HYP_ID, "certificate": None}


def test_show_is_the_newest_certificate(runner: CliRunner, researched: Path) -> None:
    made = payload(at(runner, researched, "cert", "run", HYP_ID, "--json"))

    result = at(runner, researched, "cert", "show", HYP_ID, "--json")

    document = payload(result)
    assert document["strategy_sha"] == made["strategy_sha"]
    assert document["verdict"] == made["verdict"]
    assert document["path"] == made["path"]


def test_show_reads_as_the_verdict_and_its_gates(runner: CliRunner, researched: Path) -> None:
    at(runner, researched, "cert", "run", HYP_ID)

    result = at(runner, researched, "cert", "show", HYP_ID)

    assert result.exit_code == Exit.OK
    assert HYP_ID in result.stdout
    assert "embargoed_window" in result.stdout


def test_show_of_something_that_is_not_a_hypothesis_is_refused(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "cert", "show", "not_a_hypothesis", "--json")

    assert result.exit_code == Exit.PRECONDITION


# -- the global option ---------------------------------------------------------------


def test_the_json_option_is_the_same_before_or_after_the_command(
    runner: CliRunner, researched: Path
) -> None:
    at(runner, researched, "cert", "plan", HYP_ID)

    result = at(runner, researched, "--json", "cert", "show", HYP_ID)

    assert payload(result) == {"id": HYP_ID, "certificate": None}


# -- the stall path -------------------------------------------------------------------


def test_a_stall_certifies_the_best_it_leaves_behind(runner: CliRunner, mocked_ws: Path) -> None:
    """The loop closes here: research runs until it stalls, and the stall certifies."""
    mocked.tuned(mocked_ws, stall_k=2)
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "research", "run", HYP_ID, "--json")

    outcome = payload(result)
    assert result.exit_code == Exit.OK, outcome
    assert outcome["reason"] == "stalled", "a keep, a discard and a crash is two misses"
    assert outcome["best_sha"], "the keep is the subject the stall certifies"
    shown = payload(at(runner, mocked_ws, "cert", "show", HYP_ID, "--json"))
    assert shown["strategy_sha"] == outcome["best_sha"]
    assert shown["verdict"] in ("pass", "fail")
    assert Path(shown["path"]).is_file()
    assert certificates(mocked_ws) == [Path(shown["path"])]


def test_a_stalled_hypothesis_comes_back_at_the_lower_priority(
    runner: CliRunner, mocked_ws: Path
) -> None:
    mocked.tuned(mocked_ws, stall_k=2)
    at(runner, mocked_ws, "research", "begin", HYP_ID)

    at(runner, mocked_ws, "research", "run", HYP_ID)

    queue = payload(at(runner, mocked_ws, "research", "status", "--json"))["queue"]
    assert [(item["id"], item["priority"]) for item in queue] == [(HYP_ID, -1)]


def test_a_stall_that_certified_nothing_still_comes_back(
    runner: CliRunner, mocked_ws: Path
) -> None:
    """No keep, no subject: the run ends, nothing is certified, the queue keeps it."""
    mocked.tuned(mocked_ws, stall_k=1)
    mocked.scripted(mocked_ws, propose=[mocked.proposal("boom")])
    at(runner, mocked_ws, "research", "begin", HYP_ID)

    at(runner, mocked_ws, "research", "run", HYP_ID)

    assert payload(at(runner, mocked_ws, "cert", "show", HYP_ID, "--json"))["certificate"] is None
    queue = payload(at(runner, mocked_ws, "research", "status", "--json"))["queue"]
    assert [(item["id"], item["priority"]) for item in queue] == [(HYP_ID, -1)]
