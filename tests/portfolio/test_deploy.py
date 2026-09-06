"""Deployment: what it admits, what it funds, what it runs and what it refuses."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kanso.errors import Exit, KansoError
from kanso.portfolio import deploy, files, records, show
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.portfolio.conftest import FORWARD_START, deployable, second_version


def test_deploy_admits_a_composed_version_and_runs_the_stage(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    made = deploy(ws, store, "paper")

    assert [one.label for one in made.admitted] == [f"{composed_strategy.id}@1"]
    assert made.blocked == ()
    assert made.session is not None
    assert made.session.mode == "paper"
    assert made.session.released > 0
    assert made.halted is None


def test_the_stage_file_records_the_version_and_its_capital(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    stage = files.stage_of(files.read(ws), "paper")
    entry = files.held(stage, composed_strategy.id)
    assert entry is not None
    assert entry.version == 1
    assert entry.capital == pytest.approx(0.4 * stage.capital)


def test_the_version_becomes_paper_in_the_file_and_in_the_store(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    from kanso import strategy as strategies

    assert strategies.require(ws, composed_strategy.id).latest().state == "paper"
    row = store.connection.execute(
        "SELECT state, stage, capital FROM strategy_versions WHERE strategy_id = ?",
        (composed_strategy.id,),
    ).fetchone()
    assert (row[0], row[1]) == ("paper", "paper")
    assert row[2] > 0


def test_the_node_realises_a_window_into_the_record(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    made = deploy(ws, store, "paper")

    assert len(made.results) == 1
    realised = made.results[0]
    assert realised.stage == "paper"
    assert realised.run.window[0] == FORWARD_START, "a stage lives in the forward window"
    assert realised.run.trades, "the saw-tooth sleeve trades the forward window"
    assert records.stage_results(store, strategy_id=composed_strategy.id, version=1)


def test_a_second_deploy_resumes_from_the_session_clock(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    first = deploy(ws, store, "paper")
    assert first.session is not None
    clock = first.session.clock_ns

    second = deploy(ws, store, "paper")

    assert deploy_module_clock(store) == clock, "nothing new was replayed, so the clock stands"
    assert second.session is not None
    assert second.session.released == 0


def deploy_module_clock(store: StateStore) -> int | None:
    """The paper stage's clock, as the next restart would read it."""
    from kanso.portfolio import clock_of

    return clock_of(store, "paper")


def test_a_stage_always_restarts_flat(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    made = deploy(ws, store, "paper")

    assert made.results
    # The book measured is the one the window closed with; the node flattened after it, so
    # nothing survives into the next restart.
    assert deploy(ws, store, "paper").results[0].positions == ()


def test_capital_is_inherited_by_the_next_version(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    stage = files.stage_of(files.read(ws), "paper")
    inherited = files.held(stage, composed_strategy.id)
    assert inherited is not None
    files.write(
        ws,
        files.with_stage(
            files.read(ws),
            "paper",
            files.place(
                stage,
                composed_strategy.id,
                1,
                inherited.capital / 2,
                inherited.joined_at,
            ),
        ),
    )

    second = second_version(ws, store, composed_strategy.id)
    made = deploy(ws, store, "paper")

    assert [one.version for one in made.admitted] == [second]
    assert made.admitted[0].capital == pytest.approx(inherited.capital / 2)
    assert made.retired == (f"{composed_strategy.id}@1",)


def test_a_replaced_version_retires(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    second_version(ws, store, composed_strategy.id)

    deploy(ws, store, "paper")

    from kanso import strategy as strategies

    versions = strategies.require(ws, composed_strategy.id).versions
    assert [v.state for v in versions] == ["retired", "paper"]


def test_a_stage_with_no_capital_left_blocks_rather_than_deploying_at_nothing(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    from tests.portfolio.conftest import reconfigure

    reconfigure(ws, "paper", capital=0.0)

    made = deploy(ws, store, "paper")

    assert made.admitted == ()
    assert made.blocked == (f"{composed_strategy.id}@1",)
    unread = store.connection.execute(
        "SELECT kind, subject FROM escalations ORDER BY created_at"
    ).fetchall()
    assert [(row[0], row[1]) for row in unread] == [("deploy_blocked", f"{composed_strategy.id}@1")]


def test_two_versions_share_a_stage_and_are_measured_apart(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    from tests.replay.conftest import FLAT, document

    deployable(ws, store, "quiet", sleeve=FLAT, doc=document(id="quiet"))

    made = deploy(ws, store, "paper")

    assert sorted(one.label for one in made.admitted) == [
        f"{composed_strategy.id}@1",
        "quiet@1",
    ]
    by_id = {result.strategy_id: result for result in made.results}
    assert by_id[composed_strategy.id].run.trades
    assert by_id["quiet"].run.trades == (), "a sleeve that trades nothing records nothing"


def test_show_reports_the_stage_its_liveness_and_the_realised_pnl(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    report = show(ws, store)
    paper = report.stage("paper")

    assert paper.live is True
    assert paper.exec_id == "sandbox"
    assert [one.label for one in paper.strategies] == [f"{composed_strategy.id}@1"]
    assert paper.strategies[0].windows == 1
    assert paper.strategies[0].recorded is True
    assert paper.pnl == pytest.approx(paper.strategies[0].pnl)
    assert report.stage("live").live is False


def hand_add(ws: Workspace, stage: str, strategy_id: str, version: int = 1) -> None:
    """Add an entry to a stage of `portfolio.yaml` by hand, as an operator can.

    This is the one edit the file admits that no command would make: it funds nothing,
    approves nothing and starts nothing, and `deploy` reads the store rather than this.
    """
    portfolio = files.read(ws)
    held = files.stage_of(portfolio, stage)
    placed = files.place(
        held.model_copy(update={"capital": 100_000.0}),
        strategy_id,
        version,
        40_000.0,
        datetime.now(tz=UTC),
    )
    files.write(ws, files.with_stage(portfolio, stage, placed))


def test_show_marks_a_stage_entry_the_record_does_not_know(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """A hand-added entry is the file's claim, and `deploy` admits nothing for it."""
    hand_add(ws, "live", composed_strategy.id)

    live = show(ws, store).stage("live")

    assert [one.label for one in live.strategies] == [f"{composed_strategy.id}@1"]
    assert live.strategies[0].recorded is False
    assert live.held == (), "nothing the record knows is on the stage"
    assert deploy(ws, store, "live").admitted == ()


def test_an_entry_the_record_does_not_know_holds_no_capital_and_no_stage(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """It is counted in nothing: not the allocation, not the P&L, not the liveness."""
    deploy(ws, store, "paper")
    hand_add(ws, "live", composed_strategy.id)

    live = show(ws, store).stage("live")

    assert live.allocated == 0.0
    assert live.pnl == 0.0
    assert live.live is False
    assert live.served_to is None, "a stage entry no node ran needs no data served"


def test_a_version_the_record_forgot_is_not_deployed_and_agrees_with_show(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """A clone's `strategy.yaml` says paper while its `state.db` never travelled.

    `deploy` reads the record, as `portfolio show` does, so it admits nothing and runs no
    node for a version the record does not know — rather than admitting from the file, which
    left `show` calling the stage down while `deploy` and the monitor called it deployed.
    """
    from kanso.portfolio.deploy import restated
    from kanso.strategy import files as strategy_files

    # a clone: strategy.yaml marks the version paper and portfolio.yaml lists it, exactly as
    # the original committed them, but the record a deployment writes never travelled
    strategy_files.write(ws, restated(composed_strategy, {1: "paper"}))
    hand_add(ws, "paper", composed_strategy.id)
    store.connection.execute("DELETE FROM strategy_versions")

    assert show(ws, store).stage("paper").held == (), "show already reads the record"

    made = deploy(ws, store, "paper")

    assert made.admitted == (), "deploy admits nothing the record does not know"
    assert made.session is None, "and runs no node, so the monitor finds no book to judge"
    after = show(ws, store).stage("paper")
    assert after.held == () and after.live is False
    assert (
        records.stage_results(store, strategy_id=composed_strategy.id, version=1, stage="paper")
        == []
    ), "no phantom window was realised for a version the record does not hold"


def test_show_on_an_undeployed_workspace_reports_two_empty_stages(
    ws: Workspace, store: StateStore
) -> None:
    report = show(ws, store)

    assert [one.stage for one in report.stages] == ["paper", "live"]
    assert all(one.strategies == () and one.live is False for one in report.stages)
    assert report.stage("paper").clock is None


def test_a_workspace_with_no_portfolio_file_is_refused(ws: Workspace, store: StateStore) -> None:
    files.portfolio_file(ws).unlink()

    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "paper")

    assert raised.value.code == Exit.PRECONDITION


def test_a_name_that_is_not_a_stage_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(KansoError) as raised:
        deploy(ws, store, "shadow")

    assert raised.value.code == Exit.VALIDATION
