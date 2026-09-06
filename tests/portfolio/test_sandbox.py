"""kanso's own simulated venue: what it fills, what binds it to a market, and how it is built.

These tests run real trading nodes against real simulated exchanges, because everything the
module claims is a claim about the engine: that a bar reaches the venue at all, that an order
larger than one book state fills in a node exactly as it fills in a backtest, and that the
engine's own convenience client is the reason it did not.

The comparison is the point. Neither path's numbers are written down here — a run of the
research path is measured, the same request is run in a node, and the two are required to be
the same object. Wire the node with the engine's client instead of kanso's and the same
comparison fails, which is what the second test measures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.accounting.margin_models import LeveragedMarginModel
from nautilus_trader.adapters.sandbox.config import SandboxExecutionClientConfig
from nautilus_trader.adapters.sandbox.execution import SandboxExecutionClient
from nautilus_trader.backtest.engine import SimulatedExchange
from nautilus_trader.backtest.models import FillModel, MakerTakerFeeModel
from nautilus_trader.common import Environment
from nautilus_trader.common.component import is_matching_py
from nautilus_trader.config import (
    BacktestVenueConfig,
    LiveExecEngineConfig,
    LoggingConfig,
    TradingNodeConfig,
)
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.data import Bar
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.events import OrderDenied
from nautilus_trader.model.identifiers import (
    ClientOrderId,
    InstrumentId,
    StrategyId,
    TraderId,
    Venue,
)
from nautilus_trader.model.objects import Quantity

from kanso.data.types.corporate_action import CorporateAction
from kanso.nautilus import backtest, sandbox, session
from kanso.nautilus.venue import venue_configs
from tests.replay.conftest import (
    CAPITAL,
    FORWARD,
    INSTRUMENT,
    REVERTING,
    VENUE,
    bars,
    hypothesis,
    instrument,
    quotes,
    request_for,
    trades,
    venue_model,
)

WINDOW = (date(2024, 3, 1), date(2024, 3, 3))

SHIPPED = f"data.*.{VENUE}.*"
"""What a live execution client's own `connect` subscribes its `on_data` handler to."""

DEEP = 10_000
"""A bar volume no order this hypothesis sizes can exhaust."""

THIN = 100
"""A bar volume one order exhausts, so a fill has to walk into the next price."""

TRADER = TraderId("KANSO-SANDBOX")


# --- the two paths ------------------------------------------------------------


def kanso_venues(request: Any, kernel: Any, points: Any) -> None:
    """The wiring a stage node already uses, applied to a replay session's node too.

    `kanso.nautilus.node` builds its simulated venues through `sandbox.attach`; a replay
    session still builds its own inline. Pointing the session at the same function is what
    makes the two one piece of code, and it is what the comparisons below measure.
    """
    sandbox.attach(kernel, venue_configs(request.hyp, request.venue_model, request.capital), points)


def vendor_venues(request: Any, kernel: Any, points: Any) -> None:
    """The same wiring around the engine's own convenience client, kept as the control.

    This is what kanso ran before it built its own venue, and it is the arm of the
    comparison that has to disagree — without it, a green suite would not say whether the
    agreement below was earned or merely shallow.
    """
    topics = sandbox.bar_topics(points)
    for venue in venue_configs(request.hyp, request.venue_model, request.capital):
        client = SandboxExecutionClient(
            loop=kernel.loop,
            portfolio=kernel.portfolio,
            msgbus=kernel.msgbus,
            cache=kernel.cache,
            clock=kernel.clock,
            config=SandboxExecutionClientConfig(
                venue=venue.name,
                starting_balances=list(venue.starting_balances),
                base_currency=venue.base_currency,
                oms_type=venue.oms_type,
                account_type=venue.account_type,
                default_leverage=Decimal(str(venue.default_leverage)),
                bar_execution=venue.bar_execution,
            ),
        )
        kernel.exec_engine.register_client(client)
        kernel.exec_engine.register_venue_routing(client, Venue(venue.name))
        for topic in topics.get(venue.name, ()):
            kernel.msgbus.subscribe(
                topic=topic, handler=client.on_data, priority=sandbox.MARKET_FIRST
            )


def both(volume: int, venues: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """One request over bars of this depth, run on the research path and then in a node."""
    groups = [thin(volume)]
    request = request_for(source=REVERTING, window=FORWARD)
    engine = backtest.execute(request, [instrument()], groups)
    monkeypatch.setattr(session, "_venues", venues)
    node = session.run_node(request, [instrument()], groups)
    return engine, node.result


def thin(volume: int) -> tuple[Bar, ...]:
    """The window's saw-tooth, restated with a volume an order may or may not exhaust."""
    return tuple(
        Bar(
            one.bar_type,
            one.open,
            one.high,
            one.low,
            one.close,
            Quantity.from_int(volume),
            ts_event=one.ts_event,
            ts_init=one.ts_init,
        )
        for one in bars(FORWARD)
    )


def test_an_order_larger_than_one_book_state_fills_the_same_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim this module exists for: one order, two engines, one sequence of fills."""
    engine, node = both(THIN, kanso_venues, monkeypatch)

    assert node.run.fills == engine.run.fills
    assert node.intents == engine.intents
    assert len(engine.run.fills) > len(engine.run.trades), "the order walked a second price"


def test_the_engines_own_client_fills_only_what_one_book_state_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: what does not fit is never filled, never cancelled and never reported."""
    engine, node = both(THIN, vendor_venues, monkeypatch)

    assert node.run.fills != engine.run.fills
    assert len(node.run.fills) < len(engine.run.fills)


def test_the_relay_is_what_the_agreement_rests_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point kanso's own venue at the node's bus and the missing fills come back.

    The exchange is otherwise built identically, so this isolates the one difference that
    matters: whether the first fill of an order has been applied to it by the time the venue
    decides what to do with the rest.
    """
    monkeypatch.setattr(sandbox, "relay", lambda kernel: kernel.msgbus)

    engine, node = both(THIN, kanso_venues, monkeypatch)

    assert len(node.run.fills) < len(engine.run.fills)


def test_the_two_paths_already_agreed_where_one_book_state_was_deep_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below that depth nothing changed, which is why the divergence stayed hidden."""
    engine, node = both(DEEP, kanso_venues, monkeypatch)

    assert node.run.fills == engine.run.fills
    assert node.run.equity == engine.run.equity


# --- the binding the engine does not make -------------------------------------


def test_the_shipped_subscription_reaches_a_quote_and_a_trade() -> None:
    assert is_matching_py(f"data.quotes.{VENUE}.DEMO", SHIPPED)
    assert is_matching_py(f"data.trades.{VENUE}.DEMO", SHIPPED)


def test_the_shipped_subscription_never_reaches_a_bar() -> None:
    # A bar is published to `data.bars.{bar_type}` and a bar type begins with the instrument
    # id, so the venue lands inside the last segment where the pattern needs a separator.
    # Without kanso's own subscription the exchange sees no market on a bar-only universe.
    topic = f"data.bars.DEMO.{VENUE}-1-DAY-LAST-EXTERNAL"
    assert not is_matching_py(topic, SHIPPED)


def test_bar_topics_names_every_grain_the_window_holds_by_venue() -> None:
    found = sandbox.bar_topics(bars(WINDOW))

    assert found == {VENUE: (f"data.bars.DEMO.{VENUE}-1-DAY-LAST-EXTERNAL",)}
    assert is_matching_py(found[VENUE][0], "data.bars.*")


def test_bar_topics_is_read_off_the_points_not_off_a_universe() -> None:
    assert sandbox.bar_topics(quotes(WINDOW)) == {}
    assert sandbox.bar_topics(trades(WINDOW)) == {}
    assert sandbox.bar_topics(()) == {}


def test_bar_topics_separates_two_instruments_of_one_venue() -> None:
    found = sandbox.bar_topics([*bars(WINDOW, "DEMO"), *bars(WINDOW, "OTHER")])

    assert set(found) == {VENUE}
    assert len(found[VENUE]) == 2
    assert found[VENUE] == tuple(sorted(found[VENUE])), "topics are ordered, so a node is"


# --- one node, built and disposed for the tests below -------------------------


@pytest.fixture
def kernel() -> Iterator[Any]:
    """A built live node's kernel: real engines with real queues, nothing started."""
    loop = asyncio.new_event_loop()
    node = TradingNode(
        config=TradingNodeConfig(
            environment=Environment.SANDBOX,
            trader_id=TRADER,
            logging=LoggingConfig(bypass_logging=True),
            exec_engine=LiveExecEngineConfig(reconciliation=False, inflight_check_interval_ms=0),
            data_clients={},
            exec_clients={},
        ),
        loop=loop,
    )
    node.build()
    try:
        yield node.kernel
    finally:
        node.dispose()


def venue() -> BacktestVenueConfig:
    """The one venue this hypothesis trades, as both code paths are configured from it."""
    return venue_configs(hypothesis(), venue_model(), CAPITAL)[0]


def turn(kernel: Any) -> None:
    """One turn of the node's loop, which is what a live engine's queue is filled on."""
    kernel.loop.run_until_complete(asyncio.sleep(0))


# --- the venue as it is configured --------------------------------------------


def test_the_exchange_carries_the_venue_model_unchanged(kernel: Any) -> None:
    """Nothing that touches a fill is stated twice: it is read off the one venue config."""
    configured = venue()

    client = sandbox.SimulatedVenue(kernel, configured)

    assert client.exchange.id == Venue(configured.name)
    assert client.exchange.account_type.name == configured.account_type
    assert client.exchange.base_currency.code == configured.base_currency
    assert [str(money) for money in client.exchange.starting_balances] == list(
        configured.starting_balances
    )
    assert client.exchange.default_leverage == Decimal(str(configured.default_leverage))
    assert client.exchange.bar_execution is configured.bar_execution
    assert client.exchange.oms_type.name == configured.oms_type


def test_the_exchange_is_built_the_way_the_research_path_builds_one(kernel: Any) -> None:
    """The models a backtest venue is given by default, and the book type it is given."""
    client = sandbox.SimulatedVenue(kernel, venue())

    assert isinstance(client.exchange.fill_model, FillModel)
    assert isinstance(client.exchange.fee_model, MakerTakerFeeModel)
    assert isinstance(client.exchange.margin_model, LeveragedMarginModel)
    assert client.exchange.latency_model is None
    assert client.exchange.book_type == BookType.L1_MBP


def test_the_exchange_matches_in_the_call_that_submits(kernel: Any) -> None:
    """Turning the queue on moves the match to the next point, which is a second bug.

    It is also what makes flattening a node possible: an order sent after the last point
    of a window has no next point to be matched on.
    """
    client = sandbox.SimulatedVenue(kernel, venue())

    assert client.exchange.use_message_queue is False


def test_the_venues_account_is_funded_before_anything_trades(kernel: Any) -> None:
    """The account exists in the node's cache, which the relay is what makes true."""
    client = sandbox.SimulatedVenue(kernel, venue())

    account = kernel.cache.account(client.account_id)

    assert account is not None
    assert str(account.balance_total()) in list(venue().starting_balances)


# --- the relay ----------------------------------------------------------------


def test_the_relay_carries_every_endpoint_the_node_carries(kernel: Any) -> None:
    """Anything an engine version adds keeps working, rather than being dropped silently."""
    taken: list[object] = []
    kernel.msgbus.register(endpoint="Probe.take", handler=taken.append)

    bus = sandbox.relay(kernel)

    assert set(bus.endpoints()) == set(kernel.msgbus.endpoints())
    bus.send("Probe.take", "a message for the node's own bus")
    assert taken == ["a message for the node's own bus"]


def test_an_execution_event_is_applied_rather_than_queued(kernel: Any) -> None:
    """The one endpoint that differs, and the whole reason the fills agree."""
    event = OrderDenied(
        trader_id=TRADER,
        strategy_id=StrategyId("S-1"),
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        client_order_id=ClientOrderId("O-1"),
        reason="nothing at all",
        event_id=UUID4(),
        ts_init=0,
    )

    kernel.msgbus.send(sandbox.EXEC_EVENTS, event)
    turn(kernel)
    queued = kernel.exec_engine.evt_qsize()
    sandbox.relay(kernel).send(sandbox.EXEC_EVENTS, event)
    turn(kernel)

    assert queued == 1, "the node's own route holds the event until its own task runs"
    assert kernel.exec_engine.evt_qsize() == queued, "the relay's route already applied it"


# --- the market, and the orders it matches ------------------------------------


def test_every_route_names_a_call_the_exchange_has(kernel: Any) -> None:
    """The dispatch table is data, so it is checked against the engine rather than trusted."""
    for kind, into in sandbox.ROUTES:
        assert callable(getattr(SimulatedExchange, into)), f"{kind.__name__} -> {into}"


def test_a_bar_moves_the_market_and_advances_the_venue(kernel: Any) -> None:
    """A bar the client is handed becomes a book the next order can be matched against."""
    kernel.cache.add_instrument(instrument())
    client = sandbox.SimulatedVenue(kernel, venue())
    client.connect()
    bar = bars(WINDOW)[0]

    client.on_data(bar)

    assert client.exchange.best_bid_price(bar.bar_type.instrument_id) is not None
    assert client.test_clock.timestamp_ns() == bar.ts_init


def test_a_point_of_an_unrouted_kind_still_advances_the_venue(kernel: Any) -> None:
    """A custom type published into a venue's topic space must not stop its clock."""
    client = sandbox.SimulatedVenue(kernel, venue())
    action = CorporateAction(
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        kind="dividend",
        ratio=1.0,
        cash=0.5,
        currency="USD",
        ex_date_ns=7,
        ts_event=5,
        ts_init=7,
    )

    client.on_data(action)

    assert client.test_clock.timestamp_ns() == 7


def test_every_order_command_is_the_exchanges_to_answer(kernel: Any) -> None:
    """This client routes; the exchange decides. Nothing is answered here."""
    client = sandbox.SimulatedVenue(kernel, venue())
    seen: list[str] = []

    class Recorder:
        def __getattr__(self, name: str) -> Any:
            return lambda command: seen.append(f"{name}({command})")

    client._client = Recorder()
    client.submit_order("a")
    client.submit_order_list("b")
    client.modify_order("c")
    client.cancel_order("d")
    client.cancel_all_orders("e")

    assert seen == [
        "submit_order(a)",
        "submit_order_list(b)",
        "modify_order(c)",
        "cancel_order(d)",
        "cancel_all_orders(e)",
    ]


# --- the client's own surface -------------------------------------------------


def test_connecting_takes_the_venues_instruments_and_reports_up(kernel: Any) -> None:
    kernel.cache.add_instrument(instrument())
    client = sandbox.SimulatedVenue(kernel, venue())

    client.connect()

    assert client.is_connected
    assert instrument().id in client.exchange.instruments


def test_disconnecting_reports_down(kernel: Any) -> None:
    client = sandbox.SimulatedVenue(kernel, venue())
    client.connect()

    client.disconnect()

    assert not client.is_connected


def test_a_simulated_venue_has_no_reports_to_reconcile(kernel: Any) -> None:
    """Its orders, fills and positions are the node's own, already in the node's cache."""
    client = sandbox.SimulatedVenue(kernel, venue())
    ran = kernel.loop.run_until_complete

    assert ran(client.generate_order_status_report(None)) is None
    assert ran(client.generate_order_status_reports(None)) == []
    assert ran(client.generate_fill_reports(None)) == []
    assert ran(client.generate_position_status_reports(None)) == []


# --- attaching ----------------------------------------------------------------


def test_attach_registers_one_venue_per_configuration_in_order(kernel: Any) -> None:
    configured = venue_configs(
        hypothesis(universe=[INSTRUMENT, "OTHER.XNYS"]), venue_model(), CAPITAL
    )

    made = sandbox.attach(kernel, configured, bars(WINDOW))

    assert [client.venue.value for client in made] == [one.name for one in configured]
    assert [client.id for client in made] == kernel.exec_engine.registered_clients


def test_attach_subscribes_each_venue_to_its_own_bars_above_a_strategy(kernel: Any) -> None:
    """The exchange is given the point, then the strategy is, which is the backtest's order."""
    sandbox.attach(kernel, [venue()], bars(WINDOW))

    topic = f"data.bars.DEMO.{VENUE}-1-DAY-LAST-EXTERNAL"
    subscribed = kernel.msgbus.subscriptions(topic)

    assert [one.priority for one in subscribed] == [sandbox.MARKET_FIRST]
    assert sandbox.MARKET_FIRST > 0


def test_attach_binds_nothing_where_the_window_holds_no_bar(kernel: Any) -> None:
    made = sandbox.attach(kernel, [venue()], quotes(WINDOW))

    assert len(made) == 1
    assert kernel.msgbus.subscriptions("data.bars.*") == []
