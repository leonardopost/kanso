"""The execution client: what may be sent, what comes back, and what a restart repeats.

This is the first code in kanso that can move money, so most of it is about refusing to.

**An order shape the broker cannot honour is refused by name, before it is sent.** The
deny table below is walked before a request is built, and a refusal produces an
`OrderDenied` naming the field that caused it — never a translation into the nearest
thing the broker does offer. A post-only flag silently dropped, a good-till-date quietly
made good-till-cancel or an if-touched sent as a stop would each fill a strategy at a
price it never asked for, and the strategy would have no way to know. The engine's own
tables in this package's parser hold the order types, the sides and the times in force;
this module holds the shapes that have no field at all on the wire.

**The client order id is the idempotency handle, and it is sent verbatim.** It is
supplied by kanso, returned unchanged by the broker, and is the only field that survives a
restart on both sides. Nothing here rewrites it, and a broker row that came back carrying
a different one fails the submission rather than being accepted under an identity the
strategy cannot recognise. Everything derived from an order row is derived from that id.

**A restart produces no duplicate fill, by two independent mechanisms.** The broker
reports a *cumulative* filled quantity rather than a list of executions, so a fill report
is the difference between what the row says and what this process has already recorded —
read from the cache by the row's own client order id, and by the venue order id when a
replacement changed it. Read the same row twice and the second read yields nothing at
all. And even where the caller cannot suppress a report — a fresh process with an empty
cache, a reconciliation sweep — the trade id is derived from the client order id and that
cumulative quantity, so the same row always produces the same trade id and the engine
skips a trade id the order already carries. The first mechanism stops the report being
made; the second stops it being applied.

**There is no order stream here, and that is deliberate.** The broker publishes a
websocket carrying order updates, and neither its endpoint nor its message shapes were
measured. Reading an unmeasured shape would mean writing fixtures that encode what was
expected rather than what was served, which is the failure this build has already paid
for once. So every fact this client reports comes from the measured request surface —
the account, the orders and the positions — and the engine's own polling is what turns
those into events: `open_check_interval_secs` and `position_check_interval_secs` on the
live execution engine are the two settings a stage on this broker must set.

**Nothing here carries a credential anywhere but into a request header.** The
credentials are held for the life of the client because a client cannot reach a
workspace, and their `repr` names the account and never the values; no message, log line,
report or exception in this module contains one, and a key the broker refuses is reported
by the name of the variable it came from.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`LiveExecutionClient(loop, client_id, venue, oms_type, account_type, base_currency,
instrument_provider, msgbus, cache, clock, config)` is the base a broker client
subclasses. `venue=None` declares a multi-venue client, which this broker is: it routes
to whichever venue an equity is listed on, and the exec engine is given one venue routing
per venue. `ExecutionClient._set_account_id` asserts that the client id equals the
account id's issuer, so the engine client id here is the issuer the parser stamps on an
account id and not the kanso client id, which names the account rather than the broker.
`connect()` and `disconnect()` wrap `_connect`/`_disconnect` and own the connected flag.
`generate_mass_status` is the fifth report generator and is the base's: it calls the four
implemented here and assembles their answers. A `FillReport` whose trade id an order
already carries is skipped by reconciliation. `generate_order_updated` takes
`venue_order_id_modified`, which is what a broker that answers a replacement with a new
order id needs. `Money` raises `ValueError` on a magnitude its fixed point cannot hold,
so every wire amount is built through a guard that names the field instead.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.enums import (
    AccountType,
    ContingencyType,
    OmsType,
    OrderType,
    TrailingOffsetType,
    TriggerType,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    VenueOrderId,
)
from nautilus_trader.model.objects import AccountBalance, Currency, MarginBalance, Money

from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus.adapters.alpaca.config import (
    Credentials,
    Response,
    Transport,
    account,
    endpoint,
    key_name,
)
from kanso.nautilus.adapters.alpaca.parsing import (
    ISSUER,
    NS_PER_SECOND,
    account_id,
    client_order_id,
    decimal_of,
    fill_report,
    instant,
    order_status_report,
    position_report,
    rfc3339,
    symbol_of,
    to_broker_order_type,
    to_broker_side,
    to_broker_time_in_force,
    unsupported_reason,
)
from kanso.nautilus.adapters.alpaca.venue import CURRENCY, serves

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from nautilus_trader.common.providers import InstrumentProvider
    from nautilus_trader.model.instruments import Instrument

__all__ = [
    "ACCOUNT_BLOCKS",
    "ACCOUNT_PATH",
    "BY_CLIENT_ORDER_ID_PATH",
    "CLIENT_ID",
    "MAX_PAGES",
    "ORDERS_PATH",
    "PAGE_LIMIT",
    "POSITIONS_PATH",
    "TRAILING_OFFSETS",
    "TRIGGERS",
    "UNSUPPORTED",
    "AccountRow",
    "AlpacaExecutionClient",
    "Sender",
    "account_row",
    "denial",
    "sender",
    "submission",
]

ACCOUNT_PATH: Final = "/v2/account"
ORDERS_PATH: Final = "/v2/orders"
BY_CLIENT_ORDER_ID_PATH: Final = "/v2/orders:by_client_order_id"
POSITIONS_PATH: Final = "/v2/positions"
"""The four request paths this client reads. Every one of them was measured; the order
stream that would push the same facts was not, and is deliberately not read."""

PAGE_LIMIT: Final = 500
"""How many order rows one page carries, which is the broker's published ceiling."""

MAX_PAGES: Final = 200
"""How many pages one walk may fetch: a hundred thousand orders, far past any account
kanso deploys to, and a cheap guard against a cursor that does not advance."""

CLIENT_ID: Final = ClientId(ISSUER)
"""The engine's id for this client. It is the issuer half of the account id rather than
the kanso client id, because the engine asserts the two are equal when the account is
registered; which of the broker's two accounts is being traded is the account id's own
second half, and the client carries the kanso id beside it for every message an operator
reads."""

OMS_TYPE: Final = OmsType.NETTING
"""One net position per symbol, which is what a US equity account holds."""

ACCOUNT_TYPE: Final = AccountType.MARGIN
"""The account type this broker declares for the venues it serves."""

ACCOUNT_BLOCKS: Final[tuple[str, ...]] = ("account_blocked", "trading_blocked")
"""The flags that say the broker will not accept an order at all.

Read when the row carries them and never assumed: a flag the broker did not send is a
flag it did not set. The account row's key set was not part of the read-only measurement,
so this module requires only the fields an account state cannot be built without, and
treats everything else as present-or-absent rather than as true-or-false."""

TRIGGERS: Final[frozenset[TriggerType]] = frozenset(
    {TriggerType.DEFAULT, TriggerType.LAST_PRICE, TriggerType.NO_TRIGGER}
)
"""What a stop may be triggered by. The broker triggers on the last traded price and
offers no choice, so a strategy asking for a mark, an index or a bid-ask trigger is
refused rather than given a last-price stop that fires at a different moment."""

TRAILING_OFFSETS: Final[dict[TrailingOffsetType, str]] = {
    TrailingOffsetType.PRICE: "trail_price",
    TrailingOffsetType.BASIS_POINTS: "trail_percent",
}
"""How a trailing stop's offset is spelled. The broker offers an absolute offset and a
percentage; a basis point is a hundredth of a percent, so that conversion is arithmetic
rather than a guess about what the broker means. Ticks and price tiers have no spelling
here and are refused by name — a tick is a property of the instrument and the broker
would be given a number in the wrong unit."""

BODY: Final = "body"
"""The transport parameter an order needs. A submission is a request with a JSON body,
and the shared transport is the only connection this adapter is allowed to open."""

ZERO: Final = Decimal(0)


def _flagged(order: Any, name: str) -> bool:
    """Whether an order carries a flag, for a flag only some order types have."""
    return bool(getattr(order, name, False))


UNSUPPORTED: Final[tuple[tuple[str, Callable[[Any], bool], str], ...]] = (
    (
        "venue",
        lambda order: not serves(order.instrument_id.venue.value),
        "the broker does not trade this venue",
    ),
    (
        "post_only",
        lambda order: _flagged(order, "is_post_only"),
        "the broker has no post-only flag, and an order sent without one may take liquidity",
    ),
    (
        "reduce_only",
        lambda order: _flagged(order, "is_reduce_only"),
        "the broker has no reduce-only flag, and an order sent without one may open a position",
    ),
    (
        "display_qty",
        lambda order: getattr(order, "display_qty", None) is not None,
        "the broker shows an order in full and has no reserve quantity",
    ),
    (
        "quote_quantity",
        lambda order: _flagged(order, "is_quote_quantity"),
        "a quantity in the quote currency is a notional order, and kanso sizes in shares",
    ),
    (
        "contingency_type",
        lambda order: order.contingency_type != ContingencyType.NO_CONTINGENCY,
        "the broker's bracket and one-cancels-other legs are not mapped by this adapter",
    ),
    (
        "trigger_type",
        lambda order: getattr(order, "trigger_type", TriggerType.NO_TRIGGER) not in TRIGGERS,
        "the broker triggers a stop on the last traded price and offers no other trigger",
    ),
    (
        "trailing_offset_type",
        lambda order: (
            order.order_type is OrderType.TRAILING_STOP_MARKET
            and order.trailing_offset_type not in TRAILING_OFFSETS
        ),
        "the broker trails by an absolute offset or a percentage and by nothing else",
    ),
)
"""The order shapes that have no field on this broker's wire, each named by the field it
was read from. Walked in order; the first match is the reason the order is denied. The
order type, the side and the time in force are not here — those are the parser's own
tables, and are consulted after this one so that a refusal always names one field."""


class Sender(Protocol):
    """How a request that may carry a body is sent.

    The same rate-limited connection the whole adapter shares, with the one addition an
    execution client needs of it: an order is a POST with a JSON body, and a body dropped
    on the way to the broker would be an order with no symbol, no side and no quantity.
    """

    def __call__(
        self,
        method: str,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        keys: Sequence[str],
        body: bytes | None = None,
    ) -> Response: ...


def sender(transport: Transport) -> Sender:
    """The shared transport, checked once for the one thing this client needs of it.

    Checked here, when the client is built, rather than discovered at the first order:
    a connection that cannot carry a body can read every report in this module and then
    fail on the one request that matters, in the middle of a session. The check is a
    signature check because the transport is a plain callable: this adapter's own carries
    the argument and a type checker proves it, but an extension or a test may hand over any
    callable at all. It refuses rather than falling back to a connection of its own — a
    second connection would be a second quota, and two quotas under one account is twice
    the published limit.
    """
    try:
        accepts = tuple(inspect.signature(transport).parameters)
    except (TypeError, ValueError):
        accepts = ()
    if BODY not in accepts:
        raise PreconditionError(
            "alpaca: the connection this client was given cannot carry a request body, and "
            "an order is a request with a body",
            remedy=(
                f"give it this adapter's own transport, whose {BODY!r} argument is where an "
                "order's fields travel"
            ),
        )
    return transport


# --- the account --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountRow:
    """The account endpoint's row, reduced to what an account state is built from.

    The row's key set was not part of the read-only measurement, so only the fields a
    state cannot be built without are required; the margins are optional because an
    account holding nothing reports none, and the blocks are read when present because a
    flag the broker did not send is a flag it did not set.
    """

    number: str
    currency: str
    cash: Decimal
    equity: Decimal
    initial_margin: Decimal | None
    maintenance_margin: Decimal | None
    blocked: tuple[str, ...]

    def balances(self) -> list[AccountBalance]:
        """The account's funds: everything it is worth, and how much of it is committed.

        The total is the account's equity — its cash plus the value of what it holds —
        because that is what a position is marked against. The locked part is the initial
        margin the broker says is required against those positions, which is the broker's
        own word for funds that are not available to open another one. A requirement
        larger than the equity is an account in a margin call rather than a broken row,
        and it locks everything rather than reporting a negative free balance the engine
        would refuse.
        """
        currency = Currency.from_str(self.currency)
        total = _money(self.equity, currency, "equity")
        required = min(self.initial_margin or ZERO, self.equity)
        locked = _money(max(required, ZERO), currency, "initial_margin")
        return [AccountBalance(total, locked, Money(total - locked, currency))]

    def margins(self) -> list[MarginBalance]:
        """What the broker requires against the account's positions, when it says."""
        if self.initial_margin is None or self.maintenance_margin is None:
            return []
        currency = Currency.from_str(self.currency)
        return [
            MarginBalance(
                _money(self.initial_margin, currency, "initial_margin"),
                _money(self.maintenance_margin, currency, "maintenance_margin"),
            )
        ]


def account_row(row: Mapping[str, Any]) -> AccountRow:
    """One account row, read field by field because its key set was never measured."""
    return AccountRow(
        number=_text(row, "account_number"),
        currency=_text(row, "currency"),
        cash=_amount(row, "cash"),
        equity=_amount(row, "equity"),
        initial_margin=_optional(row, "initial_margin"),
        maintenance_margin=_optional(row, "maintenance_margin"),
        blocked=tuple(name for name in ACCOUNT_BLOCKS if row.get(name) is True),
    )


def _text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"account.{name}: {value!r} is not a value the broker writes here")
    return value


def _amount(row: Mapping[str, Any], name: str) -> Decimal:
    return decimal_of(row.get(name), f"account.{name}")


def _optional(row: Mapping[str, Any], name: str) -> Decimal | None:
    value = row.get(name)
    return None if value is None else decimal_of(value, f"account.{name}")


def _money(value: Decimal, currency: Currency, name: str) -> Money:
    """One wire amount as money, failing by name on a magnitude the engine cannot hold."""
    try:
        return Money(value, currency)
    except (ValueError, ArithmeticError, InvalidOperation) as exc:
        raise ValidationError(f"account.{name}: {value} is not an amount: {exc}") from None


# --- what may be sent ---------------------------------------------------------


def denial(order: Any) -> str | None:
    """Why this order cannot be sent to the broker at all, or `None` when it can.

    The deny table first, so a shape with no field on the wire is named by that field;
    then the parser's own tables for the client order id, the side, the type and the time
    in force, so an order the broker has no spelling for is named by the value it carries.
    A refusal is a refusal: nothing here returns a substitute, and everything `submission`
    could refuse is refused here, so an order that survives this is an order that can be
    built into a request.
    """
    for field, matches, reason in UNSUPPORTED:
        if matches(order):
            return f"{field}: {reason}"
    try:
        client_order_id(order.client_order_id)
        to_broker_side(order.side)
        to_broker_order_type(order.order_type)
        to_broker_time_in_force(order.time_in_force)
    except ValidationError as exc:
        return exc.message
    return None


def submission(order: Any) -> dict[str, Any]:
    """The broker's JSON body for one order, refusing a shape it cannot honour.

    The client order id travels verbatim, which is the whole of the idempotency: the
    broker returns it unchanged, it is the only handle that survives a restart on both
    sides, and every identity derived downstream is derived from it.
    """
    reason = denial(order)
    if reason is not None:
        raise ValidationError(f"order {order.client_order_id.value}: {reason}")
    body: dict[str, Any] = {
        "symbol": symbol_of(order.instrument_id),
        "qty": _plain(order.quantity.as_decimal()),
        "side": to_broker_side(order.side),
        "type": to_broker_order_type(order.order_type),
        "time_in_force": to_broker_time_in_force(order.time_in_force),
        "client_order_id": client_order_id(order.client_order_id),
    }
    if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
        body["limit_price"] = _plain(order.price.as_decimal())
    if order.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
        body["stop_price"] = _plain(order.trigger_price.as_decimal())
    if order.order_type is OrderType.TRAILING_STOP_MARKET:
        body[TRAILING_OFFSETS[order.trailing_offset_type]] = _trailing(order)
    return body


def _trailing(order: Any) -> str:
    """A trailing offset in the unit the broker names it in.

    An absolute offset travels as it stands. Basis points travel as a percentage, which
    is the same number divided by a hundred and is exact in decimal, so nothing is lost
    and nothing is invented.
    """
    offset = Decimal(order.trailing_offset)
    if order.trailing_offset_type is TrailingOffsetType.BASIS_POINTS:
        offset = offset.scaleb(-2)
    return _plain(offset)


def _plain(value: Decimal) -> str:
    """A decimal in the one spelling the wire carries, without an exponent."""
    return format(value.normalize(), "f")


# --- the client ---------------------------------------------------------------


class AlpacaExecutionClient(LiveExecutionClient):
    """One of this broker's two accounts, as an execution client.

    Holds the credentials of the account it was opened for, because a client cannot reach
    a workspace and a request needs them; their `repr` names the account and never the
    values, and nothing in this class puts one anywhere but in a request header. The
    account the credentials were resolved for is checked against the account the client
    was built for, which is the second of the two independent guards keeping a paper key
    off the real host and a real key off the paper one.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        client_id: str,
        credentials: Credentials,
        send: Sender,
        host: str,
        instrument_provider: InstrumentProvider,
        msgbus: Any,
        cache: Any,
        clock: Any,
        config: Any = None,
    ) -> None:
        found = account(client_id)
        if credentials.account.client_id != found.client_id:
            raise PreconditionError(
                f"alpaca: {credentials.account.client_id!r} credentials cannot open "
                f"{found.client_id!r}; a key belongs to one account",
                remedy=f"resolve the credentials of {found.client_id!r}",
            )
        super().__init__(
            loop=loop,
            client_id=CLIENT_ID,
            venue=None,
            oms_type=OMS_TYPE,
            account_type=ACCOUNT_TYPE,
            base_currency=Currency.from_str(CURRENCY),
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            config=config,
        )
        self.kanso_client_id = found.client_id
        self.environment = found.environment
        self.host = host.rstrip("/")
        self._credentials = credentials
        self._send = send
        self._gate = asyncio.Lock()

    def __repr__(self) -> str:
        """The account this trades and the world it trades in; never a credential."""
        return (
            f"{type(self).__name__}(client_id={self.kanso_client_id!r}, "
            f"environment={self.environment.value!r})"
        )

    # --- the connection -------------------------------------------------------

    async def _connect(self) -> None:
        """Read the account, register it, and publish what the broker says it holds.

        The account is the first thing asked for because it is what proves the credential
        opens the account it was configured for: the id it answers with becomes the engine
        account id, and an account the broker says is blocked stops the client here rather
        than at the first order.
        """
        await self._instrument_provider.initialize()
        row = await self._one(ACCOUNT_PATH)
        if row is None:
            raise PreconditionError(
                f"alpaca: the {self.environment.value} host served no account for "
                f"{self.kanso_client_id!r}",
                remedy="run `kanso doctor` to check the adapter's credentials",
            )
        found = account_row(row)
        if found.currency != CURRENCY:
            raise PreconditionError(
                f"alpaca: {self.kanso_client_id!r} is denominated in {found.currency!r} and "
                f"this adapter trades {CURRENCY} accounts",
            )
        if found.blocked:
            raise PreconditionError(
                f"alpaca: the broker reports {self.kanso_client_id!r} as "
                f"{', '.join(found.blocked)}, so it will accept no order",
                remedy="resolve the block with the broker before deploying to this client",
            )
        self._set_account_id(account_id(found.number))
        self._publish(found)

    async def _disconnect(self) -> None:
        """Nothing to close: the connection is the workspace's and is shared."""

    async def _query_account(self, command: Any) -> None:
        """Re-read the account and republish it, which is what a query asks for."""
        row = await self._one(ACCOUNT_PATH)
        if row is not None:
            self._publish(account_row(row))

    def _publish(self, found: AccountRow) -> None:
        """Publish one account row as the engine's account state."""
        self.generate_account_state(
            balances=found.balances(),
            margins=found.margins(),
            reported=True,
            ts_event=self._clock.timestamp_ns(),
        )

    # --- reports --------------------------------------------------------------

    async def generate_order_status_report(self, command: Any) -> OrderStatusReport | None:
        """One order's status, found by whichever handle the command carries.

        The client order id is preferred, because it is the handle kanso supplied and the
        one that survives a restart; the venue order id is what a report the engine
        already holds names. An order the broker does not have, or one this adapter
        cannot map, is `None` rather than a failure — the sweep that asked must be able
        to carry on.
        """
        row = await self._lookup(command)
        return None if row is None else self._order_report(row)

    async def generate_order_status_reports(self, command: Any) -> list[OrderStatusReport]:
        """Every order the command's filters select, as status reports.

        Rows this adapter cannot map are passed over with their reason rather than
        failing the sweep: an account may hold orders kanso never submitted, in shapes it
        does not model, and a reconciliation that refused to finish because of one of them
        would leave the engine with no view of the account at all.
        """
        rows = await self._orders(command, open_only=bool(getattr(command, "open_only", False)))
        found = [self._order_report(row) for row in rows]
        return [report for report in found if report is not None]

    async def generate_fill_reports(self, command: Any) -> list[FillReport]:
        """The fills the broker's order rows report beyond what this process has applied.

        The broker reports a cumulative filled quantity and one average price rather than
        a list of executions, so a fill is the difference between the row and what the
        engine already holds for that order — looked up by the client order id the row
        carries, and by the venue order id when a replacement gave the order a new handle.
        A row with nothing new yields nothing, which is what makes reading the same
        account twice produce no second fill.
        """
        wanted = getattr(command, "venue_order_id", None)
        rows = await self._orders(command, open_only=False)
        found: list[FillReport] = []
        for row in rows:
            if wanted is not None and str(row.get("id")) != wanted.value:
                continue
            report = self._fill(row)
            if report is not None:
                found.append(report)
        return found

    async def generate_position_status_reports(self, command: Any) -> list[PositionStatusReport]:
        """Every position the broker holds, as position status reports.

        A position in an instrument this workspace does not hold is skipped and said so:
        its quantity has no declared precision here, and reporting it at an invented one
        would put a number in the engine's book that nobody served.
        """
        wanted = getattr(command, "instrument_id", None)
        symbol = None if wanted is None else symbol_of(wanted)
        rows = await self._rows(POSITIONS_PATH)
        found: list[PositionStatusReport] = []
        for row in rows:
            if symbol is not None and row.get("symbol") != symbol:
                continue
            instrument = self._instrument(row.get("symbol"))
            if instrument is None:
                self._log.warning(f"Skipping position {row.get('symbol')!r}: no instrument held")
                continue
            found.append(
                position_report(
                    row,
                    account=self._account(),
                    instrument=instrument.id,
                    size_precision=instrument.size_precision,
                    ts_init=self._clock.timestamp_ns(),
                )
            )
        return found

    # --- commands -------------------------------------------------------------

    async def _submit_order(self, command: Any) -> None:
        """Send one order, or deny it by name without sending anything.

        The denial comes before the submitted event, because an order that was never sent
        was never submitted. What comes back is checked to carry the client order id that
        went out: the broker returns it verbatim, and a row carrying a different one is
        an order this strategy could not recognise afterwards.
        """
        order = command.order
        reason = denial(order)
        if reason is not None:
            self._deny(order, reason)
            return
        payload = submission(order)
        self.generate_order_submitted(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            self._clock.timestamp_ns(),
        )
        response = await self._request("POST", ORDERS_PATH, body=payload)
        row = _decoded(response)
        if not _ok(response) or row is None:
            self._reject(order, _fault(ORDERS_PATH, response))
            return
        if row.get("client_order_id") != payload["client_order_id"]:
            self._reject(
                order,
                "the broker answered with a different client_order_id, which is the handle "
                "this order would be recognised by after a restart",
            )
            return
        self.generate_order_accepted(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            VenueOrderId(str(row.get("id"))),
            _accepted_at(row),
        )

    async def _submit_order_list(self, command: Any) -> None:
        """Deny every order of a list: the broker's legs are not mapped by this adapter."""
        for order in command.order_list.orders:
            self._deny(
                order,
                "order_list: the broker's bracket and one-cancels-other legs are not mapped "
                "by this adapter",
            )

    async def _modify_order(self, command: Any) -> None:
        """Replace one order with the amendment the command carries.

        The broker's replacement is a new order that supersedes the old one and carries
        its own identity, so the update is reported with the new venue order id and the
        engine is told the id moved. kanso's own client order id continues to name the
        order, and the trade id of any fill is derived from whatever handle the row
        actually carries, so the identity of a fill stays deterministic across the change.

        An order the venue has not named, or one the engine no longer holds, is refused
        without a request: the broker amends by its own order id, and a replacement whose
        original the engine cannot find is one nothing could be reported against.
        """
        order = self._cache.order(command.client_order_id)
        changes = _amendment(command)
        if command.venue_order_id is None or order is None:
            self._reject_modify(
                command,
                "the broker replaces an order by its own order id, and this one is not yet "
                "an order the venue and the engine both name",
            )
            return
        if not changes:
            self._reject_modify(command, "the command changes nothing the broker can amend")
            return
        path = f"{ORDERS_PATH}/{command.venue_order_id.value}"
        response = await self._request("PATCH", path, body=changes)
        row = _decoded(response)
        if not _ok(response) or row is None:
            self._reject_modify(command, _fault(path, response))
            return
        self.generate_order_updated(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            VenueOrderId(str(row.get("id"))),
            command.quantity if command.quantity is not None else order.quantity,
            command.price,
            command.trigger_price,
            _accepted_at(row),
            venue_order_id_modified=True,
        )

    async def _cancel_order(self, command: Any) -> None:
        """Ask the broker to cancel one order, then report what it says the order is.

        A cancellation is a request rather than an outcome, and this adapter reads no
        order stream, so the order is re-read and its own status reported. That is the
        truthful answer: an order that is still working after a cancel request is
        reported as still working, not as cancelled because the request was accepted.
        """
        if command.venue_order_id is None:
            self._reject_cancel(
                command,
                "the broker cancels by its own order id, and this order has not been given one yet",
            )
            return
        path = f"{ORDERS_PATH}/{command.venue_order_id.value}"
        response = await self._request("DELETE", path)
        if not _ok(response):
            self._reject_cancel(command, _fault(path, response))
            return
        row = await self._one(path)
        report = None if row is None else self._order_report(row)
        if report is not None:
            self._send_order_status_report(report)

    async def _cancel_all_orders(self, command: Any) -> None:
        """Cancel every order the command's filters select, one at a time.

        Order by order rather than through the broker's cancel-everything endpoint,
        because the command may name one instrument or one side and that endpoint takes
        neither: cancelling more than was asked for would close a position the caller
        meant to keep.
        """
        for order in self._open(command):
            await self._cancel_order(_Cancel(order))

    async def _batch_cancel_orders(self, command: Any) -> None:
        """Cancel each order of a batch, in the order the batch names them."""
        for cancel in command.cancels:
            await self._cancel_order(cancel)

    # --- events ---------------------------------------------------------------

    def _deny(self, order: Any, reason: str) -> None:
        """Refuse an order before anything is sent, naming what the broker cannot honour."""
        self.generate_order_denied(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _reject(self, order: Any, reason: str) -> None:
        """Report an order the broker itself refused."""
        self.generate_order_rejected(
            order.strategy_id,
            order.instrument_id,
            order.client_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _reject_modify(self, command: Any, reason: str) -> None:
        self.generate_order_modify_rejected(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    def _reject_cancel(self, command: Any, reason: str) -> None:
        self.generate_order_cancel_rejected(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id,
            reason,
            self._clock.timestamp_ns(),
        )

    # --- reading rows ---------------------------------------------------------

    def _order_report(self, row: Mapping[str, Any]) -> OrderStatusReport | None:
        """One order row as a status report, or `None` with a reason in the log."""
        reason = unsupported_reason(row)
        if reason is not None:
            self._log.debug(f"Skipping order {row.get('id')!r}: {reason}")
            return None
        instrument = self._instrument(row.get("symbol"))
        if instrument is None:
            self._log.warning(f"Skipping order {row.get('symbol')!r}: no instrument held")
            return None
        return order_status_report(
            row,
            account=self._account(),
            instrument=instrument.id,
            price_precision=instrument.price_precision,
            size_precision=instrument.size_precision,
            ts_init=self._clock.timestamp_ns(),
        )

    def _fill(self, row: Mapping[str, Any]) -> FillReport | None:
        """The fill one order row reports beyond what this process already applied.

        A row this adapter cannot map yields nothing and says nothing, because the status
        sweep that reads the same rows has already said why in the log; saying it twice
        per reconciliation would bury what it says.
        """
        if unsupported_reason(row) is not None:
            return None
        instrument = self._instrument(row.get("symbol"))
        if instrument is None:
            return None
        return fill_report(
            row,
            account=self._account(),
            instrument=instrument.id,
            price_precision=instrument.price_precision,
            size_precision=instrument.size_precision,
            ts_init=self._clock.timestamp_ns(),
            already_filled=self._already_filled(row),
        )

    def _already_filled(self, row: Mapping[str, Any]) -> Decimal:
        """What the engine has already recorded as filled for this order, or nothing.

        Found by the row's own client order id first, and by the venue order id when that
        fails — which is what a replacement leaves behind, the broker having given the
        order a handle of its own. An order the engine does not know at all has nothing
        recorded, and the whole cumulative fill is what is new about it.
        """
        found = row.get("client_order_id")
        order = None
        if isinstance(found, str) and found:
            order = self._cache.order(ClientOrderId(found))
        if order is None:
            known = self._cache.client_order_id(VenueOrderId(str(row.get("id"))))
            order = None if known is None else self._cache.order(known)
        return ZERO if order is None else Decimal(order.filled_qty.as_decimal())

    def _instrument(self, symbol: object) -> Instrument | None:
        """The instrument one broker symbol names, from the provider then the cache.

        An order row carries a symbol and no exchange, so the venue comes from the
        instrument kanso already holds rather than from the row: inventing one would
        re-key every card, order and manifest that instrument appears in.
        """
        if not isinstance(symbol, str) or not symbol:
            return None
        for instrument in (*self._instrument_provider.list_all(), *self._cache.instruments()):
            if instrument.id.symbol.value == symbol:
                return cast("Instrument", instrument)
        return None

    def _account(self) -> AccountId:
        """The engine account id, refusing to report anything before the account is known."""
        if self.account_id is None:
            raise PreconditionError(
                f"alpaca: {self.kanso_client_id!r} has not read its account yet, so nothing "
                "can be reported against it",
                remedy="connect the client before asking it for reports",
            )
        return cast("AccountId", self.account_id)

    def _open(self, command: Any) -> list[Any]:
        """The engine's own open orders that a cancel-everything command selects."""
        instrument_id = getattr(command, "instrument_id", None)
        side = getattr(command, "order_side", None)
        return [
            order
            for order in self._cache.orders_open(instrument_id=instrument_id)
            if side is None or int(side) == 0 or order.side == side
        ]

    # --- requests -------------------------------------------------------------

    async def _orders(self, command: Any, *, open_only: bool) -> list[Mapping[str, Any]]:
        """Every order row the command's window selects, walked page by page.

        The walk pages forward on the latest instant the page carries, stepped back by one
        nanosecond so that two orders sharing an instant cannot fall either side of a page
        boundary and be lost; the rows that repeat are dropped by their own id. A full
        page that yields nothing new is a walk that cannot advance, and it fails rather
        than silently returning a short history.
        """
        wanted = getattr(command, "instrument_id", None)
        params = {
            "status": "open" if open_only else "all",
            "direction": "asc",
            "limit": str(PAGE_LIMIT),
        }
        start = _moment(getattr(command, "start", None))
        end = _moment(getattr(command, "end", None))
        if start is not None:
            params["after"] = rfc3339(start)
        if end is not None:
            params["until"] = rfc3339(end)
        if wanted is not None:
            params["symbols"] = symbol_of(wanted)
        found: dict[str, Mapping[str, Any]] = {}
        for _ in range(MAX_PAGES):
            page = await self._rows(ORDERS_PATH, params)
            fresh = {str(row.get("id")): row for row in page if str(row.get("id")) not in found}
            found.update(fresh)
            if len(page) < PAGE_LIMIT:
                return _selected(found.values(), wanted)
            if not fresh:
                raise PreconditionError(
                    f"alpaca: {ORDERS_PATH} returned a full page holding no order the walk "
                    "had not already read, so the walk cannot advance",
                    remedy="narrow the reconciliation window",
                )
            params["after"] = rfc3339(_paged_from(page) - 1)
        raise PreconditionError(
            f"alpaca: {ORDERS_PATH} returned more than {MAX_PAGES} pages, which is more "
            "orders than an account kanso deploys to holds",
            remedy="narrow the reconciliation window",
        )

    async def _lookup(self, command: Any) -> Mapping[str, Any] | None:
        """The row one order-status command names, by whichever handle it carries."""
        found = getattr(command, "client_order_id", None)
        if found is not None:
            return await self._one(BY_CLIENT_ORDER_ID_PATH, {"client_order_id": found.value})
        venue_order_id = getattr(command, "venue_order_id", None)
        if venue_order_id is None:
            raise ValidationError(
                "order status: neither a client_order_id nor a venue_order_id was given, and "
                "an order cannot be looked up without one"
            )
        return await self._one(f"{ORDERS_PATH}/{venue_order_id.value}")

    async def _one(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Mapping[str, Any] | None:
        """One row, or `None` for a row the broker does not have."""
        response = await self._request("GET", path, params=params)
        if response.status == 404:
            return None
        _check(path, response, self.kanso_client_id)
        return _decoded(response)

    async def _rows(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> list[Mapping[str, Any]]:
        """One page of rows, refusing an answer that is not a list of them."""
        response = await self._request("GET", path, params=params)
        _check(path, response, self.kanso_client_id)
        try:
            parsed = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not isinstance(parsed, list):
            raise ValidationError(f"alpaca: {path} did not answer with a list of rows")
        return [row for row in parsed if isinstance(row, Mapping)]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Response:
        """One request through the shared connection, off the event loop and one at a time.

        Off the loop because the shared transport is synchronous and a live node's loop
        must not block on a socket; one at a time because the quota lives in the one
        connection every part of this adapter shares, and a burst of concurrent requests
        would be a burst against a single account's published limit.
        """
        payload = None if body is None else json.dumps(body, sort_keys=True).encode()
        async with self._gate:
            return await asyncio.to_thread(
                self._send,
                method,
                endpoint(self.host, path),
                dict(params or {}),
                self._credentials.headers(),
                _keys(path),
                payload,
            )


@dataclass(frozen=True, slots=True)
class _Cancel:
    """One order of a cancel-everything command, in the shape a single cancel reads."""

    order: Any

    @property
    def strategy_id(self) -> Any:
        return self.order.strategy_id

    @property
    def instrument_id(self) -> Any:
        return self.order.instrument_id

    @property
    def client_order_id(self) -> Any:
        return self.order.client_order_id

    @property
    def venue_order_id(self) -> Any:
        return self.order.venue_order_id


def _selected(
    rows: Iterable[Mapping[str, Any]], instrument_id: InstrumentId | None
) -> list[Mapping[str, Any]]:
    """The rows of one instrument, in the order the broker took them.

    The filter is asked for on the wire and checked again here, because a filter the
    broker did not honour would put another instrument's orders into a reconciliation of
    this one, and the check costs a comparison. The ordering is the broker's own
    timestamps rather than the order the pages arrived in, so two runs over one account
    report the same fills in the same sequence.
    """
    found = sorted(rows, key=_sequence)
    if instrument_id is None:
        return found
    symbol = symbol_of(instrument_id)
    return [row for row in found if row.get("symbol") == symbol]


def _amendment(command: Any) -> dict[str, Any]:
    """The fields a modify command actually changes, in the broker's spelling.

    Only what was asked for: an amendment that restated a price the caller left alone
    would replace the order for no reason, and the broker's replacement is a new order.
    """
    changes: dict[str, Any] = {}
    if command.quantity is not None:
        changes["qty"] = _plain(command.quantity.as_decimal())
    if command.price is not None:
        changes["limit_price"] = _plain(command.price.as_decimal())
    if command.trigger_price is not None:
        changes["stop_price"] = _plain(command.trigger_price.as_decimal())
    return changes


STAMPS: Final[tuple[str, ...]] = ("submitted_at", "created_at", "updated_at")
"""Where an order row says when the broker took it, in the order they are read. Every
timestamp comes from the row and none from this process's clock, so two runs reading the
same account agree to the nanosecond and a page boundary lands in the same place twice."""


def _stamped(row: Mapping[str, Any]) -> int | None:
    """When the broker took this order, or `None` for a row that does not say."""
    for name in STAMPS:
        value = row.get(name)
        if value is not None:
            return instant(value, f"order.{name}")
    return None


def _accepted_at(row: Mapping[str, Any]) -> int:
    """When the broker took the order, refusing a row that does not say when."""
    found = _stamped(row)
    if found is None:
        raise ValidationError(
            f"order {row.get('id')!r}: carries no timestamp, so when the broker took it is unknown"
        )
    return found


def _sequence(row: Mapping[str, Any]) -> tuple[int, str]:
    """What orders a page of rows: when the broker took each, then its own id."""
    return _stamped(row) or 0, str(row.get("id"))


def _moment(value: object) -> int | None:
    """A window boundary as UTC nanoseconds, from whatever the engine handed over.

    The engine's own timestamp type carries a nanosecond count in `value`, and that is
    read first because it is the whole of the instant; a plain datetime carries
    microseconds and is read as those. A boundary of any other shape is refused rather
    than dropped: a window silently read as no window would reconcile the whole history
    of an account and report it as the window that was asked for.
    """
    if value is None:
        return None
    found = getattr(value, "value", None)
    if isinstance(found, int):
        return found
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return int(moment.timestamp()) * NS_PER_SECOND + moment.microsecond * 1_000
    raise ValidationError(f"window: {value!r} is not an instant a boundary can be read from")


def _paged_from(page: Sequence[Mapping[str, Any]]) -> int:
    """Where the next page of a walk starts: the latest instant the page carries.

    The latest rather than the last row's, because the walk asks for ascending order and
    a broker that answered in another one would otherwise send the walk backwards.
    """
    stamps = [found for row in page if (found := _stamped(row)) is not None]
    if not stamps:
        raise ValidationError(
            f"alpaca: {ORDERS_PATH} returned a full page of orders none of which says when "
            "it was taken, so the walk has nowhere to continue from"
        )
    return max(stamps)


def _keys(path: str) -> list[str]:
    """The rate-limit buckets one request counts against, coarsest last."""
    parts = [part for part in path.split("/") if part][:2]
    return ["/".join(parts), parts[0]] if len(parts) > 1 else parts


def _ok(response: Response) -> bool:
    """Whether the broker answered rather than refused."""
    return 200 <= response.status < 300


def _decoded(response: Response) -> Mapping[str, Any] | None:
    """The body as one JSON object, or `None` when it is not one."""
    try:
        parsed = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _fault(path: str, response: Response) -> str:
    """A short account of a refused request, carrying nothing that was sent.

    The broker's own code and message are quoted because they say what it objected to;
    the request is not, because the request carries the credential headers and a
    credential in a rejection reason is a credential in the engine's event log.
    """
    body = _decoded(response) or {}
    said = body.get("message")
    code = body.get("code")
    detail = f": {str(said)[:200]}" if isinstance(said, str) and said else ""
    numbered = f" (code {code})" if isinstance(code, int) else ""
    return f"{path} answered HTTP {response.status}{numbered}{detail}"


def _check(path: str, response: Response, client_id: str) -> None:
    """Fail a request the broker refused, naming the variable rather than the value."""
    if _ok(response):
        return
    if response.status in (401, 403):
        raise PreconditionError(
            f"{key_name(client_id)}: the broker refused this key (HTTP {response.status})",
            remedy=f"check that {key_name(client_id)} holds the key of {client_id!r}",
        )
    raise PreconditionError(f"alpaca: {_fault(path, response)}")
