"""`kanso data show` and `kanso data snapshot`: coverage, holes, and what gets frozen."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import FIRST, INSTRUMENT, LAST, at, payload, write_instruments, write_spec


def test_a_workspace_holding_nothing_says_so(runner: CliRunner, workspace: Path) -> None:
    result = at(runner, workspace, "data", "show", "--json")

    assert result.exit_code == Exit.OK
    assert payload(result) == {"series": [], "datasets": 0, "rows": 0}
    assert "none held" in at(runner, workspace, "data", "show").stdout


def test_show_reports_the_served_span_of_every_series(runner: CliRunner, loaded: Path) -> None:
    result = at(runner, loaded, "data", "show", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    [series] = document["series"]
    assert series["instrument"] == INSTRUMENT
    assert series["type"] == "bar"
    assert series["resolution"] == "1h"
    assert series["spans"] == [[str(FIRST), str(LAST)]]
    assert series["gaps"] == []
    assert series["rows"] == document["rows"] > 0


def test_show_reports_the_gap_between_two_loads(runner: CliRunner, workspace: Path) -> None:
    """Two loads either side of a month leave a hole, and a hole is what backfill closes."""
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    early = write_spec(workspace, "early.yaml", start="2024-01-02", end="2024-01-31")
    late = write_spec(workspace, "late.yaml", start="2024-03-01", end="2024-03-29")
    for spec in (early, late):
        assert (
            at(runner, workspace, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code
            == Exit.OK
        )

    document = payload(at(runner, workspace, "data", "show", "--json"))

    [series] = document["series"]
    assert series["gaps"] == [["2024-02-01", "2024-02-29"]]
    assert document["datasets"] == 2
    assert "gap 2024-02-01..2024-02-29" in at(runner, workspace, "data", "show").stdout


def test_snapshot_freezes_what_is_held_and_records_it(runner: CliRunner, loaded: Path) -> None:
    from kanso.state import StateStore

    result = at(runner, loaded, "data", "snapshot", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert len(document["snapshot_id"]) == 64
    assert document["datasets"]
    assert document["reproducible"] is True
    assert (loaded / "catalog" / "snapshots" / f"{document['snapshot_id']}.yaml").is_file()
    with StateStore(loaded / "state.db") as store:
        rows = store.connection.execute("SELECT * FROM snapshots").fetchall()
    assert [row["snapshot_id"] for row in rows] == [document["snapshot_id"]]


def test_freezing_twice_is_one_row_because_a_snapshot_is_its_content(
    runner: CliRunner, loaded: Path
) -> None:
    from kanso.state import StateStore

    first = payload(at(runner, loaded, "data", "snapshot", "--json"))
    second = payload(at(runner, loaded, "data", "snapshot", "--json"))

    assert first["snapshot_id"] == second["snapshot_id"]
    with StateStore(loaded / "state.db") as store:
        assert store.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_snapshot_reads_as_three_lines_for_a_human(runner: CliRunner, loaded: Path) -> None:
    result = at(runner, loaded, "data", "snapshot")

    assert result.exit_code == Exit.OK
    assert "reproducible" in result.stdout
    assert "instrument" in result.stdout
