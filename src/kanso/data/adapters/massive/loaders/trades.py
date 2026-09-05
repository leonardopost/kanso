"""Trade prints over the request path, timed at the tape rather than at ingest.

A trade row carries two instants and they mean different things, which is exactly the
distinction kanso's availability invariant is built on. `participant_timestamp` is when
the venue matched the trade — its economic reference time. `sip_timestamp` is when the
consolidated tape carried it — the instant the print became public. So `ts_event` is the
participant instant and `ts_init` is the tape instant, and `ts_init >= ts_event` holds
because the tape cannot carry a print before the venue made it. Taking either field for
both would either backdate availability by the consolidation hop or forward-date the
reference time by it; taking the moment of ingest for `ts_init` would make a backtest's
timing depend on when the operator happened to run the loader.

Both instants are nanoseconds here, where the aggregate endpoint's are milliseconds. The
unit is the endpoint's own and is never inferred from the magnitude of a number.

A row the engine cannot represent is dropped rather than repaired: a print with no price,
no size or a size of zero is not a trade, and the engine refuses a non-positive size
outright. The drop is visible, because the row count and the served span are measured
from the points that survived.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`TradeTick` requires a strictly positive size and a non-empty trade id, and carries an
`AggressorSide`. The vendor's tape does not say which side lifted, so the side is
`NO_AGGRESSOR` rather than a guess — a fabricated aggressor would flow straight into
every microstructure statistic computed over the series.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar, Final

from nautilus_trader.model.enums import AggressorSide

from kanso.data.adapters.massive.entitlement import TRADES, Endpoint
from kanso.data.adapters.massive.loaders.bars import (
    TICK_LIMIT,
    Kind,
    Request,
    RequestLoader,
    instant,
    numbers,
    ticks,
)
from kanso.data.loaders.points import instrument_id, make_trade

__all__ = [
    "AVAILABLE_FIELD",
    "EVENT_FIELD",
    "TICK_UNIT",
    "TRADES_KIND",
    "MassiveTradesLoader",
    "build_trade",
    "trade_id",
    "trades_endpoint",
]

EVENT_FIELD: Final = "participant_timestamp"
"""When the venue matched the print: its economic reference time. Declared here, where the
tape's rows are first read, and used by the quotes loader over the same two fields."""

AVAILABLE_FIELD: Final = "sip_timestamp"
"""When the consolidated tape carried it: the instant it became public."""

TICK_UNIT: Final = "ns"
"""The tick endpoints time every field in nanoseconds; the aggregates use milliseconds."""

TRADES_ENDPOINT: Final = replace(TRADES, params=(*TRADES.params, ("limit", str(TICK_LIMIT))))
"""The adapter's own trades endpoint with a page size. A limit is safe here and not on a
rolled-up aggregate: this endpoint rolls nothing up, so the cap applies to the rows that
were asked for rather than to inputs the vendor would have aggregated first."""


def trades_endpoint(request: Request) -> Endpoint:
    """The trades endpoint. It has no resolution and no adjustment to vary by."""
    return TRADES_ENDPOINT


def trade_id(request: Request, row: Mapping[str, Any], ts_event: int) -> str:
    """The print's own id, its tape sequence, or the instant it happened, in that order.

    The engine refuses an empty trade id, and two prints of one instrument at one instant
    must still be two points, so the fallbacks descend from the vendor's identifier to the
    sequence number to the reference time itself.
    """
    for name in ("id", "sequence_number"):
        value = row.get(name)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value):
            return str(value)
    return f"{request.ticker}-{ts_event}"


def build_trade(request: Request, row: Mapping[str, Any]) -> Any:
    """One tape row as a trade print: matched at the venue, public at the tape.

    Where the vendor's two instants contradict each other — a tape instant before the
    venue instant, which cannot have happened — the earlier of the two is taken as the
    reference time. That is the conservative direction: availability is never moved
    earlier than the source stated it, and only an availability that is too early can leak
    the future into a backtest.
    """
    available = instant(row, AVAILABLE_FIELD, TICK_UNIT)
    if available is None:
        return None
    ts_event = instant(row, EVENT_FIELD, TICK_UNIT) or available
    values = numbers(row, ("price", "size"))
    if values is None or values[0] <= 0 or values[1] <= 0:
        return None
    size = ticks(values[1], request.size_precision)
    if size <= 0:
        return None
    return make_trade(
        instrument_id(request.symbol, request.venue),
        ticks(values[0], request.price_precision),
        size,
        AggressorSide.NO_AGGRESSOR,
        trade_id(request, row, ts_event),
        request.price_precision,
        request.size_precision,
        min(ts_event, available),
        available,
    )


TRADES_KIND: Final = Kind(
    type="trade", dataset="trades", endpoint=trades_endpoint, build=build_trade
)


class MassiveTradesLoader(RequestLoader):
    """Trade prints for the classes whose ticks this plan entitles."""

    id: ClassVar[str] = "massive_trades"
    kind: ClassVar[Kind] = TRADES_KIND
