"""`condition` through `kanso screen validate`, `run`, `show` and `draft`: a state read off the
state tape, the synthetic custom type a workspace extension registers, scored on two hourly names.

The tape's level is a slow wave the synthetic paths know nothing about, so what these tests read
is the pipeline — the signal read as of each decision, the calendar demeaning, the null, the
result's shape and its rendering — and the unit suite reads the arithmetic.
"""

from __future__ import annotations

import dataclasses
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from kanso.data.closures import US_EQUITY
from kanso.data.snapshot import snapshots
from kanso.errors import Exit
from kanso.schemas import parse_yaml
from kanso.schemas.screen import Cell, ScreenResult
from kanso.screen.condition import CIRCULAR
from kanso.workspace import find

from .conftest import (
    INSTRUMENT,
    OTHER,
    SYMBOL,
    TAPE_FIRST,
    TAPE_LAST,
    VENUE,
    at,
    payload,
    tape_flag,
    tape_level,
)
from .test_screen import write_screen
from .test_screen_run import run_screen

UP: dict[str, Any] = {
    "signal": "regime",
    "when": {"gt": 0},
    "against": {"lt": 0},
    "entry": "open",
    "exit": "close",
    "hold": "while",
    "side": "long",
}

STATE: dict[str, Any] = {
    "schema": 1,
    "id": "tape_state",
    "title": "The demo names against the state tape",
    "thesis": "The names' session returns differ while the tape's level is above zero.",
    "window": {"start": "2024-01-02", "end": "2024-03-29"},
    "legs": {
        "a": {"instrument": INSTRUMENT, "type": "bar", "resolution": "1h"},
        "b": {"instrument": OTHER, "type": "bar", "resolution": "1h"},
    },
    "groups": {"names": ["a", "b"]},
    "signals": {
        "regime": {"type": "kanso_state_tape", "field": "level", "instrument": INSTRUMENT},
        "event_day": {"type": "kanso_state_tape", "field": "flag", "instrument": INSTRUMENT},
    },
    "clock": {"hours": {"tz": "America/New_York", "span": "09:30-16:01"}},
    "costs": {
        "SIM": {"commission_bps": 0.5, "slippage_bps": 1, "spread": "fixed_bps", "fixed_bps": 2}
    },
    "measures": [
        {
            "id": "condition",
            "tests": {"up": UP, "up_cc": {**UP, "entry": "close", "hold": {"sessions": 2}}},
            "followers": "names",
            "payoff": "contrast",
            "null_by": "circular",
            "demean": ["weekday", {"turn_of_month": [1, 2]}, "pre_holiday"],
            "controls": ["event_day"],
            "latency_ms": 900000,
            "mde_power": 0.8,
        }
    ],
    "verdict": {"alpha": 0.1, "min_margin_bp": 0, "min_events_per_day": 0, "min_sessions": 40},
}

FLAT: dict[str, Any] = {
    **STATE,
    "id": "tape_flat",
    "measures": [
        {
            **STATE["measures"][0],
            "tests": {"up": {**UP, "against": None}},
            "followers": "a",
            "payoff": "long_flat",
            "null_by": "sign_flip",
            "block": 5,
            "demean": [],
            "controls": [],
            "mde_power": None,
        }
    ],
}
"""The same tape long or flat, judged by sign flips of five-session blocks, with no verdict."""
FLAT.pop("verdict")

CELL = "condition/up/a/circular"


def shown(runner: CliRunner, root: Path, screen: str, key: str) -> Cell:
    result = at(runner, root, "screen", "show", screen, "--cell", key, "--json")
    assert result.exit_code == Exit.OK, result.stdout
    return Cell.model_validate(yaml.safe_load(result.stdout))


def test_validate_counts_tests_by_followers_and_plans_the_tape(
    runner: CliRunner, taped: Path
) -> None:
    path = write_screen(taped, STATE)

    result = at(runner, taped, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert document["cells"] == 4
    assert document["measures"] == [{"id": "condition", "cells": 4}]
    assert [item["type"] for item in document["data"]] == ["bar", "bar", "kanso_state_tape"]
    human = at(runner, taped, "screen", "validate", path).stdout
    assert "condition · contrast · circular · 4 cell(s)" in human


def test_a_null_spelt_null_is_refused_by_its_place_as_input(runner: CliRunner, taped: Path) -> None:
    measure = {
        (None if key == "null_by" else key): value for key, value in STATE["measures"][0].items()
    }
    path = write_screen(taped, {**STATE, "id": "tape_null", "measures": [measure]})
    assert "\n  null: circular\n" in path.read_text(encoding="utf-8")

    result = at(runner, taped, "screen", "validate", path, "--json")

    assert result.exit_code == Exit.VALIDATION, result.stdout
    assert "measures.0: the key null is read by YAML as no value" in payload(result)["error"]
    assert "quote the key" in payload(result)["remedy"]


def test_a_run_reads_the_tape_as_of_each_decision_and_judges_by_shifts(
    runner: CliRunner, taped: Path
) -> None:
    document = run_screen(runner, taped, STATE)

    assert document["cells"] == 4 and document["verdict"]["declared"] is True
    rendered = parse_yaml(ScreenResult, Path(document["result"]).read_text(encoding="utf-8"))
    assert rendered.assumptions == [CIRCULAR] and rendered.assumption is None
    assert [(item.type, item.legs) for item in rendered.series] == [
        ("bar", ["a"]),
        ("bar", ["b"]),
        ("kanso_state_tape", ["regime", "event_day"]),
    ]
    cell = shown(runner, taped, "tape_state", CELL)
    found = cell.condition
    assert found is not None and cell.response is not None
    assert found.null_by == "circular" and found.exact is True
    assert found.shifts is not None and found.shifts > 0
    assert 0 < found.exposure <= 1 and found.sessions_when > 0 and found.sessions_against
    # The decision is 15 minutes before the 09:30 open; the tape is published at 13:00 UTC.
    assert 0 < found.signal_age_median_s < 24 * 3600
    assert found.critical_t is not None and found.mde_bp is not None
    assert found.long_flat_bp is not None and found.control_r2 is not None
    assert cell.response.unfilled == 0 and cell.response.hurdle_bp > 0
    # Each cell is judged as a cell that trades is: thin under the verdict's sessions, else on
    # its p, on its positions' margin and events a day, and on its mean's being on the declared
    # side, its p being two-sided. The fixed holds, flat between onsets, contrast fewer
    # sessions than the verdict asks.
    for item in rendered.cells:
        assert item.response is not None
        missed = [
            item.p > 0.1,
            item.mean <= 0,
            item.response.margin_bp < 0,
            item.response.events_per_day < 0,
        ]
        expected = "thin" if item.sessions < 40 else "fail" if any(missed) else "pass"
        assert item.judged == expected, item.key
        assert (item.reason is None) == (expected == "pass")
        if expected != "thin":
            against = "against the declared side" in (item.reason or "")
            assert against == (item.mean <= 0), item.key
    assert [item.judged == "thin" for item in rendered.cells] == [False, False, True, True]
    held = shown(runner, taped, "tape_state", "condition/up_cc/b/circular")
    assert held.response is not None and held.response.unfilled == 1, "no close before the first"
    again = run_screen(runner, taped, STATE)
    assert again["stored"] is True


HOLD: dict[str, Any] = {
    **STATE,
    "id": "tape_hold",
    "measures": [
        {
            **STATE["measures"][0],
            "tests": {"up": {**UP, "entry": "close", "against": "rest"}, "up_oc": UP},
            "benchmark": "hold",
        }
    ],
}
"""The tape's tests set against holding each name: always in the market close to close, and
open to close in either state, flat in neither."""


def test_a_run_against_the_hold_judges_each_cell_by_its_sharpe_over_the_hold(
    runner: CliRunner, taped: Path
) -> None:
    path = write_screen(taped, HOLD)
    human = at(runner, taped, "screen", "validate", path).stdout
    assert "condition · contrast · circular · against the hold · 4 cell(s)" in human

    document = run_screen(runner, taped, HOLD)

    rendered = parse_yaml(ScreenResult, Path(document["result"]).read_text(encoding="utf-8"))
    for item in rendered.cells:
        assert item.condition is not None and item.response is not None
        hold = item.condition.hold
        assert hold is not None and item.params["benchmark"] == "hold"
        assert 0 < hold.p <= 1 and hold.annualisation > 0 and hold.critical is not None
        assert len(hold.folds) == rendered.folds
        missed = [hold.p > 0.1, hold.p_above_hold > 0.1, item.response.margin_bp < 0]
        expected = "thin" if item.sessions < 40 else "fail" if any(missed) else "pass"
        assert item.judged == expected, item.key
        assert "against the declared side" not in (item.reason or ""), "the contrast is not judged"
    names = {item.params["follower"]: item.condition.hold for item in rendered.cells[:2]}  # type: ignore[union-attr]
    assert names["a"].sharpe_hold != names["b"].sharpe_hold, "each name's own hold"  # type: ignore[union-attr]
    detail = at(runner, taped, "screen", "show", "tape_hold", "--cell", CELL).stdout
    assert "against the hold's" in detail and "hold folds" in detail
    assert "against random timing" in detail and "p above the hold" in detail
    assert "sessions a year" in detail


@pytest.mark.parametrize(
    ("calendar", "held"),
    [
        (None, "none is on file"),
        (
            dataclasses.replace(US_EQUITY, last=date(2024, 3, 15)),
            "the one on file holds 2003-09-10..2024-03-15",
        ),
    ],
)
def test_a_calendar_control_no_calendar_on_file_covers_is_refused_before_anything_is_measured(
    runner: CliRunner,
    taped: Path,
    monkeypatch: pytest.MonkeyPatch,
    calendar: Any,
    held: str,
) -> None:
    monkeypatch.setattr("kanso.screen.condition._calendar", lambda definition: calendar)

    result = at(runner, taped, "screen", "run", write_screen(taped, STATE), "--json")

    assert result.exit_code == Exit.PRECONDITION, result.stdout
    assert (
        "measures.0.demean: turn_of_month and pre_holiday read a's market calendar over every "
        f"month the window touches, 2024-01-01..2024-03-31, and {held}"
    ) in payload(result)["error"]
    assert "supply them as 0/1 controls" in payload(result)["remedy"]


def test_a_refused_control_is_reloaded_by_the_superseding_load_and_the_snapshot_its_remedy_names(
    runner: CliRunner, taped: Path
) -> None:
    from tests.screen.conftest import TAPE_ID

    odd = date(2024, 2, 1)

    def written(flagged: int) -> Path:
        """The tape filed under the one name alone, its flag `flagged` on `odd`."""
        rows = ["t,level,flag,switch,label"]
        day = TAPE_FIRST
        while day <= TAPE_LAST:
            flag = flagged if day == odd else tape_flag(day)
            rows.append(f"{day} 13:00:00,{tape_level(day)},{flag},{'true' if flag else 'false'},x")
            day = date.fromordinal(day.toordinal() + 1)
        (taped / "one.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        columns = {name: name for name in ("level", "flag", "switch", "label")}
        source = {"path": str(taped / "one.csv"), "instrument": SYMBOL, "venue": VENUE}
        spec = taped / "one.yaml"
        files = [{**source, "type": TAPE_ID, "columns": {"ts_event": "t", **columns}}]
        spec.write_text(
            yaml.safe_dump({"loader": "csv_parquet", "timezone": "UTC", "files": files}),
            encoding="utf-8",
        )
        return spec

    def dataset() -> str:
        series = payload(at(runner, taped, "data", "show", "--json"))["series"]
        (held,) = (
            item["datasets"]
            for item in series
            if item["type"] == TAPE_ID and item["instrument"] == INSTRUMENT
        )
        (only,) = held
        return str(only["dataset_id"])

    def loaded(spec: Path, *flags: str) -> Any:
        return at(runner, taped, "data", "load", "--loader", "csv_parquet", "--spec", spec, *flags)

    def newest() -> str:
        return max(snapshots(find(taped)), key=lambda item: item.created_at).snapshot_id

    assert loaded(written(2), "--supersedes", dataset()).exit_code == Exit.OK
    pinned = newest()
    path = write_screen(taped, STATE)

    refused = at(runner, taped, "screen", "run", path, "--json")

    assert refused.exit_code == Exit.PRECONDITION, refused.stdout
    assert "controls.event_day: 2 on 2024-02-01; a control is 0 or 1" in payload(refused)["error"]
    remedy = payload(refused)["remedy"]
    assert f"reload {TAPE_ID} under {INSTRUMENT} one dataset at a time" in remedy
    assert "`kanso data show`" in remedy
    assert "kanso data load --loader csv_parquet --spec <spec> --supersedes <dataset>" in remedy
    assert "then run `kanso data snapshot`" in remedy
    # The run pinned the dataset before it read the point, so a replace over it is refused and
    # the superseding load the remedy names is what puts the fixed tape in its place. The fixed
    # tape is not the one first loaded: its flag on that day is the other control value.
    assert tape_flag(odd) == 0
    fixed = written(1)
    assert loaded(fixed, "--replace").exit_code == Exit.PRECONDITION
    assert loaded(fixed, "--supersedes", dataset()).exit_code == Exit.OK
    # Each load kept the dataset's id, so the snapshot taken before either still covers the
    # series: without a newer one the run pins it again, its checksums those of the tape first
    # loaded, and a second run over the same pins hands that result back unread.
    stale = run_screen(runner, taped, STATE)
    assert stale["snapshot"] == pinned and stale["stored"] is False
    assert run_screen(runner, taped, STATE)["stored"] is True
    assert at(runner, taped, "data", "snapshot").exit_code == Exit.OK
    again = run_screen(runner, taped, STATE)
    assert again["snapshot"] != pinned and again["stored"] is False


def test_two_workspaces_holding_the_same_bytes_measure_the_same_result(
    runner: CliRunner, taped: Path, tmp_path: Path
) -> None:
    other = tmp_path / "again"
    shutil.copytree(taped, other, symlinks=True)

    documents = [run_screen(runner, root, STATE) for root in (taped, other)]

    assert [document["stored"] for document in documents] == [False, False]
    one, two = (
        parse_yaml(ScreenResult, Path(document["result"]).read_text(encoding="utf-8"))
        for document in documents
    )
    # Each workspace read its own catalog, drew its own null and kept its own clock; only the
    # clock may differ.
    volatile = {"created_at", "wall_s", "peak_mem_gb"}
    assert one.model_dump_json(exclude=volatile) == two.model_dump_json(exclude=volatile)
    assert any(cell.condition is not None and cell.condition.exact for cell in one.cells)


def test_show_renders_a_condition_cell_and_its_detail(runner: CliRunner, taped: Path) -> None:
    run_screen(runner, taped, STATE)
    run_screen(runner, taped, FLAT)

    listed = at(runner, taped, "screen", "show", "tape_state").stdout
    detail = at(runner, taped, "screen", "show", "tape_state", "--cell", CELL).stdout
    flat = at(runner, taped, "screen", "show", "tape_flat", "--cell", "condition/up/a/sign_flip")

    assert "condition/up/a/circular  mean" in listed and "exposure" in listed
    for label in ("null", "states", "demeaned", "long_flat", "adjusted", "mde", "signal age"):
        assert label in detail, label
    exact = shown(runner, taped, "tape_state", CELL).condition
    assert exact is not None and exact.exact is True
    assert f"circular shifts, {exact.shifts} of them · exact" in detail and "against" in detail
    assert flat.exit_code == Exit.OK
    assert "sign flips of" in flat.stdout and "long_flat" not in flat.stdout
    assert "mde" not in flat.stdout
    rendered = Cell.model_validate(
        yaml.safe_load(
            at(
                runner,
                taped,
                "screen",
                "show",
                "tape_flat",
                "--cell",
                "condition/up/a/sign_flip",
                "--json",
            ).stdout
        )
    )
    assert rendered.condition is not None and rendered.condition.blocks
    assert rendered.condition.sessions_against is None and rendered.condition.shifts is None


def test_a_drawn_circular_null_says_how_many_of_its_shifts_it_drew(
    runner: CliRunner, taped: Path
) -> None:
    from kanso.cli.screen import _condition_lines

    run_screen(runner, taped, STATE)
    found = shown(runner, taped, "tape_state", CELL).condition
    assert found is not None and found.exact is True and found.shifts is not None

    # The workspace's window has fewer shifts than the 99 draws a workspace may set at the
    # least, so the drawn case is the same cell's statistics with more shifts than draws.
    drawn = found.model_copy(update={"shifts": 1249, "exact": False})

    assert f"circular shifts, {found.shifts} of them · exact" in _condition_lines(found, 999)[0]
    assert "circular shifts, 999 of 1249 · drawn" in _condition_lines(drawn, 999)[0]


def test_a_condition_cell_is_not_drafted(runner: CliRunner, taped: Path) -> None:
    run_screen(runner, taped, STATE)

    result = at(
        runner,
        taped,
        "screen",
        "draft",
        "tape_state",
        "--cell",
        CELL,
        "--as",
        "tape_lane",
        "--certify",
        "2024-04-15..2024-05-31",
        "--json",
    )

    assert result.exit_code == Exit.VALIDATION, result.stdout
    assert "is a condition cell" in payload(result)["error"]
    assert "`kanso hyp new tape_lane`" in payload(result)["remedy"]
    assert not (taped / "hypotheses" / "tape_lane").exists()
