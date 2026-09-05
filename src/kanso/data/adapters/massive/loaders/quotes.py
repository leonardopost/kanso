"""Top-of-book quotes over the request path, and the one-sided rows that are not quotes.

The two instants mean the same here as on the tape's trade rows and are read the same
way: `participant_timestamp` is when the venue published the change — the economic
reference time — and `sip_timestamp` is when the consolidated tape carried it, which is
the instant it became public. So `ts_event` is the participant instant, `ts_init` is the
tape instant, and both are nanoseconds where the aggregate endpoint's are milliseconds.

**A zero-sided row is dropped.** The tape carries rows with a bid and no ask, or an ask
and no bid — at an open, at a halt, and on any book that is one-sided at that instant —
and it spells the absent side as a zero price with a zero size. The engine will build
such a quote quite happily: `QuoteTick` accepts a zero price, so nothing downstream would
complain, and every mid-price computed over the series would be half the side that was
there. That is the whole reason these rows are refused rather than carried: a quote with
one side is not a quote a backtest can cross, and the failure it causes is silent.

The drop is measured rather than announced. The row count and the served span come from
the points that survived, so a window whose rows were all one-sided serves nothing and is
recorded as serving nothing, which is the truth about it.

NautilusTrader facts (`nautilus_trader 1.231.0`)
------------------------------------------------
`QuoteTick` requires its two prices to share a precision and its two sizes to share a
precision, and it accepts a zero price and a zero size without complaint — verified
against the installed engine, and the reason the one-sided check lives here rather than
being left to the constructor. A zero size with two real prices is kept: an indicative
top of book is still two-sided.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, ClassVar, Final

from kanso.data.adapters.massive.entitlement import QUOTES, Endpoint
from kanso.data.adapters.massive.loaders.bars import (
    TICK_LIMIT,
    Kind,
    Request,
    RequestLoader,
    instant,
    numbers,
    ticks,
)
from kanso.data.adapters.massive.loaders.trades import AVAILABLE_FIELD, EVENT_FIELD, TICK_UNIT
from kanso.data.loaders.points import instrument_id, make_quote

__all__ = ["QUOTES_KIND", "MassiveQuotesLoader", "build_quote", "quotes_endpoint"]

PRICES: Final = ("bid_price", "ask_price")
SIZES: Final = ("bid_size", "ask_size")

QUOTES_ENDPOINT: Final = replace(QUOTES, params=(*QUOTES.params, ("limit", str(TICK_LIMIT))))
"""The adapter's own quotes endpoint with a page size. Safe here for the same reason it is
safe on trades and unsafe on an aggregate: this endpoint rolls nothing up."""


def quotes_endpoint(request: Request) -> Endpoint:
    """The quotes endpoint. It has no resolution and no adjustment to vary by."""
    return QUOTES_ENDPOINT


def build_quote(request: Request, row: Mapping[str, Any]) -> Any:
    """One book row as a two-sided quote, or `None` when it has only one side.

    A row missing either price, or carrying a non-positive one, is not a two-sided quote
    and is dropped. Sizes are not held to the same rule: a real price with no size is an
    indicative top of book, which is two-sided and is kept as it was served.

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
    prices = numbers(row, PRICES)
    if prices is None or prices[0] <= 0 or prices[1] <= 0:
        return None
    sizes = numbers(row, SIZES) or [0.0, 0.0]
    return make_quote(
        instrument_id(request.symbol, request.venue),
        ticks(prices[0], request.price_precision),
        ticks(prices[1], request.price_precision),
        ticks(sizes[0], request.size_precision),
        ticks(sizes[1], request.size_precision),
        request.price_precision,
        request.size_precision,
        min(ts_event, available),
        available,
    )


QUOTES_KIND: Final = Kind(
    type="quote", dataset="quotes", endpoint=quotes_endpoint, build=build_quote
)


class MassiveQuotesLoader(RequestLoader):
    """Top-of-book quotes for the classes whose ticks this plan entitles."""

    id: ClassVar[str] = "massive_quotes"
    kind: ClassVar[Kind] = QUOTES_KIND
