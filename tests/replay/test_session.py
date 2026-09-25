"""The node session: one window, two code paths, and the wiring that makes them agree.

These tests run a real trading node against a real simulated venue, because everything the
module claims is a claim about the engine: that a bar reaches the exchange, that a fill lands
before the next point is released, that an exception stops the node rather than the process.
None of that can be established against a double.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from nautilus_trader.model.data import CustomData, DataType
from nautilus_trader.model.identifiers import ClientId, InstrumentId

from kanso.criteria.run import midnight_ns
from kanso.data.types import CorporateAction
from kanso.errors import PreconditionError
from kanso.nautilus import backtest, session
from kanso.nautilus.cross_section import is_marker
from kanso.nautilus.session import Halt, measured, ordered
from tests.replay.conftest import (
    BLOCKING_FILTER,
    FLAT,
    FORWARD,
    HOLDING,
    INSTRUMENT,
    RAISING,
    RESTING,
    REVERTING,
    SPAN,
    SPLIT_EX,
    SPLIT_SCHEDULE,
    bars,
    hypothesis,
    instrument,
    quotes,
    request_for,
    restated,
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


def test_the_two_paths_apply_a_corporate_action_identically() -> None:
    """A split is applied by the venue, and both paths run the same simulated venue."""
    node, engine = both(
        request_for(source=HOLDING),
        [instrument(info=SPLIT_SCHEDULE)],
        [tuple(restated(FORWARD))],
    )

    assert node.intents == engine.intents
    assert [(order[2], order[3]) for order in node.intents] == [("BUY", 1_005.0), ("SELL", 100.0)]
    assert node.run.equity == engine.run.equity


def test_the_two_paths_cancel_across_a_corporate_action_identically() -> None:
    """The order the venue has to reach before the exchange does, on both paths.

    A take-profit resting at fifty is unreachable at ten and marketable at a hundred, so a
    path that cancelled a bar late would fill 1,005 shares at fifty and end tens of
    thousands richer than the other. Both cancel before the ex-date's bar is matched, so
    neither fills and the equity curves are the same number at every period end.
    """
    node, engine = both(
        request_for(source=RESTING),
        [instrument(info=SPLIT_SCHEDULE)],
        [tuple(restated(FORWARD))],
    )

    assert node.intents == engine.intents
    assert [(order[2], order[3]) for order in node.intents] == [("BUY", 1_005.0), ("SELL", 1_005.0)]
    assert [(fill.side, fill.qty) for fill in node.run.fills] == [("BUY", 1_005.0)]
    assert node.run.equity == engine.run.equity


def test_the_sleeve_is_told_of_a_corporate_action_on_both_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The venue announces a split as it applies it, and the sleeve hears it on either path.

    The node's exchange sends on a relay rather than on the node's own bus, and the
    announcement used to stop there: the sleeve on a stage held no split and booked no
    payment in lieu, so a strategy that restates its own history at a split, or sizes its
    next order by its balance, traded differently in a stage than in its card — measured,
    a stock sleeve whose certification warmup held a ten-for-one split entered its first
    three names in another order. Both sleeves now hold the one split and the same cash.
    """
    made: list[Any] = []
    original = backtest._sleeve

    def recording(request: backtest.RunRequest) -> tuple[Any, Any]:
        cls, config = original(request)

        class Recorded(cls):  # type: ignore[misc, valid-type]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                made.append(self)

        return Recorded, config

    monkeypatch.setattr(backtest, "_sleeve", recording)
    both(request_for(source=HOLDING), [instrument(info=SPLIT_SCHEDULE)], [tuple(restated(FORWARD))])

    engine, node = made
    assert [split.ex_date for split in node._restated[INSTRUMENT]] == [SPLIT_EX]
    assert node._restated == engine._restated
    assert node._cash == engine._cash


DIVIDEND_TAKER = b"""
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Strategy(KansoStrategy):
    \"\"\"Buys a share for every cent of dividend declared, the moment it is declared.\"\"\"

    config_cls = KansoConfig

    def on_bar(self, bar) -> None:
        return

    def on_data(self, data) -> None:
        if data.kind == "dividend":
            self.submit_entry(data.instrument_id, "BUY", qty=round(data.cash * 100))
"""


def declared(cents: int, day: int) -> CustomData:
    """A dividend of `cents` declared at the close of the window's `day`-th day."""
    at = midnight_ns(FORWARD[0]) + day * 86_400 * 1_000_000_000 + 16 * 3_600 * 1_000_000_000
    return CustomData(
        DataType(CorporateAction),
        CorporateAction(
            ts_event=at,
            ts_init=at + 1_000_000_000,
            instrument_id=InstrumentId.from_str(INSTRUMENT),
            kind="dividend",
            ratio=1.0,
            cash=cents / 100,
            currency="USD",
            ex_date_ns=at + 14 * 86_400 * 1_000_000_000,
        ),
    )


def test_a_custom_requirement_reaches_the_sleeve_without_a_subscription_of_its_own() -> None:
    """The harness subscribes every data requirement, a registered custom type included.

    A researched `strategy.py` may not import `kanso.data`, so the class a subscription
    needs is out of its reach: a hypothesis that required `corporate_action` used to be
    loaded its points and never shown one. Both paths now hand each declaration to
    `on_data` as the type itself, at the instant it became public.
    """
    hyp = hypothesis(data_requirements=["bar", "corporate_action"])
    groups = [tuple(bars(FORWARD)), (declared(24, 3), declared(26, 10))]

    node, engine = both(request_for(source=DIVIDEND_TAKER, hyp=hyp), [instrument()], groups)

    assert node.intents == engine.intents
    assert [(order[2], order[3]) for order in engine.intents] == [("BUY", 24.0), ("BUY", 26.0)]


RESTING_BUY = b'''
from kanso.nautilus.strategy import KansoConfig, KansoStrategy


class Config(KansoConfig):
    limit: float = 9.75


class Strategy(KansoStrategy):
    """Rests one buy under the market on the first session and waits for the market."""

    config_cls = Config

    def on_start(self) -> None:
        self.placed = False

    def on_bar(self, bar) -> None:
        if not self.placed:
            self.placed = True
            self.submit_entry(
                bar.bar_type.instrument_id, "BUY", qty=100, price=self.kanso_config.limit
            )
'''
"""The saw-tooth's lowest print is 9.75, reached on the fourth session and never passed."""


def rested(limit: float, rule: str) -> tuple[backtest.RunResult, backtest.RunResult]:
    """A buy resting at `limit` under a venue model whose `limit_fill` is `rule`, on both
    paths."""
    request = request_for(source=RESTING_BUY)
    model = dict(request.venue_model)
    model["costs"] = {**dict(model["costs"]), "limit_fill": rule}  # type: ignore[arg-type]
    subject = replace(request, venue_model=model, overrides={"limit": limit})
    return both(subject, [instrument()], [tuple(bars(FORWARD))])


def test_a_limit_the_market_only_touches_fills_on_a_touch_on_both_paths() -> None:
    node, engine = rested(9.75, "touch")

    assert node.run.fills == engine.run.fills
    assert [(fill.side, fill.qty, fill.px) for fill in engine.run.fills] == [("BUY", 100.0, 9.75)]


def test_a_limit_the_market_only_touches_never_fills_through_on_both_paths() -> None:
    node, engine = rested(9.75, "through")

    assert node.intents == engine.intents
    assert node.intents, "the order was placed, and it rested"
    assert node.run.fills == engine.run.fills == ()


@pytest.mark.parametrize("rule", ["touch", "through"])
def test_a_limit_the_market_trades_through_fills_at_its_price_either_way(rule: str) -> None:
    node, engine = rested(9.8, rule)

    assert node.run.fills == engine.run.fills
    assert [(fill.side, fill.qty, fill.px) for fill in engine.run.fills] == [("BUY", 100.0, 9.8)]


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
    from kanso.nautilus.cross_section import is_marker

    first, second = bars(FORWARD), bars(FORWARD, OTHER)

    stream = ordered([tuple(first), tuple(second)])

    assert stream[0] is first[0]
    assert stream[1] is second[0]
    assert is_marker(stream[2]) and is_marker(stream[3])
    assert len(stream) == 2 * len(first) + 2 * len(second)
    assert stream[4] is first[1]
    assert stream[5] is second[1]


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


# --- warming ------------------------------------------------------------------

PREFIX = (date(2024, 2, 27), date(2024, 2, 29))
"""The three sessions before the forward window, over a series that prints every day."""


def warmed_request() -> backtest.RunRequest:
    return replace(request_for(hyp=hypothesis(warmup={"sessions": 3})), prefix=PREFIX)


def test_a_warmed_session_warms_on_both_paths_and_claims_only_its_range() -> None:
    """The prefix is fed on the live path too, and neither path counts it."""
    fed = tuple(bars((PREFIX[0], FORWARD[1])))
    opens = midnight_ns(FORWARD[0])

    replayed = session.run_node(warmed_request(), [instrument()], [fed])
    engine = backtest.execute(warmed_request(), [instrument()], [fed])

    node = replayed.result
    assert node.intents == engine.intents
    assert node.intents, "the prefix filled the three closes the rule needs"
    assert node.intents[0][0] == fed[4].ts_event, "so it buys the first trough of the range"
    assert all(opens <= fill.ts_ns for fill in node.run.fills)
    assert node.run.equity == engine.run.equity
    assert replayed.released == len(bars(FORWARD))
    assert replayed.clock_ns == int(fed[-1].ts_init)


def test_a_session_that_stops_inside_the_prefix_reaches_no_clock() -> None:
    """A clock inside the prefix would resume a stage before its window; none is reported."""
    fed = tuple(bars((PREFIX[0], FORWARD[1])))

    replayed = session.run_node(
        replace(warmed_request(), strategy_source=RAISING), [instrument()], [fed]
    )

    assert replayed.result.crashed
    assert replayed.released == 0
    assert replayed.clock_ns is None


def test_measured_is_the_window_s_own_catalog_points() -> None:
    fed = tuple(bars((PREFIX[0], FORWARD[1])))
    stream = ordered([fed])

    kept = measured(stream, midnight_ns(FORWARD[0]))

    assert len(kept) == len(bars(FORWARD))
    assert kept[0] is fed[3]
    assert not any(is_marker(point) for point in kept)


# --- the book policy ------------------------------------------------------------

BALANCED = REVERTING.replace(
    b"notional=self.kanso_config.notional", b"notional=self.balance / 10"
).replace(b"Buys the trough", b"Buys a tenth of its balance at the trough")


def booked_request(book: dict[str, Any] | None) -> backtest.RunRequest:
    fields = {} if book is None else {"book": book}
    return request_for(source=BALANCED, window=SPAN, hyp=hypothesis(**fields))


def test_the_two_paths_settle_a_book_policy_identically() -> None:
    """A strategy sizing from its balance buys what the reset left on both paths, and both
    measure the same book: the live path's harness settles each period as the backtest's."""
    policy = {"reset": "monthly"}
    node, engine = both(booked_request(policy), [instrument()], [tuple(bars(SPAN))])
    unbooked = backtest.execute(booked_request(None), [instrument()], [tuple(bars(SPAN))])

    assert node.intents == engine.intents
    assert node.intents != unbooked.intents, "the reset moved what the balance sizes"
    assert any(engine.run.cushion), "a surplus was set aside"
    assert node.run.equity == engine.run.equity
    assert node.run.cushion == engine.run.cushion
