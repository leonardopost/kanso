"""The node session: one window, two code paths, and the wiring that makes them agree.

These tests run a real trading node against a real simulated venue, because everything the
module claims is a claim about the engine: that a bar reaches the exchange, that a fill lands
before the next point is released, that an exception stops the node rather than the process.
None of that can be established against a double.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
from nautilus_trader.model.identifiers import ClientId

from kanso.errors import PreconditionError
from kanso.nautilus import backtest, session
from kanso.nautilus.session import Halt, ordered
from tests.replay.conftest import (
    BLOCKING_FILTER,
    FLAT,
    FORWARD,
    INSTRUMENT,
    RAISING,
    bars,
    hypothesis,
    instrument,
    quotes,
    request_for,
    trades,
)

OTHER = "OTHR"
OTHER_ID = f"{OTHER}.XNAS"

DAY_IN_A_BLINK = 1e9
"""A speed that replays a day of pacing in under a tenth of a millisecond."""


def both(request: backtest.RunRequest, instruments: list[object], groups: list[tuple[object, ...]]):
    """The same request on both code paths."""
    engine = backtest.execute(request, instruments, groups)
    node = session.run_node(request, instruments, groups)
    return node.result, engine


# --- the claim ----------------------------------------------------------------


def test_the_live_path_submits_what_the_research_path_submits() -> None:
    """One strategy, one window, two engines, one sequence of order intents."""
    node, engine = both(request_for(), [instrument()], [tuple(bars(FORWARD))])

    assert node.intents == engine.intents
    assert node.intents


def test_the_two_paths_agree_on_quotes_and_trades_too() -> None:
    """Every data requirement a hypothesis can declare reaches both exchanges alike."""
    hyp = hypothesis(data_requirements=["bar", "quote", "trade"])
    groups = [tuple(bars(FORWARD)), tuple(quotes(FORWARD)), tuple(trades(FORWARD))]

    node, engine = both(request_for(hyp=hyp), [instrument()], groups)

    assert node.intents == engine.intents


def test_the_two_paths_agree_across_two_instruments() -> None:
    """Points sharing an instant are released in the order the engine would deliver them."""
    hyp = hypothesis(universe=[INSTRUMENT, OTHER_ID])
    groups = [tuple(bars(FORWARD)), tuple(bars(FORWARD, OTHER))]

    node, engine = both(request_for(hyp=hyp), [instrument(), instrument(OTHER)], groups)

    assert node.intents == engine.intents


def test_the_two_paths_agree_with_a_construct_attached() -> None:
    """An attached construct is an actor on both paths and decides the same thing on each."""
    request = request_for(source=FLAT, modifiers=(("filter", BLOCKING_FILTER, {"allow": True}),))

    node, engine = both(request, [instrument()], [tuple(bars(FORWARD))])

    assert node.intents == engine.intents


def test_the_measured_run_is_the_same_on_both_paths() -> None:
    """One extraction, one cost model: the equity curve matches, not only the orders."""
    node, engine = both(request_for(), [instrument()], [tuple(bars(FORWARD))])

    assert node.run.equity == engine.run.equity
    assert [trade.pnl_net for trade in node.run.trades] == [
        trade.pnl_net for trade in engine.run.trades
    ]


def test_the_paths_agree_at_a_speed_as_well_as_at_speed_zero() -> None:
    """The flow control holds at every speed; pacing is on top of it, not instead of it."""
    request = request_for()
    groups = [tuple(bars(FORWARD))]
    engine = backtest.execute(request, [instrument()], groups)

    paced = session.run_node(request, [instrument()], groups, speed=DAY_IN_A_BLINK)

    assert paced.intents == engine.intents


def test_a_free_running_feed_does_not_agree() -> None:
    """Without the wait the node fills bars late, which is the whole reason for the wait."""
    request = request_for()
    groups = [tuple(bars(FORWARD))]
    engine = backtest.execute(request, [instrument()], groups)

    loose = session.run_node(request, [instrument()], groups, settle_turns=0)

    assert loose.intents != engine.intents


# --- what makes it work -------------------------------------------------------


def test_a_bar_reaches_the_simulated_venue() -> None:
    """The sandbox's own subscription never matches a bar, so the session subscribes it."""
    node, _ = both(request_for(), [instrument()], [tuple(bars(FORWARD))])

    assert node.run.trades
    assert node.run.fills


def test_ordered_is_the_stable_sort_the_engine_uses() -> None:
    """Points sharing an instant keep the order their groups were added in."""
    first, second = bars(FORWARD), bars(FORWARD, OTHER)

    stream = ordered([tuple(first), tuple(second)])

    assert len(stream) == len(first) + len(second)
    assert stream[0] is first[0]
    assert stream[1] is second[0]


# --- failure ------------------------------------------------------------------


def test_a_strategy_that_raises_stops_the_node_rather_than_the_process() -> None:
    """A live engine would kill the interpreter; the session asks for a graceful stop."""
    replayed = session.run_node(request_for(source=RAISING), [instrument()], [tuple(bars(FORWARD))])

    assert replayed.result.crashed
    assert replayed.result.reason == backtest.EXCEPTION
    assert replayed.intents == ()
    assert replayed.released < len(bars(FORWARD))
    assert replayed.clock_ns is not None


def test_a_window_with_no_points_is_refused() -> None:
    """A range the catalog serves nothing for is a precondition failure, not a silent run."""
    with pytest.raises(PreconditionError, match="holds nothing"):
        session.run_node(request_for(), [instrument()], [])


def test_a_point_outside_the_window_is_refused() -> None:
    """A session handed data from another range refuses it rather than trading on it."""
    request = request_for(window=(date(2024, 3, 1), date(2024, 3, 10)))

    with pytest.raises(PreconditionError, match="lies outside the requested window"):
        session.run_node(request, [instrument()], [tuple(bars(FORWARD))])


def test_a_halt_takes_the_reason_the_engine_gave() -> None:
    """The shutdown command carries why, and the session reports that rather than a guess."""
    halt = Halt()

    assert halt.running()
    halt.record(_Command("queue processing failed"))
    assert not halt.running()
    assert halt.reason == "queue processing failed"


def test_a_halt_without_a_reason_still_stops_the_feed() -> None:
    """A shutdown nobody explained is still a shutdown."""
    halt = Halt()
    halt.record(_Command(None))

    assert halt.reason == session.STOPPED


class _Command:
    """A shutdown command, as the message bus delivers one."""

    def __init__(self, reason: str | None) -> None:
        self.reason = reason


# --- the client's own surface -------------------------------------------------


LIFECYCLE = ("_connect", "_disconnect")

SUBSCRIPTIONS = (
    "_subscribe",
    "_subscribe_instrument",
    "_subscribe_instruments",
    "_subscribe_quote_ticks",
    "_subscribe_trade_ticks",
    "_subscribe_bars",
    "_unsubscribe",
    "_unsubscribe_instrument",
    "_unsubscribe_instruments",
    "_unsubscribe_quote_ticks",
    "_unsubscribe_trade_ticks",
    "_unsubscribe_bars",
)


@pytest.mark.parametrize("name", SUBSCRIPTIONS)
def test_every_subscription_hook_is_a_no_op(name: str, client: object) -> None:
    """The window is held whole and released whole; routing is the data engine's business."""
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(getattr(client, name)(None)) is None
    finally:
        loop.close()


@pytest.mark.parametrize("name", LIFECYCLE)
def test_connecting_takes_no_network(name: str, client: object) -> None:
    """The catalog is already open; connecting and disconnecting are bookkeeping."""
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(getattr(client, name)()) is None
    finally:
        loop.close()


def test_the_client_is_registered_under_one_id(client: object) -> None:
    """A replayed node's data arrives from one client, whatever venues it covers."""
    assert client.id == ClientId("REPLAY")  # type: ignore[attr-defined]
    assert client.venue is None  # type: ignore[attr-defined]


@pytest.fixture
def client() -> object:
    """A replay client built the way a session builds one, with nothing to feed."""
    from nautilus_trader.cache.cache import Cache
    from nautilus_trader.common.component import LiveClock, MessageBus
    from nautilus_trader.model.identifiers import TraderId

    from kanso.nautilus.replay_client import ReplayDataClient

    clock = LiveClock()
    loop = asyncio.new_event_loop()
    try:
        return ReplayDataClient(
            loop,
            MessageBus(trader_id=TraderId("KANSO-001"), clock=clock),
            Cache(),
            clock,
        )
    finally:
        loop.close()
