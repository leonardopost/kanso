"""Promotion, demotion, and the kill switch that must not deadlock either of them."""

from __future__ import annotations

import pytest

from kanso import strategy as strategies
from kanso.errors import Exit, KansoError
from kanso.portfolio import approvals, demote, deploy, files, promote
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace
from tests.portfolio.conftest import reconfigure, second_version


def make_promotable(ws: Workspace, store: StateStore, strategy_id: str) -> int:
    """Put the deployed paper version into `promotable`, as passing paper gates would."""
    file = strategy_files.require(ws, strategy_id)
    latest = file.latest()
    strategy_files.write(
        ws,
        file.model_copy(
            update={
                "versions": [
                    *file.versions[:-1],
                    latest.model_copy(update={"state": "promotable"}),
                ]
            }
        ),
    )
    store.connection.execute(
        "UPDATE strategy_versions SET state = 'promotable' WHERE strategy_id = ? AND version = ?",
        (strategy_id, latest.version),
    )
    return latest.version


@pytest.fixture
def promotable(ws: Workspace, store: StateStore, composed_strategy: StrategyFile) -> str:
    """A version on the paper stage whose paper gates have passed."""
    deploy(ws, store, "paper")
    reconfigure(ws, "live", capital=50_000.0)
    make_promotable(ws, store, composed_strategy.id)
    return composed_strategy.id


# --- promotion ----------------------------------------------------------------


def test_a_promotion_without_a_name_is_refused(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable)

    assert raised.value.code == Exit.APPROVAL
    assert "--as NAME" in (raised.value.remedy or "")
    assert approvals(store, promotable, 1) == []


def test_a_blank_name_is_no_name(ws: Workspace, store: StateStore, promotable: str) -> None:
    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="   ")

    assert raised.value.code == Exit.APPROVAL


def test_a_promotion_records_the_approval_and_moves_the_version(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    made = promote(ws, store, promotable, operator="Leonardo")

    assert made.approval.operator == "Leonardo"
    assert [one.operator for one in approvals(store, promotable, 1)] == ["Leonardo"]
    assert strategies.require(ws, promotable).latest().state == "live"
    live = files.stage_of(files.read(ws), "live")
    assert files.held(live, promotable) is not None
    assert files.held(files.stage_of(files.read(ws), "paper"), promotable) is None


def test_a_named_version_in_the_right_state_is_promoted(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    made = promote(ws, store, promotable, version=1, operator="Leonardo")

    assert made.version == 1
    assert made.label == f"{promotable}@1"
    assert made.retired is None


def test_a_second_promotion_retires_the_version_it_replaces(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")

    assert strategies.require(ws, promotable).latest().state == "live"


def test_a_demotion_names_itself(ws: Workspace, store: StateStore, promotable: str) -> None:
    promote(ws, store, promotable, operator="Leonardo")

    made = demote(ws, store, promotable)

    assert made.label == f"{promotable}@1"


def test_a_promotion_redeploys_both_stages(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    made = promote(ws, store, promotable, operator="Leonardo")

    assert [one.stage for one in made.deployments] == ["live", "paper"]
    assert [one.label for one in made.deployments[0].admitted] == [f"{promotable}@1"]
    assert made.deployments[1].admitted == ()


def test_a_version_that_is_not_promotable_is_refused(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    with pytest.raises(KansoError) as raised:
        promote(ws, store, composed_strategy.id, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION


def test_a_named_version_in_the_wrong_state_is_refused(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, version=99, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION


def test_a_live_stage_with_no_capital_blocks_the_promotion(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    reconfigure(ws, "live", capital=0.0)

    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION
    assert approvals(store, promotable, 1) == [], "nothing is approved that cannot be funded"
    kinds = store.connection.execute("SELECT kind FROM escalations").fetchall()
    assert [row[0] for row in kinds] == ["deploy_blocked"]


def test_a_halted_live_stage_refuses_a_promotion_into_it(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    reconfigure(ws, "live", kill_switch=True)

    with pytest.raises(KansoError) as raised:
        promote(ws, store, promotable, operator="Leonardo")

    assert raised.value.code == Exit.PRECONDITION
    assert approvals(store, promotable, 1) == []


# --- demotion -----------------------------------------------------------------


def test_a_demotion_returns_the_version_to_the_paper_stage(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")

    made = demote(ws, store, promotable)

    assert made.state == "paper"
    assert strategies.require(ws, promotable).latest().state == "paper"
    assert files.held(files.stage_of(files.read(ws), "live"), promotable) is None
    assert files.held(files.stage_of(files.read(ws), "paper"), promotable) is not None
    assert made.halted == ()


def test_a_demotion_of_a_version_that_is_not_live_is_refused(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        demote(ws, store, promotable)

    assert raised.value.code == Exit.PRECONDITION


def test_an_unknown_strategy_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(KansoError) as raised:
        demote(ws, store, "nothing_here")

    assert raised.value.code == Exit.PRECONDITION


# --- the kill switch and the demotion it triggers -----------------------------


def test_a_halted_stage_stays_halted_through_a_demotion(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    reconfigure(ws, "live", kill_switch=True)

    made = demote(ws, store, promotable)

    assert made.halted == ("live",)
    assert [one.stage for one in made.deployments] == ["paper"]
    assert files.stage_of(files.read(ws), "live").kill_switch is True
    assert strategies.require(ws, promotable).latest().state == "paper"


def test_the_demotion_names_the_halted_stage_and_how_to_resume_it(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    reconfigure(ws, "live", kill_switch=True)

    demote(ws, store, promotable)

    row = store.connection.execute(
        "SELECT kind, subject, summary, actions FROM escalations WHERE kind = 'demoted'"
    ).fetchone()
    assert row is not None
    assert row[1] == f"{promotable}@1"
    assert "live" in row[2]
    assert "kanso portfolio deploy --stage live" in row[3]


def test_both_stages_halted_still_moves_the_version(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    reconfigure(ws, "live", kill_switch=True)
    reconfigure(ws, "paper", kill_switch=True)

    made = demote(ws, store, promotable)

    assert made.halted == ("paper", "live")
    assert made.deployments == ()
    assert strategies.require(ws, promotable).latest().state == "paper"


def test_a_named_version_that_is_not_live_cannot_be_demoted(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    with pytest.raises(KansoError) as raised:
        demote(ws, store, promotable, version=1)

    assert raised.value.code == Exit.PRECONDITION
    assert "promotable, not live" in raised.value.message


def test_promoting_a_second_version_retires_the_one_it_replaces(
    ws: Workspace, store: StateStore, promotable: str
) -> None:
    promote(ws, store, promotable, operator="Leonardo")
    second_version(ws, store, promotable)
    deploy(ws, store, "paper")
    make_promotable(ws, store, promotable)

    made = promote(ws, store, promotable, operator="Leonardo")

    assert made.version == 2
    assert made.retired == 1
    assert [v.state for v in strategies.require(ws, promotable).versions] == ["retired", "live"]
    live = files.stage_of(files.read(ws), "live")
    held = files.held(live, promotable)
    assert held is not None
    assert held.version == 2
