"""Every pure mapping between the broker's JSON and the engine's types.

Nothing here opens a socket, holds a credential or keeps state, which is the point: the
execution client, the market data client and the tradability overlay all read their wire
through this module, so the whole of what kanso believes about the broker's shapes is
testable from frozen rows with no network and no key.

**What was measured, and what was not.** The key sets of the clock, the asset row, the
order row and the position row were read from the live account and are relied on as
facts; the daily bar row was read for both tapes. The quote and trade rows, the account
row and the exchange spellings beyond `NASDAQ` are the broker's published schema and were
not measured, so every one of them is read field by field: a row missing something a
point needs yields nothing at all rather than a point with an invented number in it. A
missing point is visible — the count and the span are taken from what was kept — and a
fabricated one is not.

**A bar is stamped at its close, and no zone is consulted.** The row's `t` is the instant
the aggregation window opened, and this build's rule — settled once, for every source —
is that a bar's `ts_event` is the window start plus one resolution step, so a bar lands
at the instant its period ended, which is also the first instant it could be known. The
broker anchors a daily window at the same instant the vendor already in this build does,
so an equity's daily bar from either source stamps identically and the two are comparable
without a calendar.

**Three facts about an order row shape everything downstream.** The row carries both
`order_type` and `type`, which are the same value under two names. It reports a
*cumulative* fill — a filled quantity and an average price, never a list of executions —
so a fill built from it is the whole fill to date, and what makes a restart safe is that
the report's identity is derived from the order and that cumulative quantity: read the
same row twice and the same trade id comes back, and the engine skips a trade id an order
already carries. And `client_order_id` is the idempotency handle: it is supplied by the
caller, returned verbatim, and is the only field that survives a restart on both sides,
which is why it is what a deterministic trade id is built on.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`OrderStatusReport`, `FillReport` and `PositionStatusReport` are the reconciliation
reports a `LiveExecutionClient` generates; each takes a `report_id: UUID4` and nanosecond
`ts_*` integers. `TradeId` accepts between 1 and 36 characters, which a client order id of
up to 128 does not fit, so a derived id is hashed rather than concatenated. A `FillReport`
whose `trade_id` an order already carries is skipped by reconciliation, which is what
makes a deterministic id worth having. `Bar` validates its own OHLC ordering and raises
`ValueError` on a high below the low; `QuoteTick` requires its two prices to share a
precision and its two sizes to share a precision; `TradeTick` requires a strictly positive
size and a non-empty trade id. `Price` and `Quantity` are fixed point, so a decimal from
the wire is quantised to the instrument's declared precision rather than to a float's.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import FillReport, OrderStatusReport, PositionStatusReport
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggressorSide,
    LiquiditySide,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
    TriggerType,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

from kanso.data.loaders.points import bar_type, instrument_id
from kanso.errors import ValidationError
from kanso.nautilus.adapters.alpaca.venue import ASSET_CLASS, CURRENCY, venue_of
from kanso.schemas import parse_duration

__all__ = [
    "BAR_FIELDS",
    "CLIENT_ORDER_ID_MAX",
    "ISSUER",
    "ORDER_SIDES",
    "ORDER_STATUSES",
    "ORDER_TYPES",
    "POSITION_SIDES",
    "QUOTE_FIELDS",
    "SIMPLE_ORDER_CLASSES",
    "SUPPORTED_ORDER_TYPES",
    "SUPPORTED_TIME_IN_FORCE",
    "TIME_IN_FORCE",
    "TRADE_FIELDS",
    "TRADE_ID_DIGITS",
    "Asset",
    "MarketClock",
    "account_id",
    "asset",
    "bar",
    "client_order_id",
    "decimal_of",
    "fill_report",
    "instant",
    "instrument_id_of",
    "market_clock",
    "maybe_instant",
    "order_status_report",
    "position_report",
    "price_of",
    "quantity_of",
    "quote",
    "rfc3339",
    "symbol_of",
    "to_broker_order_type",
    "to_broker_side",
    "to_broker_time_in_force",
    "trade",
    "trade_id",
    "unsupported_reason",
]

NS_PER_SECOND: Final = 1_000_000_000
NANOS: Final = 9
"""Nanosecond resolution: every engine timestamp is an integer count of them."""

TIMESTAMP: Final = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(?:(?P<zulu>[Zz])|(?P<sign>[+-])(?P<oh>\d{2}):?(?P<om>\d{2}))$"
)
"""The one timestamp grammar this broker writes: RFC 3339, with either `Z` or a numeric
offset, and up to nine fractional digits. Both were measured — the clock answers with a
`-04:00` offset and a bar with `Z` — and the fraction is parsed rather than truncated,
because two prints in the same microsecond are ordered by the digits below it."""

ISSUER: Final = "ALPACA"
"""The issuer half of an engine account id. One issuer for both environments: the account
numbers differ, and which environment an account belongs to is the client's own
declaration rather than something to encode twice."""

CLIENT_ORDER_ID_MAX: Final = 128
"""How long a client order id the broker accepts. The engine's own ids are far shorter;
the ceiling is checked so an order is refused here rather than at the broker."""

TRADE_ID_DIGITS: Final = 32
"""How much of the digest a derived trade id carries. The engine's `TradeId` accepts at
most 36 characters and a client order id may be 128, so the pair is hashed."""

ZERO: Final = Decimal(0)

BAR_FIELDS: Final[tuple[str, ...]] = ("t", "o", "h", "l", "c", "v")
"""What a bar row must carry. Measured on both tapes, together with `n` and `vw`, which a
bar in this build does not use."""

QUOTE_FIELDS: Final[tuple[str, ...]] = ("t", "bp", "bs", "ap", "as")
TRADE_FIELDS: Final[tuple[str, ...]] = ("t", "p", "s", "i")
"""What a quote and a trade row must carry, in the broker's published spelling — the same
short keys on the request path and on the stream. Not measured in the read-only pass, so
every one of them is required and a row missing any yields no point."""

SIMPLE_ORDER_CLASSES: Final[frozenset[str]] = frozenset({"", "simple"})
"""The order classes this adapter maps. A bracket, an OCO or an OTO row carries legs whose
contingencies the engine models differently, and mapping one without having measured it
would report a relationship between orders that may not be the one the broker holds."""

ORDER_SIDES: Final[dict[str, OrderSide]] = {
    "buy": OrderSide.BUY,
    "sell": OrderSide.SELL,
}

BROKER_SIDES: Final[dict[OrderSide, str]] = {
    OrderSide.BUY: "buy",
    OrderSide.SELL: "sell",
}

ORDER_TYPES: Final[dict[str, OrderType]] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop": OrderType.STOP_MARKET,
    "stop_limit": OrderType.STOP_LIMIT,
    "trailing_stop": OrderType.TRAILING_STOP_MARKET,
}

BROKER_ORDER_TYPES: Final[dict[OrderType, str]] = {
    engine: broker for broker, engine in ORDER_TYPES.items()
}
SUPPORTED_ORDER_TYPES: Final[frozenset[OrderType]] = frozenset(BROKER_ORDER_TYPES)
"""The order types the broker accepts. Everything else the engine can express — a
market-to-limit, an if-touched, a trailing stop limit — has no spelling here, and an
attempt to send one is refused by name rather than translated into something else."""

TIME_IN_FORCE: Final[dict[str, TimeInForce]] = {
    "day": TimeInForce.DAY,
    "gtc": TimeInForce.GTC,
    "opg": TimeInForce.AT_THE_OPEN,
    "cls": TimeInForce.AT_THE_CLOSE,
    "ioc": TimeInForce.IOC,
    "fok": TimeInForce.FOK,
}

BROKER_TIME_IN_FORCE: Final[dict[TimeInForce, str]] = {
    engine: broker for broker, engine in TIME_IN_FORCE.items()
}
SUPPORTED_TIME_IN_FORCE: Final[frozenset[TimeInForce]] = frozenset(BROKER_TIME_IN_FORCE)
"""Every time in force the broker offers. `GTD` is the one the engine has and the broker
does not, so an order carrying it is refused rather than silently made good-till-cancel."""

ORDER_STATUSES: Final[dict[str, OrderStatus]] = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "accepted_for_bidding": OrderStatus.ACCEPTED,
    "calculated": OrderStatus.ACCEPTED,
    "done_for_day": OrderStatus.ACCEPTED,
    "held": OrderStatus.ACCEPTED,
    "stopped": OrderStatus.ACCEPTED,
    "suspended": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.SUBMITTED,
    "pending_review": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.PENDING_CANCEL,
    "pending_replace": OrderStatus.PENDING_UPDATE,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}
"""The broker's order states, as the engine's.

The engine has no state for an order that is resting but not workable, so the several ways
the broker says exactly that — done for the day, held, stopped, suspended, calculated —
all map to `ACCEPTED`, which is what they have in common: the order is with the broker and
has not ended. `replaced` maps to `CANCELED` because the engine models a replacement as
one order living on rather than as two, and the row that was replaced has ended. A state
outside this table is refused by name: guessing at an unknown state is how an order that
is still working gets reported as finished."""

POSITION_SIDES: Final[dict[str, PositionSide]] = {
    "long": PositionSide.LONG,
    "short": PositionSide.SHORT,
}


# --- timestamps ---------------------------------------------------------------


def instant(value: object, field: str = "timestamp") -> int:
    """One RFC 3339 timestamp as UTC nanoseconds, exactly, or a validation failure.

    Parsed rather than handed to a general date library so the fractional digits below a
    microsecond survive: the broker writes up to nine of them, a datetime holds six, and
    the ones that would be dropped are what orders two prints inside the same microsecond.
    """
    if not isinstance(value, str):
        raise ValidationError(
            f"{field}: {value!r} is not a timestamp; the broker writes RFC 3339 text"
        )
    found = TIMESTAMP.match(value.strip())
    if found is None:
        raise ValidationError(
            f"{field}: {value!r} is not an RFC 3339 timestamp, such as "
            "2026-08-03T04:00:00Z or 2026-09-08T09:30:00-04:00"
        )
    year, month, day, hour, minute, second = (int(part) for part in found.groups()[:6])
    fraction = found.group(7) or ""
    try:
        moment = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        raise ValidationError(f"{field}: {value!r} names no instant: {exc}") from None
    offset = 0
    if found.group("zulu") is None:
        sign = -1 if found.group("sign") == "-" else 1
        offset = sign * (int(found.group("oh")) * 3600 + int(found.group("om")) * 60)
    return (int(moment.timestamp()) - offset) * NS_PER_SECOND + int(fraction.ljust(NANOS, "0"))


def maybe_instant(value: object, field: str = "timestamp") -> int | None:
    """`instant`, but `None` for a field the broker leaves null.

    Half the timestamps on an order row are null until something happens — a fill, a
    cancellation, an expiry — so their absence is information rather than a fault.
    """
    return None if value is None else instant(value, field)


def rfc3339(ts_ns: int) -> str:
    """UTC nanoseconds as the timestamp text a request parameter carries.

    Nine fractional digits whenever there are any, so a window boundary asked for is the
    boundary meant rather than the microsecond it rounded to.
    """
    whole, fraction = divmod(ts_ns, NS_PER_SECOND)
    moment = datetime.fromtimestamp(whole, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{moment}Z" if fraction == 0 else f"{moment}.{fraction:09d}Z"


# --- numbers ------------------------------------------------------------------


def decimal_of(value: object, field: str = "value") -> Decimal:
    """One wire number as an exact decimal, whether it arrived as text or as a number.

    The broker writes money and quantities as strings and bar prices as JSON numbers, and
    both are read through the decimal the text spells rather than through the binary float
    nearest it, so a price that is exact on the wire is exact in a report.
    """
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValidationError(f"{field}: {value!r} is not a number")
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        raise ValidationError(f"{field}: {value!r} is not a number") from None


def maybe_decimal(value: object, field: str = "value") -> Decimal | None:
    """`decimal_of`, but `None` for a field the broker leaves null."""
    return None if value is None else decimal_of(value, field)


def price_of(value: object, precision: int, field: str = "price") -> Price:
    """A wire number as a price at the instrument's own precision.

    A number the engine's fixed point cannot hold — an absurd magnitude, a precision no
    instrument has — fails naming the field that carried it, rather than escaping as a
    decimal or engine exception in the middle of a session.
    """
    found = decimal_of(value, field)
    try:
        return Price.from_str(_quantised(found, precision))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(
            f"{field}: {found} is not a price at {precision} decimals: {exc}"
        ) from None


def quantity_of(value: object, precision: int, field: str = "quantity") -> Quantity:
    """A wire number as a quantity at the instrument's own precision.

    A negative one is refused rather than made positive. The one place a sign is expected
    is a short position, whose direction the engine keeps in the side, and that caller
    takes the magnitude deliberately; everywhere else a negative size is a broken row, and
    quietly reading it as its magnitude would put a number in a point that nobody served.
    """
    found = decimal_of(value, field)
    if found < ZERO:
        raise ValidationError(f"{field}: {value!r} is a negative size, which is not a quantity")
    try:
        return Quantity.from_str(_quantised(found, precision))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(
            f"{field}: {found} is not a size at {precision} decimals: {exc}"
        ) from None


def _quantised(value: Decimal, precision: int) -> str:
    """A decimal rendered at `precision`, half away from zero, spelled exactly."""
    step = Decimal(1).scaleb(-precision)
    return f"{value.quantize(step, rounding=ROUND_HALF_UP):.{precision}f}"


def _canonical(value: Decimal) -> str:
    """A decimal's one spelling, so `10`, `10.0` and `1E+1` hash to the same trade id."""
    return format(value.normalize(), "f")


# --- identity -----------------------------------------------------------------


def account_id(number: str) -> AccountId:
    """The engine account id of one broker account number."""
    return AccountId(f"{ISSUER}-{number}")


def symbol_of(instrument: InstrumentId) -> str:
    """The broker's symbol for an instrument kanso holds: the symbol, without the venue."""
    return str(instrument.symbol.value)


def instrument_id_of(symbol: str, exchange: str) -> InstrumentId | None:
    """`SYMBOL.VENUE` from a broker row's symbol and exchange, or `None`.

    `None` when the exchange is one this adapter does not map, which is one instrument's
    failure rather than the universe's: an invented venue would re-key every card, order
    and manifest that instrument appears in.
    """
    venue = venue_of(exchange)
    return None if venue is None else instrument_id(symbol, venue)


def client_order_id(order_id: ClientOrderId | str) -> str:
    """The engine's client order id as the broker's `client_order_id`, verbatim.

    Verbatim is the whole point: it is supplied by the caller, returned unchanged, and is
    the only handle that survives a restart on both sides, so it is what an order is
    recognised by after a process comes back and what a deterministic trade id is built on.
    """
    text = str(order_id)
    if not text or len(text) > CLIENT_ORDER_ID_MAX:
        raise ValidationError(
            f"client_order_id: {text!r} is {len(text)} characters, and the broker accepts "
            f"between 1 and {CLIENT_ORDER_ID_MAX}"
        )
    return text


def trade_id(order_ref: str, filled_qty: Decimal) -> TradeId:
    """The deterministic identity of one fill report: the order, and its fill to date.

    The broker's order row reports a cumulative filled quantity rather than a list of
    executions, so what identifies a fill is the order plus the quantity the order stood
    filled at when it was read. Reading the same row again — after a restart, after a
    reconnection, after a reconciliation sweep — produces the same id, and the engine
    skips a trade id the order already carries, which is what makes a restart produce zero
    duplicate fills. The pair is hashed because the engine's `TradeId` holds 36 characters
    and a client order id may hold 128.
    """
    digest = hashlib.sha256(f"{order_ref}|{_canonical(filled_qty)}".encode()).hexdigest()
    return TradeId(digest[:TRADE_ID_DIGITS])


# --- the clock ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketClock:
    """The venue clock a wall-clock stage runs against.

    Every instant is UTC nanoseconds, so nothing downstream consults a calendar or a zone
    to compare one with a data point's own timestamp.
    """

    is_open: bool
    timestamp: int
    next_open: int
    next_close: int


def market_clock(row: Mapping[str, Any]) -> MarketClock:
    """The clock endpoint's row, whose four keys were measured in full."""
    state = row.get("is_open")
    if not isinstance(state, bool):
        raise ValidationError(f"clock.is_open: {state!r} is not a boolean")
    return MarketClock(
        is_open=state,
        timestamp=instant(row.get("timestamp"), "clock.timestamp"),
        next_open=instant(row.get("next_open"), "clock.next_open"),
        next_close=instant(row.get("next_close"), "clock.next_close"),
    )


# --- assets -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Asset:
    """One tradability row, as the overlay reads it.

    The three increment fields are `None` for an equity — measured, in full, on a live
    row: they are crypto fields and an equity row does not carry them — so an overlay that
    required them would refuse every equity there is. `None` here means the broker said
    nothing, and the instrument's own conventions stand.
    """

    symbol: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    marginable: bool
    shortable: bool
    easy_to_borrow: bool
    fractionable: bool
    min_order_size: Decimal | None = None
    min_trade_increment: Decimal | None = None
    price_increment: Decimal | None = None

    @property
    def active(self) -> bool:
        """Whether the broker will accept an order for this instrument at all."""
        return self.status == "active" and self.tradable

    @property
    def equity(self) -> bool:
        """Whether this row is the one asset class kanso trades through this broker."""
        return self.asset_class == ASSET_CLASS


def asset(row: Mapping[str, Any]) -> Asset:
    """One asset row, whose key set was measured on a live equity.

    Every flag is required and none is defaulted: a missing `shortable` read as `False`
    would quietly forbid every short, and read as `True` would quietly submit one the
    broker refuses.
    """
    return Asset(
        symbol=_text(row, "symbol"),
        asset_class=_text(row, "class"),
        exchange=_text(row, "exchange"),
        status=_text(row, "status"),
        tradable=_flag(row, "tradable"),
        marginable=_flag(row, "marginable"),
        shortable=_flag(row, "shortable"),
        easy_to_borrow=_flag(row, "easy_to_borrow"),
        fractionable=_flag(row, "fractionable"),
        min_order_size=maybe_decimal(row.get("min_order_size"), "asset.min_order_size"),
        min_trade_increment=maybe_decimal(
            row.get("min_trade_increment"), "asset.min_trade_increment"
        ),
        price_increment=maybe_decimal(row.get("price_increment"), "asset.price_increment"),
    )


def _text(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"asset.{name}: {value!r} is not a value the broker writes here")
    return value


def _flag(row: Mapping[str, Any], name: str) -> bool:
    value = row.get(name)
    if not isinstance(value, bool):
        raise ValidationError(f"asset.{name}: {value!r} is not a boolean")
    return value


# --- market data --------------------------------------------------------------


def bar(
    row: Mapping[str, Any],
    *,
    instrument: InstrumentId,
    resolution: str,
    price_precision: int,
    size_precision: int,
) -> Bar | None:
    """One aggregate row as a bar timestamped at the close of its own window.

    `t` is the instant the window opened, so one resolution step is added: a bar is known
    at its close and at no earlier instant, and `ts_init` equals that close because a bar
    served as complete is public as soon as it is complete. A row missing any field a bar
    needs yields `None` rather than a bar with a number nobody served.
    """
    if any(row.get(name) is None for name in BAR_FIELDS):
        return None
    opened = instant(row.get("t"), "bar.t")
    step = int(parse_duration(resolution, "resolution").total_seconds()) * NS_PER_SECOND
    ts_event = opened + step
    try:
        return Bar(
            bar_type(instrument, resolution),
            price_of(row.get("o"), price_precision, "bar.o"),
            price_of(row.get("h"), price_precision, "bar.h"),
            price_of(row.get("l"), price_precision, "bar.l"),
            price_of(row.get("c"), price_precision, "bar.c"),
            quantity_of(row.get("v"), size_precision, "bar.v"),
            ts_event,
            ts_event,
        )
    except ValueError as exc:
        raise ValidationError(
            f"bar: the row the broker served for {instrument} at {rfc3339(opened)} is not a "
            f"bar: {exc}"
        ) from None


def quote(
    row: Mapping[str, Any],
    *,
    instrument: InstrumentId,
    price_precision: int,
    size_precision: int,
) -> QuoteTick | None:
    """One quote row as a two-sided quote, or `None` when the row is not one.

    Both sides are built at one precision, which is the only thing the engine refuses a
    quote for, so a number that cannot be held fails where it is read rather than here.
    """
    if any(row.get(name) is None for name in QUOTE_FIELDS):
        return None
    ts_event = instant(row.get("t"), "quote.t")
    return QuoteTick(
        instrument,
        price_of(row.get("bp"), price_precision, "quote.bp"),
        price_of(row.get("ap"), price_precision, "quote.ap"),
        quantity_of(row.get("bs"), size_precision, "quote.bs"),
        quantity_of(row.get("as"), size_precision, "quote.as"),
        ts_event,
        ts_event,
    )


def trade(
    row: Mapping[str, Any],
    *,
    instrument: InstrumentId,
    price_precision: int,
    size_precision: int,
) -> TradeTick | None:
    """One trade row as a print, or `None` when the row is not one.

    The aggressor is unknown rather than assumed: the broker's print says which venue and
    which conditions, and never which side crossed, so a side is not invented from the
    price. `i` is the venue's own print id and is what the trade is identified by.
    """
    if any(row.get(name) is None for name in TRADE_FIELDS):
        return None
    ts_event = instant(row.get("t"), "trade.t")
    try:
        return TradeTick(
            instrument,
            price_of(row.get("p"), price_precision, "trade.p"),
            quantity_of(row.get("s"), size_precision, "trade.s"),
            AggressorSide.NO_AGGRESSOR,
            TradeId(str(row.get("i"))),
            ts_event,
            ts_event,
        )
    except ValueError as exc:
        raise ValidationError(
            f"trade: the row the broker served for {instrument} at {rfc3339(ts_event)} is not "
            f"a trade: {exc}"
        ) from None


# --- orders -------------------------------------------------------------------


def to_broker_side(side: OrderSide) -> str:
    """The broker's spelling of an order side, or a refusal naming what it accepts."""
    found = BROKER_SIDES.get(side)
    if found is None:
        raise ValidationError(
            f"side: {side!r} is not a side the broker accepts; it accepts "
            f"{', '.join(sorted(BROKER_SIDES.values()))}"
        )
    return found


def to_broker_order_type(order_type: OrderType) -> str:
    """The broker's spelling of an order type, refusing one it does not offer.

    Refusing by name is the whole of the deny table: an order type the broker has no
    equivalent for is rejected before it is sent, because the alternative — sending the
    nearest thing — would fill a strategy at a price it never asked for.
    """
    found = BROKER_ORDER_TYPES.get(order_type)
    if found is None:
        raise ValidationError(
            f"order_type: the broker does not offer {order_type.name}; it offers "
            f"{', '.join(sorted(BROKER_ORDER_TYPES.values()))}"
        )
    return found


def to_broker_time_in_force(time_in_force: TimeInForce) -> str:
    """The broker's spelling of a time in force, refusing one it does not offer."""
    found = BROKER_TIME_IN_FORCE.get(time_in_force)
    if found is None:
        raise ValidationError(
            f"time_in_force: the broker does not offer {time_in_force.name}; it offers "
            f"{', '.join(sorted(BROKER_TIME_IN_FORCE.values()))}"
        )
    return found


def unsupported_reason(row: Mapping[str, Any]) -> str | None:
    """Why this order row cannot be reported faithfully, or `None` when it can.

    A separate question from reporting it, because the two callers differ: reconciliation
    walks every order in an account, including ones kanso never submitted, and must be
    able to pass over what it cannot map without failing the sweep. Every reason names the
    field that produced it.
    """
    order_class = row.get("order_class")
    if isinstance(order_class, str) and order_class.lower() not in SIMPLE_ORDER_CLASSES:
        return f"order_class {order_class!r} carries legs this adapter does not map"
    if row.get("asset_class") not in (None, ASSET_CLASS):
        return f"asset_class {row.get('asset_class')!r} is not an asset class kanso trades here"
    if row.get("qty") is None:
        return "qty is absent, which is a notional order and kanso sizes in whole shares"
    status = row.get("status")
    if not isinstance(status, str) or status.lower() not in ORDER_STATUSES:
        return f"status {status!r} is not one this adapter maps"
    side = row.get("side")
    if not isinstance(side, str) or side.lower() not in ORDER_SIDES:
        return f"side {side!r} is not one this adapter maps"
    kind = row.get("type") or row.get("order_type")
    if not isinstance(kind, str) or kind.lower() not in ORDER_TYPES:
        return f"type {kind!r} is not one this adapter maps"
    tif = row.get("time_in_force")
    if not isinstance(tif, str) or tif.lower() not in TIME_IN_FORCE:
        return f"time_in_force {tif!r} is not one this adapter maps"
    return None


def order_status_report(
    row: Mapping[str, Any],
    *,
    account: AccountId,
    instrument: InstrumentId,
    price_precision: int,
    size_precision: int,
    ts_init: int,
    report_id: UUID4 | None = None,
) -> OrderStatusReport:
    """One order row as the engine's order status report.

    The row carries both `order_type` and `type`, which hold the same value; `type` is
    read and `order_type` is the fallback, so a row from either shape reports the same
    order. `ts_accepted` is when the broker took the order and `ts_last` when it last
    changed, both from the row rather than from this process's clock, so two runs
    reconciling the same account agree to the nanosecond.
    """
    reason = unsupported_reason(row)
    if reason is not None:
        raise ValidationError(f"order {row.get('id')!r}: {reason}")
    filled = decimal_of(row.get("filled_qty", "0"), "order.filled_qty")
    trigger = _maybe_price(row.get("stop_price"), price_precision, "order.stop_price")
    created = instant(row.get("created_at"), "order.created_at")
    accepted = maybe_instant(row.get("submitted_at"), "order.submitted_at") or created
    last = maybe_instant(row.get("updated_at"), "order.updated_at") or accepted
    return OrderStatusReport(
        account_id=account,
        instrument_id=instrument,
        client_order_id=_client_order_id(row),
        venue_order_id=VenueOrderId(str(row.get("id"))),
        order_side=ORDER_SIDES[str(row["side"]).lower()],
        order_type=ORDER_TYPES[str(row.get("type") or row.get("order_type")).lower()],
        time_in_force=TIME_IN_FORCE[str(row["time_in_force"]).lower()],
        order_status=ORDER_STATUSES[str(row["status"]).lower()],
        quantity=quantity_of(row.get("qty"), size_precision, "order.qty"),
        filled_qty=Quantity.from_str(_quantised(filled, size_precision)),
        avg_px=maybe_decimal(row.get("filled_avg_price"), "order.filled_avg_price"),
        price=_maybe_price(row.get("limit_price"), price_precision, "order.limit_price"),
        trigger_price=trigger,
        trigger_type=TriggerType.NO_TRIGGER if trigger is None else TriggerType.LAST_PRICE,
        trailing_offset=maybe_decimal(row.get("trail_percent"), "order.trail_percent"),
        report_id=report_id or UUID4(),
        ts_accepted=accepted,
        ts_last=last,
        ts_init=ts_init,
    )


def fill_report(
    row: Mapping[str, Any],
    *,
    account: AccountId,
    instrument: InstrumentId,
    price_precision: int,
    size_precision: int,
    ts_init: int,
    already_filled: Decimal = ZERO,
    report_id: UUID4 | None = None,
) -> FillReport | None:
    """The fill an order row reports beyond what has already been applied, or `None`.

    The broker reports a cumulative filled quantity and one average price, never a list of
    executions, so a report is the difference between what the row says and what the
    engine has already recorded. `already_filled` is that recorded quantity: pass what the
    order carries and a re-read of the same row produces nothing at all, which is the
    first half of a restart with no duplicate fills. The second half is the trade id,
    which is derived from the client order id and the cumulative quantity, so even a
    report the caller cannot suppress is one the engine recognises and skips.

    The price is the row's running average rather than the last execution's price, because
    the row does not carry the last execution's price; over a single-fill order — which is
    what a market order on a liquid US equity is — the two are the same number. The
    commission is zero and stated rather than omitted: this broker charges none on US
    equities, and kanso applies its own cost model once, in the runner's extraction.
    """
    filled = decimal_of(row.get("filled_qty", "0"), "order.filled_qty")
    fresh = filled - already_filled
    if fresh <= ZERO:
        return None
    average = row.get("filled_avg_price")
    if average is None:
        raise ValidationError(
            f"order {row.get('id')!r}: reports {filled} filled and no average price, so the "
            "fill cannot be priced"
        )
    ts_event = maybe_instant(row.get("filled_at"), "order.filled_at") or instant(
        row.get("updated_at"), "order.updated_at"
    )
    reference = _client_order_id(row)
    return FillReport(
        account_id=account,
        instrument_id=instrument,
        client_order_id=reference,
        venue_order_id=VenueOrderId(str(row.get("id"))),
        trade_id=trade_id(str(reference), filled),
        order_side=ORDER_SIDES[str(row["side"]).lower()],
        last_qty=Quantity.from_str(_quantised(fresh, size_precision)),
        last_px=price_of(average, price_precision, "order.filled_avg_price"),
        commission=Money(0, Currency.from_str(CURRENCY)),
        liquidity_side=LiquiditySide.NO_LIQUIDITY_SIDE,
        report_id=report_id or UUID4(),
        ts_event=ts_event,
        ts_init=ts_init,
    )


def position_report(
    row: Mapping[str, Any],
    *,
    account: AccountId,
    instrument: InstrumentId,
    size_precision: int,
    ts_init: int,
    report_id: UUID4 | None = None,
) -> PositionStatusReport:
    """One position row as the engine's position status report.

    `qty` is the position and `qty_available` is what is not held against a resting order;
    the position is what is reported, because a share committed to an open order is still
    a share owned, and the engine tracks the commitment through the order rather than
    through the position. The broker signs a short position's quantity and the engine
    keeps that direction in the side, so the magnitude is what is reported — the one place
    in this module where a sign is dropped, and it is dropped here on purpose.
    """
    side = row.get("side")
    if not isinstance(side, str) or side.lower() not in POSITION_SIDES:
        raise ValidationError(
            f"position.side: {side!r} is not a side; the broker writes "
            f"{', '.join(sorted(POSITION_SIDES))}"
        )
    return PositionStatusReport(
        account_id=account,
        instrument_id=instrument,
        position_side=POSITION_SIDES[side.lower()],
        quantity=quantity_of(
            abs(decimal_of(row.get("qty"), "position.qty")), size_precision, "position.qty"
        ),
        avg_px_open=maybe_decimal(row.get("avg_entry_price"), "position.avg_entry_price"),
        report_id=report_id or UUID4(),
        ts_last=ts_init,
        ts_init=ts_init,
    )


def _client_order_id(row: Mapping[str, Any]) -> ClientOrderId:
    """The row's client order id, which is the handle a restart recognises an order by."""
    found = row.get("client_order_id")
    if not isinstance(found, str) or not found:
        raise ValidationError(
            f"order {row.get('id')!r}: carries no client_order_id, which is the only handle "
            "that survives a restart on both sides"
        )
    return ClientOrderId(found)


def _maybe_price(value: object, precision: int, field: str) -> Price | None:
    """A price field that is null on every order type that does not carry one."""
    return None if value is None else price_of(value, precision, field)
