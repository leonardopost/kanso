"""`kanso classify`: what one call decides, and what it leaves in the file.

The classification is the one answer that directs every run of a hypothesis, so what these
tests assert is not that a call was made but that what came back was checked, written and
re-pinned — and that a hypothesis kanso cannot run is still classified honestly rather than
refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from . import mocked
from .conftest import DRAFT, HYP_ID, at, certify, payload, write_hypothesis


def draft(runner: CliRunner, root: Path, **changes: Any) -> Path:
    """Register the unclassified idea, so `classify` has something to decide about."""
    path = write_hypothesis(root, mocked.SEED, base=DRAFT, **changes)
    assert at(runner, root, "hyp", "add", path).exit_code == Exit.OK
    return path


def document(root: Path) -> dict[str, Any]:
    text = (root / "hypotheses" / HYP_ID / "hypothesis.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return parsed


def test_classify_writes_the_three_keys_into_the_file_and_re_pins_it(
    runner: CliRunner, loaded: Path
) -> None:
    draft(runner, loaded)
    mocked.scripted(loaded, classify=[mocked.CLASSIFICATION])

    result = at(runner, loaded, "classify", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    answer = payload(result)
    assert answer["construct"]["id"] == "sleeve"
    assert answer["runnable"] is True
    assert answer["objective"]["params"] == {"min_delta": 0.0, "k_se": 0.5}
    assert [item["id"] for item in answer["constraints"]] == ["strategy_integrity", "min_trades"]

    on_disk = document(loaded)
    assert on_disk["construct"]["id"] == "sleeve"
    assert on_disk["objective"]["id"] == answer["objective"]["id"]
    shown = payload(at(runner, loaded, "hyp", "show", HYP_ID, "--json"))
    assert shown["status"] == "classified"
    # Re-pinned: the registration is the bytes the classification left on disk.
    assert shown["pinned"] is True


def test_classify_reads_as_the_decision_and_the_next_command(
    runner: CliRunner, loaded: Path
) -> None:
    draft(runner, loaded)
    mocked.scripted(loaded, classify=[mocked.CLASSIFICATION])

    result = at(runner, loaded, "classify", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "sleeve" in result.stdout
    assert "net_edge_bps" in result.stdout
    assert mocked.CLASSIFICATION["rationale"] in result.stdout
    assert f"kanso research begin {HYP_ID}" in result.stdout


def test_a_construct_this_build_cannot_run_is_classified_and_says_so(
    runner: CliRunner, loaded: Path
) -> None:
    certify(loaded)
    draft(runner, loaded)
    seam = {
        **mocked.CLASSIFICATION,
        "construct": {"id": "allocation", "host": "portfolio"},
        "objective": "marginal_net_edge_bps",
    }
    mocked.scripted(loaded, classify=[seam, seam])

    result = at(runner, loaded, "classify", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    answer = payload(result)
    assert answer["construct"]["id"] == "allocation"
    assert answer["runnable"] is False
    human = at(runner, loaded, "classify", HYP_ID)
    assert "not runnable in this build" in human.stdout


CONTRIBUTION: dict[str, Any] = {
    "id": "wf_contribution_bps",
    "params": {"min_delta": 0.5, "k_se": 1.0},
}
GUARD: list[dict[str, Any]] = [{"id": "edge_per_trade", "params": {"min_bps": 0.0, "k_se": 1.0}}]


def test_an_objective_the_file_states_is_kept_and_the_report_says_so(
    runner: CliRunner, loaded: Path
) -> None:
    draft(runner, loaded, objective=CONTRIBUTION, required_constraints=GUARD)
    mocked.scripted(loaded, classify=[mocked.CLASSIFICATION, mocked.CLASSIFICATION])

    result = at(runner, loaded, "classify", HYP_ID, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    answer = payload(result)
    assert answer["objective"] == CONTRIBUTION
    assert answer["stated_objective"] == {"id": "wf_contribution_bps", "kept": True, "reason": None}
    assert document(loaded)["objective"] == CONTRIBUTION
    human = at(runner, loaded, "classify", HYP_ID)
    assert "wf_contribution_bps · min_delta 0.5 · k_se 1.0 · stated in the file, kept" in (
        human.stdout
    )
    assert "replaced" not in human.stdout


def test_an_objective_classify_replaces_is_named_with_the_reason(
    runner: CliRunner, loaded: Path
) -> None:
    """The draft names a relative objective, and the model answers a sleeve, which is
    measured on its own: the stated one cannot score it, so it is replaced, and said to be."""
    stated = {"id": "marginal_wf_contribution_bps", "params": {"min_delta": 0.0, "k_se": 1.0}}
    draft(runner, loaded, objective=stated)
    mocked.scripted(loaded, classify=[mocked.CLASSIFICATION])

    result = at(runner, loaded, "classify", HYP_ID)

    assert result.exit_code == Exit.OK, result.stdout
    assert (
        "replaced   marginal_wf_contribution_bps: marginal_wf_contribution_bps is a relative "
        "objective, and a sleeve is measured absolute"
    ) in result.stdout
    assert document(loaded)["objective"]["id"] == "net_edge_bps"


def test_a_classification_with_nothing_stated_reports_none(runner: CliRunner, loaded: Path) -> None:
    draft(runner, loaded)
    mocked.scripted(loaded, classify=[mocked.CLASSIFICATION])

    assert payload(at(runner, loaded, "classify", HYP_ID, "--json"))["stated_objective"] is None


def test_an_answer_outside_the_catalogue_takes_the_ladder_and_then_refuses(
    runner: CliRunner, loaded: Path
) -> None:
    draft(runner, loaded)
    mocked.scripted(loaded, classify=[{**mocked.CLASSIFICATION, "construct": {"id": "nonesuch"}}])

    result = at(runner, loaded, "classify", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "no model answered in a usable shape" in payload(result)["error"]
    # Nothing half-valid reached the file.
    assert "construct" not in document(loaded)


def test_classifying_a_hypothesis_with_an_active_run_is_refused(
    runner: CliRunner, mocked_ws: Path
) -> None:
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK

    result = at(runner, mocked_ws, "classify", HYP_ID, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "active run" in payload(result)["error"]
