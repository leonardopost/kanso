"""Building engine data points, and the conversions every loader needs to do it.

Prices and quantities are fixed-point in the engine, so this module offers two ways to
reach them and is opinionated about which is which. A **generated** value arrives as a
whole number of ticks and is rendered as a decimal string, because integer arithmetic
and decimal text are exact on every host and a generator that must reproduce
byte-identically cannot afford a rounding that depends on a platform's libm. A **read**
value arrives from a file as text or a float and is quantised to the declared precision
by the engine itself, which is the only reasonable thing to do with somebody else's
decimals.

Bar timestamps are close timestamps: a bar's `ts_event` is the instant its aggregation
period ended, which is also the first instant the bar could be known, so a real-time bar
has `ts_init == ts_event` at its close and never at its open.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`BarType(instrument_id, BarSpecification(step, aggregation, price_type),
aggregation_source)` renders as `SYMBOL.VENUE-step-AGGREGATION-PRICE-SOURCE`, and bars
loaded from outside the engine are `AggregationSource.EXTERNAL` over `PriceType.LAST`.
`Bar` validates the OHLC ordering and raises on a high below the open; `QuoteTick`
requires its two prices to share a precision and its two sizes to share a precision;
`TradeTick` requires a strictly positive size and a non-empty trade id.
"""

from __future__ import annotations

from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nautilus_trader.model.data import Bar, BarSpecification, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BarAggregation,
    PriceType,
)
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.objects import Price, Quantity

from kanso.errors import ValidationError
from kanso.schemas.duration import DURATION_PATTERN, is_duration

AGGREGATIONS: Final[dict[str, BarAggregation]] = {
    "s": BarAggregation.SECOND,
    "m": BarAggregation.MINUTE,
    "h": BarAggregation.HOUR,
    "d": BarAggregation.DAY,
    "w": BarAggregation.WEEK,
}
"""The duration grammar's units, as the engine's bar aggregations."""

AGGRESSORS: Final[dict[str, AggressorSide]] = {
    "buyer": AggressorSide.BUYER,
    "buy": AggressorSide.BUYER,
    "b": AggressorSide.BUYER,
    "seller": AggressorSide.SELLER,
    "sell": AggressorSide.SELLER,
    "s": AggressorSide.SELLER,
    "": AggressorSide.NO_AGGRESSOR,
    "none": AggressorSide.NO_AGGRESSOR,
    "no_aggressor": AggressorSide.NO_AGGRESSOR,
}
"""The spellings of a trade's aggressor a file may carry."""


def zone(name: str) -> ZoneInfo:
    """The IANA time zone `name`, or a `ValueError` naming it.

    A `ValueError` rather than a kanso error because every caller is a pydantic validator
    over a spec the operator wrote, and the model turns it into the same validation
    failure as every other bad field.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"timezone: {name!r} is not an IANA time zone on this host ({exc})"
        ) from None


def instrument_id(instrument: str, venue: str) -> InstrumentId:
    """`SYMBOL.VENUE` for a workspace instrument id listed on `venue`."""
    try:
        return InstrumentId(Symbol(instrument), Venue(venue))
    except ValueError as exc:
        raise ValidationError(
            f"instrument: {instrument!r} on venue {venue!r} is not an instrument id: {exc}"
        ) from None


def bar_type(instrument: InstrumentId, resolution: str) -> BarType:
    """The external, last-price bar type of `resolution` for `instrument`."""
    if not is_duration(resolution):
        raise ValidationError(
            f"resolution: {resolution!r} is not a bar size; expected {DURATION_PATTERN}"
        )
    step, unit = int(resolution[:-1]), resolution[-1]
    if step <= 0:
        raise ValidationError(f"resolution: {resolution!r} must be longer than zero")
    return BarType(
        instrument,
        BarSpecification(step, AGGREGATIONS[unit], PriceType.LAST),
        AggregationSource.EXTERNAL,
    )


def ticks_to_price(ticks: int, precision: int) -> Price:
    """A whole number of ticks as a price, through its exact decimal spelling."""
    return Price.from_str(_decimal(ticks, precision))


def units_to_quantity(units: int, precision: int) -> Quantity:
    """A whole number of size increments as a quantity, exactly."""
    return Quantity.from_str(_decimal(units, precision))


def make_bar(
    bar: BarType,
    ohlc: tuple[int, int, int, int],
    volume: int,
    precision: int,
    size_precision: int,
    ts_event: int,
    ts_init: int,
) -> Bar:
    """A bar from open, high, low and close in ticks, timestamped at its close."""
    open_, high, low, close = ohlc
    return Bar(
        bar,
        ticks_to_price(open_, precision),
        ticks_to_price(high, precision),
        ticks_to_price(low, precision),
        ticks_to_price(close, precision),
        units_to_quantity(volume, size_precision),
        ts_event,
        ts_init,
    )


def make_quote(
    instrument: InstrumentId,
    bid: int,
    ask: int,
    bid_size: int,
    ask_size: int,
    precision: int,
    size_precision: int,
    ts_event: int,
    ts_init: int,
) -> QuoteTick:
    """A two-sided quote from prices and sizes in ticks and increments."""
    return QuoteTick(
        instrument,
        ticks_to_price(bid, precision),
        ticks_to_price(ask, precision),
        units_to_quantity(bid_size, size_precision),
        units_to_quantity(ask_size, size_precision),
        ts_event,
        ts_init,
    )


def make_trade(
    instrument: InstrumentId,
    price: int,
    size: int,
    side: AggressorSide,
    trade_id: str,
    precision: int,
    size_precision: int,
    ts_event: int,
    ts_init: int,
) -> TradeTick:
    """A trade print from a price and a size in ticks and increments."""
    return TradeTick(
        instrument,
        ticks_to_price(price, precision),
        units_to_quantity(size, size_precision),
        side,
        TradeId(trade_id),
        ts_event,
        ts_init,
    )


def aggressor(text: str) -> AggressorSide:
    """A file's spelling of a trade's aggressor, or a refusal naming the ones accepted."""
    side = AGGRESSORS.get(text.strip().lower())
    if side is None:
        raise ValidationError(
            f"aggressor_side: {text!r} is not a side; accepted spellings are "
            f"{', '.join(sorted(s for s in AGGRESSORS if s))}"
        )
    return side


def _decimal(value: int, precision: int) -> str:
    """`value` scaled by `10 ** -precision`, spelled exactly.

    Every caller takes its precision from a validated spec, so a negative one is not
    guarded against here.
    """
    if precision == 0:
        return str(value)
    sign = "-" if value < 0 else ""
    unit = 10**precision
    whole, part = divmod(abs(value), unit)
    return f"{sign}{whole}.{part:0{precision}d}"
