"""The embargo both ways: a screen never reads certification data, and certification never
judges on data a screen read for a shared instrument — whichever arrives first."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import HYP_ID, at, payload, write_hypothesis
from .test_screen_run import run_screen


def certifying(start: str) -> dict[str, dict[str, str]]:
    return {
        "research": {"start": "2024-01-02", "end": "2024-02-20"},
        "certification": {"start": start, "end": "2024-05-31"},
        "forward": {"start": "2024-06-03"},
    }


def test_a_hypothesis_certifying_on_screened_data_is_refused_at_validate_and_add(
    runner: CliRunner, loaded: Path
) -> None:
    run_screen(runner, loaded)
    path = write_hypothesis(loaded, windows=certifying("2024-03-25"))

    for command in ("validate", "add"):
        result = at(runner, loaded, "hyp", command, path, "--json")
        assert result.exit_code == Exit.VALIDATION, result.stdout
        error = payload(result)
        assert "screen ou_rev read on DEMO.SIM" in error["error"]
        assert "on or after 2024-03-30" in error["remedy"]


def test_a_certification_window_clear_of_the_embargo_after_a_screen_is_admitted(
    runner: CliRunner, loaded: Path
) -> None:
    run_screen(runner, loaded)
    # The screen read to 2024-03-29 and the embargo is a day: the 30th is kanso's own
    # earliest certification start, and the screen holds it back no further.
    path = write_hypothesis(loaded, windows=certifying("2024-03-30"))

    assert at(runner, loaded, "hyp", "validate", path).exit_code == Exit.OK
    late = write_hypothesis(loaded, windows=certifying("2024-03-29"))
    assert at(runner, loaded, "hyp", "validate", late).exit_code == Exit.VALIDATION


def test_a_hypothesis_on_other_instruments_is_not_held_to_a_screen_s_window(
    runner: CliRunner, loaded: Path
) -> None:
    run_screen(runner, loaded)
    with sqlite3.connect(loaded / "state.db") as connection:
        connection.execute("UPDATE screen_results SET instruments = '[\"ELSE.SIM\"]'")
    path = write_hypothesis(loaded, windows=certifying("2024-03-25"))

    assert at(runner, loaded, "hyp", "validate", path).exit_code == Exit.OK


def test_a_store_that_recorded_no_screen_holds_nothing_back(
    runner: CliRunner, loaded: Path
) -> None:
    path = write_hypothesis(loaded, windows=certifying("2024-03-25"))
    with sqlite3.connect(loaded / "state.db") as connection:
        connection.execute("DROP TABLE screen_results")
    assert at(runner, loaded, "hyp", "validate", path).exit_code == Exit.OK
    (loaded / "state.db").unlink()
    assert at(runner, loaded, "hyp", "validate", path).exit_code == Exit.OK
    assert HYP_ID in path.read_text(encoding="utf-8")
