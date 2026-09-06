"""The execution client: what it refuses, what it reports, and what a restart repeats.

Six properties are under test.

An order shape the broker cannot honour is refused **by name and before anything is
sent** — every entry of the deny table, and every value the parser's own tables have no
spelling for, produces an `OrderDenied` naming the field and leaves the wire untouched.

The client order id goes out verbatim and is checked to have come back verbatim, because
it is the only handle that survives a restart on both sides.

**A restart produces no duplicate fill**, twice over: a row whose cumulative fill the
engine has already recorded yields no report at all, and a row that is reported anyway
carries the same trade id it carried the first time, which is the id reconciliation skips.
A genuine second fill is not suppressed by either mechanism, which is the other half of
the property and the one a naive implementation gets wrong.

All five report generators answer from the measured wire, pass over what they cannot map
rather than failing the sweep, and page through a long history without losing or
repeating a row.

A key belongs to one account: a client cannot be opened with the other account's
credentials, and a broker refusal names the variable the key came from and never the key.

**No credential reaches anything.** Every path is driven, and the key and the secret are
then looked for in every message the client put on the bus, in its `repr`, in its
configuration, in every refusal it raised, and in the source of the two modules — which
also name no credential variable of their own, deriving all four from the standard scheme.

Nothing here opens a socket, resolves a credential or carries a recorded secret. The keys
below are not credentials; only their prefixes matter.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    BatchCancelOrders,
    CancelAllOrders,
    CancelOrder,
    GenerateFillReports,
    GenerateOrderStatusReport,
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
    ModifyOrder,
    QueryAccount,
    SubmitOrder,
    SubmitOrderList,
)
from nautilus_trader.model.enums import (
    ContingencyType,
    LiquiditySide,
    OrderSide,
    OrderStatus,
    TimeInForce,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.model.events import OrderAccepted, OrderFilled, OrderSubmitted
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    OrderListId,
    PositionId,
    StrategyId,
    Symbol,
    TradeId,
    TraderId,
    VenueOrderId,
)
from nautilus_trader.model.instruments import Equity
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.model.orders import (
    LimitOrder,
    MarketOrder,
    MarketToLimitOrder,
    OrderList,
    StopLimitOrder,
    StopMarketOrder,
    TrailingStopMarketOrder,
)

from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.adapters import alpaca
from kanso.nautilus.adapters.alpaca import BROKER
from kanso.nautilus.adapters.alpaca import execution as executing
from kanso.nautilus.adapters.alpaca import factory as factories
from kanso.nautilus.adapters.alpaca.config import (
    LIVE_CLIENT,
    LIVE_HOST,
    PAPER_CLIENT,
    PAPER_HOST,
    Account,
    Credentials,
    Response,
    account,
    credential_names,
)
from kanso.nautilus.adapters.alpaca.data import DATA_CLIENT_FACTORY
from kanso.nautilus.adapters.alpaca.execution import (
    ACCOUNT_PATH,
    BY_CLIENT_ORDER_ID_PATH,
    CLIENT_ID,
    ORDERS_PATH,
    PAGE_LIMIT,
    POSITIONS_PATH,
    TRAILING_OFFSETS,
    UNSUPPORTED,
    AlpacaExecutionClient,
    account_row,
    denial,
    sender,
    submission,
)
from kanso.nautilus.adapters.alpaca.factory import EXEC_CLIENT_FACTORY, AlpacaExecClientConfig
from kanso.nautilus.adapters.alpaca.parsing import ISSUER, trade_id
from kanso.workspace import Workspace, init

from . import ACCOUNT_NUMBER, LIVE_KEY, PAPER_KEY, SECRET, order, position

TRADER = TraderId("KANSO-LIVE")
STRATEGY = StrategyId("Sleeve-demo-1")
SYMBOL = "AAPL"
VENUE = "XNAS"
INSTRUMENT = InstrumentId.from_str(f"{SYMBOL}.{VENUE}")
ACCOUNT_ID = AccountId(f"{ISSUER}-{ACCOUNT_NUMBER}")
VENUE_ORDER_ID = VenueOrderId("61e69015-8549-4bfd-b9c3-01e75843f47d")

EVENTS = "ExecEngine.process"
REPORTS = "ExecEngine.reconcile_execution_report"
STATES = "Portfolio.update_account"
"""The three endpoints an execution client sends to. Registered in the fixture, because
a message bus drops what nothing is registered for and a dropped event is an invisible one."""

ACCOUNT: Mapping[str, Any] = {
    "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "account_number": ACCOUNT_NUMBER,
    "status": "ACTIVE",
    "currency": "USD",
    "cash": "100000",
    "equity": "103043.6",
    "buying_power": "206087.2",
    "long_market_value": "3043.6",
    "short_market_value": "0",
    "initial_margin": "1521.8",
    "maintenance_margin": "913.08",
    "multiplier": "2",
    "shorting_enabled": True,
    "pattern_day_trader": False,
    "trading_blocked": False,
    "transfers_blocked": False,
    "account_blocked": False,
    "daytrade_count": 0,
    "created_at": "2026-01-02T00:00:00Z",
}
"""One account row in the broker's published spelling.

**Not measured.** The read-only pass covered the clock, the asset, the order and the
position rows and did not cover this one, so the client reads it field by field and
requires only what an account state cannot be built without. Every assertion below is on
those fields; the rest are here so the row is the shape the broker documents rather than
a reduction nobody would ever be served.
"""


# --- the wire ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sent:
    """One request the client handed the transport, body included."""

    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    keys: tuple[str, ...]
    body: bytes | None

    @property
    def payload(self) -> Mapping[str, Any]:
        """The JSON body, for a request that carried one."""
        assert self.body is not None
        parsed = json.loads(self.body)
        assert isinstance(parsed, Mapping)
        return parsed


@dataclass
class Wire:
    """A transport answering frozen bodies, which refuses a path nobody routed.

    A refusal rather than a default, because a test that passes against a path the client
    never asked for is a test that proves nothing about the client.
    """

    host: str
    routes: dict[tuple[str, str], list[Response]] = field(default_factory=dict)
    sent: list[Sent] = field(default_factory=list)

    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
        body: bytes | None = None,
    ) -> Response:
        path = url.removeprefix(self.host)
        self.sent.append(Sent(method, path, dict(params), dict(headers), tuple(keys), body))
        answers = self.routes.get((method, path))
        if not answers:
            raise AssertionError(f"nothing routed for {method} {path}")
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def route(self, method: str, path: str, *answers: Response) -> Wire:
        """Route one path to the answers it gives, in order; the last one repeats."""
        self.routes[method, path] = list(answers)
        return self

    def paths(self, method: str | None = None) -> list[str]:
        """The paths that were actually asked for."""
        return [one.path for one in self.sent if method is None or one.method == method]


def body(payload: Any, status: int = 200) -> Response:
    """A response carrying `payload` as JSON."""
    return Response(status=status, body=json.dumps(payload).encode())


def wire(*, host: str = PAPER_HOST, account_row_: Mapping[str, Any] | None = None) -> Wire:
    """A wire whose account path already answers, since every client reads it first."""
    return Wire(host).route("GET", ACCOUNT_PATH, body(dict(account_row_ or ACCOUNT)))


# --- the client ----------------------------------------------------------------


APPLE = Equity(
    instrument_id=INSTRUMENT,
    raw_symbol=Symbol(SYMBOL),
    currency=Currency.from_str("USD"),
    price_precision=2,
    price_increment=Price.from_str("0.01"),
    lot_size=Quantity.from_int(1),
    ts_event=0,
    ts_init=0,
)


class Held(InstrumentProvider):
    """An instrument provider holding what the workspace resolved, and loading nothing."""

    def __init__(self, instruments: Sequence[Any] = (APPLE,)) -> None:
        super().__init__()
        for instrument in instruments:
            self.add(instrument)

    async def load_all_async(self, filters: dict | None = None) -> None:
        """Already loaded: this provider stands in for the workspace's own catalogue."""


@dataclass
class Node:
    """One client and everything it published, as a test drives it."""

    client: AlpacaExecutionClient
    wire: Wire
    cache: Cache
    loop: asyncio.AbstractEventLoop
    events: list[Any] = field(default_factory=list)
    reports: list[Any] = field(default_factory=list)
    states: list[Any] = field(default_factory=list)

    def run(self, coroutine: Any) -> Any:
        """Drive one of the client's coroutines to completion."""
        return self.loop.run_until_complete(coroutine)

    def connect(self) -> Node:
        """Read the account, which is what every other call needs to have happened."""
        self.run(self.client._connect())
        return self

    @property
    def messages(self) -> list[Any]:
        """Everything the client put on the bus, whatever endpoint it went to."""
        return [*self.events, *self.reports, *self.states]


@pytest.fixture
def node() -> Iterator[Node]:
    """A client of the paper account over a wire with nothing but the account routed."""
    built = build(client_id=PAPER_CLIENT, transport=wire(), host=PAPER_HOST)
    try:
        yield built
    finally:
        built.loop.close()


@pytest.fixture
def live() -> Iterator[Node]:
    """The same, for the account that trades real money."""
    built = build(client_id=LIVE_CLIENT, transport=wire(host=LIVE_HOST), host=LIVE_HOST)
    try:
        yield built
    finally:
        built.loop.close()


def build(
    *,
    client_id: str,
    transport: Wire,
    host: str,
    instruments: Sequence[Any] = (APPLE,),
) -> Node:
    """One client wired to a message bus that keeps everything it is sent.

    Each call is a separate process as far as this adapter is concerned: its own cache,
    its own client, its own loop. Two of them over one frozen wire are a restart.
    """
    clock = LiveClock()
    bus = MessageBus(trader_id=TRADER, clock=clock)
    cache = Cache()
    for instrument in instruments:
        cache.add_instrument(instrument)
    loop = asyncio.new_event_loop()
    built = Node(
        client=AlpacaExecutionClient(
            loop,
            client_id=client_id,
            credentials=credentials(client_id),
            send=transport,
            host=host,
            instrument_provider=Held(instruments),
            msgbus=bus,
            cache=cache,
            clock=clock,
            config=AlpacaExecClientConfig(client_id=client_id),
        ),
        wire=transport,
        cache=cache,
        loop=loop,
    )
    bus.register(endpoint=EVENTS, handler=built.events.append)
    bus.register(endpoint=REPORTS, handler=built.reports.append)
    bus.register(endpoint=STATES, handler=built.states.append)
    return built


def credentials(client_id: str) -> Credentials:
    """The account's credentials, from values that are not credentials."""
    found: Account = account(client_id)
    key = PAPER_KEY if client_id == PAPER_CLIENT else LIVE_KEY
    return Credentials(account=found, key=key, secret=SECRET)


# --- orders --------------------------------------------------------------------


def market(**changes: Any) -> MarketOrder:
    """A market buy of ten shares, which is the order every other one varies from."""
    fields: dict[str, Any] = {
        "trader_id": TRADER,
        "strategy_id": STRATEGY,
        "instrument_id": INSTRUMENT,
        "client_order_id": ClientOrderId("O-20260905-160405-001-000-1"),
        "order_side": OrderSide.BUY,
        "quantity": Quantity.from_str("10"),
        "init_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return MarketOrder(**fields)


def limit(**changes: Any) -> LimitOrder:
    fields: dict[str, Any] = {
        "trader_id": TRADER,
        "strategy_id": STRATEGY,
        "instrument_id": INSTRUMENT,
        "client_order_id": ClientOrderId("O-20260905-160405-001-000-2"),
        "order_side": OrderSide.BUY,
        "quantity": Quantity.from_str("10"),
        "price": Price.from_str("303.42"),
        "init_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return LimitOrder(**fields)


def stop(**changes: Any) -> StopMarketOrder:
    fields: dict[str, Any] = {
        "trader_id": TRADER,
        "strategy_id": STRATEGY,
        "instrument_id": INSTRUMENT,
        "client_order_id": ClientOrderId("O-20260905-160405-001-000-3"),
        "order_side": OrderSide.SELL,
        "quantity": Quantity.from_str("10"),
        "trigger_price": Price.from_str("300.00"),
        "trigger_type": TriggerType.LAST_PRICE,
        "init_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return StopMarketOrder(**fields)


def stop_limit(**changes: Any) -> StopLimitOrder:
    fields: dict[str, Any] = {
        "trader_id": TRADER,
        "strategy_id": STRATEGY,
        "instrument_id": INSTRUMENT,
        "client_order_id": ClientOrderId("O-20260905-160405-001-000-4"),
        "order_side": OrderSide.SELL,
        "quantity": Quantity.from_str("10"),
        "price": Price.from_str("299.50"),
        "trigger_price": Price.from_str("300.00"),
        "trigger_type": TriggerType.LAST_PRICE,
        "init_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return StopLimitOrder(**fields)


def trailing(**changes: Any) -> TrailingStopMarketOrder:
    fields: dict[str, Any] = {
        "trader_id": TRADER,
        "strategy_id": STRATEGY,
        "instrument_id": INSTRUMENT,
        "client_order_id": ClientOrderId("O-20260905-160405-001-000-5"),
        "order_side": OrderSide.SELL,
        "quantity": Quantity.from_str("10"),
        "trigger_price": Price.from_str("300.00"),
        "trigger_type": TriggerType.LAST_PRICE,
        "trailing_offset": Decimal("1.50"),
        "trailing_offset_type": TrailingOffsetType.PRICE,
        "time_in_force": TimeInForce.GTC,
        "init_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return TrailingStopMarketOrder(**fields)


def submit(one: Any) -> SubmitOrder:
    """The command a strategy's submission arrives as."""
    return SubmitOrder(
        trader_id=TRADER,
        strategy_id=STRATEGY,
        order=one,
        command_id=UUID4(),
        ts_init=0,
    )


def opened(node: Node, one: Any, venue_order_id: VenueOrderId = VENUE_ORDER_ID) -> Any:
    """One order in the cache, accepted by the venue and therefore open."""
    node.cache.add_order(one)
    one.apply(
        OrderSubmitted(
            trader_id=TRADER,
            strategy_id=STRATEGY,
            instrument_id=one.instrument_id,
            client_order_id=one.client_order_id,
            account_id=ACCOUNT_ID,
            event_id=UUID4(),
            ts_event=0,
            ts_init=0,
        )
    )
    one.apply(
        OrderAccepted(
            trader_id=TRADER,
            strategy_id=STRATEGY,
            instrument_id=one.instrument_id,
            client_order_id=one.client_order_id,
            venue_order_id=venue_order_id,
            account_id=ACCOUNT_ID,
            event_id=UUID4(),
            ts_event=0,
            ts_init=0,
        )
    )
    node.cache.update_order(one)
    return one


def filled(node: Node, one: Any, quantity: str, price: str = "303.42") -> Any:
    """The same order with a fill applied, as the engine would hold it after one."""
    one.apply(
        OrderFilled(
            trader_id=TRADER,
            strategy_id=STRATEGY,
            instrument_id=one.instrument_id,
            client_order_id=one.client_order_id,
            venue_order_id=one.venue_order_id,
            account_id=ACCOUNT_ID,
            trade_id=TradeId("EXISTING-FILL"),
            position_id=PositionId("P-1"),
            order_side=one.side,
            order_type=one.order_type,
            last_qty=Quantity.from_str(quantity),
            last_px=Price.from_str(price),
            currency=Currency.from_str("USD"),
            commission=Money(0, Currency.from_str("USD")),
            liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
            event_id=UUID4(),
            ts_event=0,
            ts_init=0,
        )
    )
    node.cache.update_order(one)
    return one


# --- the transport an order needs ----------------------------------------------


def test_a_transport_that_cannot_carry_a_body_is_refused_when_the_client_is_built() -> None:
    """An order is a POST with a body, and a connection that drops one sends no order."""

    def five(
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
    ) -> Response:
        raise AssertionError("never called")  # pragma: no cover

    with pytest.raises(PreconditionError) as raised:
        sender(five)
    assert "body" in str(raised.value)


def test_a_transport_with_no_signature_at_all_is_refused_the_same_way() -> None:
    """Whatever cannot be inspected cannot be shown to carry a body either."""
    with pytest.raises(PreconditionError):
        sender(object())  # type: ignore[arg-type]


def test_a_transport_that_carries_a_body_is_returned_as_it_stands() -> None:
    """The shared connection is used, never wrapped: one quota per account."""
    transport = wire()
    assert sender(transport) is transport


def test_the_client_opens_no_connection_of_its_own() -> None:
    """One quota per account, so the connection is given to the client, never built by it."""
    source = Path(executing.__file__ or "").read_text()
    assert "HttpClient" not in source
    assert "pyo3_transport" not in source


# --- the deny table ------------------------------------------------------------


DENIED: tuple[tuple[str, Any], ...] = (
    ("venue", lambda: market(instrument_id=InstrumentId.from_str("AAPL.XLON"))),
    ("post_only", lambda: limit(post_only=True)),
    ("reduce_only", lambda: limit(reduce_only=True)),
    ("display_qty", lambda: limit(display_qty=Quantity.from_str("1"))),
    ("quote_quantity", lambda: market(quote_quantity=True)),
    (
        "contingency_type",
        lambda: limit(
            contingency_type=ContingencyType.OTO,
            linked_order_ids=[ClientOrderId("O-linked")],
        ),
    ),
    ("trigger_type", lambda: stop(trigger_type=TriggerType.MARK_PRICE)),
    ("trailing_offset_type", lambda: trailing(trailing_offset_type=TrailingOffsetType.TICKS)),
)
"""One order per entry of the deny table, so no entry can rot into unreachable code."""


@pytest.mark.parametrize(("field_name", "build"), DENIED, ids=[name for name, _ in DENIED])
def test_every_entry_of_the_deny_table_refuses_an_order_by_the_field_it_read(
    field_name: str, build: Any
) -> None:
    """A shape with no field on the wire is named by that field, never translated."""
    reason = denial(build())
    assert reason is not None
    assert reason.startswith(f"{field_name}:")


def test_the_deny_table_is_exactly_what_the_tests_exercise() -> None:
    """A table entry nothing covers would be a refusal nobody has ever seen work."""
    assert [name for name, _, _ in UNSUPPORTED] == [name for name, _ in DENIED]


def test_a_good_till_date_order_is_refused_rather_than_made_good_till_cancel() -> None:
    """The one time in force the engine has and the broker does not."""
    order_ = limit(
        time_in_force=TimeInForce.GTD,
        expire_time_ns=1_800_000_000_000_000_000,
    )
    reason = denial(order_)
    assert reason is not None and "GTD" in reason


def test_a_client_order_id_longer_than_the_broker_accepts_is_refused_here() -> None:
    """Refused before the request is built, rather than by the broker mid-session."""
    reason = denial(market(client_order_id=ClientOrderId("O" * 129)))
    assert reason is not None and "client_order_id" in reason


def test_an_order_type_the_broker_does_not_offer_is_refused_by_its_own_name() -> None:
    """A market-to-limit has no spelling here and is not sent as a limit."""
    order_ = MarketToLimitOrder(
        trader_id=TRADER,
        strategy_id=STRATEGY,
        instrument_id=INSTRUMENT,
        client_order_id=ClientOrderId("O-9"),
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str("10"),
        init_id=UUID4(),
        ts_init=0,
    )
    reason = denial(order_)
    assert reason is not None and "MARKET_TO_LIMIT" in reason


def test_an_order_the_broker_can_honour_is_not_denied() -> None:
    """The table refuses shapes, not orders."""
    assert denial(market()) is None
    assert denial(limit()) is None
    assert denial(stop()) is None
    assert denial(stop_limit()) is None
    assert denial(trailing()) is None


def test_a_denied_order_cannot_be_turned_into_a_request_at_all() -> None:
    """`submission` refuses rather than dropping the field the broker cannot honour."""
    with pytest.raises(ValidationError) as raised:
        submission(limit(post_only=True))
    assert "post_only" in str(raised.value)


def test_a_denied_order_is_denied_before_anything_is_sent(node: Node) -> None:
    """The wire is untouched: an order refused here never reached the broker."""
    node.connect()
    before = len(node.wire.sent)
    node.run(node.client._submit_order(submit(limit(post_only=True))))
    assert len(node.wire.sent) == before
    denied = node.events[-1]
    assert type(denied).__name__ == "OrderDenied"
    assert "post_only" in denied.reason


def test_a_list_of_orders_is_denied_order_by_order(node: Node) -> None:
    """The broker's legs are not mapped, so no leg of a bracket is sent."""
    node.connect()
    before = len(node.wire.sent)
    orders = [market(), limit()]
    node.run(
        node.client._submit_order_list(
            SubmitOrderList(
                trader_id=TRADER,
                strategy_id=STRATEGY,
                order_list=OrderList(order_list_id=OrderListId("OL-1"), orders=orders),
                command_id=UUID4(),
                ts_init=0,
            )
        )
    )
    assert len(node.wire.sent) == before
    assert [type(one).__name__ for one in node.events] == ["OrderDenied", "OrderDenied"]
    assert all("order_list" in one.reason for one in node.events)


# --- what a submission looks like on the wire ----------------------------------


def test_a_market_order_carries_the_six_fields_the_broker_needs() -> None:
    """Nothing more: a field the caller did not ask for is a field nobody chose."""
    assert submission(market()) == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "market",
        "time_in_force": "gtc",
        "client_order_id": "O-20260905-160405-001-000-1",
    }
    assert submission(market(time_in_force=TimeInForce.DAY))["time_in_force"] == "day"


def test_a_limit_order_carries_its_price_and_a_stop_its_trigger() -> None:
    """Each price field appears on exactly the order types that have one."""
    assert submission(limit())["limit_price"] == "303.42"
    assert "stop_price" not in submission(limit())
    assert submission(stop())["stop_price"] == "300"
    assert "limit_price" not in submission(stop())
    both = submission(stop_limit())
    assert (both["limit_price"], both["stop_price"]) == ("299.5", "300")


def test_a_trailing_stop_travels_in_the_unit_the_broker_names() -> None:
    """An absolute offset as it stands; basis points as the percentage they are."""
    assert submission(trailing())["trail_price"] == "1.5"
    percent = submission(
        trailing(
            trailing_offset=Decimal("25"),
            trailing_offset_type=TrailingOffsetType.BASIS_POINTS,
        )
    )
    assert percent["trail_percent"] == "0.25"
    assert set(TRAILING_OFFSETS.values()) == {"trail_price", "trail_percent"}


def test_the_client_order_id_goes_out_exactly_as_the_engine_wrote_it() -> None:
    """It is the handle a restart recognises the order by, so nothing rewrites it."""
    one = market()
    assert submission(one)["client_order_id"] == one.client_order_id.value


# --- the account ---------------------------------------------------------------


def test_the_account_row_becomes_the_engine_account_and_its_balances(node: Node) -> None:
    """Connecting reads the account, registers it and publishes what it holds."""
    node.connect()
    assert node.client.account_id == ACCOUNT_ID
    assert node.client.id == CLIENT_ID
    state = node.states[-1]
    assert state.account_id == ACCOUNT_ID
    balance = state.balances[0]
    assert balance.total.as_decimal() == Decimal("103043.60")
    assert balance.locked.as_decimal() == Decimal("1521.80")
    assert balance.free.as_decimal() == Decimal("101521.80")
    margin = state.margins[0]
    assert (margin.initial.as_decimal(), margin.maintenance.as_decimal()) == (
        Decimal("1521.80"),
        Decimal("913.08"),
    )


def test_the_engine_client_id_is_the_issuer_the_account_id_carries(node: Node) -> None:
    """The engine refuses an account whose issuer is not the client's own id.

    Which is why this client is registered under the issuer the parser stamps rather than
    under the kanso client id: the account number is what tells the two accounts apart.
    """
    node.connect()
    assert node.client.id.value == ISSUER
    assert node.client.account_id.get_issuer() == node.client.id.value
    assert node.client.account_id.get_id() == ACCOUNT_NUMBER


def test_an_account_with_no_margins_reported_reports_none(node: Node) -> None:
    """An account holding nothing has no requirement, and none is invented for it."""
    row = dict(ACCOUNT)
    row.pop("initial_margin")
    row.pop("maintenance_margin")
    found = account_row(row)
    assert found.margins() == []
    assert found.balances()[0].locked.as_decimal() == Decimal(0)


def test_a_requirement_larger_than_the_equity_locks_everything_and_frees_nothing() -> None:
    """A margin call is an account with nothing free, not a negative balance."""
    found = account_row({**ACCOUNT, "equity": "1000", "initial_margin": "2500"})
    balance = found.balances()[0]
    assert balance.locked.as_decimal() == Decimal("1000.00")
    assert balance.free.as_decimal() == Decimal(0)


def test_an_account_field_the_broker_did_not_write_is_refused_by_name() -> None:
    """The row's key set was never measured, so nothing about it is assumed."""
    for missing in ("account_number", "currency", "cash", "equity"):
        row = dict(ACCOUNT)
        row.pop(missing)
        with pytest.raises(ValidationError) as raised:
            account_row(row)
        assert missing in str(raised.value)


def test_an_amount_the_engine_cannot_hold_fails_naming_the_field() -> None:
    """An absurd magnitude is a validation failure, not an engine exception mid-session."""
    found = account_row({**ACCOUNT, "equity": "1e30"})
    with pytest.raises(ValidationError) as raised:
        found.balances()
    assert "equity" in str(raised.value)


def test_a_blocked_account_stops_the_client_before_it_is_connected(node: Node) -> None:
    """An account the broker will accept no order for is not one to deploy onto."""
    node.wire.route("GET", ACCOUNT_PATH, body({**ACCOUNT, "trading_blocked": True}))
    with pytest.raises(PreconditionError) as raised:
        node.connect()
    assert "trading_blocked" in str(raised.value)


def test_an_account_in_another_currency_is_refused(node: Node) -> None:
    """This adapter trades dollar accounts and says so rather than converting."""
    node.wire.route("GET", ACCOUNT_PATH, body({**ACCOUNT, "currency": "EUR"}))
    with pytest.raises(PreconditionError) as raised:
        node.connect()
    assert "EUR" in str(raised.value)


def test_an_account_the_broker_does_not_serve_stops_the_client(node: Node) -> None:
    """A 404 on the account is not an account, however well the credential resolved."""
    node.wire.route("GET", ACCOUNT_PATH, Response(status=404))
    with pytest.raises(PreconditionError) as raised:
        node.connect()
    assert "no account" in str(raised.value)


def test_a_query_republishes_the_account(node: Node) -> None:
    """Which is the whole of what a query asks for."""
    node.connect()
    node.run(
        node.client._query_account(
            QueryAccount(trader_id=TRADER, account_id=ACCOUNT_ID, command_id=UUID4(), ts_init=0)
        )
    )
    assert len(node.states) == 2


def test_a_query_that_finds_no_account_publishes_nothing(node: Node) -> None:
    """Nothing is published rather than an emptied one."""
    node.connect()
    node.wire.route("GET", ACCOUNT_PATH, Response(status=404))
    node.run(
        node.client._query_account(
            QueryAccount(trader_id=TRADER, account_id=ACCOUNT_ID, command_id=UUID4(), ts_init=0)
        )
    )
    assert len(node.states) == 1


def test_disconnecting_closes_nothing_because_the_connection_is_shared(node: Node) -> None:
    """The transport belongs to the workspace and outlives any one client."""
    node.connect()
    assert node.run(node.client._disconnect()) is None


def test_nothing_can_be_reported_before_the_account_is_known(node: Node) -> None:
    """A report needs an account id, and inventing one would mis-key every report."""
    node.wire.route("GET", POSITIONS_PATH, body([position()]))
    with pytest.raises(PreconditionError):
        node.run(node.client.generate_position_status_reports(positions_command()))


# --- the five report generators ------------------------------------------------


def status_command(**changes: Any) -> GenerateOrderStatusReport:
    fields: dict[str, Any] = {
        "instrument_id": INSTRUMENT,
        "client_order_id": None,
        "venue_order_id": VENUE_ORDER_ID,
        "command_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return GenerateOrderStatusReport(**fields)


def statuses_command(**changes: Any) -> GenerateOrderStatusReports:
    fields: dict[str, Any] = {
        "instrument_id": None,
        "start": None,
        "end": None,
        "open_only": False,
        "command_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return GenerateOrderStatusReports(**fields)


def fills_command(**changes: Any) -> GenerateFillReports:
    fields: dict[str, Any] = {
        "instrument_id": None,
        "venue_order_id": None,
        "start": None,
        "end": None,
        "command_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return GenerateFillReports(**fields)


def positions_command(**changes: Any) -> GeneratePositionStatusReports:
    fields: dict[str, Any] = {
        "instrument_id": None,
        "start": None,
        "end": None,
        "command_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return GeneratePositionStatusReports(**fields)


def test_one_order_is_found_by_its_venue_order_id(node: Node) -> None:
    """Which is the handle a report the engine already holds names it by."""
    node.wire.route("GET", f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}", body(order()))
    node.connect()
    report = node.run(node.client.generate_order_status_report(status_command()))
    assert report is not None
    assert report.client_order_id == ClientOrderId("O-20260905-160405-001-000-1")
    assert report.order_status == OrderStatus.ACCEPTED
    assert report.account_id == ACCOUNT_ID


def test_one_order_is_found_by_the_client_order_id_kanso_supplied(node: Node) -> None:
    """The handle that survives a restart is preferred to the one the venue assigned."""
    node.wire.route("GET", BY_CLIENT_ORDER_ID_PATH, body(order()))
    node.connect()
    report = node.run(
        node.client.generate_order_status_report(
            status_command(client_order_id=ClientOrderId("O-20260905-160405-001-000-1"))
        )
    )
    assert report is not None
    asked = node.wire.sent[-1]
    assert asked.params == {"client_order_id": "O-20260905-160405-001-000-1"}


def test_an_order_status_lookup_with_no_handle_at_all_is_refused(node: Node) -> None:
    """An order cannot be looked up by nothing."""
    node.connect()
    with pytest.raises(ValidationError):
        node.run(
            node.client.generate_order_status_report(
                status_command(client_order_id=None, venue_order_id=None)
            )
        )


def test_an_order_the_broker_does_not_have_is_nothing_rather_than_a_failure(node: Node) -> None:
    """A sweep that asked must be able to carry on."""
    node.wire.route("GET", f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}", Response(status=404))
    node.connect()
    assert node.run(node.client.generate_order_status_report(status_command())) is None


def test_an_order_shape_this_adapter_cannot_map_is_passed_over(node: Node) -> None:
    """Reconciliation walks orders kanso never submitted, in shapes it does not model."""
    node.wire.route(
        "GET", f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}", body(order(order_class="bracket"))
    )
    node.connect()
    assert node.run(node.client.generate_order_status_report(status_command())) is None


def test_an_order_in_an_instrument_nothing_holds_is_passed_over(node: Node) -> None:
    """A venue invented for a symbol would re-key every card the instrument appears in."""
    node.wire.route("GET", f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}", body(order(symbol="TSLA")))
    node.connect()
    assert node.run(node.client.generate_order_status_report(status_command())) is None


def test_every_order_the_window_selects_is_reported(node: Node) -> None:
    """And a row that cannot be mapped is left out rather than failing the sweep."""
    rows = [order(), order(id="second", client_order_id="O-2", order_class="bracket")]
    node.wire.route("GET", ORDERS_PATH, body(rows))
    node.connect()
    reports = node.run(node.client.generate_order_status_reports(statuses_command()))
    assert [str(one.venue_order_id) for one in reports] == [str(VENUE_ORDER_ID)]


def test_the_window_the_engine_asked_for_is_the_window_that_is_asked_of_the_broker(
    node: Node,
) -> None:
    """A boundary read as no boundary would reconcile an account's whole history."""
    node.wire.route("GET", ORDERS_PATH, body([]))
    node.connect()
    node.run(
        node.client.generate_order_status_reports(
            statuses_command(
                instrument_id=INSTRUMENT,
                start=datetime(2026, 9, 1, tzinfo=UTC),
                end=datetime(2026, 9, 5, tzinfo=UTC),
                open_only=True,
            )
        )
    )
    asked = node.wire.sent[-1]
    assert asked.params["status"] == "open"
    assert asked.params["after"] == "2026-09-01T00:00:00Z"
    assert asked.params["until"] == "2026-09-05T00:00:00Z"
    assert asked.params["symbols"] == "AAPL"
    assert asked.params["limit"] == str(PAGE_LIMIT)


def test_the_engines_own_timestamp_is_read_to_the_nanosecond(node: Node) -> None:
    """The engine hands its clock's own type over, and its whole instant is kept."""
    node.wire.route("GET", ORDERS_PATH, body([]))
    node.connect()
    moment = LiveClock().utc_now()
    node.run(node.client.generate_order_status_reports(statuses_command(start=moment, end=None)))
    assert node.wire.sent[-1].params["after"] == executing.rfc3339(moment.value)


def test_a_window_boundary_of_an_unreadable_shape_is_refused(node: Node) -> None:
    """Rather than dropped, which would silently widen the window to everything."""
    node.wire.route("GET", ORDERS_PATH, body([]))
    node.connect()
    with pytest.raises(ValidationError):
        node.run(node.client.generate_order_status_reports(_untimed()))


def _untimed() -> Any:
    """A command whose window boundary is not an instant at all."""

    class Untimed:
        instrument_id = None
        start = "yesterday"
        end = None
        open_only = False

    return Untimed()


def test_another_instrument_is_filtered_out_even_if_the_broker_returned_it(node: Node) -> None:
    """The filter is asked for on the wire and checked here, because it matters."""
    node.wire.route("GET", ORDERS_PATH, body([order(), order(id="x", symbol="TSLA")]))
    node.connect()
    reports = node.run(
        node.client.generate_order_status_reports(statuses_command(instrument_id=INSTRUMENT))
    )
    assert len(reports) == 1


def test_a_long_history_is_walked_page_by_page_without_losing_a_row(node: Node) -> None:
    """The walk steps back one nanosecond, so two orders in one instant both survive."""
    first = [order(id=f"{index:03d}", client_order_id=f"O-{index}") for index in range(PAGE_LIMIT)]
    second = [order(id="last", client_order_id="O-last", submitted_at="2026-09-05T16:05:00Z")]
    node.wire.route("GET", ORDERS_PATH, body(first), body([*first[-1:], *second]))
    node.connect()
    reports = node.run(node.client.generate_order_status_reports(statuses_command()))
    assert len(reports) == PAGE_LIMIT + 1
    assert node.wire.sent[-1].params["after"] == "2026-09-05T16:04:05.199999999Z"


def test_a_full_page_holding_nothing_new_fails_rather_than_truncating(node: Node) -> None:
    """A walk that cannot advance is a short history reported as a complete one."""
    page = [order(id=f"{index:03d}", client_order_id=f"O-{index}") for index in range(PAGE_LIMIT)]
    node.wire.route("GET", ORDERS_PATH, body(page))
    node.connect()
    with pytest.raises(PreconditionError) as raised:
        node.run(node.client.generate_order_status_reports(statuses_command()))
    assert "cannot advance" in str(raised.value)


def test_a_page_that_says_when_of_no_order_ends_the_walk_loudly(node: Node) -> None:
    """There is nowhere to continue a walk from, and stopping quietly would truncate it."""
    page = [
        order(
            id=f"{index:03d}",
            client_order_id=f"O-{index}",
            submitted_at=None,
            created_at=None,
            updated_at=None,
        )
        for index in range(PAGE_LIMIT)
    ]
    node.wire.route("GET", ORDERS_PATH, body(page))
    node.connect()
    with pytest.raises(ValidationError):
        node.run(node.client.generate_order_status_reports(statuses_command()))


def test_a_history_deeper_than_the_walk_allows_is_refused(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cursor that does not end is a fault, not a very large account."""
    monkeypatch.setattr(executing, "MAX_PAGES", 2)
    monkeypatch.setattr(executing, "PAGE_LIMIT", 1)
    node.wire.route(
        "GET",
        ORDERS_PATH,
        body([order(id="a", client_order_id="O-a", submitted_at="2026-09-05T16:00:00Z")]),
        body([order(id="b", client_order_id="O-b", submitted_at="2026-09-05T16:01:00Z")]),
        body([order(id="c", client_order_id="O-c", submitted_at="2026-09-05T16:02:00Z")]),
    )
    node.connect()
    with pytest.raises(PreconditionError) as raised:
        node.run(node.client.generate_order_status_reports(statuses_command()))
    assert "more than 2 pages" in str(raised.value)


def test_an_answer_that_is_not_a_list_of_rows_is_refused(node: Node) -> None:
    """A gateway's error page is not an empty account."""
    node.wire.route("GET", ORDERS_PATH, Response(status=200, body=b"<html>nope</html>"))
    node.connect()
    with pytest.raises(ValidationError):
        node.run(node.client.generate_order_status_reports(statuses_command()))


def test_every_position_the_broker_holds_is_reported(node: Node) -> None:
    """At the size the broker holds, not at the size that is free to trade."""
    node.wire.route("GET", POSITIONS_PATH, body([position()]))
    node.connect()
    reports = node.run(node.client.generate_position_status_reports(positions_command()))
    assert len(reports) == 1
    assert reports[0].quantity == Quantity.from_str("10")
    assert reports[0].instrument_id == INSTRUMENT


def test_a_position_in_an_instrument_nothing_holds_is_passed_over(node: Node) -> None:
    """Its size has no declared precision here, and one would have to be invented."""
    node.wire.route("GET", POSITIONS_PATH, body([position(symbol="TSLA")]))
    node.connect()
    assert node.run(node.client.generate_position_status_reports(positions_command())) == []


def test_positions_are_filtered_to_the_instrument_that_was_asked_for(node: Node) -> None:
    """The endpoint takes no filter, so the filter is applied to what it returned."""
    node.wire.route("GET", POSITIONS_PATH, body([position()]))
    node.connect()
    reports = node.run(
        node.client.generate_position_status_reports(
            positions_command(instrument_id=InstrumentId.from_str("MSFT.XNAS"))
        )
    )
    assert reports == []


def test_the_mass_status_is_the_other_four_generators_assembled(node: Node) -> None:
    """The fifth generator is the engine's own and drives the four implemented here."""
    node.wire.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    node.wire.route("GET", POSITIONS_PATH, body([position()]))
    node.connect()
    status = node.run(node.client.generate_mass_status())
    assert status is not None
    assert len(status.order_reports) == 1
    assert len(status.fill_reports) == 1
    assert len(status.position_reports) == 1


def _filled(quantity: str, price: str = "303.42") -> dict[str, Any]:
    """The three fields an order row carries once it has filled."""
    return {
        "filled_qty": quantity,
        "filled_avg_price": price,
        "filled_at": "2026-09-05T16:04:06.000000000Z",
    }


# --- fills, and a restart ------------------------------------------------------


def test_a_fill_is_what_the_row_reports_beyond_what_the_engine_holds(node: Node) -> None:
    """The broker reports a cumulative quantity, and a fill is the difference."""
    node.wire.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    node.connect()
    reports = node.run(node.client.generate_fill_reports(fills_command()))
    assert len(reports) == 1
    assert reports[0].last_qty == Quantity.from_str("10")
    assert reports[0].last_px == Price.from_str("303.42")


def test_a_restart_that_re_reads_a_filled_order_reports_no_fill_at_all(node: Node) -> None:
    """The first of the two mechanisms: nothing new is nothing to report."""
    node.wire.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    node.connect()
    filled(node, opened(node, market()), "10")
    assert node.run(node.client.generate_fill_reports(fills_command())) == []


def test_a_replaced_order_is_recognised_by_the_handle_the_broker_gave_it(node: Node) -> None:
    """A replacement carries the broker's own client order id, and the venue id joins them."""
    row = order(client_order_id="broker-assigned-after-replace", status="filled", **_filled("10"))
    node.wire.route("GET", ORDERS_PATH, body([row]))
    node.connect()
    filled(node, opened(node, market()), "10")
    assert node.run(node.client.generate_fill_reports(fills_command())) == []


def test_the_same_row_read_twice_carries_the_same_trade_id(node: Node) -> None:
    """The second mechanism: the engine skips a trade id the order already carries."""
    node.wire.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    node.connect()
    first = node.run(node.client.generate_fill_reports(fills_command()))
    second = node.run(node.client.generate_fill_reports(fills_command()))
    assert first[0].trade_id == second[0].trade_id
    assert first[0].trade_id == trade_id("O-20260905-160405-001-000-1", Decimal("10"))


def test_a_second_fill_is_neither_suppressed_nor_given_the_first_ones_identity(
    node: Node,
) -> None:
    """The other half: a partially filled order that fills further reports the difference."""
    node.wire.route("GET", ORDERS_PATH, body([order(status="partially_filled", **_filled("4"))]))
    node.connect()
    first = node.run(node.client.generate_fill_reports(fills_command()))
    assert first[0].last_qty == Quantity.from_str("4")
    filled(node, opened(node, market()), "4")
    node.wire.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    second = node.run(node.client.generate_fill_reports(fills_command()))
    assert second[0].last_qty == Quantity.from_str("6")
    assert second[0].trade_id != first[0].trade_id


def test_a_restart_reconciles_the_same_account_into_the_same_fills(node: Node) -> None:
    """A second process over the same account: same identities, and nothing new to apply.

    The whole property in one test. The first process reads a filled order and reports
    the fill; the engine applies it. A second process — its own client, its own cache,
    reading the same account — derives the same trade id from the same row, which is the
    id reconciliation skips, and once the fill is in its cache reports nothing at all.
    """
    routes = wire()
    routes.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    first = build(client_id=PAPER_CLIENT, transport=routes, host=PAPER_HOST)
    restarted = build(client_id=PAPER_CLIENT, transport=routes, host=PAPER_HOST)
    try:
        first.connect()
        restarted.connect()
        before = first.run(first.client.generate_fill_reports(fills_command()))
        after = restarted.run(restarted.client.generate_fill_reports(fills_command()))
        assert [one.trade_id for one in before] == [one.trade_id for one in after]
        assert [one.last_qty for one in before] == [one.last_qty for one in after]
        filled(restarted, opened(restarted, market()), "10")
        assert restarted.run(restarted.client.generate_fill_reports(fills_command())) == []
    finally:
        first.loop.close()
        restarted.loop.close()


def test_a_fill_report_can_be_asked_for_one_venue_order_only(node: Node) -> None:
    """Which is how a position discrepancy asks for the fills of one order."""
    rows = [
        order(status="filled", **_filled("10")),
        order(id="other", client_order_id="O-other", status="filled", **_filled("5")),
    ]
    node.wire.route("GET", ORDERS_PATH, body(rows))
    node.connect()
    reports = node.run(
        node.client.generate_fill_reports(fills_command(venue_order_id=VenueOrderId("other")))
    )
    assert [str(one.venue_order_id) for one in reports] == ["other"]


def test_an_unfilled_or_unmappable_row_yields_no_fill(node: Node) -> None:
    """A resting order has filled nothing, and a shape nobody maps reports nothing."""
    node.wire.route(
        "GET", ORDERS_PATH, body([order(), order(id="x", client_order_id="O-x", qty=None)])
    )
    node.connect()
    assert node.run(node.client.generate_fill_reports(fills_command())) == []


def test_a_row_that_names_no_instrument_at_all_yields_nothing(node: Node) -> None:
    """A symbol the broker did not write is not a symbol to look an instrument up by."""
    node.wire.route(
        "GET", ORDERS_PATH, body([order(symbol=None, status="filled", **_filled("10"))])
    )
    node.connect()
    assert node.run(node.client.generate_fill_reports(fills_command())) == []


def test_a_fill_in_an_instrument_nothing_holds_yields_nothing(node: Node) -> None:
    """The same rule as everywhere else: no invented venue, no invented precision."""
    node.wire.route(
        "GET", ORDERS_PATH, body([order(symbol="TSLA", status="filled", **_filled("10"))])
    )
    node.connect()
    assert node.run(node.client.generate_fill_reports(fills_command())) == []


# --- submitting, cancelling, replacing -----------------------------------------


def test_a_submitted_order_is_sent_once_and_accepted_on_the_brokers_own_timestamp(
    node: Node,
) -> None:
    """The broker's row says when it took the order, and that is what is recorded."""
    node.wire.route("POST", ORDERS_PATH, body(order()))
    node.connect()
    node.run(node.client._submit_order(submit(market())))
    posted = [one for one in node.wire.sent if one.method == "POST"]
    assert len(posted) == 1
    assert posted[0].payload["client_order_id"] == "O-20260905-160405-001-000-1"
    assert [type(one).__name__ for one in node.events] == ["OrderSubmitted", "OrderAccepted"]
    accepted = node.events[-1]
    assert accepted.venue_order_id == VENUE_ORDER_ID
    assert accepted.ts_event == 1_788_624_245_200_000_000


def test_an_accepted_order_that_says_nothing_about_when_is_refused(node: Node) -> None:
    """When the broker took an order is read from its row and never from this clock."""
    node.wire.route(
        "POST",
        ORDERS_PATH,
        body(order(submitted_at=None, created_at=None, updated_at=None)),
    )
    node.connect()
    with pytest.raises(ValidationError):
        node.run(node.client._submit_order(submit(market())))


def test_a_submission_carries_the_credential_in_the_two_headers_and_nowhere_else(
    node: Node,
) -> None:
    """Never a query parameter: a URL reaches proxy logs and a recorded request."""
    node.wire.route("POST", ORDERS_PATH, body(order()))
    node.connect()
    node.run(node.client._submit_order(submit(market())))
    posted = [one for one in node.wire.sent if one.method == "POST"][0]
    assert posted.headers == {"APCA-API-KEY-ID": PAPER_KEY, "APCA-API-SECRET-KEY": SECRET}
    assert PAPER_KEY not in posted.path
    assert PAPER_KEY not in json.dumps(posted.params)
    assert PAPER_KEY.encode() not in (posted.body or b"")


def test_an_order_the_broker_refuses_is_rejected_with_what_the_broker_said(node: Node) -> None:
    """Its own code and message, and nothing that was sent to it."""
    node.wire.route(
        "POST",
        ORDERS_PATH,
        body({"code": 40010001, "message": "client_order_id must be unique"}, status=422),
    )
    node.connect()
    node.run(node.client._submit_order(submit(market())))
    rejected = node.events[-1]
    assert type(rejected).__name__ == "OrderRejected"
    assert "must be unique" in rejected.reason
    assert "40010001" in rejected.reason


def test_an_unreadable_answer_to_a_submission_is_a_rejection(node: Node) -> None:
    """An order whose fate cannot be read is not an order that was accepted."""
    node.wire.route("POST", ORDERS_PATH, Response(status=200, body=b"<html>nope</html>"))
    node.connect()
    node.run(node.client._submit_order(submit(market())))
    assert type(node.events[-1]).__name__ == "OrderRejected"


def test_an_order_that_came_back_under_another_identity_is_rejected(node: Node) -> None:
    """The client order id is the handle a restart recognises the order by."""
    node.wire.route("POST", ORDERS_PATH, body(order(client_order_id="something-else")))
    node.connect()
    node.run(node.client._submit_order(submit(market())))
    rejected = node.events[-1]
    assert type(rejected).__name__ == "OrderRejected"
    assert "client_order_id" in rejected.reason


def test_cancelling_reports_what_the_broker_says_the_order_now_is(node: Node) -> None:
    """A cancel request is a request; the order's own status is the answer."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("DELETE", path, Response(status=204))
    node.wire.route("GET", path, body(order(status="canceled")))
    node.connect()
    node.run(node.client._cancel_order(cancel(market())))
    assert node.wire.paths("DELETE") == [path]
    assert node.reports[-1].order_status == OrderStatus.CANCELED


def test_a_cancel_the_broker_refuses_is_a_cancel_rejection(node: Node) -> None:
    """And the order is not re-read, because nothing about it changed."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("DELETE", path, body({"message": "order is not cancelable"}, status=422))
    node.connect()
    node.run(node.client._cancel_order(cancel(market())))
    assert type(node.events[-1]).__name__ == "OrderCancelRejected"
    assert node.wire.paths("GET") == [ACCOUNT_PATH]


def test_an_order_gone_by_the_time_it_is_re_read_reports_nothing(node: Node) -> None:
    """A cancellation that raced the broker's own bookkeeping is not an event."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("DELETE", path, Response(status=204))
    node.wire.route("GET", path, Response(status=404))
    node.connect()
    node.run(node.client._cancel_order(cancel(market())))
    assert node.reports == []


def test_cancelling_everything_cancels_only_what_the_command_selects(node: Node) -> None:
    """The broker's cancel-everything takes no filter, so the engine's own orders do."""
    node.connect()
    opened(node, market())
    opened(node, limit(instrument_id=InstrumentId.from_str("MSFT.XNAS")), VenueOrderId("other"))
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("DELETE", path, Response(status=204))
    node.wire.route("GET", path, body(order(status="canceled")))
    node.run(
        node.client._cancel_all_orders(
            CancelAllOrders(
                trader_id=TRADER,
                strategy_id=STRATEGY,
                instrument_id=INSTRUMENT,
                order_side=OrderSide.NO_ORDER_SIDE,
                command_id=UUID4(),
                ts_init=0,
            )
        )
    )
    assert node.wire.paths("DELETE") == [path]


def test_cancelling_everything_of_one_side_leaves_the_other_side_alone(node: Node) -> None:
    """A stage flattening its longs must not cancel the hedge it means to keep."""
    node.connect()
    opened(node, market())
    node.run(
        node.client._cancel_all_orders(
            CancelAllOrders(
                trader_id=TRADER,
                strategy_id=STRATEGY,
                instrument_id=INSTRUMENT,
                order_side=OrderSide.SELL,
                command_id=UUID4(),
                ts_init=0,
            )
        )
    )
    assert node.wire.paths("DELETE") == []


def test_a_batch_cancels_every_order_it_names(node: Node) -> None:
    """In the order the batch names them, one request each."""
    node.connect()
    for index, venue_order_id in enumerate(("a", "b")):
        path = f"{ORDERS_PATH}/{venue_order_id}"
        node.wire.route("DELETE", path, Response(status=204))
        node.wire.route("GET", path, body(order(id=venue_order_id, client_order_id=f"O-{index}")))
    node.run(
        node.client._batch_cancel_orders(
            BatchCancelOrders(
                trader_id=TRADER,
                strategy_id=STRATEGY,
                instrument_id=INSTRUMENT,
                cancels=[cancel(market(), VenueOrderId(one)) for one in ("a", "b")],
                command_id=UUID4(),
                ts_init=0,
            )
        )
    )
    assert node.wire.paths("DELETE") == [f"{ORDERS_PATH}/a", f"{ORDERS_PATH}/b"]


def test_an_order_the_venue_has_not_named_yet_cannot_be_cancelled_at_the_broker(
    node: Node,
) -> None:
    """The broker cancels by its own id, so there is nothing to ask it to cancel."""
    node.connect()
    node.run(node.client._cancel_order(cancel(market(), None)))
    rejected = node.events[-1]
    assert type(rejected).__name__ == "OrderCancelRejected"
    assert "own order id" in rejected.reason
    assert node.wire.paths("DELETE") == []


def test_a_cancel_of_everything_reports_each_refusal_against_its_own_order(
    node: Node,
) -> None:
    """A rejection names the order it belongs to, not the command that swept it up."""
    node.connect()
    one = opened(node, market())
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("DELETE", path, body({"message": "order is not cancelable"}, status=422))
    node.run(
        node.client._cancel_all_orders(
            CancelAllOrders(
                trader_id=TRADER,
                strategy_id=STRATEGY,
                instrument_id=INSTRUMENT,
                order_side=OrderSide.NO_ORDER_SIDE,
                command_id=UUID4(),
                ts_init=0,
            )
        )
    )
    rejected = node.events[-1]
    assert type(rejected).__name__ == "OrderCancelRejected"
    assert rejected.client_order_id == one.client_order_id
    assert rejected.venue_order_id == VENUE_ORDER_ID
    assert rejected.instrument_id == INSTRUMENT


def cancel(one: Any, venue_order_id: VenueOrderId = VENUE_ORDER_ID) -> CancelOrder:
    """The command a cancellation arrives as."""
    return CancelOrder(
        trader_id=TRADER,
        strategy_id=STRATEGY,
        instrument_id=one.instrument_id,
        client_order_id=one.client_order_id,
        venue_order_id=venue_order_id,
        command_id=UUID4(),
        ts_init=0,
    )


def test_a_replacement_is_reported_with_the_identity_the_broker_gave_it(node: Node) -> None:
    """The broker replaces rather than amends, so the venue order id moves."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("PATCH", path, body(order(id="replacement", qty="5")))
    node.connect()
    one = opened(node, limit())
    node.run(node.client._modify_order(modify(one, quantity=Quantity.from_str("5"))))
    assert node.wire.sent[-1].payload == {"qty": "5"}
    updated = node.events[-1]
    assert type(updated).__name__ == "OrderUpdated"
    assert updated.venue_order_id == VenueOrderId("replacement")
    assert updated.quantity == Quantity.from_str("5")


def test_a_replacement_restates_only_what_the_command_changed(node: Node) -> None:
    """A price restated for no reason would replace the order for no reason."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("PATCH", path, body(order(id="replacement")))
    node.connect()
    one = opened(node, stop_limit())
    node.run(
        node.client._modify_order(
            modify(one, price=Price.from_str("298.00"), trigger_price=Price.from_str("299.00"))
        )
    )
    assert node.wire.sent[-1].payload == {"limit_price": "298", "stop_price": "299"}
    assert node.events[-1].quantity == one.quantity


def test_a_replacement_that_changes_nothing_is_refused(node: Node) -> None:
    """There is nothing to ask the broker for, and asking would replace the order."""
    node.connect()
    one = opened(node, limit())
    before = len(node.wire.sent)
    node.run(node.client._modify_order(modify(one)))
    assert len(node.wire.sent) == before
    assert type(node.events[-1]).__name__ == "OrderModifyRejected"


def test_a_replacement_of_an_order_the_venue_has_not_named_is_refused(node: Node) -> None:
    """The broker amends by its own order id, so there is nothing to amend yet."""
    node.connect()
    one = limit()
    node.cache.add_order(one)
    node.run(node.client._modify_order(modify(one, quantity=Quantity.from_str("5"))))
    rejected = node.events[-1]
    assert type(rejected).__name__ == "OrderModifyRejected"
    assert "own order id" in rejected.reason
    assert node.wire.paths("PATCH") == []


def test_a_replacement_of_an_order_the_engine_no_longer_holds_is_refused(node: Node) -> None:
    """There would be nothing to report the replacement against."""
    node.connect()
    node.run(node.client._modify_order(modify(limit(), quantity=Quantity.from_str("5"))))
    assert type(node.events[-1]).__name__ == "OrderModifyRejected"
    assert node.wire.paths("PATCH") == []


def test_a_replacement_the_broker_refuses_is_a_modify_rejection(node: Node) -> None:
    """With the broker's own account of it and nothing that was sent."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("PATCH", path, body({"message": "order is not replaceable"}, status=422))
    node.connect()
    one = opened(node, limit())
    node.run(node.client._modify_order(modify(one, quantity=Quantity.from_str("5"))))
    rejected = node.events[-1]
    assert type(rejected).__name__ == "OrderModifyRejected"
    assert "not replaceable" in rejected.reason


def modify(one: Any, **changes: Any) -> ModifyOrder:
    """The command a replacement arrives as."""
    fields: dict[str, Any] = {
        "trader_id": TRADER,
        "strategy_id": STRATEGY,
        "instrument_id": one.instrument_id,
        "client_order_id": one.client_order_id,
        "venue_order_id": one.venue_order_id,
        "quantity": None,
        "price": None,
        "trigger_price": None,
        "command_id": UUID4(),
        "ts_init": 0,
    }
    fields.update(changes)
    return ModifyOrder(**fields)


# --- the key belongs to one account --------------------------------------------


def test_a_client_cannot_be_opened_with_the_other_accounts_credentials() -> None:
    """The second of the two independent guards keeping a key on its own host."""
    clock = LiveClock()
    with pytest.raises(PreconditionError) as raised:
        AlpacaExecutionClient(
            asyncio.new_event_loop(),
            client_id=LIVE_CLIENT,
            credentials=credentials(PAPER_CLIENT),
            send=wire(),
            host=LIVE_HOST,
            instrument_provider=Held(),
            msgbus=MessageBus(trader_id=TRADER, clock=clock),
            cache=Cache(),
            clock=clock,
        )
    assert LIVE_CLIENT in str(raised.value)
    assert PAPER_KEY not in str(raised.value)


def test_a_client_of_an_id_this_broker_does_not_provide_is_refused() -> None:
    """Before a credential is read, and naming the ids there are."""
    clock = LiveClock()
    with pytest.raises(PreconditionError):
        AlpacaExecutionClient(
            asyncio.new_event_loop(),
            client_id="alpaca_futures",
            credentials=credentials(PAPER_CLIENT),
            send=wire(),
            host=PAPER_HOST,
            instrument_provider=Held(),
            msgbus=MessageBus(trader_id=TRADER, clock=clock),
            cache=Cache(),
            clock=clock,
        )


def test_the_real_account_addresses_the_real_host_and_says_which_it_is(live: Node) -> None:
    """One client per account: the id decides the variables and the host together."""
    assert live.client.host == LIVE_HOST
    assert live.client.kanso_client_id == LIVE_CLIENT
    assert "live" in repr(live.client)
    live.connect()
    assert live.wire.sent[0].headers["APCA-API-KEY-ID"] == LIVE_KEY


def test_a_key_the_broker_refuses_is_reported_by_its_variable_and_never_its_value(
    node: Node,
) -> None:
    """An operator needs the name of the variable to fix it, and nothing else."""
    node.wire.route("GET", ACCOUNT_PATH, body({"message": "forbidden."}, status=403))
    with pytest.raises(PreconditionError) as raised:
        node.connect()
    message = str(raised.value) + str(raised.value.remedy)
    assert credential_names(PAPER_CLIENT)[0] in message
    assert PAPER_KEY not in message
    assert SECRET not in message


def test_a_broker_fault_that_is_not_a_refusal_names_the_path_and_the_status(
    node: Node,
) -> None:
    """A gateway failure is not an entitlement failure and does not read like one."""
    node.wire.route("GET", ACCOUNT_PATH, body({"message": "bad gateway"}, status=502))
    with pytest.raises(PreconditionError) as raised:
        node.connect()
    assert "502" in str(raised.value)
    assert credential_names(PAPER_CLIENT)[0] not in str(raised.value)


# --- the credential leak scan --------------------------------------------------


def test_no_credential_reaches_a_message_a_repr_a_report_or_a_configuration(
    node: Node,
) -> None:
    """Every path is driven, and then the key and the secret are looked for everywhere."""
    path = f"{ORDERS_PATH}/{VENUE_ORDER_ID.value}"
    node.wire.route("POST", ORDERS_PATH, body(order()))
    node.wire.route("GET", ORDERS_PATH, body([order(status="filled", **_filled("10"))]))
    node.wire.route("GET", POSITIONS_PATH, body([position()]))
    node.wire.route("DELETE", path, Response(status=204))
    node.wire.route("GET", path, body(order(status="canceled")))
    node.wire.route("PATCH", path, body(order(id="replacement", qty="5")))
    node.connect()
    node.run(node.client._submit_order(submit(market())))
    node.run(node.client._submit_order(submit(limit(post_only=True))))
    node.run(node.client.generate_mass_status())
    one = opened(node, limit(), VENUE_ORDER_ID)
    node.run(node.client._modify_order(modify(one, quantity=Quantity.from_str("5"))))
    node.run(node.client._cancel_order(cancel(one)))
    written = [
        repr(node.client),
        str(node.client.id),
        str(AlpacaExecClientConfig(client_id=PAPER_CLIENT, workspace="/tmp/ws")),
        *[str(one) for one in node.messages],
        *[repr(one) for one in node.messages],
        repr(node.client._credentials),
    ]
    for text in written:
        assert PAPER_KEY not in text
        assert SECRET not in text


def test_neither_module_names_a_credential_variable_of_its_own() -> None:
    """All four names are derived from the standard scheme, in one place, in `config`."""
    for module in (executing, factories):
        source = Path(module.__file__ or "").read_text()
        assert "KANSO_" not in source
        assert "APCA" not in source


def test_the_client_asks_for_nothing_but_the_four_measured_paths(node: Node) -> None:
    """No order stream, and no endpoint whose shape was never served to anybody."""
    node.wire.route("GET", ORDERS_PATH, body([order()]))
    node.wire.route("GET", POSITIONS_PATH, body([position()]))
    node.connect()
    node.run(node.client.generate_mass_status())
    known = {ACCOUNT_PATH, ORDERS_PATH, POSITIONS_PATH, BY_CLIENT_ORDER_ID_PATH}
    for asked in node.wire.sent:
        assert asked.path in known or asked.path.startswith(f"{ORDERS_PATH}/")


def test_every_request_counts_against_the_accounts_own_rate_limit_buckets(node: Node) -> None:
    """One quota per account, and every request named in it."""
    node.connect()
    assert node.wire.sent[0].keys == ("v2/account", "v2")


# --- the factory ---------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A workspace whose `.env` holds both accounts' keys, each under its own names."""
    for names in (credential_names(PAPER_CLIENT), credential_names(LIVE_CLIENT)):
        for name in names:
            monkeypatch.delenv(name, raising=False)
    built = init(tmp_path / "ws")
    path = built.root / ".env"
    lines = [path.read_text()]
    for client_id, key_value in ((PAPER_CLIENT, PAPER_KEY), (LIVE_CLIENT, LIVE_KEY)):
        key, secret = credential_names(client_id)
        lines.append(f"{key}={key_value}\n{secret}={SECRET}\n")
    path.write_text("\n".join(lines))
    return built


@pytest.fixture
def stub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """This adapter's instrument provider, which another slice owns, stubbed out."""
    module = types.ModuleType("kanso.nautilus.adapters.alpaca.provider")
    module.provider = lambda ws: Held()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """The adapter's own connection builder, replaced so nothing opens a socket.

    The factory asks the broker for the one rate-limited connection the whole adapter
    shares; here that connection is the frozen wire, so the factory's whole path — the
    workspace, the id, the account, the host and the credentials — runs with no network.
    """
    transport = wire()
    monkeypatch.setattr(alpaca, "pyo3_transport", lambda **settings: transport)
    return transport


@pytest.mark.parametrize(
    ("client_id", "host"), [(PAPER_CLIENT, PAPER_HOST), (LIVE_CLIENT, LIVE_HOST)]
)
def test_the_factory_builds_a_client_paired_with_the_account_the_id_names(
    client_id: str, host: str, workspace: Workspace, stub_provider: None, offline: Wire
) -> None:
    """One factory, both ids: the id decides the variables, the host and the account."""
    loop = asyncio.new_event_loop()
    clock = LiveClock()
    try:
        built = EXEC_CLIENT_FACTORY.create(
            loop=loop,
            name=client_id,
            config=AlpacaExecClientConfig(workspace=str(workspace.root)),
            msgbus=MessageBus(trader_id=TRADER, clock=clock),
            cache=Cache(),
            clock=clock,
        )
    finally:
        loop.close()
    assert isinstance(built, AlpacaExecutionClient)
    assert built.kanso_client_id == client_id
    assert built.host == host
    assert built.id == CLIENT_ID


def test_the_configuration_may_name_the_account_the_node_did_not(
    workspace: Workspace, stub_provider: None, offline: Wire
) -> None:
    """A stage registering the client under another name still says which account it is."""
    loop = asyncio.new_event_loop()
    clock = LiveClock()
    try:
        built = EXEC_CLIENT_FACTORY.create(
            loop=loop,
            name="whatever-the-node-called-it",
            config=AlpacaExecClientConfig(client_id=LIVE_CLIENT, workspace=str(workspace.root)),
            msgbus=MessageBus(trader_id=TRADER, clock=clock),
            cache=Cache(),
            clock=clock,
        )
    finally:
        loop.close()
    assert built.kanso_client_id == LIVE_CLIENT
    assert built.host == LIVE_HOST


def test_both_execution_client_ids_reach_this_one_factory() -> None:
    """The broker declares two clients and the core reaches both through one entry point."""
    assert {spec.id for spec in BROKER.exec_clients} == {PAPER_CLIENT, LIVE_CLIENT}
    assert BROKER.exec_client_factory() is EXEC_CLIENT_FACTORY


def test_the_factory_refuses_a_configuration_that_is_not_this_adapters(
    workspace: Workspace,
) -> None:
    """A client configured with another adapter's table is not this broker's client."""
    from nautilus_trader.config import LiveExecClientConfig

    loop = asyncio.new_event_loop()
    clock = LiveClock()
    try:
        with pytest.raises(PreconditionError):
            EXEC_CLIENT_FACTORY.create(
                loop=loop,
                name=PAPER_CLIENT,
                config=LiveExecClientConfig(),
                msgbus=MessageBus(trader_id=TRADER, clock=clock),
                cache=Cache(),
                clock=clock,
            )
    finally:
        loop.close()


def test_the_factory_refuses_an_id_this_broker_does_not_provide(workspace: Workspace) -> None:
    """Before any credential is read."""
    loop = asyncio.new_event_loop()
    clock = LiveClock()
    try:
        with pytest.raises(PreconditionError):
            EXEC_CLIENT_FACTORY.create(
                loop=loop,
                name="alpaca_futures",
                config=AlpacaExecClientConfig(workspace=str(workspace.root)),
                msgbus=MessageBus(trader_id=TRADER, clock=clock),
                cache=Cache(),
                clock=clock,
            )
    finally:
        loop.close()


def test_the_factories_the_registry_reaches_are_the_ones_each_client_module_defines(
    workspace: Workspace,
) -> None:
    """The adapter's entry points are the two factories, imported when asked for."""
    assert BROKER.exec_client_factory() is EXEC_CLIENT_FACTORY
    assert BROKER.data_client_factory() is DATA_CLIENT_FACTORY
