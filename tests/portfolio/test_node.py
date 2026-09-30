"""The stage node's configuration: its venues, its per-order backstop and its identities."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from nautilus_trader.live.node import TradingNode

from kanso.errors import Exit, KansoError
from kanso.nautilus import node, sandbox
from kanso.nautilus.node import Placement, StageNode
from kanso.nautilus.venue import fill_model, latency_model, venue_configs
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
    assert venues[0].fill_model == fill_model("touch"), "the one the version was certified on"
    assert venues[0].latency_model is None


def test_a_stage_venue_fills_a_touched_limit_the_way_its_versions_were_measured(
    placement: Placement,
) -> None:
    """The stage's exchange is the venue model's, as a card's was: `through` stays through."""
    through = _limit_fill(placement, "through")

    (venue,) = node.venues_for((through,), 100_000.0)

    assert venue.fill_model == fill_model("through")


def test_two_versions_disagreeing_about_a_touched_limit_are_refused(
    ws: Workspace, store: StateStore, placement: Placement
) -> None:
    """One venue is one exchange, and an exchange has one fill model."""
    deployable(ws, store, "strict", doc=document(id="strict"))
    other = _limit_fill(a_placement(ws, "strict"), "through")

    with pytest.raises(KansoError) as raised:
        node.venues_for((placement, other), 100_000.0)

    assert raised.value.code == Exit.PRECONDITION
    assert f"venues.{VENUE}.costs.limit_fill" in raised.value.message
    assert "'touch'" in raised.value.message and "'through'" in raised.value.message
    assert "limit_fill" in str(raised.value.remedy)


def _limit_fill(placed: Placement, rule: str) -> Placement:
    """The same placement, certified under another limit-fill rule."""
    return _costs(placed, limit_fill=rule)


def _costs(placed: Placement, **costs: object) -> Placement:
    """The same placement, certified under a venue model whose costs differ in these keys."""
    model = placed.venue_model
    return replace(
        placed,
        venue_model=model.model_copy(update={"costs": model.costs.model_copy(update=costs)}),
    )


def _on_a_book(placed: Placement) -> Placement:
    """The same placement, certified by a hypothesis that requires a level-two book."""
    return replace(
        placed,
        hyp=hypothesis(
            id=placed.strategy_id,
            resolution="tick",
            horizon="1d",
            data_requirements=["book", "trade"],
        ),
    )


# --- the stage venue is the card's venue ----------------------------------------


def test_a_stage_venue_carries_the_latency_its_version_was_certified_under(
    placement: Placement,
) -> None:
    """A version certified under a round trip of 50 ms trades the stage under 50 ms, not
    under none: the latency model is the card's, built from the same venue model."""
    slow = _costs(placement, latency_ms=50)

    (venue,) = node.venues_for((slow,), 100_000.0)

    assert venue.latency_model == latency_model(50)


def test_a_stage_venue_keeps_the_book_its_versions_hypothesis_requires(
    placement: Placement,
) -> None:
    """A version certified on a level-two book with queue position trades the stage on
    one, rather than on the top-of-book venue a bar hypothesis gets."""
    (top,) = node.venues_for((placement,), 100_000.0)
    (deep,) = node.venues_for((_on_a_book(placement),), 100_000.0)

    assert (top.book_type, top.queue_position) == ("L1_MBP", False)
    assert (deep.book_type, deep.queue_position) == ("L2_MBP", True)


def test_a_stage_venue_is_field_for_field_the_cards_venue(placement: Placement) -> None:
    """One evaluation path: for one hypothesis, one venue model and one capital, the
    configuration a stage builds is the configuration a card builds, every field."""
    placed = _costs(_on_a_book(placement), latency_ms=50, limit_fill="through")

    (card,) = venue_configs(placed.hyp, placed.venue_model, CAPITAL)
    (stage,) = node.venues_for((placed,), CAPITAL)

    assert stage.dict() == card.dict()
    assert stage.latency_model == latency_model(50) and stage.book_type == "L2_MBP"


def test_the_stages_exchange_honours_the_latency_the_node_configures(
    placement: Placement,
) -> None:
    """The sandbox reads the node's configuration, so once the node carries the latency the
    stage's exchange waits it out: a placement certified at 50 ms yields an exchange whose
    latency model says 50 ms and whose command queue is on to honour it."""
    staged = a_node((_costs(placement, latency_ms=50),))
    built = TradingNode(config=staged.config(), loop=asyncio.new_event_loop())
    built.build()
    try:
        (venue,) = staged.venues()
        client = sandbox.SimulatedVenue(built.kernel, venue)

        assert client.exchange.latency_model is not None
        assert client.exchange.latency_model.base_latency_nanos == 50_000_000
        assert client.exchange.use_message_queue is True
    finally:
        built.dispose()


def test_two_versions_certified_under_different_latencies_are_refused(
    ws: Workspace, store: StateStore, placement: Placement
) -> None:
    """One venue is one round trip: a version certified at 0 ms and one at 50 ms cannot
    share an exchange, and the refusal names both."""
    deployable(ws, store, "slow", doc=document(id="slow"))
    slow = _costs(a_placement(ws, "slow"), latency_ms=50)

    with pytest.raises(KansoError) as raised:
        node.venues_for((placement, slow), 100_000.0)

    assert raised.value.code == Exit.PRECONDITION
    assert f"venues.{VENUE}.costs.latency_ms" in raised.value.message
    assert placement.label in raised.value.message and slow.label in raised.value.message
    assert "one venue is one round trip" in raised.value.message
    assert "separate stages" in str(raised.value.remedy)
    assert "re-certify" in str(raised.value.remedy)


@pytest.mark.parametrize("book_first", [True, False])
def test_a_book_version_and_a_top_of_book_version_are_refused_one_venue(
    ws: Workspace, store: StateStore, placement: Placement, book_first: bool
) -> None:
    """One venue keeps one book: a version certified on a level-two book and one on the
    top of the book cannot share it, whichever was deployed first."""
    deployable(ws, store, "deep", doc=document(id="deep"))
    deep = _on_a_book(a_placement(ws, "deep"))
    placed = (deep, placement) if book_first else (placement, deep)

    with pytest.raises(KansoError) as raised:
        node.venues_for(placed, 100_000.0)

    assert raised.value.code == Exit.PRECONDITION
    assert f"{deep.label} was certified on a level-two book" in raised.value.message
    assert f"{placement.label} on the top of the book" in raised.value.message
    assert "one venue keeps one book" in raised.value.message
    assert "separate stages" in str(raised.value.remedy)


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

    realised = node._realised(placement, request, None, ((),), {}, ())

    assert realised.run.returns == ()
    assert realised.run.trades == ()
    assert realised.positions == ()
    assert realised.pnl == 0.0


def test_a_version_with_nothing_released_holds_nothing(
    ws: Workspace, store: StateStore, composed_strategy: StrategyFile
) -> None:
    """A stage-mate's market moved and this version's did not: an empty hold, not a refusal."""
    held = hypothesis(
        id=composed_strategy.id,
        benchmark={"hold": "first_leg"},
        objective={"id": "wf_sharpe_vs_hold", "params": {"min_delta": 0.0, "k_se": 0.5}},
    )
    placed = a_placement(ws, composed_strategy.id, hyp=held)
    request = placed.request((date(2024, 3, 1), date(2024, 3, 2)))
    realised = node._realised(placed, request, None, ((),), {}, ())

    benchmarked = node._benchmarked(realised, placed, request, ((),), ())

    assert benchmarked.benchmark is not None
    assert (benchmarked.benchmark.returns, benchmarked.benchmark.fills) == ((), ())
    assert benchmarked.run == realised.run


def test_a_stage_window_holding_an_undeclared_split_is_refused_like_any_other(
    placement: Placement,
) -> None:
    """A stage extracts through the same gate a card and a replay do.

    A stage that reported a corporate action as return would promote on it, and a refusal
    one path makes and another does not is a divergence waiting to happen.
    """
    from nautilus_trader.model.identifiers import InstrumentId

    from kanso.data.types import CorporateAction
    from tests.replay.conftest import instrument

    window = (date(2024, 3, 1), date(2024, 3, 2))
    effective = 1_709_251_200_000_000_000  # 2024-03-01T00:00:00Z
    action = CorporateAction(
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        kind="split",
        ratio=0.1,
        cash=0.0,
        currency="USD",
        ex_date_ns=effective,
        ts_event=effective,
        ts_init=effective,
    )

    with pytest.raises(KansoError) as raised:
        node._realised(
            placement,
            placement.request(window),
            None,
            ((action,),),
            {},
            (instrument(),),
        )

    assert "the window holds a split effective 2024-03-01" in raised.value.message


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
