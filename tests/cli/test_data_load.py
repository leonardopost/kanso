"""`kanso data load`: what it writes, what it refuses, and what it says it served."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import (
    FIRST,
    INSTRUMENT,
    LAST,
    at,
    payload,
    write_instruments,
    write_spec,
)


@pytest.fixture
def ready(runner: CliRunner, workspace: Path) -> Path:
    """A scaffolded workspace whose one instrument is manual and resolved."""
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    return workspace


def test_load_writes_the_range_the_spec_names(runner: CliRunner, ready: Path) -> None:
    spec = write_spec(ready)

    result = at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec, "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["loader"] == "synthetic"
    assert document["rows"] > 0
    [dataset] = document["datasets"]
    assert dataset["span"] == [str(FIRST), str(LAST)]
    assert dataset["publication"] == "realtime"
    assert dataset["truncated"] is False
    assert dataset["shortfall"] is None
    assert dataset["dataset_id"].startswith(INSTRUMENT)


def test_load_reads_as_a_few_lines_for_a_human(runner: CliRunner, ready: Path) -> None:
    spec = write_spec(ready)

    result = at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec)

    assert result.exit_code == Exit.OK
    assert "synthetic" in result.stdout
    assert f"{FIRST}..{LAST}" in result.stdout
    assert "1 dataset(s)" in result.stdout


def test_a_second_load_of_the_same_span_is_refused(runner: CliRunner, ready: Path) -> None:
    """A destructive default on a command that reads like an import is the wrong default."""
    spec = write_spec(ready)
    assert at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0

    result = at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec, "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "--replace" in payload(result)["remedy"]


def test_replace_rewrites_the_overlapped_span(runner: CliRunner, ready: Path) -> None:
    spec = write_spec(ready)
    first = at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec, "--json")
    held = payload(first)["datasets"][0]["dataset_id"]

    result = at(
        runner,
        ready,
        "data",
        "load",
        "--loader",
        "synthetic",
        "--spec",
        spec,
        "--replace",
        "--json",
    )

    assert result.exit_code == Exit.OK
    assert payload(result)["datasets"][0]["replaced"] == [held]


def test_a_load_into_a_pinned_dataset_is_refused_even_with_replace(
    runner: CliRunner, ready: Path
) -> None:
    spec = write_spec(ready)
    assert at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0
    assert at(runner, ready, "data", "snapshot").exit_code == Exit.OK

    result = at(
        runner,
        ready,
        "data",
        "load",
        "--loader",
        "synthetic",
        "--spec",
        spec,
        "--replace",
        "--json",
    )

    assert result.exit_code == Exit.PRECONDITION
    assert "snapshot" in payload(result)["error"]


def test_an_unknown_loader_names_the_ones_that_exist(runner: CliRunner, ready: Path) -> None:
    spec = write_spec(ready)

    result = at(runner, ready, "data", "load", "--loader", "nope", "--spec", spec, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "synthetic" in payload(result)["error"]


def test_a_spec_naming_another_loader_is_refused(runner: CliRunner, ready: Path) -> None:
    spec = write_spec(ready)

    result = at(runner, ready, "data", "load", "--loader", "csv_parquet", "--spec", spec, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "synthetic" in payload(result)["error"]


def test_a_missing_spec_file_is_a_validation_failure(runner: CliRunner, ready: Path) -> None:
    result = at(
        runner,
        ready,
        "data",
        "load",
        "--loader",
        "synthetic",
        "--spec",
        ready / "gone.yaml",
        "--json",
    )

    assert result.exit_code == Exit.VALIDATION
    assert "cannot be read" in payload(result)["error"]


def test_a_spec_that_is_not_a_mapping_is_refused(runner: CliRunner, ready: Path) -> None:
    path = ready / "list.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")

    result = at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", path, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "mapping" in payload(result)["error"]


def test_a_spec_that_is_not_yaml_is_refused(runner: CliRunner, ready: Path) -> None:
    path = ready / "broken.yaml"
    path.write_text("loader: [synthetic\n", encoding="utf-8")

    result = at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", path, "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "not valid YAML" in payload(result)["error"]


def test_a_shortfall_is_stated_rather_than_smoothed_over(runner: CliRunner, ready: Path) -> None:
    """The spec asks into the weekend; coverage is what the sessions actually served."""
    spec = write_spec(ready, start="2024-01-02", end="2024-01-07")

    document = payload(
        at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec, "--json")
    )

    [dataset] = document["datasets"]
    assert dataset["requested"] == ["2024-01-02", "2024-01-05"]
    assert dataset["span"] == ["2024-01-02", "2024-01-05"]


def test_the_load_is_recorded_in_the_event_log(runner: CliRunner, ready: Path) -> None:
    from kanso.state import StateStore

    spec = write_spec(ready)
    assert at(runner, ready, "data", "load", "--loader", "synthetic", "--spec", spec).exit_code == 0

    with StateStore(ready / "state.db") as store:
        kinds = [event.kind for event in store.events()]
    assert "data_loaded" in kinds


def test_a_workspace_a_migration_behind_refuses_rather_than_migrating(
    runner: CliRunner, ready: Path
) -> None:
    """Moving the schema is `migrate`'s job and nothing else's."""
    import sqlite3

    connection = sqlite3.connect(ready / "state.db")
    try:
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
    finally:
        connection.close()

    result = at(runner, ready, "data", "snapshot", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "kanso migrate" in payload(result)["remedy"]


OVERPROMISING = """\
\"\"\"A loader that declares a wider span than it can serve, as a real source may.\"\"\"

from dataclasses import replace
from datetime import timedelta

from kanso.data.loaders.synthetic import SyntheticLoader

PROVIDES = {"loaders": ("overpromising",)}

OVERHANG = timedelta(days=14)


class Overpromising:
    id = "overpromising"

    def _inner(self, spec):
        return dict(spec, loader="synthetic")

    def discover(self, spec):
        return [
            replace(ref, span=(ref.span[0], ref.span[1] + OVERHANG))
            for ref in SyntheticLoader().discover(self._inner(spec))
        ]

    def load(self, ref, window):
        return SyntheticLoader().load(ref, window)

    def load_arrow(self, ref, window):
        return None

    def manifest(self, ref):
        return SyntheticLoader().manifest(ref)


LOADERS = {"overpromising": Overpromising()}
"""


def test_a_source_that_serves_less_than_it_promised_says_so(runner: CliRunner, ready: Path) -> None:
    """Coverage is what was served: a source may answer a range it cannot fill in silence."""
    package = ready / "kanso_ext" / "overpromising"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(OVERPROMISING, encoding="utf-8")
    spec = write_spec(ready, "over.yaml", loader="overpromising")

    document = payload(
        at(runner, ready, "data", "load", "--loader", "overpromising", "--spec", spec, "--json")
    )

    [dataset] = document["datasets"]
    assert dataset["truncated"] is True
    assert dataset["shortfall"] is not None
    human = at(
        runner, ready, "data", "load", "--loader", "overpromising", "--spec", spec, "--replace"
    )
    assert human.exit_code == Exit.OK
    assert "missing" in human.stdout
