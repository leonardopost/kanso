"""`kanso data backfill`: history down to the floor, gaps closed, and nothing fetched twice."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import CHUNK_EDGES, INSTRUMENT, at, payload, write_instruments, write_spec

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


def backfill(
    runner: CliRunner,
    root: Path,
    *args: object,
    loader: str = "synthetic",
    spec: str = "full.yaml",
) -> dict[str, Any]:
    result = at(
        runner, root, "data", "backfill", "--loader", loader, "--spec", root / spec, *args, "--json"
    )
    assert result.exit_code == Exit.OK, result.stdout
    return payload(result)


def shown(runner: CliRunner, root: Path) -> list[dict[str, Any]]:
    """Every series `data show --json` reports."""
    series: list[dict[str, Any]] = payload(at(runner, root, "data", "show", "--json"))["series"]
    return series


def outcomes(document: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Each chunk a backfill planned or fetched, as its first day, last day and outcome."""
    return [(chunk["start"], chunk["end"], chunk["outcome"]) for chunk in document["chunks"]]


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

    assert again["chunks"] == []
    assert any("nothing missing" in str(note) for note in again["notes"])
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


# -- the days a source answers empty -------------------------------------------------


def test_a_weekend_at_a_chunk_edge_is_a_gap_until_the_source_answers_it_empty(
    runner: CliRunner, chunked: Path
) -> None:
    """The spans either side of a weekend are a weekend apart, and no session is missing."""
    [before] = shown(runner, chunked)
    assert before["gaps"] == CHUNK_EDGES
    assert before["empty"] == []

    again = backfill(runner, chunked, spec="data.yaml")

    assert outcomes(again) == [(start, end, "empty") for start, end in CHUNK_EDGES]
    [after] = shown(runner, chunked)
    assert after["spans"] == before["spans"]
    assert after["empty"] == CHUNK_EDGES
    assert after["gaps"] == []


def test_a_day_answered_empty_is_never_planned_again(runner: CliRunner, chunked: Path) -> None:
    """Neither as a gap nor inside a window an explicit end reaches past, dry run or not."""
    backfill(runner, chunked, spec="data.yaml")

    for args in ((), ("--dry-run",), ("--to", "2024-04-15", "--dry-run")):
        document = backfill(runner, chunked, *args, spec="data.yaml")
        assert document["chunks"] == [], args
        assert any("nothing missing" in str(note) for note in document["notes"]), args


def test_the_days_of_a_gap_nobody_answered_are_still_planned(
    runner: CliRunner, chunked: Path
) -> None:
    """An answer for the Sunday alone closes the Sunday, and the Saturday is still asked for."""
    from kanso.data.manifest import EMPTY_CHUNK, series_subject
    from kanso.state import StateStore

    with StateStore(chunked / "state.db") as store:
        store.event(
            EMPTY_CHUNK,
            series_subject((INSTRUMENT, "bar", "1h")),
            {"start": "2024-03-03", "end": "2024-03-03"},
        )

    [series] = shown(runner, chunked)
    assert series["empty"] == [["2024-03-03", "2024-03-03"]]
    assert series["gaps"] == [["2024-03-02", "2024-03-02"], ["2024-03-30", "2024-03-31"]]
    for args in (("--dry-run",), ("--to", "2024-04-15", "--dry-run")):
        planned = backfill(runner, chunked, *args, spec="data.yaml")
        assert outcomes(planned) == [
            ("2024-03-02", "2024-03-02", "planned"),
            ("2024-03-30", "2024-03-31", "planned"),
        ], args


SOURCE = '''\
"""A synthetic source with the holidays and the page limit a real one has."""

from kanso.data.loader import utc_day
from kanso.data.loaders.synthetic import SyntheticLoader

PROVIDES = {{"loaders": ("{name}",)}}

CLOSED = frozenset({closed!r})
"""The days this source holds nothing for, as it would for an exchange holiday."""

PAGE = {page}
"""The most sessions one request is served, or 0 for as many as it asks for."""


class Source:
    id = "{name}"

    def discover(self, spec):
        return SyntheticLoader().discover(dict(spec, loader="synthetic"))

    def load(self, ref, window):
        served, days = [], set()
        for point in SyntheticLoader().load(ref, window):
            day = utc_day(point.ts_event)
            if str(day) in CLOSED:
                continue
            if day not in days and PAGE and len(days) == PAGE:
                break
            days.add(day)
            served.append(point)
        return served

    def load_arrow(self, ref, window):
        return None

    def manifest(self, ref):
        return SyntheticLoader().manifest(ref)


LOADERS = {{"{name}": Source()}}
'''


def a_source(
    root: Path, name: str, *, closed: tuple[str, ...] = (), page: int = 0, **span: str
) -> str:
    """Install that source as a workspace extension, with a spec for it as `full.yaml`."""
    package = root / "kanso_ext" / name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        SOURCE.format(name=name, closed=closed, page=page), encoding="utf-8"
    )
    write_spec(root, "full.yaml", loader=name, **span)
    return name


@pytest.fixture
def resolved(runner: CliRunner, workspace: Path) -> Path:
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    return workspace


@pytest.mark.usefixtures("_leave_the_interpreter_as_found")
def test_a_holiday_at_a_chunk_edge_is_answered_like_a_weekend(
    runner: CliRunner, resolved: Path
) -> None:
    """Memorial Day opens the third chunk, so the series breaks from Saturday to Monday."""
    from kanso.data import snapshot
    from kanso.schemas.hypothesis import Windows
    from kanso.state import StateStore
    from kanso.workspace import find

    loader = a_source(
        resolved, "holidays", closed=("2024-05-27",), start="2024-03-28", end="2024-06-25"
    )
    edges = [["2024-04-27", "2024-04-28"], ["2024-05-25", "2024-05-27"]]
    windows = Windows.model_validate(
        {
            "research": {"start": "2024-03-28", "end": "2024-04-30"},
            "certification": {"start": "2024-05-06", "end": "2024-06-25"},
            "forward": {"start": "2024-07-01"},
        }
    )

    def covered() -> bool:
        with StateStore(resolved / "state.db") as store:
            found = snapshot.covering(
                find(resolved), [INSTRUMENT], ["bar"], "1h", windows, store=store
            )
        return found is not None

    first = backfill(runner, resolved, loader=loader)

    assert [outcome for *_, outcome in outcomes(first)] == ["written"] * 3
    [series] = shown(runner, resolved)
    assert (series["empty"], series["gaps"]) == ([], edges)
    assert at(runner, resolved, "data", "snapshot").exit_code == Exit.OK
    assert not covered()

    again = backfill(runner, resolved, loader=loader)

    assert outcomes(again) == [(start, end, "empty") for start, end in edges]
    [series] = shown(runner, resolved)
    assert (series["empty"], series["gaps"]) == (edges, [])
    assert covered()


@pytest.mark.usefixtures("_leave_the_interpreter_as_found")
def test_a_truncated_chunk_leaves_its_missing_days_a_hole_until_they_are_asked(
    runner: CliRunner, resolved: Path
) -> None:
    """A month asked and fifteen sessions served: the days asked and not served are no one's.

    Asked again, the source serves them — up to the weekend the second answer stopped short
    of, which is a hole in its turn until it is asked alone and answered empty.
    """
    loader = a_source(resolved, "paged", page=15, **FULL)

    backfill(runner, resolved, loader=loader)

    [series] = shown(runner, resolved)
    assert series["spans"] == [
        ["2024-01-02", "2024-01-22"],
        ["2024-02-01", "2024-02-21"],
        ["2024-03-04", "2024-03-22"],
    ]
    assert series["gaps"] == [["2024-01-23", "2024-01-31"], ["2024-02-22", "2024-03-03"]]
    assert series["empty"] == []

    second = backfill(runner, resolved, loader=loader)

    assert outcomes(second) == [
        ("2024-01-23", "2024-01-31", "written"),
        ("2024-02-22", "2024-03-03", "written"),
    ]
    [series] = shown(runner, resolved)
    assert series["spans"] == [["2024-01-02", "2024-03-01"], ["2024-03-04", "2024-03-22"]]
    assert (series["empty"], series["gaps"]) == ([], [["2024-03-02", "2024-03-03"]])

    third = backfill(runner, resolved, loader=loader)

    assert outcomes(third) == [("2024-03-02", "2024-03-03", "empty")]
    [series] = shown(runner, resolved)
    assert (series["empty"], series["gaps"]) == ([["2024-03-02", "2024-03-03"]], [])


@pytest.mark.usefixtures("_leave_the_interpreter_as_found")
def test_a_month_answered_empty_before_the_first_served_day_is_not_coverage(
    runner: CliRunner, resolved: Path
) -> None:
    """Asked before the series begins — before a listing, below a floor — it extends nothing."""
    before = tuple(str(date(2024, 3, 28) + timedelta(days=n)) for n in range(30))
    loader = a_source(resolved, "listed_late", closed=before, start="2024-03-28", end="2024-06-25")

    document = backfill(runner, resolved, loader=loader)

    assert [outcome for *_, outcome in outcomes(document)] == ["empty", "written", "written"]
    [series] = shown(runner, resolved)
    assert series["spans"] == [["2024-04-29", "2024-05-24"], ["2024-05-27", "2024-06-25"]]
    assert series["empty"] == []
    assert series["gaps"] == [["2024-05-25", "2024-05-26"]]
