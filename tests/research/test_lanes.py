"""Lane directories: named, isolated, restorable, and written atomically."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.errors import ValidationError
from kanso.research import lanes
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import HYP_ID


def test_a_lane_directory_is_named_for_its_lane_and_hypothesis(ws: Workspace) -> None:
    assert lanes.lane_dir(ws, "op", HYP_ID) == ws.path("runs", "op", HYP_ID)
    assert lanes.lane_dir(ws, "l1", HYP_ID) == ws.path("runs", "l1", HYP_ID)


@pytest.mark.parametrize("lane", ["", "OP", "a-b", "x" * 17, "l 1"])
def test_a_name_a_directory_cannot_be_is_refused(ws: Workspace, lane: str) -> None:
    with pytest.raises(ValidationError, match="is not a lane name"):
        lanes.lane_dir(ws, lane, HYP_ID)


def test_preparing_a_lane_replaces_whatever_a_dead_run_left(ws: Workspace) -> None:
    directory = lanes.lane_dir(ws, "op", HYP_ID)
    (directory / "stale").mkdir(parents=True)
    (directory / "strategy.py").write_bytes(b"old")
    assert lanes.prepare(directory) == directory
    assert list(directory.iterdir()) == []


def test_removing_a_lane_that_is_not_there_is_not_an_error(ws: Workspace) -> None:
    lanes.remove(lanes.lane_dir(ws, "op", HYP_ID))


def test_a_write_is_a_rename_so_a_reader_never_sees_half_a_file(tmp_path: Path) -> None:
    path = tmp_path / "strategy.py"
    lanes.write_atomic(path, b"first")
    lanes.write_atomic(path, b"second")
    assert path.read_bytes() == b"second"
    assert [child.name for child in tmp_path.iterdir()] == ["strategy.py"]


def test_restoring_copies_the_blobs_the_run_pinned(ws: Workspace, store: StateStore) -> None:
    directory = lanes.prepare(lanes.lane_dir(ws, "op", HYP_ID))
    pins = {name: store.put_blob(name.encode()) for name in lanes.SCOPED_FILES}
    lanes.restore(store, directory, pins)
    assert {child.name for child in directory.iterdir()} == set(lanes.SCOPED_FILES)
    assert (directory / "strategy.py").read_bytes() == b"strategy.py"


def test_two_lanes_never_share_a_path(ws: Workspace, store: StateStore) -> None:
    left = lanes.prepare(lanes.lane_dir(ws, "op", HYP_ID))
    right = lanes.prepare(lanes.lane_dir(ws, "l1", HYP_ID))
    assert left != right
    lanes.write_atomic(left / "strategy.py", b"left")
    lanes.write_atomic(right / "strategy.py", b"right")
    assert (left / "strategy.py").read_bytes() == b"left"
    assert (right / "strategy.py").read_bytes() == b"right"
    lanes.remove(left)
    assert (right / "strategy.py").read_bytes() == b"right"
