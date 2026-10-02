"""A loader that declares `chunk_days`: every data verb writes it a dataset per chunk.

The loader here is the package's own synthetic one, handed out under a wrapper that
declares `chunk_days = 1` and, when a test asks, refuses one day, so the commands meet a
day-at-a-time loader and a failure on a later day without any vendor involved. The wrapper
is put where the commands look a loader up, so the command, the catalog and the manifests
are the ones that ship.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from kanso.data.loader import DatasetRef
from kanso.data.loaders import BUILTIN_LOADERS
from kanso.data.manifest import Manifest, manifests
from kanso.errors import Exit, ValidationError
from kanso.workspace import find

from .conftest import at, payload, write_instruments, write_spec

WEEK = {"start": "2024-01-02", "end": "2024-01-05"}
"""Tuesday to Friday: four days the synthetic market opens on, so four datasets."""

DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]


@dataclass
class Daily:
    """The synthetic loader, declaring that a dataset of it holds one day."""

    id: ClassVar[str] = "synthetic"
    chunk_days: ClassVar[int] = 1
    refuse: date | None = None
    asked: int = 0

    def discover(self, spec: Mapping[str, object]) -> list[DatasetRef]:
        return BUILTIN_LOADERS["synthetic"].discover(spec)

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        self.asked += 1
        if self.refuse is not None and window[0] <= self.refuse <= window[1]:
            raise ValidationError(f"the source has no {self.refuse}", remedy="ask again later")
        return BUILTIN_LOADERS["synthetic"].load(ref, window)

    def load_arrow(self, ref: DatasetRef, window: tuple[date, date]) -> None:
        return None

    def manifest(self, ref: DatasetRef) -> Manifest:
        return BUILTIN_LOADERS["synthetic"].manifest(ref)


@pytest.fixture
def daily(monkeypatch: pytest.MonkeyPatch, workspace: Path) -> Daily:
    """The workspace's loaders, with `synthetic` declaring a day per dataset."""
    loader = Daily()
    monkeypatch.setattr("kanso.data.commands.loader_for", lambda ws, loader_id: loader)
    write_instruments(workspace)
    assert at(CliRunner(), workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    return loader


def held(root: Path) -> list[Manifest]:
    return sorted(manifests(find(root)).values(), key=lambda m: m.span)


def load(runner: CliRunner, root: Path, spec: Path, *args: object) -> Any:
    return at(
        runner, root, "data", "load", "--loader", "synthetic", "--spec", spec, *args, "--json"
    )


def test_a_load_writes_a_dataset_and_a_manifest_per_day(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    spec = write_spec(workspace, "week.yaml", **WEEK)

    result = load(runner, workspace, spec)

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert [d["span"] for d in document["datasets"]] == [[day, day] for day in DAYS]
    assert [(str(m.start), str(m.end)) for m in held(workspace)] == [(day, day) for day in DAYS]
    assert document["rows"] == sum(m.row_count for m in held(workspace)) > 0
    assert daily.asked == 4


def test_a_failure_on_a_later_day_keeps_the_days_before_it_and_names_the_resume(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    spec = write_spec(workspace, "week.yaml", **WEEK)
    daily.refuse = date(2024, 1, 4)

    result = load(runner, workspace, spec)

    assert result.exit_code == Exit.VALIDATION
    error = payload(result)
    assert "2024-01-04..2024-01-04 was not written" in error["error"]
    assert "2 dataset(s) before it were" in error["error"]
    assert "the source has no 2024-01-04" in error["error"]
    assert error["remedy"].startswith("ask again later; then `kanso data backfill")
    resume = f"`kanso data backfill --loader synthetic --spec {spec} --to {WEEK['end']}`"
    assert f"then {resume} writes 2024-01-04 and the rest of the range" in error["remedy"]
    assert [str(m.start) for m in held(workspace)] == DAYS[:2]

    daily.refuse = None
    command = resume.strip("`").split()[1:]
    resumed = at(runner, workspace, *command, "--json")

    assert resumed.exit_code == Exit.OK, resumed.stdout
    chunks = payload(resumed)["chunks"]
    assert [(c["start"], c["end"], c["outcome"]) for c in chunks] == [
        (day, day, "written") for day in DAYS[2:]
    ]
    assert [str(m.start) for m in held(workspace)] == DAYS


def test_a_failure_on_the_first_day_is_the_loader_s_own_refusal(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    spec = write_spec(workspace, "week.yaml", **WEEK)
    daily.refuse = date(2024, 1, 2)

    result = load(runner, workspace, spec)

    assert result.exit_code == Exit.VALIDATION
    assert payload(result) == {
        "error": "the source has no 2024-01-02",
        "code": int(Exit.VALIDATION),
        "remedy": "ask again later",
    }
    assert held(workspace) == []


def test_a_supersede_is_recorded_on_the_one_day_that_takes_its_place(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    thursday = write_spec(workspace, "thursday.yaml", start="2024-01-04", end="2024-01-04")
    assert load(runner, workspace, thursday).exit_code == Exit.OK
    [old] = held(workspace)
    spec = write_spec(workspace, "week.yaml", **WEEK)

    result = load(runner, workspace, spec, "--supersedes", old.dataset_id)

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    assert [d.get("supersedes") for d in document["datasets"]] == [
        None,
        None,
        old.dataset_id,
        None,
    ]
    assert [d["replaced"] for d in document["datasets"]] == [[], [], [old.dataset_id], []]


def test_a_supersede_of_a_dataset_no_day_overlaps_goes_on_its_series_first_day(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    before = write_spec(workspace, "before.yaml", start="2023-12-28", end="2023-12-29")
    assert load(runner, workspace, before).exit_code == Exit.OK
    older = [m.dataset_id for m in held(workspace)]
    spec = write_spec(workspace, "week.yaml", **WEEK)

    result = load(runner, workspace, spec, "--supersedes", older[-1])

    assert result.exit_code == Exit.OK, result.stdout
    recorded = [d.get("supersedes") for d in payload(result)["datasets"]]
    assert recorded == [older[-1], None, None, None]


def test_a_supersede_of_another_series_goes_on_the_first_day(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    quotes = write_spec(
        workspace, "quotes.yaml", start="2024-01-04", end="2024-01-04", types=["quote"]
    )
    assert load(runner, workspace, quotes).exit_code == Exit.OK
    [other] = held(workspace)
    spec = write_spec(workspace, "week.yaml", **WEEK)

    result = load(runner, workspace, spec, "--supersedes", other.dataset_id)

    assert result.exit_code == Exit.OK, result.stdout
    recorded = [d.get("supersedes") for d in payload(result)["datasets"]]
    assert recorded == [other.dataset_id, None, None, None]


def test_a_supersede_of_nothing_held_is_refused_on_the_first_day(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    spec = write_spec(workspace, "week.yaml", **WEEK)

    result = load(runner, workspace, spec, "--supersedes", "nope")

    assert result.exit_code == Exit.PRECONDITION
    assert "'nope' is not a dataset this workspace holds" in payload(result)["error"]
    assert held(workspace) == []


def test_backfill_and_sync_cut_by_the_loader_s_days(
    runner: CliRunner, workspace: Path, daily: Daily
) -> None:
    friday = write_spec(workspace, "friday.yaml", start="2024-01-05", end="2024-01-05")
    assert load(runner, workspace, friday).exit_code == Exit.OK
    spec = write_spec(workspace, "week.yaml", **WEEK)

    filled = at(
        runner, workspace, "data", "backfill", "--loader", "synthetic", "--spec", spec, "--json"
    )
    synced = at(runner, workspace, "data", "sync", "--to", "2024-01-09", "--json")

    assert filled.exit_code == Exit.OK, filled.stdout
    assert [(c["start"], c["end"]) for c in payload(filled)["chunks"]] == [
        (day, day) for day in DAYS[:3]
    ]
    assert synced.exit_code == Exit.OK, synced.stdout
    assert [(c["start"], c["end"]) for c in payload(synced)["chunks"]] == [
        (day, day) for day in ("2024-01-06", "2024-01-07", "2024-01-08", "2024-01-09")
    ]
    assert len(held(workspace)) == 4


@dataclass
class Lagging(Daily):
    """The day-a-dataset loader over a source that has published only to `newest`, and
    serves nothing after it — as `okx_trades` and `okx_book` serve a day past the newest
    archive the exchange lists."""

    newest: date = date(2024, 1, 3)

    def load(self, ref: DatasetRef, window: tuple[date, date]) -> Iterable[object]:
        if window[0] > self.newest:
            self.asked += 1
            return iter(())
        return super().load(ref, window)


def test_days_a_sync_met_before_they_were_published_are_asked_again(
    runner: CliRunner, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync to Friday when the source has published to Wednesday answers Thursday and
    Friday empty. Once it publishes them, the next sync writes them, and nothing is left for
    a backfill: no day of the week was recorded as answered empty."""
    from kanso.state import StateStore

    lagging = Lagging()
    monkeypatch.setattr("kanso.data.commands.loader_for", lambda ws, loader_id: lagging)
    write_instruments(workspace)
    assert at(runner, workspace, "data", "instruments", "resolve").exit_code == Exit.OK
    spec = write_spec(workspace, "january.yaml", start="2024-01-02", end="2024-01-31")

    def backfill(to: str) -> Any:
        return payload(
            at(
                runner, workspace, "data", "backfill", "--loader", "synthetic", "--spec", spec,
                "--to", to, "--json",
            )
        )  # fmt: skip

    assert [c["outcome"] for c in backfill("2024-01-02")["chunks"]] == ["written"]
    one = payload(at(runner, workspace, "data", "sync", "--to", WEEK["end"], "--json"))
    lagging.newest = date(2024, 1, 31)
    two = payload(at(runner, workspace, "data", "sync", "--to", "2024-01-09", "--json"))
    three = backfill("2024-01-09")

    assert [(c["start"], c["outcome"]) for c in one["chunks"]] == [
        ("2024-01-03", "written"),
        ("2024-01-04", "empty"),
        ("2024-01-05", "empty"),
    ]
    assert [(c["start"], c["outcome"]) for c in two["chunks"]][:2] == [
        ("2024-01-04", "written"),
        ("2024-01-05", "written"),
    ]
    assert three["chunks"] == []
    assert {str(m.start) for m in held(workspace)} >= {*DAYS, "2024-01-08", "2024-01-09"}
    with StateStore(workspace / "state.db") as store:
        assert store.events(kind="data_chunk_empty") == []
