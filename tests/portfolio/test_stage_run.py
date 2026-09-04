"""What a stage node does with a book, with a construct attached, and with a strategy that fails."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kanso.portfolio import deploy, files, records, set_state, show
from kanso.schemas import StrategyFile
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable, reconfigure
from tests.replay.conftest import (
    BLOCKING_FILTER,
    RAISING,
    REVERTING,
    composed,
    document,
)
from tests.replay.conftest import carded as a_card

BUYER = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    notional: float = 5_000.0


class Strategy(KansoStrategy):
    """Buys once and holds, so the window closes with a position still open."""

    config_cls = Config

    def on_start(self) -> None:
        self.bought = False

    def on_bar(self, bar) -> None:
        if not self.bought:
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", notional=self.kanso_config.notional
            )
            self.bought = True
'''


def with_filter(ws: Workspace, store: StateStore, hyp_id: str) -> StrategyFile:
    """A composed strategy whose sleeve carries a filter that refuses every entry."""
    a_card(ws, store, doc=document(id=hyp_id), strategy=REVERTING)
    file = composed(
        ws,
        store,
        hyp_id,
        sleeve=REVERTING,
        attached=(("blocker", "filter", BLOCKING_FILTER, {}),),
    )
    from kanso.strategy import files as strategy_files

    strategy_files.record(store, hyp_id, file.latest())
    return file


def test_the_book_is_measured_before_the_flatten_and_realised_by_it(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "holder", sleeve=BUYER, doc=document(id="holder"))

    made = deploy(ws, store, "paper")

    realised = made.results[0]
    assert [name for name, _, _ in realised.positions] == ["DEMO.XNAS"]
    assert realised.positions[0][1] > 0, "the window closed holding a long"
    assert realised.gross == pytest.approx(abs(realised.net))
    assert len(realised.run.trades) == 1, "the flatten closed the position it was holding"


def test_a_second_restart_finds_the_stage_flat(ws: Workspace, store: StateStore) -> None:
    deployable(ws, store, "holder", sleeve=BUYER, doc=document(id="holder"))
    deploy(ws, store, "paper")

    assert deploy(ws, store, "paper").results[0].positions == ()


def test_an_attached_construct_answers_only_its_own_sleeve(
    ws: Workspace, store: StateStore
) -> None:
    with_filter(ws, store, "guarded")
    deployable(ws, store, "plain", sleeve=REVERTING, doc=document(id="plain"))

    made = deploy(ws, store, "paper")

    by_id = {one.strategy_id: one for one in made.results}
    assert not any(trade.qty > 0 for trade in by_id["guarded"].run.trades), (
        "its filter refuses every entry, so it never opens a long"
    )
    assert any(trade.qty > 0 for trade in by_id["plain"].run.trades), (
        "and answers nobody else's sleeve, which trades the saw-tooth as it always does"
    )


def test_a_strategy_that_raises_halts_the_node_rather_than_the_process(
    ws: Workspace, store: StateStore
) -> None:
    deployable(ws, store, "boomer", sleeve=RAISING, doc=document(id="boomer"))

    made = deploy(ws, store, "paper")

    assert made.halted is not None
    assert made.session is not None
    assert made.results[0].run.trades == ()


def test_a_clock_past_the_catalog_runs_no_node(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    ahead = int(datetime(2024, 5, 1, tzinfo=UTC).timestamp()) * 1_000_000_000
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(ahead),))

    made = deploy(ws, store, "paper")

    assert made.session is None
    assert made.results == ()
    assert made.admitted, "the version stays deployed; there is simply nothing to replay"


def test_a_stage_that_has_fallen_behind_is_not_live(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    behind = int(datetime(2024, 3, 2, tzinfo=UTC).timestamp()) * 1_000_000_000
    store.connection.execute("UPDATE sessions SET clock_ts = ?", (str(behind),))

    assert show(ws, store).stage("paper").live is False


def test_a_halted_stage_is_never_live(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")
    files.halt(ws, "paper")

    paper = show(ws, store).stage("paper")
    assert paper.kill_switch is True
    assert paper.live is False
    assert paper.allocated == pytest.approx(paper.strategies[0].capital)


def test_clearing_the_kill_switch_lets_the_stage_deploy_again(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    files.halt(ws, "paper")
    files.halt(ws, "paper", on=False)

    assert deploy(ws, store, "paper").admitted


def test_a_universe_the_catalog_holds_nothing_for_has_no_last_day(ws: Workspace) -> None:
    from kanso.portfolio.deploy import served_to

    assert served_to(ws, ("GHOST.XNAS",)) is None
    assert served_to(ws, ()) is None


def test_a_broken_extension_leaves_the_clients_that_load(ws: Workspace) -> None:
    from kanso.portfolio import exec_clients

    directory = ws.path("kanso_ext")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "brokenext.py").write_text("this is not python(", encoding="utf-8")

    assert set(exec_clients(ws)) == {"sandbox"}


def test_a_state_move_is_written_to_the_file_and_the_store(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    written = set_state(ws, store, composed_strategy.id, 1, "promotable")

    assert written.latest().state == "promotable"
    row = store.connection.execute(
        "SELECT state FROM strategy_versions WHERE strategy_id = ?", (composed_strategy.id,)
    ).fetchone()
    assert row[0] == "promotable"


def test_the_deployment_reports_the_capital_it_committed(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    reconfigure(ws, "paper", capital=200_000.0)

    made = deploy(ws, store, "paper")

    assert made.capital == pytest.approx(80_000.0)
    assert made.admitted[0].label == f"{composed_strategy.id}@1"
    assert records.subject_of(composed_strategy.id, 1) == made.admitted[0].label
