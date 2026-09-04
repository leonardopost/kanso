"""Discovery: the nearest `kanso.toml`, and the workspace that owns a lane directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from kanso.errors import Exit, PreconditionError
from kanso.workspace import CONFIG_NAME, find, init, lane_owner


def _lane(root: Path, lane: str = "op", hyp: str = "demo_mr") -> Path:
    directory = root / "runs" / lane / hyp
    directory.mkdir(parents=True)
    for name in ("hypothesis.yaml", "program.md", "strategy.py"):
        (directory / name).write_text("", encoding="utf-8")
    return directory


def test_find_at_the_root(fresh: Path) -> None:
    ws = init(fresh)

    assert find(ws.root).root == ws.root
    assert find(ws.root).config.schema_version == ws.config.schema_version


def test_find_from_a_subdirectory(fresh: Path) -> None:
    ws = init(fresh)
    deep = ws.path("hypotheses", "a", "b")
    deep.mkdir(parents=True)

    assert find(deep).root == ws.root


def test_find_from_a_file_path(fresh: Path) -> None:
    ws = init(fresh)

    assert find(ws.path("kanso.toml")).root == ws.root


def test_find_defaults_to_the_current_directory(
    fresh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = init(fresh)
    monkeypatch.chdir(ws.path("catalog"))

    assert find().root == ws.root


def test_find_from_a_lane_directory(fresh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The interactive loop runs with cwd set to the lane directory."""
    ws = init(fresh)
    lane = _lane(ws.root)
    monkeypatch.chdir(lane)

    assert find().root == ws.root


def test_a_lane_directory_holding_a_config_re_resolves(fresh: Path) -> None:
    """A `kanso.toml` inside a lane directory belongs to the run, not to a workspace."""
    ws = init(fresh)
    lane = _lane(ws.root)
    (lane / CONFIG_NAME).write_text(ws.path(CONFIG_NAME).read_text(encoding="utf-8"), "utf-8")

    assert find(lane).root == ws.root
    assert find(lane / "nested").root == ws.root


def test_nested_lane_directories_climb_to_the_owner(fresh: Path) -> None:
    ws = init(fresh)
    outer = _lane(ws.root)
    (outer / CONFIG_NAME).write_text(ws.path(CONFIG_NAME).read_text(encoding="utf-8"), "utf-8")
    inner = _lane(outer, lane="op", hyp="inner")
    (inner / CONFIG_NAME).write_text(ws.path(CONFIG_NAME).read_text(encoding="utf-8"), "utf-8")

    assert find(inner).root == ws.root


def test_lane_owner_of_an_ordinary_directory(fresh: Path) -> None:
    ws = init(fresh)

    assert lane_owner(ws.root) is None
    assert lane_owner(ws.path("hypotheses")) is None
    assert lane_owner(ws.path("logs", "op", "x")) is None


def test_lane_owner_without_a_workspace_above(tmp_path: Path) -> None:
    orphan = tmp_path / "runs" / "op" / "h"
    orphan.mkdir(parents=True)

    assert lane_owner(orphan) is None


def test_outside_a_workspace(tmp_path: Path) -> None:
    with pytest.raises(PreconditionError) as caught:
        find(tmp_path)

    assert caught.value.code == Exit.PRECONDITION
    assert CONFIG_NAME in caught.value.message
    assert "--workspace" in (caught.value.remedy or "")


def test_the_nearest_workspace_wins(fresh: Path) -> None:
    outer = init(fresh)
    inner = init(outer.path("nested"))

    assert find(inner.path("catalog")).root == inner.root
    assert find(outer.path("catalog")).root == outer.root
