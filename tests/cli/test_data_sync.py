"""`kanso data sync`: extending a held dataset forwards, as a successor and never in place."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from kanso.errors import Exit

from .conftest import INSTRUMENT, at, payload, write_instruments, write_spec

CSV_HEADER = "day,open,high,low,close,volume\n"
FIRST_DAY = date(2024, 1, 2)


def rows(count: int, start: int = 0) -> str:
    """`count` daily bars, one per calendar day, priced on a gentle ramp."""
    made = []
    for index in range(start, start + count):
        day = FIRST_DAY + timedelta(days=index)
        price = 100 + index
        made.append(f"{day} 16:00:00,{price}.00,{price}.50,{price - 1}.00,{price}.25,1000\n")
    return "".join(made)


def same_series(root: Path, name: str, content: str) -> Path:
    """A second CSV of the same instrument, type and resolution, and the spec that reads it.

    One series to the catalog and two datasets to kanso, which is the shape a backfill
    leaves behind: the loader and the columns are the fixture's own, so only the file the
    rows come from differs.
    """
    (root / f"{name}.csv").write_text(content, encoding="utf-8")
    spec = yaml.safe_load((root / "files.yaml").read_text(encoding="utf-8"))
    spec["files"][0]["path"] = str(root / f"{name}.csv")
    path = root / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def files(runner: CliRunner, workspace: Path) -> Path:
    """A workspace holding five days of a CSV file, and the spec that reads it."""
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    (workspace / "bars.csv").write_text(CSV_HEADER + rows(5), encoding="utf-8")
    spec = {
        "loader": "csv_parquet",
        "timezone": "UTC",
        "files": [
            {
                "path": str(workspace / "bars.csv"),
                "instrument": "DEMO",
                "venue": "SIM",
                "type": "bar",
                "resolution": "1d",
                "columns": {
                    "ts_event": "day",
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "close": "close",
                    "volume": "volume",
                },
            }
        ],
    }
    (workspace / "files.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    assert (
        at(
            runner,
            workspace,
            "data",
            "load",
            "--loader",
            "csv_parquet",
            "--spec",
            workspace / "files.yaml",
        ).exit_code
        == Exit.OK
    )
    return workspace


def test_sync_extends_a_dataset_into_a_successor(runner: CliRunner, files: Path) -> None:
    held = payload(at(runner, files, "data", "show", "--json"))["series"][0]["datasets"][0]
    (files / "bars.csv").write_text(CSV_HEADER + rows(9), encoding="utf-8")

    result = at(runner, files, "data", "sync", "--to", "2024-01-10", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["rows"] == 4
    [chunk] = document["chunks"]
    assert chunk["outcome"] == "written"
    [series] = payload(at(runner, files, "data", "show", "--json"))["series"]
    successor = [item for item in series["datasets"] if item["dataset_id"] != held["dataset_id"]]
    assert [item["supersedes"] for item in successor] == [held["dataset_id"]]
    assert series["spans"] == [["2024-01-02", "2024-01-10"]]


def test_a_synced_dataset_never_rewrites_the_one_a_snapshot_pins(
    runner: CliRunner, files: Path
) -> None:
    assert at(runner, files, "data", "snapshot").exit_code == Exit.OK
    pinned = payload(at(runner, files, "data", "show", "--json"))["series"][0]["datasets"][0]
    (files / "bars.csv").write_text(CSV_HEADER + rows(9), encoding="utf-8")

    assert at(runner, files, "data", "sync", "--to", "2024-01-10").exit_code == Exit.OK

    [series] = payload(at(runner, files, "data", "show", "--json"))["series"]
    still = [item for item in series["datasets"] if item["dataset_id"] == pinned["dataset_id"]]
    assert still and still[0]["checksum"] == pinned["checksum"]


def test_sync_extends_only_the_newest_dataset_of_each_series(
    runner: CliRunner, files: Path
) -> None:
    """A series that has been extended once is several datasets, and only its last grows.

    Extending an interior dataset asks for days the dataset after it already serves, which
    the catalog refuses — outright once a snapshot pins it. A sync that reached for every
    manifest could therefore not succeed at all on a series a backfill or an earlier sync
    had left in chunks, which is every series with any history behind it.
    """
    (files / "bars.csv").write_text(CSV_HEADER + rows(9), encoding="utf-8")
    assert at(runner, files, "data", "sync", "--to", "2024-01-10").exit_code == Exit.OK
    (files / "bars.csv").write_text(CSV_HEADER + rows(13), encoding="utf-8")

    result = at(runner, files, "data", "sync", "--to", "2024-01-14", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert [chunk["start"] for chunk in document["chunks"]] == ["2024-01-11"]
    [series] = payload(at(runner, files, "data", "show", "--json"))["series"]
    assert series["spans"] == [["2024-01-02", "2024-01-14"]]


def test_every_series_is_extended_and_not_only_one_of_them(runner: CliRunner, files: Path) -> None:
    """Newest *per series*: two series in a workspace are two datasets to continue."""
    write_spec(files, "synthetic.yaml", start="2024-01-02", end="2024-01-08")
    assert (
        at(
            runner,
            files,
            "data",
            "load",
            "--loader",
            "synthetic",
            "--spec",
            files / "synthetic.yaml",
        ).exit_code
        == Exit.OK
    )
    (files / "bars.csv").write_text(CSV_HEADER + rows(9), encoding="utf-8")

    document = payload(at(runner, files, "data", "sync", "--to", "2024-01-10", "--json"))

    assert sorted(chunk["resolution"] for chunk in document["chunks"]) == ["1d", "1h"]


def test_a_named_dataset_is_extended_even_when_it_is_not_the_newest(
    runner: CliRunner, files: Path
) -> None:
    """Naming one is how an operator extends the dataset in front of a hole on purpose.

    The default is the newest dataset because that is the only one that can be extended
    without knowing what the series looks like; `--dataset` is the operator saying which
    end of which dataset they meant, and it is not second-guessed.
    """
    later = same_series(files, "later", CSV_HEADER + rows(5, 10))
    assert (
        at(runner, files, "data", "load", "--loader", "csv_parquet", "--spec", later).exit_code
        == Exit.OK
    )
    behind = min(
        payload(at(runner, files, "data", "show", "--json"))["series"][0]["datasets"],
        key=lambda item: item["span"][1],
    )["dataset_id"]
    (files / "bars.csv").write_text(CSV_HEADER + rows(8), encoding="utf-8")

    document = payload(
        at(runner, files, "data", "sync", "--dataset", behind, "--to", "2024-01-09", "--json")
    )

    assert document["rows"] == 3
    [series] = payload(at(runner, files, "data", "show", "--json"))["series"]
    assert series["spans"] == [["2024-01-02", "2024-01-09"], ["2024-01-12", "2024-01-16"]]


def test_a_source_with_nothing_further_says_so(runner: CliRunner, files: Path) -> None:
    result = at(runner, files, "data", "sync", "--to", "2024-02-01", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["rows"] == 0
    assert any("served nothing after" in str(note) for note in document["notes"])


def test_a_repeated_sync_asks_the_source_nothing_twice(runner: CliRunner, files: Path) -> None:
    """Sync checkpoints per chunk exactly as backfill does, so a repeat is free."""
    from kanso.state import StateStore

    first = payload(at(runner, files, "data", "sync", "--to", "2024-01-20", "--json"))
    assert [chunk["outcome"] for chunk in first["chunks"]] == ["empty"]
    with StateStore(files / "state.db") as store:
        recorded = len(store.events(kind="data_chunk_empty"))

    second = payload(at(runner, files, "data", "sync", "--to", "2024-01-20", "--json"))

    assert [chunk["outcome"] for chunk in second["chunks"]] == ["empty"]
    with StateStore(files / "state.db") as store:
        assert len(store.events(kind="data_chunk_empty")) == recorded == 1


def test_a_dataset_already_at_the_horizon_is_left_alone(runner: CliRunner, files: Path) -> None:
    result = at(runner, files, "data", "sync", "--to", "2024-01-03", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert document["chunks"] == []
    assert any("at or past" in str(note) for note in document["notes"])


def test_sync_can_be_narrowed_to_one_loader(runner: CliRunner, files: Path) -> None:
    write_spec(files, "synthetic.yaml", start="2024-01-02", end="2024-01-31")
    assert (
        at(
            runner,
            files,
            "data",
            "load",
            "--loader",
            "synthetic",
            "--spec",
            files / "synthetic.yaml",
        ).exit_code
        == Exit.OK
    )

    document = payload(
        at(runner, files, "data", "sync", "--loader", "csv_parquet", "--to", "2024-01-08", "--json")
    )

    assert all(chunk["resolution"] == "1d" for chunk in document["chunks"])


def test_sync_can_be_narrowed_to_one_dataset(runner: CliRunner, files: Path) -> None:
    held = payload(at(runner, files, "data", "show", "--json"))["series"][0]["datasets"][0]
    (files / "bars.csv").write_text(CSV_HEADER + rows(9), encoding="utf-8")

    document = payload(
        at(
            runner,
            files,
            "data",
            "sync",
            "--dataset",
            held["dataset_id"],
            "--to",
            "2024-01-10",
            "--json",
        )
    )

    assert document["rows"] == 4


def test_an_unknown_dataset_is_a_precondition_failure(runner: CliRunner, files: Path) -> None:
    result = at(runner, files, "data", "sync", "--dataset", "nope", "--json")

    assert result.exit_code == Exit.PRECONDITION
    assert "kanso data show" in payload(result)["remedy"]


def test_sync_defaults_its_horizon_to_today(runner: CliRunner, files: Path) -> None:
    """No `--to` means now, which for a file that stopped in January serves nothing."""
    before = datetime.now(tz=UTC).date()
    result = at(runner, files, "data", "sync")
    after = datetime.now(tz=UTC).date()

    assert result.exit_code == Exit.OK
    # UTC, because that is the clock `sync` reads: comparing with a local date fails for
    # part of every day for anyone east of UTC. Either day the call spanned will do — a run
    # that crosses midnight is not a horizon that was computed wrongly.
    assert any(str(day) in result.stdout for day in {before, after})


def test_sync_reads_as_a_few_lines_for_a_human(runner: CliRunner, files: Path) -> None:
    (files / "bars.csv").write_text(CSV_HEADER + rows(9), encoding="utf-8")

    result = at(runner, files, "data", "sync", "--to", "2024-01-10")

    assert result.exit_code == Exit.OK
    assert INSTRUMENT in result.stdout
    assert "written" in result.stdout


def test_sync_is_recorded_in_the_event_log(runner: CliRunner, files: Path) -> None:
    from kanso.state import StateStore

    assert at(runner, files, "data", "sync", "--to", "2024-01-06").exit_code == Exit.OK

    with StateStore(files / "state.db") as store:
        assert store.events(kind="data_synced")
