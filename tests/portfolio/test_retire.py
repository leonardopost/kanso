"""Retirement: the last move a version makes, and the stage it has to leave to make it."""

from __future__ import annotations

import pytest

from kanso.errors import Exit, KansoError
from kanso.portfolio import deploy, files, retire
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace
from tests.portfolio.conftest import reconfigure, second_version


def test_a_deployed_version_leaves_its_stage_and_is_retired(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    done = retire(ws, store, composed_strategy.id)

    assert (done.version, done.was, done.stages) == (1, "paper", ("paper",))
    assert strategy_files.require(ws, composed_strategy.id).versions[0].state == "retired"
    assert files.stage_of(files.read(ws), "paper").strategies == []
    assert [one.stage for one in done.deployments] == ["paper", "live"]
    assert done.halted == ()


def test_the_move_is_recorded_in_the_row_the_portfolio_queries(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    retire(ws, store, composed_strategy.id)

    row = store.connection.execute(
        "SELECT state, stage FROM strategy_versions WHERE strategy_id = ? AND version = 1",
        (composed_strategy.id,),
    ).fetchone()
    assert (row["state"], row["stage"]) == ("retired", None)
    kinds = [event.kind for event in store.events(subject=f"{composed_strategy.id}@1")]
    assert "retired" in kinds


def test_a_version_that_never_reached_a_stage_still_retires(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """Composition makes a version; retiring one nothing deployed is not a special case."""
    done = retire(ws, store, composed_strategy.id)

    assert (done.was, done.stages) == ("composed", ())
    assert strategy_files.require(ws, composed_strategy.id).versions[0].state == "retired"


def test_the_version_retired_by_default_is_the_latest(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """A retirement redeploys, so what is left composed takes the stage the retired one had."""
    second = second_version(ws, store, composed_strategy.id)

    done = retire(ws, store, composed_strategy.id)

    assert done.version == second
    states = [one.state for one in strategy_files.require(ws, composed_strategy.id).versions]
    assert states == ["paper", "retired"]


def test_a_version_may_be_named(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    second_version(ws, store, composed_strategy.id)

    done = retire(ws, store, composed_strategy.id, 1)

    assert done.version == 1
    states = [one.state for one in strategy_files.require(ws, composed_strategy.id).versions]
    assert states == ["retired", "paper"]


def test_a_version_the_strategy_does_not_have_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    with pytest.raises(KansoError) as raised:
        retire(ws, store, composed_strategy.id, 4)

    assert raised.value.code == Exit.PRECONDITION
    assert "has no version 4" in raised.value.message


def test_retiring_twice_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """The second retirement would restart two stages to change nothing."""
    retire(ws, store, composed_strategy.id)

    with pytest.raises(KansoError) as raised:
        retire(ws, store, composed_strategy.id)

    assert raised.value.code == Exit.PRECONDITION
    assert "already retired" in raised.value.message


def test_a_halted_stage_is_left_halted(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """The kill switch is the operator's, and a retirement does not clear it by restarting."""
    deploy(ws, store, "paper")
    reconfigure(ws, "paper", kill_switch=True)

    done = retire(ws, store, composed_strategy.id)

    assert done.halted == ("paper",)
    assert [one.stage for one in done.deployments] == ["live"]
    assert files.stage_of(files.read(ws), "paper").kill_switch is True
