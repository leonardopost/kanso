"""Reading the register, and the two questions the router asks of it."""

from __future__ import annotations

import pytest

from kanso.errors import Exit, PreconditionError
from kanso.models import REGISTER_NAME, escalation, first_for_tier, read_register, tiers_covered
from kanso.models.register import script_path
from kanso.schemas.models import TIERS
from kanso.workspace import Workspace

from .conftest import model, write_register


def test_the_register_is_read_from_the_workspace(ws: Workspace) -> None:
    register = read_register(ws)
    assert [m.id for m in register.models] == ["cheap_mock", "mid_mock", "frontier_mock"]


def test_a_workspace_with_no_register_refuses_and_names_the_file(ws: Workspace) -> None:
    ws.path(REGISTER_NAME).unlink()
    with pytest.raises(PreconditionError) as caught:
        read_register(ws)
    assert REGISTER_NAME in caught.value.message
    assert caught.value.code == Exit.PRECONDITION


def test_the_shipped_placeholder_register_parses(tmp_path_factory: pytest.TempPathFactory) -> None:
    """`init` writes a register an operator edits; it must be readable before they do."""
    from kanso.workspace import init

    workspace = init(tmp_path_factory.mktemp("plain") / "ws")
    assert {t for m in read_register(workspace).models for t in m.tiers} == set(TIERS)


def test_every_tier_needs_a_model(ws: Workspace) -> None:
    write_register(ws, [model("only_cheap", "cheap"), model("only_mid", "mid")])
    with pytest.raises(PreconditionError) as caught:
        tiers_covered(ws)
    assert "frontier" in caught.value.message
    assert caught.value.code == Exit.PRECONDITION


def test_a_full_register_covers_every_tier(ws: Workspace) -> None:
    assert tiers_covered(ws) is None


def test_one_model_may_serve_every_tier(ws: Workspace) -> None:
    write_register(ws, [model("mock", ["cheap", "mid", "frontier"])])
    assert tiers_covered(ws) is None
    for tier in TIERS:
        assert first_for_tier(read_register(ws), tier).id == "mock"


def test_a_tier_is_served_by_the_first_model_the_file_lists(ws: Workspace) -> None:
    write_register(
        ws,
        [
            model("cheap_mock", "cheap"),
            model("second", "mid"),
            model("first_listed_mid", "mid"),
            model("frontier_mock", "frontier"),
        ],
    )
    assert first_for_tier(read_register(ws), "mid").id == "second"


def test_asking_for_a_tier_with_no_model_refuses(ws: Workspace) -> None:
    write_register(ws, [model("only_cheap", "cheap")])
    with pytest.raises(PreconditionError, match="no model serves the frontier tier"):
        first_for_tier(read_register(ws), "frontier")


def test_escalation_climbs_one_tier_and_stops_at_the_top() -> None:
    assert escalation("cheap") == "mid"
    assert escalation("mid") == "frontier"
    assert escalation("frontier") is None


def test_a_mock_model_must_name_a_script(ws: Workspace) -> None:
    write_register(ws, [model("scriptless", "cheap") | {"script": None}])
    spec = read_register(ws).models[0]
    with pytest.raises(PreconditionError) as caught:
        script_path(ws.root, spec)
    assert "names no script" in caught.value.message


def test_a_script_path_is_relative_to_the_workspace_root(ws: Workspace) -> None:
    spec = read_register(ws).models[0]
    assert script_path(ws.root, spec) == ws.path("mock", "cheap_mock.yaml")
