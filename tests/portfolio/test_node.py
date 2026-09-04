"""The stage node's configuration: its venues, its per-order backstop and its identities."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kanso.errors import Exit, KansoError
from kanso.nautilus import node
from kanso.nautilus.node import Placement, StageNode
from kanso.portfolio import deploy
from kanso.schemas import Limits, StrategyFile
from kanso.state import StateStore
from kanso.strategy import load as load_impl
from kanso.workspace import Workspace
from tests.portfolio.conftest import deployable
from tests.replay.conftest import CAPITAL, INSTRUMENT, VENUE, document, hypothesis, venue_model

LIMITS = Limits(max_gross_pct=100, max_net_pct=100, per_strategy_max_pct=40, daily_loss_pct=3)


def a_placement(
    ws: Workspace,
    strategy_id: str,
    *,
    capital: float = CAPITAL,
    hyp: object | None = None,
) -> Placement:
    """One deployed version, loaded from the implementation a stage would load."""
    return Placement(
        strategy_id=strategy_id,
        version=1,
        capital=capital,
        hyp=hyp or hypothesis(id=strategy_id),  # type: ignore[arg-type]
        venue_model=venue_model(),
        snapshot_id="a" * 64,
        period="1d",
        source=b"",
        loaded=load_impl(ws, strategy_id, 1),
    )


@pytest.fixture
def placement(ws: Workspace, store: StateStore, composed_strategy: StrategyFile) -> Placement:
    """The demo sleeve as a node would hold it."""
    return a_placement(ws, composed_strategy.id)


def a_node(placements: tuple[Placement, ...], capital: float = 100_000.0) -> StageNode:
    """A stage node over a one-day window; nothing here runs it."""
    return StageNode(
        stage="paper",
        capital=capital,
        limits=LIMITS,
        placements=placements,
        window=(date(2024, 3, 1), date(2024, 3, 2)),
        catalog=Path("."),
    )


def test_a_trader_is_named_for_its_stage() -> None:
    assert node.trader_id("paper").value == "KANSO-PAPER"
    assert node.trader_id("live").value == "KANSO-LIVE"


def test_the_per_order_backstop_is_one_strategys_whole_allowance(placement: Placement) -> None:
    capped = node.max_notional_per_order((placement,), LIMITS, 100_000.0)

    assert capped == {INSTRUMENT: 40_000}


def test_the_backstop_covers_every_instrument_any_version_trades(
    ws: Workspace, store: StateStore, placement: Placement
) -> None:
    deployable(ws, store, "wide", doc=document(id="wide", universe=[INSTRUMENT]))
    other = a_placement(ws, "wide")

    capped = node.max_notional_per_order((placement, other), LIMITS, 50_000.0)

    assert capped == {INSTRUMENT: 20_000}


def test_a_venue_is_funded_once_with_the_whole_stage_capital(placement: Placement) -> None:
    venues = node.venues_for((placement,), 100_000.0)

    assert [venue.name for venue in venues] == [VENUE]
    assert venues[0].starting_balances == ["100000.00 USD"]
    assert venues[0].fee_model is None, "the runner applies costs once, not the venue"
    assert venues[0].fill_model is None
    assert venues[0].latency_model is None


def test_a_stage_with_no_capital_cannot_fund_a_venue(placement: Placement) -> None:
    with pytest.raises(KansoError) as raised:
        node.venues_for((placement,), 0.0)

    assert raised.value.code == Exit.VALIDATION


def test_two_versions_on_one_venue_take_the_higher_leverage(
    ws: Workspace, store: StateStore, placement: Placement
) -> None:
    deployable(ws, store, "levered", doc=document(id="levered"))
    geared = a_placement(
        ws,
        "levered",
        hyp=hypothesis(
            id="levered",
            risk_limits={"max_position_pct": 20, "max_drawdown_pct": 40, "max_leverage": 3},
        ),
    )

    venues = node.venues_for((placement, geared), 100_000.0)

    assert [venue.default_leverage for venue in venues] == [3.0]


def test_two_versions_disagreeing_about_a_venues_account_are_refused(
    ws: Workspace, store: StateStore, placement: Placement
) -> None:
    from dataclasses import replace

    from kanso.schemas import resolve_venue_model
    from kanso.schemas.venue import CostsOverride, VenueOverride

    deployable(ws, store, "cashy", doc=document(id="cashy"))
    other = replace(
        a_placement(ws, "cashy"),
        venue_model=resolve_venue_model(
            VENUE,
            override=VenueOverride(
                account="cash", costs=CostsOverride(spread="fixed_bps", fixed_bps=2)
            ),
            quotes_available=False,
        ),
    )

    with pytest.raises(KansoError) as raised:
        node.venues_for((placement, other), 100_000.0)

    assert raised.value.code == Exit.PRECONDITION
    assert "one venue is one account" in raised.value.message


def test_a_node_with_nothing_deployed_has_nothing_to_run() -> None:
    with pytest.raises(KansoError) as raised:
        node.run(a_node(()))

    assert raised.value.code == Exit.PRECONDITION


def test_the_node_configuration_carries_the_backstop_and_no_client(placement: Placement) -> None:
    config = a_node((placement,)).config()

    assert config.trader_id.value == "KANSO-PAPER"
    assert config.risk_engine is not None
    assert config.risk_engine.max_notional_per_order == {INSTRUMENT: 40_000}
    assert config.data_clients == {}
    assert config.exec_clients == {}


def test_each_version_runs_under_an_identity_of_its_own(placement: Placement) -> None:
    assert placement.tag == f"{placement.strategy_id}-1"
    assert placement.identity.endswith(placement.tag)
    assert placement.label == f"{placement.strategy_id}@1"


def test_a_restart_with_nothing_new_runs_no_node_at_all(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    deploy(ws, store, "paper")

    second = deploy(ws, store, "paper")

    assert second.session is not None
    assert second.session.released == 0
    assert second.results[0].run.returns == ()
    assert second.results[0].positions == ()


def test_a_version_with_nothing_new_gets_an_empty_window_rather_than_a_refusal(
    placement: Placement,
) -> None:
    request = placement.request((date(2024, 3, 1), date(2024, 3, 2)))

    realised = node._realised(placement, request, None, ((),), {})

    assert realised.run.returns == ()
    assert realised.run.trades == ()
    assert realised.positions == ()
    assert realised.pnl == 0.0


def test_a_book_carries_its_signed_exposure() -> None:
    from kanso.nautilus.node import Book

    assert Book(instrument_id="DEMO.XNAS", qty=-3.0, price=10.0).notional == pytest.approx(-30.0)


def test_a_stage_run_says_whether_the_node_stopped_itself() -> None:
    from kanso.nautilus.node import StageRun

    made = StageRun(
        stage="paper",
        window=(date(2024, 3, 1), date(2024, 3, 2)),
        points=(),
        released=0,
        clock_ns=None,
        realised=(),
        intents=(),
    )

    assert made.crashed is False
    assert node.StageRun(**{**made.__dict__, "halted": "it raised"}).crashed is True
