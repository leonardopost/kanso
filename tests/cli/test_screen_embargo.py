"""The embargo both ways: a screen never reads certification data, and certification never
judges on data a screen read for a shared instrument — whichever arrives first."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit
from kanso.schemas import parse_yaml
from kanso.schemas.screen import ScreenResult

from .conftest import HYP_ID, OTHER, at, payload, write_hypothesis
from .test_screen import write_screen
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


SIGNALLED: dict[str, object] = {
    "schema": 1,
    "id": "signalled",
    "title": "The demo path conditioned on a tape filed under the other name",
    "thesis": "The demo name's sessions differ while a tape filed elsewhere is above zero.",
    "window": {"start": "2024-01-02", "end": "2024-03-29"},
    "legs": {"a": {"instrument": "DEMO.SIM", "type": "bar", "resolution": "1h"}},
    "signals": {"regime": {"type": "kanso_state_tape", "field": "level", "instrument": OTHER}},
    "clock": {"hours": {"tz": "America/New_York", "span": "09:30-16:01"}},
    "costs": {
        "SIM": {"commission_bps": 0.5, "slippage_bps": 1, "spread": "fixed_bps", "fixed_bps": 2}
    },
    "measures": [
        {
            "id": "condition",
            "tests": {
                "up": {
                    "signal": "regime",
                    "when": {"gt": 0},
                    "against": {"lt": 0},
                    "entry": "open",
                    "exit": "close",
                    "hold": "while",
                    "side": "long",
                }
            },
            "followers": "a",
            "payoff": "contrast",
            "null_by": "circular",
            "demean": [],
            "controls": [],
            "latency_ms": 900000,
        }
    ],
}
"""A condition screen whose follower is the demo name and whose only reading of the other name
is the signal it conditions on, filed under it."""


def test_an_instrument_read_only_for_a_signal_is_held_to_the_embargo(
    runner: CliRunner, taped: Path
) -> None:
    document = run_screen(runner, taped, SIGNALLED)  # type: ignore[arg-type]
    rendered = parse_yaml(ScreenResult, Path(document["result"]).read_text(encoding="utf-8"))
    assert ("kanso_state_tape", ["regime"]) in [(item.type, item.legs) for item in rendered.series]
    (cell,) = rendered.cells
    assert cell.sessions > 0, "the follower was conditioned on the signal filed elsewhere"
    path = write_hypothesis(taped, id="othr_mr", universe=[OTHER], windows=certifying("2024-03-25"))

    result = at(runner, taped, "hyp", "validate", path, "--json")

    assert result.exit_code == Exit.VALIDATION, result.stdout
    assert f"screen signalled read on {OTHER}" in payload(result)["error"]
    with sqlite3.connect(taped / "state.db") as connection:
        (instruments,) = connection.execute("SELECT instruments FROM screen_results").fetchone()
    assert instruments == f'["DEMO.SIM", "{OTHER}"]'


def test_a_free_screen_meeting_a_certification_span_through_a_signal_is_refused(
    runner: CliRunner, taped: Path
) -> None:
    path = write_hypothesis(taped, id="othr_mr", universe=[OTHER])
    assert at(runner, taped, "hyp", "add", path).exit_code == Exit.OK
    late = {**SIGNALLED, "window": {"start": "2024-01-02", "end": "2024-04-30"}}

    result = at(runner, taped, "screen", "run", write_screen(taped, late), "--json")

    assert result.exit_code == Exit.PRECONDITION, result.stdout
    assert "othr_mr certifies on" in payload(result)["error"]
    assert payload(result)["error"].endswith(
        "on OTHR.SIM; a screen chooses ideas, and the data that judges one may not have chosen it"
    )
