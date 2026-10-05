"""A screen's sessions, and each leg's points in one, read through the runner's own reader.

**What a session is.** A calendar day in the clock's time zone — the zone `hours` names, or
UTC under `overlap` — on which a cell's legs printed. A day is never decided by a calendar:
a leg that did not print on it leaves it out of every cell that leg is in, so a US holiday
drops out of a BTC-against-MARA cell and stays in a BTC-against-ETH cell. Returns are never
taken across two sessions.

**What a session's points are.** Each leg is read with `kanso.nautilus.backtest.market_points`,
the function a card's window is read with, so a screen is handed the same points at the same
grain, the points of one instant in the order a card gets them. Prints, quotes and book
changes are read an hour at a time and bars a session at a time, and each read is turned
into arrays and let go, so a screen holds one session of its legs and never its window.

**Every read is clamped to the window**, `[start 00:00Z, end + 1 00:00Z)`, the span a card of
the same window may read. A session in a zone west of Greenwich ends after the UTC day
that holds its last date; the part past the window's last midnight is never read.

**A book leg is a book.** Its changes are applied, one instant's batch at a time, to the
engine's own `OrderBook` at level two, and the top is recorded after each instant: the
mid is its price and the touch its bid and ask. A book is only right when it is built from
the changes that made it, so a book leg is read from the start of its session's day, not
from the opening of its hours, and recorded only inside them.

**Availability.** Every instant here is a `ts_init`: a point is where it became public.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

import numpy as np

from kanso.errors import PreconditionError
from kanso.nautilus.backtest import READ_TICK_NS, market_points
from kanso.schemas.screen import Hours, Leg, Screen, hours_minutes

TICK_TYPES: Final = frozenset({"trade", "quote", "book"})
"""The unaggregated types, read an hour at a time."""

UTC_ZONE: Final = ZoneInfo("UTC")
NS_PER_MINUTE: Final = 60_000_000_000


@dataclass(frozen=True)
class Series:
    """One leg over one session: instants and prices, the touch where it has one.

    `price` is a bar's close or a print's price, and the mid of a quote or a book; `bid` and
    `ask` are present for a quote or a book leg. `ts` never decreases, and the points of one
    instant keep the order the catalog hands a card.
    """

    ts: np.ndarray
    price: np.ndarray
    bid: np.ndarray | None = None
    ask: np.ndarray | None = None

    @property
    def empty(self) -> bool:
        """Whether the leg printed nothing in the span read."""
        return len(self.ts) == 0


def zone_of(screen: Screen) -> ZoneInfo:
    """The zone a screen's sessions are days in."""
    hours = screen.clock.hours
    return ZoneInfo(hours.tz) if isinstance(hours, Hours) else UTC_ZONE


def days(screen: Screen, window: tuple[date, date]) -> list[date]:
    """Every day the window could hold a session on, in the clock's zone.

    A zone west of Greenwich can put a local day partly before the window's first midnight
    and partly after its last; the days are those whose span meets the window at all, and
    each read is clamped to the window.
    """
    first, last = window
    start = first - timedelta(days=1) if zone_of(screen) != UTC_ZONE else first
    found: list[date] = []
    day = start
    while day <= last + timedelta(days=1):
        opens, closes = hours_of(screen, day)
        span = window_ns(window)
        if max(opens, span[0]) < min(closes, span[1]):
            found.append(day)
        day += timedelta(days=1)
    return found


def window_ns(window: tuple[date, date]) -> tuple[int, int]:
    """The window's span, `[start 00:00Z, end + 1 00:00Z)`, in nanoseconds."""
    return _utc_ns(window[0]), _utc_ns(window[1] + timedelta(days=1))


def hours_of(screen: Screen, day: date) -> tuple[int, int]:
    """The span a session of `day` is read over: its hours, or the whole day."""
    tz = zone_of(screen)
    hours = screen.clock.hours
    if isinstance(hours, Hours):
        opens, closes = hours_minutes(hours.span)
    else:
        opens, closes = 0, 24 * 60
    base = datetime.combine(day, time(0), tzinfo=tz)
    return (
        _ns(base + timedelta(minutes=opens)),
        _ns(base + timedelta(minutes=closes)),
    )


def day_of(screen: Screen, day: date) -> tuple[int, int]:
    """The whole local day, `[00:00, next 00:00)` in the clock's zone, in nanoseconds."""
    tz = zone_of(screen)
    start = datetime.combine(day, time(0), tzinfo=tz)
    return _ns(start), _ns(datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz))


def definitions(catalog: Any, screen: Screen) -> dict[str, Any]:
    """The store's definition of every leg's instrument, refused when one is missing."""
    wanted = sorted({leg.instrument for leg in screen.legs.values()})
    held = {str(item.id): item for item in catalog.instruments(instrument_ids=wanted)}
    missing = [name for name in wanted if name not in held]
    if missing:
        raise PreconditionError(
            f"instruments: the store holds no definition for {', '.join(missing)}",
            remedy="run `kanso data instruments resolve` for them, then take a snapshot",
        )
    return held


def read(
    catalog: Any,
    held: Mapping[str, Any],
    screen: Screen,
    names: Sequence[str],
    day: date,
    window: tuple[date, date],
) -> dict[str, Series]:
    """The named legs over one session, each read once however many legs share it."""
    bounds = window_ns(window)
    opens, closes = hours_of(screen, day)
    span = (max(opens, bounds[0]), min(closes, bounds[1]))
    found: dict[str, Series] = {}
    cache: dict[tuple[str, str, str | None], Series] = {}
    for name in names:
        leg = screen.legs[name]
        key = (leg.instrument, leg.type, leg.resolution)
        if key not in cache:
            if span[0] >= span[1]:
                cache[key] = _empty(leg)
            elif leg.type == "book":
                start = max(day_of(screen, day)[0], bounds[0])
                cache[key] = _book(catalog, held[leg.instrument], start, span)
            else:
                cache[key] = _series(catalog, held[leg.instrument], leg, span)
        found[name] = cache[key]
    return found


def _series(catalog: Any, instrument: Any, leg: Leg, span: tuple[int, int]) -> Series:
    """A bar, trade or quote leg over `[span)`, read in steps and turned into arrays."""
    stamps: list[np.ndarray] = []
    prices: list[np.ndarray] = []
    bids: list[np.ndarray] = []
    asks: list[np.ndarray] = []
    for start, end in _steps(leg, span):
        points: tuple[Any, ...] = market_points(
            catalog, leg.type, instrument, leg.resolution or "", start, end - 1
        )
        if not points:
            continue
        stamps.append(np.fromiter((point.ts_init for point in points), np.int64, len(points)))
        if leg.type == "quote":
            bid = np.fromiter((float(point.bid_price) for point in points), np.float64)
            ask = np.fromiter((float(point.ask_price) for point in points), np.float64)
            bids.append(bid)
            asks.append(ask)
            prices.append((bid + ask) / 2.0)
        elif leg.type == "bar":
            prices.append(np.fromiter((float(point.close) for point in points), np.float64))
        else:
            prices.append(np.fromiter((float(point.price) for point in points), np.float64))
        del points
    if not stamps:
        return _empty(leg)
    return Series(
        ts=np.concatenate(stamps),
        price=np.concatenate(prices),
        bid=np.concatenate(bids) if leg.type == "quote" else None,
        ask=np.concatenate(asks) if leg.type == "quote" else None,
    )


def _book(catalog: Any, instrument: Any, start: int, span: tuple[int, int]) -> Series:
    """A book leg: its changes applied one instant at a time, its touch kept inside `span`."""
    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType

    book = OrderBook(instrument.id, BookType.L2_MBP)
    stamps: list[int] = []
    bids: list[float] = []
    asks: list[float] = []
    for read_start in range(start, span[1], READ_TICK_NS):
        end = min(read_start + READ_TICK_NS, span[1])
        points: tuple[Any, ...] = market_points(
            catalog, "book", instrument, "", read_start, end - 1
        )
        for instant, changes in _instants(points):
            for change in changes:
                book.apply_delta(change)
            if instant < span[0]:
                continue
            bid, ask = book.best_bid_price(), book.best_ask_price()
            if bid is None or ask is None:
                continue
            stamps.append(instant)
            bids.append(float(bid))
            asks.append(float(ask))
        del points
    if not stamps:
        return _empty_touch()
    bid_array = np.asarray(bids, dtype=np.float64)
    ask_array = np.asarray(asks, dtype=np.float64)
    return Series(
        ts=np.asarray(stamps, dtype=np.int64),
        price=(bid_array + ask_array) / 2.0,
        bid=bid_array,
        ask=ask_array,
    )


def _instants(points: Sequence[Any]) -> Iterator[tuple[int, list[Any]]]:
    """The points grouped by instant, in the order they came."""
    group: list[Any] = []
    instant: int | None = None
    for point in points:
        if point.ts_init != instant and group:
            assert instant is not None
            yield instant, group
            group = []
        instant = point.ts_init
        group.append(point)
    if group:
        assert instant is not None
        yield instant, group


def _steps(leg: Leg, span: tuple[int, int]) -> Iterator[tuple[int, int]]:
    """The reads one span is made in: an hour for an unaggregated leg, the span for bars."""
    if leg.type not in TICK_TYPES:
        yield span
        return
    for start in range(span[0], span[1], READ_TICK_NS):
        yield start, min(start + READ_TICK_NS, span[1])


def _empty(leg: Leg) -> Series:
    return _empty_touch() if leg.type in {"quote", "book"} else Series(_no_ts(), _no_price())


def _empty_touch() -> Series:
    return Series(_no_ts(), _no_price(), _no_price(), _no_price())


def _no_ts() -> np.ndarray:
    return np.zeros(0, dtype=np.int64)


def _no_price() -> np.ndarray:
    return np.zeros(0, dtype=np.float64)


def _utc_ns(day: date) -> int:
    return _ns(datetime.combine(day, time(0), tzinfo=UTC))


def _ns(moment: datetime) -> int:
    """A zone-aware instant as UTC nanoseconds, exactly: no float ever touches it."""
    delta = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000
