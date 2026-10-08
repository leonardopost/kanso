"""`kanso state prune` deletes the books no run can select, after a backup, and shrinks the file.

The workspace is a scaffolded one with rows written straight into its store: two
hypotheses, one researched under the pins a run begun now would carry and one retired with
books large enough to free whole pages. What the rule keeps is `tests/research/
test_records.py`'s business; here it is the command's — the backup, the lock, the room on
the disk, the rewrite, and what each refusal leaves behind.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
from typer.testing import CliRunner

from kanso.cli import state as state_commands
from kanso.criteria import criteria_version
from kanso.errors import Exit
from kanso.research import daemon, records
from kanso.schemas import RunRecord
from kanso.state import StateStore
from kanso.workspace import find
from tests.processes import holding

from .conftest import at, payload

KEPT, GONE = 3, 40
"""Books under the pins a run begun now would carry, and books of a retired hypothesis."""

BOOK = {
    (date(2020, 1, 1) + timedelta(days=day)).isoformat(): [
        [f"N{n}.XNAS", 1, True] for n in range(8)
    ]
    for day in range(400)
}
"""A book of four hundred sessions, about seventy kilobytes: each one fills pages of its own."""


def write_books(root: Path) -> None:
    """Register `live` and `gone`, give each an ended run, and store their books."""
    with StateStore(root / "state.db") as store:
        for hyp_id, status in (("live", "researching"), ("gone", "retired")):
            sha = store.put_blob(f"{hyp_id} file".encode())
            store.connection.execute(
                "INSERT INTO hypotheses (hyp_id, status, hypothesis_sha, created_at, updated_at)"
                " VALUES (?, ?, ?, '2026-10-02', '2026-10-02')",
                (hyp_id, status, sha),
            )
            began = datetime(2026, 10, 2, tzinfo=UTC)
            run = records.insert(
                store,
                RunRecord(
                    run_id=hyp_id,
                    hyp_id=hyp_id,
                    tag="20261002-1",
                    lane="op",
                    dir=f"runs/op/{hyp_id}",
                    base_sha=sha,
                    hypothesis_sha=sha,
                    program_sha=sha,
                    snapshot_id="snap",
                    criteria_version=criteria_version(),
                    card_budget_s=60.0,
                    baseline_wall_s=1.0,
                    baseline_peak_mem_gb=1.0,
                    started_at=began,
                    ended_at=began + timedelta(minutes=1),
                ),
            )
            for n in range(KEPT if hyp_id == "live" else GONE):
                book = store.put_blob(f"{hyp_id} {n}".encode())
                records.record_signature(store, run, book, BOOK, float(n), "reading")


def books(path: Path) -> dict[str, int]:
    """How many books each hypothesis has stored in the database at `path`."""
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT hyp_id, COUNT(*) FROM signatures GROUP BY hyp_id")
        return dict(rows.fetchall())


def backups(root: Path) -> list[Path]:
    return sorted((root / "runs").glob("state-*.db"))


def on_disk(root: Path) -> int:
    """The bytes the store holds on disk: the database and its write-ahead log."""
    files = (root / "state.db", root / "state.db-wal")
    return sum(os.path.getsize(path) for path in files if path.exists())


@pytest.fixture
def ws(workspace: Path) -> Path:
    write_books(workspace)
    return workspace


@pytest.fixture
def daemon_running(ws: Path) -> Iterator[None]:
    """The daemon's lock held, as a running supervisor holds it."""
    held = daemon._acquire(find(ws))
    try:
        yield
    finally:
        held.close()


class Usage(NamedTuple):
    total: int
    used: int
    free: int


def disk_with(monkeypatch: pytest.MonkeyPatch, *free: int) -> None:
    """Report `free` bytes on the disk for each look the command takes, the last for the rest."""
    answers = list(free)

    def usage(_: object) -> Usage:
        left = answers.pop(0) if len(answers) > 1 else answers[0]
        return Usage(total=left, used=0, free=left)

    monkeypatch.setattr(state_commands.shutil, "disk_usage", usage)


def test_prune_backs_up_the_store_deletes_the_superseded_books_and_shrinks_it(
    runner: CliRunner, ws: Path
) -> None:
    before = on_disk(ws)

    result = at(runner, ws, "state", "prune", "--json")

    assert result.exit_code == Exit.OK, result.stdout
    document = payload(result)
    (backup,) = backups(ws)
    assert document["pruned"] == GONE and document["kept"] == KEPT
    assert document["backup"] == str(backup)
    assert document["vacuumed"] is True
    assert document["size_after"] < document["size_before"]
    assert document["criteria_version"] == criteria_version()
    assert books(ws / "state.db") == {"live": KEPT}
    assert books(backup) == {"live": KEPT, "gone": GONE}, "the copy holds what was deleted"
    assert on_disk(ws) < before / 4, "the file and its log give the pages back to the disk"
    with StateStore(ws / "state.db") as store:
        (event,) = store.events(kind=state_commands.PRUNED)
    assert event.detail == {"rows": GONE, "kept": KEPT, "backup": str(backup)}


def test_prune_says_what_it_did_in_lines(runner: CliRunner, ws: Path) -> None:
    result = at(runner, ws, "state", "prune")

    assert result.exit_code == Exit.OK
    assert f"pruned     {GONE} signature(s)" in result.stdout
    assert f"· {KEPT} kept" in result.stdout
    assert "backup     " in result.stdout and "runs/state-" in result.stdout


def test_a_dry_run_counts_what_would_go_and_writes_nothing(
    runner: CliRunner, ws: Path, daemon_running: None
) -> None:
    """It needs no lock, so it can be asked while the daemon works."""
    before = on_disk(ws)

    result = at(runner, ws, "state", "prune", "--dry-run", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert (document["dry_run"], document["pruned"], document["kept"]) == (True, GONE, KEPT)
    assert document["pruned_bytes"] > GONE * 50_000
    assert (document["backup"], document["vacuumed"], document["size_after"]) == (
        None,
        False,
        None,
    )
    assert books(ws / "state.db") == {"live": KEPT, "gone": GONE}
    assert backups(ws) == [] and on_disk(ws) == before
    lines = at(runner, ws, "state", "prune", "--dry-run").stdout
    assert f"would prune {GONE} signature(s)" in lines and "nothing written" in lines


def test_prune_is_refused_while_a_daemon_runs(
    runner: CliRunner, ws: Path, daemon_running: None
) -> None:
    result = at(runner, ws, "state", "prune", "--json")

    assert result.exit_code == Exit.PRECONDITION
    document = payload(result)
    assert "a daemon is running in this workspace" in document["error"]
    assert "kanso research stop" in document["remedy"]
    assert books(ws / "state.db") == {"live": KEPT, "gone": GONE} and backups(ws) == []


def test_prune_is_refused_while_a_lane_of_a_daemon_that_is_gone_still_runs(
    runner: CliRunner, ws: Path
) -> None:
    """A lane whose supervisor was killed — waiting on a model, say — still writes the store,
    and it holds no daemon lock to be refused by: its own lock is what refuses the prune."""
    lane = holding(ws, "l1")
    try:
        result = at(runner, ws, "state", "prune", "--json")
    finally:
        lane.kill()
        lane.wait()

    assert result.exit_code == Exit.PRECONDITION
    document = payload(result)
    assert (
        document["error"] == f"still running from a daemon that is gone: lane l1 (pid {lane.pid})"
    )
    assert document["remedy"] == "run `kanso research stop`, which ends it, then run this again"
    assert books(ws / "state.db") == {"live": KEPT, "gone": GONE} and backups(ws) == []
    released = daemon._lock(find(ws))
    assert released is not None, "the refusal let go of the daemon's lock"
    released.close()


def test_prune_is_refused_when_the_disk_cannot_hold_the_copy_and_the_rewrite(
    runner: CliRunner, ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk_with(monkeypatch, 1_000)

    result = at(runner, ws, "state", "prune", "--json")

    assert result.exit_code == Exit.PRECONDITION
    document = payload(result)
    assert "free on its disk" in document["error"] and "0.0 MB" in document["error"]
    assert "kanso state prune" in document["remedy"]
    assert books(ws / "state.db") == {"live": KEPT, "gone": GONE} and backups(ws) == []


def test_a_prune_whose_rewrite_was_refused_rewrites_on_the_next(
    runner: CliRunner, ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk filled between the copy and the rewrite: the books are gone and backed up,
    the file is as large as it was, and the next prune has nothing to delete and rewrites."""
    disk_with(monkeypatch, 10**12, 1_000)

    refused = at(runner, ws, "state", "prune", "--json")

    assert refused.exit_code == Exit.PRECONDITION
    assert books(ws / "state.db") == {"live": KEPT}
    (backup,) = backups(ws)
    disk_with(monkeypatch, 10**12)
    before = on_disk(ws)

    result = at(runner, ws, "state", "prune", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert (document["pruned"], document["backup"], document["vacuumed"]) == (0, None, True)
    assert backups(ws) == [backup] and on_disk(ws) < before / 4


def test_a_second_prune_has_nothing_to_do(runner: CliRunner, ws: Path) -> None:
    assert at(runner, ws, "state", "prune").exit_code == Exit.OK

    result = at(runner, ws, "state", "prune", "--json")

    assert result.exit_code == Exit.OK
    document = payload(result)
    assert (document["pruned"], document["kept"], document["backup"]) == (0, KEPT, None)
    assert document["vacuumed"] is False
    assert document["size_after"] == document["size_before"]
    assert len(backups(ws)) == 1


def test_prune_releases_the_daemon_s_lock(runner: CliRunner, ws: Path) -> None:
    assert at(runner, ws, "state", "prune").exit_code == Exit.OK

    held = daemon._acquire(find(ws))
    held.close()
