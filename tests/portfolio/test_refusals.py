"""Every refusal deployment makes, by name, and the one case each of them lets through."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from kanso.errors import Exit, KansoError
from kanso.portfolio import approve, deploy, files
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable, reconfigure, repin
from tests.replay.conftest import DOCUMENT, document

LATE = "2024-06-01"
"""A forward window opening after the last day this workspace's catalog serves."""


@pytest.fixture(autouse=True)
def _leave_the_interpreter_as_found() -> Iterator[None]:
    """An extension is imported by name, so each test's own must not outlive it."""
    modules = set(sys.modules)
    path = list(sys.path)
    yield
    for name in set(sys.modules) - modules:
        del sys.modules[name]
    sys.path[:] = path


def a_client(ws: Workspace, name: str, *, capital: str, clock: str) -> str:
    """An adapter declaring one execution client, as a broker package would."""
    directory = ws.path("kanso_ext")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(
        "from kanso.schemas import ExecutionClientSpec\n\n"
        f'EXEC_CLIENTS = [ExecutionClientSpec(id="{name}", capital="{capital}", '
        f'clock="{clock}")]\n',
        encoding="utf-8",
    )
    return name


def forward(start: str) -> dict[str, Any]:
    """The demo hypothesis's windows with its forward window moved."""
    windows = dict(DOCUMENT["windows"])  # type: ignore[arg-type]
    return {**windows, "forward": {"start": date.fromisoformat(start)}}


# --- exit 2: the four preconditions -------------------------------------------


def test_a_halted_stage_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    reconfigure(ws, "paper", kill_switch=True)

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION
    assert "kill_switch" in raised.value.message


def test_an_engine_pin_that_moved_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    repin(ws, composed_strategy.id, nautilus_version="0.0.1")

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION
    assert "0.0.1" in raised.value.message


def test_a_stage_with_no_data_at_or_after_the_forward_start_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "later", doc=document(id="later", windows=forward(LATE)))

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION
    assert LATE in raised.value.message


def test_a_wall_clock_client_fed_a_replay_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    name = a_client(ws, "brokerpaper", capital="broker_paper", clock="wall")
    reconfigure(ws, "paper", exec=name, data="replay", speed=1)

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION
    assert "live data client" in raised.value.message


def test_a_wall_clock_client_at_any_speed_but_one_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    name = a_client(ws, "brokerfeed", capital="broker_paper", clock="wall")
    reconfigure(ws, "paper", exec=name, data="brokerfeed", speed=4)

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION
    assert "speed 1" in raised.value.message


def test_an_execution_client_nothing_provides_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    reconfigure(ws, "paper", exec="ghost")

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION
    assert "sandbox" in raised.value.message


# --- exit 4: real capital -----------------------------------------------------


def onto_live_by_hand(ws: Workspace, store: StateStore, strategy_id: str) -> None:
    """Put a version on the live stage the way an operator with an editor would.

    This is the act the approval record exists to defeat: the file says the version is
    live, and nothing in the file says anyone allowed it.
    """
    from datetime import UTC, datetime

    file = strategy_files.require(ws, strategy_id)
    latest = file.latest()
    strategy_files.write(
        ws,
        file.model_copy(
            update={"versions": [*file.versions[:-1], latest.model_copy(update={"state": "live"})]}
        ),
    )
    portfolio = files.read(ws)
    live = files.place(
        files.stage_of(portfolio, "live"),
        strategy_id,
        latest.version,
        10_000.0,
        datetime.now(tz=UTC),
    )
    files.write(
        ws, files.with_stage(portfolio, "live", live.model_copy(update={"capital": 50_000}))
    )


def test_a_real_capital_client_refuses_a_version_with_no_approval(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    name = a_client(ws, "house", capital="real", clock="replay")
    onto_live_by_hand(ws, store, composed_strategy.id)
    reconfigure(ws, "live", exec=name)

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "live")

    assert raised.value.code == Exit.APPROVAL
    assert "no recorded operator approval" in raised.value.message


def test_a_real_capital_client_deploys_a_version_that_has_one(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    name = a_client(ws, "housetwo", capital="real", clock="replay")
    onto_live_by_hand(ws, store, composed_strategy.id)
    reconfigure(ws, "live", exec=name)
    approve(store, composed_strategy.id, 1, "Leonardo")

    made = deploy(ws, store, "live")

    assert [one.label for one in made.admitted] == [f"{composed_strategy.id}@1"]


def test_an_approval_of_another_version_does_not_carry(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    name = a_client(ws, "housethree", capital="real", clock="replay")
    onto_live_by_hand(ws, store, composed_strategy.id)
    reconfigure(ws, "live", exec=name)
    approve(store, composed_strategy.id, 2, "Leonardo")

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "live")

    assert raised.value.code == Exit.APPROVAL


def test_real_capital_off_the_live_stage_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    name = a_client(ws, "housefour", capital="real", clock="replay")
    reconfigure(ws, "paper", exec=name)

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.APPROVAL
    assert "only on the live stage" in raised.value.message
