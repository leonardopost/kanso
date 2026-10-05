"""`kanso screen`: scaffolding a screen and validating it, free or bound."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import HYP_ID, INSTRUMENT, RESEARCH, at, payload

FREE: dict[str, Any] = {
    "schema": 1,
    "id": "demo_lag",
    "title": "DEMO against itself",
    "thesis": "One series leads a copy of itself by an hour.",
    "window": {"start": "2024-01-02", "end": "2024-03-29"},
    "legs": {
        "a": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"},
        "b": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"},
    },
    "clock": {"grid": "1h", "hours": "overlap"},
    "measures": [
        {"id": "lead_lag", "from": "a", "to": "b", "estimator": "grid", "lags": ["-1h", "1h"]}
    ],
}


def write_screen(root: Path, document: dict[str, Any] = FREE) -> Path:
    """A screen file in the workspace, under the directory its id names."""
    directory = root / "screens" / str(document["id"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "screen.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_new_scaffolds_a_free_screen_that_is_not_yet_admissible(
    runner: CliRunner, workspace: Path
) -> None:
    result = at(runner, workspace, "screen", "new", "btc_miners", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document == {
        "id": "btc_miners",
        "dir": str(workspace / "screens" / "btc_miners"),
        "hyp": None,
    }
    path = workspace / "screens" / "btc_miners" / "screen.yaml"
    text = path.read_text(encoding="utf-8")
    assert 'id: "btc_miners"' in text and "window: {start: , end: }" in text
    refused = at(runner, workspace, "screen", "validate", path, "--json")
    assert refused.exit_code == Exit.VALIDATION
    assert "title" in payload(refused)["error"]


def test_new_binds_a_screen_to_a_hypothesis(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "screen", "new", "mr_screen", "--hyp", HYP_ID)

    assert result.exit_code == Exit.OK
    assert "bound to demo_mr" in result.stdout
    text = (workspace / "screens" / "mr_screen" / "screen.yaml").read_text(encoding="utf-8")
    assert f"hyp: {HYP_ID}" in text and "window:" not in text


def test_new_refuses_an_id_that_is_not_one_and_a_directory_that_exists(
    runner: CliRunner, workspace: Path
) -> None:
    bad = at(runner, workspace, "screen", "new", "Not An Id", "--json")
    assert bad.exit_code == Exit.VALIDATION
    assert "a-z" in payload(bad)["remedy"]
    assert at(runner, workspace, "screen", "new", "twice").exit_code == Exit.OK
    again = at(runner, workspace, "screen", "new", "twice", "--json")
    assert again.exit_code == Exit.PRECONDITION
    assert "scaffolded once" in payload(again)["error"]


def test_validate_reports_the_lattice_and_changes_nothing(
    runner: CliRunner, workspace: Path
) -> None:
    path = write_screen(workspace)
    before = path.read_bytes()

    result = at(runner, workspace, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert document["id"] == "demo_lag"
    assert document["window"] == ["2024-01-02", "2024-03-29"]
    assert document["measures"] == [{"id": "lead_lag", "cells": 2}]
    assert document["cells"] == 2
    assert document["verdict"] is None
    assert len(document["sha"]) == 64
    assert path.read_bytes() == before
    human = at(runner, workspace, "screen", "validate", path)
    assert "lead_lag · grid · 2 cell(s)" in human.stdout
    assert "none declared" in human.stdout


def test_validate_refuses_a_file_in_another_screen_s_directory(
    runner: CliRunner, workspace: Path
) -> None:
    path = write_screen(workspace)
    moved = workspace / "screens" / "elsewhere"
    path.parent.rename(moved)

    result = at(runner, workspace, "screen", "validate", moved / "screen.yaml", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "the directory its id names" in payload(result)["error"]


def test_validate_refuses_a_file_that_is_not_text(runner: CliRunner, workspace: Path) -> None:
    path = write_screen(workspace)
    path.write_bytes(b"\xff\xfe not utf-8")

    result = at(runner, workspace, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "is not UTF-8 text" in payload(result)["error"]


def test_validate_refuses_a_parameter_outside_the_library_s_range(
    runner: CliRunner, workspace: Path
) -> None:
    lead_lag = {**FREE["measures"][0], "lags": ["1h", "25h"]}
    path = write_screen(workspace, {**FREE, "measures": [lead_lag]})

    result = at(runner, workspace, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "outside the lead_lag range" in payload(result)["error"]


def bound(**legs: Any) -> dict[str, Any]:
    document = {key: value for key, value in FREE.items() if key != "window"}
    return {**document, "id": "mr_screen", "hyp": HYP_ID, "legs": legs or FREE["legs"]}


def test_a_bound_screen_reads_its_hypothesis_s_research_window(
    runner: CliRunner, registered: Path
) -> None:
    path = write_screen(registered, bound())

    result = at(runner, registered, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    assert payload(result)["window"] == [str(RESEARCH[0]), str(RESEARCH[1])]
    assert payload(result)["hyp"] == HYP_ID


def test_a_bound_screen_needs_a_registered_hypothesis(runner: CliRunner, workspace: Path) -> None:
    path = write_screen(workspace, bound())

    result = at(runner, workspace, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "is not a registered hypothesis" in payload(result)["error"]


def test_a_bound_screen_measures_only_what_its_hypothesis_researches(
    runner: CliRunner, registered: Path
) -> None:
    cases = [
        (
            {"instrument": "OTHER.SIM", "type": "bar", "resolution": "1h"},
            "not in demo_mr's universe",
        ),
        ({"instrument": INSTRUMENT, "type": "quote"}, "does not require quote data"),
        (
            {"instrument": INSTRUMENT, "type": "bar", "resolution": "5m"},
            "where demo_mr researches 1h",
        ),
    ]
    for leg, message in cases:
        path = write_screen(registered, bound(a=FREE["legs"]["a"], b=leg))
        result = at(runner, registered, "screen", "validate", path, "--json")
        assert result.exit_code == Exit.VALIDATION, (leg, result.stdout)
        assert message in payload(result)["error"]
