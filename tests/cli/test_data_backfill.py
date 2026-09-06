"""`kanso data backfill`: history down to the floor, gaps closed, and nothing fetched twice."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import at, payload, write_instruments, write_spec

FULL = {"start": "2024-01-02", "end": "2024-03-29"}
LATE = {"start": "2024-03-01", "end": "2024-03-29"}
"""The source's whole history, and the tail of it a first load already holds."""


@pytest.fixture
def held(runner: CliRunner, workspace: Path) -> Path:
    """A workspace holding only the tail of the series the full spec can serve."""
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    spec = write_spec(workspace, "late.yaml", **LATE)
    assert (
        at(runner, workspace, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code
        == Exit.OK
    )
    write_spec(workspace, "full.yaml", **FULL)
    return workspace


def backfill(runner: CliRunner, root: Path, *args: object) -> dict[str, object]:
    spec = root / "full.yaml"
    result = at(
        runner, root, "data", "backfill", "--loader", "synthetic", "--spec", spec, *args, "--json"
    )
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def test_a_dry_run_prints_the_chunks_and_fetches_nothing(runner: CliRunner, held: Path) -> None:
    from kanso.data.manifest import manifests
    from kanso.workspace import find

    before = set(manifests(find(held)))

    document = backfill(runner, held, "--dry-run")

    assert document["dry_run"] is True
    assert document["requests"] == len(document["chunks"]) > 0
    assert document["rows"] == 0
    assert set(manifests(find(held))) == before
    chunk = document["chunks"][0]
    assert chunk["outcome"] == "planned"
    assert chunk["est_rows"] > 0
    assert chunk["est_bytes"] > 0


def test_a_dry_run_estimates_nothing_when_the_workspace_holds_nothing(
    runner: CliRunner, workspace: Path
) -> None:
    """An estimate needs a measured rate, and an empty workspace has none to measure."""
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    write_spec(workspace, "full.yaml", **FULL)

    document = backfill(runner, workspace, "--dry-run")

    assert document["est_bytes"] is None
    assert all(chunk["est_rows"] is None for chunk in document["chunks"])


def test_backfill_fills_the_history_before_what_is_held(runner: CliRunner, held: Path) -> None:
    document = backfill(runner, held)

    assert document["rows"] > 0
    assert all(chunk["outcome"] in {"written", "empty"} for chunk in document["chunks"])
    shown = payload(at(runner, held, "data", "show", "--json"))
    [series] = shown["series"]
    assert series["spans"] == [["2024-01-02", "2024-03-29"]]
    assert series["gaps"] == []


def test_a_repeated_backfill_fetches_nothing(runner: CliRunner, held: Path) -> None:
    """The manifest each chunk writes is its checkpoint, so a repeat finds nothing missing."""
    first = backfill(runner, held)
    assert first["rows"] > 0

    second = backfill(runner, held)

    assert second["requests"] == 0
    assert second["rows"] == 0
    assert any("nothing missing" in str(note) for note in second["notes"])


def test_a_chunk_the_source_serves_nothing_for_is_asked_once(
    runner: CliRunner, workspace: Path
) -> None:
    """A weekend is a legitimate empty answer, and paying for it twice is waste."""
    from kanso.state import StateStore

    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    for name, span in (
        ("early.yaml", {"start": "2024-01-02", "end": "2024-01-05"}),
        ("late.yaml", {"start": "2024-01-08", "end": "2024-01-12"}),
    ):
        spec = write_spec(workspace, name, **span)
        assert (
            at(runner, workspace, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code
            == Exit.OK
        )
    write_spec(workspace, "full.yaml", start="2024-01-02", end="2024-01-12")
    [series] = payload(at(runner, workspace, "data", "show", "--json"))["series"]
    assert series["gaps"] == [["2024-01-06", "2024-01-07"]]

    document = backfill(runner, workspace)

    assert [chunk["outcome"] for chunk in document["chunks"]] == ["empty"]
    with StateStore(workspace / "state.db") as store:
        recorded = store.events(kind="data_chunk_empty")
    assert len(recorded) == 1
    assert recorded[0].detail == {"start": "2024-01-06", "end": "2024-01-07"}

    again = backfill(runner, workspace)

    assert [chunk["outcome"] for chunk in again["chunks"]] == ["empty"]
    with StateStore(workspace / "state.db") as store:
        assert len(store.events(kind="data_chunk_empty")) == 1


def test_a_start_before_the_history_floor_is_clamped_and_reported(
    runner: CliRunner, held: Path
) -> None:
    """Reaching the floor is a normal outcome, never an error."""
    document = backfill(runner, held, "--from", "2020-01-01", "--dry-run")

    assert any("history floor" in str(clamp) for clamp in document["clamps"])
    assert all(chunk["start"] >= "2024-01-02" for chunk in document["chunks"])


def test_backfill_closes_a_gap_inside_what_is_held(runner: CliRunner, workspace: Path) -> None:
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    for name, span in (
        ("early.yaml", {"start": "2024-01-02", "end": "2024-01-31"}),
        ("late.yaml", {"start": "2024-03-01", "end": "2024-03-29"}),
    ):
        spec = write_spec(workspace, name, **span)
        assert (
            at(runner, workspace, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code
            == Exit.OK
        )
    write_spec(workspace, "full.yaml", **FULL)
    assert payload(at(runner, workspace, "data", "show", "--json"))["series"][0]["gaps"]

    document = backfill(runner, workspace)

    assert document["rows"] > 0
    [series] = payload(at(runner, workspace, "data", "show", "--json"))["series"]
    assert series["gaps"] == []


def test_backfill_reads_as_a_few_lines_for_a_human(runner: CliRunner, held: Path) -> None:
    result = at(
        runner,
        held,
        "data",
        "backfill",
        "--loader",
        "synthetic",
        "--spec",
        held / "full.yaml",
        "--dry-run",
    )

    assert result.exit_code == Exit.OK
    assert "estimated" in result.stdout
    assert "planned" in result.stdout


def test_a_malformed_date_is_a_validation_failure(runner: CliRunner, held: Path) -> None:
    result = at(
        runner,
        held,
        "data",
        "backfill",
        "--loader",
        "synthetic",
        "--spec",
        held / "full.yaml",
        "--from",
        "yesterday",
        "--json",
    )

    assert result.exit_code == Exit.VALIDATION
    assert "--from" in payload(result)["error"]


def test_an_explicit_end_bounds_the_backfill(runner: CliRunner, held: Path) -> None:
    document = backfill(runner, held, "--to", "2024-01-31")

    assert document["rows"] > 0
    [series] = payload(at(runner, held, "data", "show", "--json"))["series"]
    assert series["gaps"] == [["2024-02-01", "2024-02-29"]]


def test_an_end_past_an_interior_gap_plans_the_gap_once(runner: CliRunner, workspace: Path) -> None:
    """A gap inside the window and a gap inside what is held are one gap, asked for once."""
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    for name, span in (
        ("early.yaml", {"start": "2024-01-02", "end": "2024-01-31"}),
        ("late.yaml", {"start": "2024-03-01", "end": "2024-03-29"}),
    ):
        spec = write_spec(workspace, name, **span)
        assert (
            at(runner, workspace, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code
            == Exit.OK
        )
    write_spec(workspace, "full.yaml", **FULL)

    planned = backfill(runner, workspace, "--to", "2024-03-15", "--dry-run")
    document = backfill(runner, workspace, "--to", "2024-03-15")

    assert planned["requests"] == document["requests"] == 1
    [chunk] = document["chunks"]
    assert (chunk["start"], chunk["end"], chunk["outcome"]) == (
        "2024-02-01",
        "2024-02-29",
        "written",
    )
    assert document["rows"] > 0
    [series] = payload(at(runner, workspace, "data", "show", "--json"))["series"]
    assert series["spans"] == [["2024-01-02", "2024-03-29"]]
    assert backfill(runner, workspace, "--to", "2024-03-15")["requests"] == 0


def test_backfill_is_recorded_in_the_event_log(runner: CliRunner, held: Path) -> None:
    from kanso.state import StateStore

    backfill(runner, held)

    with StateStore(held / "state.db") as store:
        assert store.events(kind="data_backfilled")
