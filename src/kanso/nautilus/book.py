"""The book: several sleeves on one account, handed room from one capital in one order.

A book runs every member's generated sleeve in one engine, on one account per venue, beside
one ledger that decides how much of the book each entry may take. The members trade their
own positions (`KansoStrategy.held` reads the strategy's own), are sized by their own rules
first, and are then cut to what the book has left. The ledger charges nothing and books
nothing: every fill belongs to one member and is costed once, by the runner, in that member's
extraction (`kanso.nautilus.backtest._extract`), and the book's equity is struck from those
same fills.

**Who is handed room first is the book's order, never the engine's.** The engine serves a
topic's handlers in the order they subscribed and hands a point only to its own
instrument's subscribers, so points of two names at one instant reach their members in the
stream's order (`kanso.nautilus.facts`). So a member's `submit_entry` does not place
anything: it is collected, and once every member has handled the marker that closes the
instant (`kanso.nautilus.cross_section.closed`) the instant's entries are placed in the
book's order:

1. descending `edge_bp`, the predicted move in basis points less the entry's round-trip cost
   at the instant, as the entry states it; an entry with none ranks after every one that has;
2. then the member's rank (`ranked`): each certificate's net per trade over its certification
   window (`kanso.schemas.certification.NetPerTrade`), descending, ranked only when it is
   above zero and its trades fell on at least the block's `k_min` sessions; ties by `net_t`,
   descending; then the earlier `joined_at`, then the strategy id. A member that is not
   ranked comes after every ranked one, by `joined_at` and then id;
3. then the instrument id.

Exits are not collected: they go to the venue in the handler, before any entry of the
instant is placed, and one in flight counts as gone. Every order a member sends but the
entries the book places for it is checked, each order of a list in turn: it is an exit only
if it is on the closing side of the member's own position and closes no more than the
position holds with the member's own orders still working on that side and the list's
earlier orders on it, a one-cancels-other pair among them counted once, at the larger of the
two. Anything else is an entry built by hand, and refused (`book_entry`), naming the member.
So a stop and a take-profit each for the whole position, sent apart or listed unlinked, are an
exit and an entry, and linked one-cancels-other they are one exit; a one-updates-other pair is
two orders, since the venue fills both of one sent marketable (`kanso.nautilus.facts`).
Resting orders the venue fills at the instant are open before the entries are placed.

**Each entry is placed in that order, then cut.** It is sized by its member's own rule at
that moment — its own room under its own limits, as a sleeve alone is — and then cut to the
smallest of:

* the book's room: `min(capital, balance) × L` less the book's gross — every member's open
  positions marked at the latest quote's mid, or at their own cost where no quote has been
  seen, its resting orders at their limits for what they would add — a member's on one side
  of a name together, so two that between them would take its position past flat add what
  is past it, and a one-cancels-other pair once, as the one of it that leaves the most — and
  its market orders in flight at the price they were sized at — left over
  the entry's own round trip, `1 + 2 ×` its cost rate (`KansoStrategy._reserve`). The balance
  is the capital plus every member's realised net profit; `L` is the block's regular-hours
  leverage, and outside 09:30–16:00 New York time, on every venue, no entry is placed while
  the leverage there is not set (`outside_rth_unset`);
* the cap on the name, `max_name_c` of the capital, less what the book already holds, rests
  and has in flight in it;
* for a taker — a market entry, or a limit marketable at the latest quote — what is left of
  the size that quote displays on its side: one budget per name, side and instant, drawn by
  every taker entry in turn. A name with no quote seen has no displayed size, and no budget.

An entry its own rule sizes below one whole lot is refused (`below_c_min`); one the cut takes
below a lot is refused and counted by the cap that bound it, `name_cap`, `touch` or `room`, in
that order at a tie; one that is opposite to any member's open position, working resting
entry or entry in flight in the name is refused before it is sized (`opposite_refused`) — an
exit that closes or reduces a position is not an entry. An entry the cut left smaller is
recorded with what its own rule asked for and what was placed (`Cut`). Nothing is skipped
whole because the room is short; what is left is placed. `book_closed` and `no_size` are
counted in the order the rules name them and stay zero here: the book has no daily exit in
this version, and a minimum of one lot leaves no cut at or above the minimum that floors to
nothing. `unfilled` counts the resting entries the book placed that filled nothing.

**The touch budget is the book's, not the venue's.** With `liquidity_consumption` off a
simulated venue credits a point to each order it fills rather than sharing it, so two members
taking one touch in one instant would each be filled the whole of it; the budget is what
stops that for the book's takers. A resting order is still credited as the venue credits it.

Engine facts this module relies on (nautilus_trader 1.231.0, `kanso.nautilus.facts`): a
topic's handlers at one priority run in subscription order; under the NETTING OMS the cache
keeps one position per (instrument, strategy); `Cache.quote_tick` answers the latest quote
the data engine has handed on, the one every member's handlers read; the backtest engine's
simulated exchange queues an order sent from a handler, its message queue on, and matches it
when the engine settles the venue after the point (`SimulatedExchange.send` in
`backtest/engine.pyx`), so an entry placed earlier in the instant is in flight when the next
is cut, and is counted at the price it was sized at (measured by
`tests/nautilus/backtest/test_shared_book.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from math import fsum, isfinite
from pathlib import Path
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

from kanso.criteria import CardRun
from kanso.errors import PreconditionError, ValidationError
from kanso.nautilus import backtest, splits
from kanso.nautilus.backtest import RunRequest
from kanso.nautilus.sizing import SizingError
from kanso.nautilus.strategy import _id_of, _one_cancels_other, _order_price
from kanso.schemas import NetPerTrade, SharedBook, VenueModel

__all__ = [
    "BELOW_C_MIN",
    "BOOK_CLOSED",
    "COUNTERS",
    "NAME_CAP",
    "NO_SIZE",
    "OPPOSITE_REFUSED",
    "OUTSIDE_RTH_UNSET",
    "ROOM",
    "TOUCH",
    "UNFILLED",
    "BookLedger",
    "BookMember",
    "BookRun",
    "Cut",
    "execute_book",
    "ranked",
    "run_book",
    "segment_of",
]

ROOM: Final = "room"
NAME_CAP: Final = "name_cap"
TOUCH: Final = "touch"
OPPOSITE_REFUSED: Final = "opposite_refused"
NO_SIZE: Final = "no_size"
BELOW_C_MIN: Final = "below_c_min"
OUTSIDE_RTH_UNSET: Final = "outside_rth_unset"
BOOK_CLOSED: Final = "book_closed"
UNFILLED: Final = "unfilled"

COUNTERS: Final = (
    ROOM,
    NAME_CAP,
    TOUCH,
    OPPOSITE_REFUSED,
    NO_SIZE,
    BELOW_C_MIN,
    OUTSIDE_RTH_UNSET,
    BOOK_CLOSED,
    UNFILLED,
)
"""What the ledger counts, per member and per session: each refused entry once."""

NEW_YORK: Final = ZoneInfo("America/New_York")
RTH: Final = "rth"
PRE: Final = "pre"
POST: Final = "post"
OVERNIGHT: Final = "overnight"
_SEGMENTS: Final = (
    (4 * 3600, PRE),
    (9 * 3600 + 1800, RTH),
    (16 * 3600, POST),
    (20 * 3600, OVERNIGHT),
)
"""Each segment from the New York second of the day it opens at; before 04:00 is overnight."""

_NS: Final = 1_000_000_000


def segment_of(ts_ns: int) -> tuple[str, date]:
    """Which segment of the session an instant is in, and the New York date of its session.

    Regular hours are 09:30 to 16:00 New York time, the pre-market from 04:00 and the
    post-market to 20:00; anything else is overnight.
    """
    local = datetime.fromtimestamp(ts_ns // _NS, tz=NEW_YORK)
    second = local.hour * 3600 + local.minute * 60 + local.second
    found = OVERNIGHT
    for opens, name in _SEGMENTS:
        if second >= opens:
            found = name
    return found, local.date()


@dataclass(frozen=True)
class BookMember:
    """One member of a book: the version's request, when it joined, and what ranks it.

    `request` is the run the version is measured by alone — its hypothesis, bytes, venue
    model and window — at the book's capital. `net` is its certificate's net per trade, or
    `None` for a version whose certificate records none.
    """

    id: str
    version: int
    joined_at: datetime
    request: RunRequest
    net: NetPerTrade | None = None


def _eligible(member: BookMember, k_min: int) -> bool:
    """Whether a member's net per trade ranks it: it exists, is above zero, and was struck
    over trades on at least `k_min` sessions."""
    net = member.net
    return net is not None and net.net_per_trade_bp > 0 and net.sessions_with_trades >= k_min


def ranked(members: Sequence[BookMember], book: SharedBook) -> tuple[BookMember, ...]:
    """The members in the book's order: ranked ones by descending net per trade, then
    descending `net_t` (one with none after one with one), then the earlier `joined_at`, then
    the strategy id; every member that is not ranked after them, by `joined_at` and id."""

    def key(member: BookMember) -> tuple[object, ...]:
        if not _eligible(member, book.k_min):
            return (1, 0.0, 1, 0.0, member.joined_at, member.id)
        net = member.net
        assert net is not None  # noqa: S101 - _eligible holds it
        t = net.net_t
        return (0, -net.net_per_trade_bp, t is None, -(t or 0.0), member.joined_at, member.id)

    return tuple(sorted(members, key=key))


@dataclass(frozen=True)
class Cut:
    """An entry the book placed smaller than its member's own rule sized it."""

    ts_ns: int
    member: str
    instrument_id: str
    side: str
    asked: float
    placed: float
    by: str
    """The cap that bound it: `name_cap`, `touch` or `room`."""


@dataclass
class _Pending:
    """One entry a member made at the instant, waiting for the book to place it."""

    rank: int
    member: Any
    instrument_id: Any
    side: Any
    edge_bp: float | None
    kwargs: dict[str, Any]
    data_time: int
    seq: int

    def order(self) -> tuple[object, ...]:
        """Its place in the book's order: edge, then the member's rank, then the name."""
        return (
            self.edge_bp is None,
            -(self.edge_bp or 0.0),
            self.rank,
            str(self.instrument_id),
            self.seq,
        )


@dataclass
class _Placing:
    """The entry the book is placing now, and what its cut found."""

    pending: _Pending
    asked: float | None = None
    reference: float = 0.0
    touch: tuple[str, Any] | None = None
    bound: str | None = None


@dataclass
class _Exposure:
    """What the book holds, rests and has in flight: its gross, each name's, and the sides
    every name is held, worked or in flight on."""

    gross: float = 0.0
    names: dict[str, float] = field(default_factory=dict)
    sides: dict[str, set[int]] = field(default_factory=dict)


class BookLedger:
    """The one room every member of a book is cut to, handed out in the book's order."""

    def __init__(self, book: SharedBook, members: Sequence[tuple[str, Any]]) -> None:
        self.book = book
        self._members = tuple(strategy for _, strategy in members)
        self._ids = {strategy.id: member_id for member_id, strategy in members}
        self._rank = {strategy.id: index for index, (_, strategy) in enumerate(members)}
        self._pending: list[_Pending] = []
        self._seq = 0
        self._closing = 0
        self._claim: Any = None
        self._placing: _Placing | None = None
        self._session: date | None = None
        self._ts = 0
        self._drawn: dict[tuple[str, Any], float] = {}
        self._sized: dict[object, float] = {}
        self._rested: list[tuple[str, Any, object, date | None]] = []
        self._room_bound = False
        self.counts: dict[str, dict[str, int]] = {
            member_id: dict.fromkeys(COUNTERS, 0) for member_id, _ in members
        }
        self.sessions: dict[date, dict[str, dict[str, int]]] = {}
        self.cuts: list[Cut] = []
        self.gross: list[float] = []
        self.at_room_limit = 0
        for _, strategy in members:
            strategy._book_ledger = self

    # --- what a member asks of the ledger ---------------------------------------

    def defer(
        self,
        member: Any,
        instrument_id: Any,
        side: Any,
        edge_bp: float | None,
        kwargs: dict[str, Any],
    ) -> None:
        """Collect an entry a member made, for the book to place when the instant ends."""
        if edge_bp is not None and not isfinite(float(edge_bp)):
            raise ValidationError(
                f"edge_bp: {edge_bp!r} is not a number of basis points, so the entry has no "
                "place in the book's order"
            )
        for held in self._pending:
            if held.member is member and held.instrument_id == instrument_id and held.side == side:
                return
        self._seq += 1
        self._pending.append(
            _Pending(
                rank=self._rank[member.id],
                member=member,
                instrument_id=instrument_id,
                side=side,
                edge_bp=None if edge_bp is None else float(edge_bp),
                kwargs=kwargs,
                data_time=member.data_time,
                seq=self._seq,
            )
        )

    def claim(self, member: Any) -> bool:
        """Whether the `submit_entry` running now is the book placing this member's entry —
        true once per placement, so an entry the member makes inside it is collected."""
        if self._claim is member:
            self._claim = None
            return True
        return False

    def placing(self, member: Any) -> bool:
        """Whether the book is placing an entry of this member now."""
        return self._placing is not None and self._placing.pending.member is member

    def member_of(self, strategy: Any) -> str:
        """The book's id of the member a strategy runs, for a refusal to name it."""
        return f"book member {self._ids[strategy.id]}"

    def marked(self, member: Any, marker: Any) -> None:
        """Note that a member handled a marker; once every member has handled the one that
        closes the instant, place the instant's entries."""
        from kanso.nautilus.cross_section import CLOSES

        if getattr(marker, "n", None) != CLOSES:
            return
        self._closing += 1
        if self._closing < len(self._members):
            return
        self._closing = 0
        if self._pending:
            self._flush(int(marker.ts_init))

    def cut(
        self,
        member: Any,
        instrument: Any,
        side: Any,
        units: float,
        reference: float,
        limit: float | None,
    ) -> float:
        """What of an entry its member's own rule sized to `units` the book lets it place:
        `units` cut to the room, the name's cap and the touch left, or nothing — counted —
        when its own rule sized less than a lot or the cut leaves less than one."""
        placing = cast("_Placing", self._placing)  # only a placement sizes an entry here
        lot = float(instrument.lot_size or instrument.size_increment)
        if units < lot:
            self._count(member, BELOW_C_MIN)
            return 0.0
        key = instrument.id.value
        unit = reference * float(instrument.multiplier)
        exposure = self._exposure()
        caps: list[tuple[str, float]] = []
        if self.book.max_name_c is not None:
            left = self.book.max_name_c * self.book.capital - exposure.names.get(key, 0.0)
            caps.append((NAME_CAP, left / unit))
        touch = self._touch_left(member, instrument.id, side, limit)
        if touch is not None:
            caps.append((TOUCH, touch))
        caps.append((ROOM, self._room(exposure) / member._reserve(key) / unit))
        bound, cap = min(caps, key=lambda found: found[1])
        if cap < lot:
            self._count(member, bound)
            self._room_bound = self._room_bound or bound == ROOM
            return 0.0
        placing.asked = units
        placing.reference = reference
        placing.touch = None if touch is None else (key, side)
        if cap < units:
            placing.bound = bound
            self._room_bound = self._room_bound or bound == ROOM
            return cap
        return units

    def placed(self, member: Any, order: Any) -> None:
        """Take in an entry the book placed: what it drew of the touch, the price it was sized
        at, whether it rests, and the cut, if it was cut."""
        from nautilus_trader.model.enums import OrderType, order_side_to_str

        placing = cast("_Placing", self._placing)  # only a placement places an entry here
        qty = float(order.quantity)
        if placing.touch is not None:
            self._drawn[placing.touch] = self._drawn.get(placing.touch, 0.0) + qty
        if order.order_type == OrderType.MARKET:
            self._sized[order.client_order_id] = placing.reference
        else:
            self._rested.append(
                (self._ids[member.id], member, order.client_order_id, self._session)
            )
        asked = placing.asked
        if placing.bound is not None and asked is not None:
            whole = member._quantise(member.cache.instrument(order.instrument_id), asked)
            if whole is not None and float(whole) > qty:
                self.cuts.append(
                    Cut(
                        ts_ns=self._ts,
                        member=self._ids[member.id],
                        instrument_id=order.instrument_id.value,
                        side=order_side_to_str(order.side),
                        asked=float(whole),
                        placed=qty,
                        by=placing.bound,
                    )
                )

    # --- the instant's allotment -------------------------------------------------

    def _flush(self, ts_ns: int) -> None:
        """Place every entry collected at the instant, in the book's order, and note the
        book's gross once they are placed."""
        segment, session = segment_of(ts_ns)
        self._session, self._ts = session, ts_ns
        self._drawn = {}
        self._room_bound = False
        self._prune()
        while self._pending:
            batch = sorted(self._pending, key=_Pending.order)
            self._pending = []
            for pending in batch:
                self._place(pending, segment)
        exposure = self._exposure()
        self.gross.append(exposure.gross)
        if self._room_bound or self._room(exposure) <= 0:
            self.at_room_limit += 1

    def _place(self, pending: _Pending, segment: str) -> None:
        """Refuse an entry outside regular hours or against a side the book holds, works or
        has in flight in its name; place it otherwise, as its member would alone, cut."""
        from nautilus_trader.model.enums import OrderSide

        member = pending.member
        if segment != RTH:
            self._count(member, OUTSIDE_RTH_UNSET)
            return
        sign = 1 if pending.side == OrderSide.BUY else -1
        if -sign in self._exposure().sides.get(pending.instrument_id.value, ()):
            self._count(member, OPPOSITE_REFUSED)
            return
        self._placing = _Placing(pending)
        self._claim = member
        held = member._data_time
        member._data_time = pending.data_time
        try:
            member.submit_entry(
                pending.instrument_id, pending.side, edge_bp=pending.edge_bp, **pending.kwargs
            )
        except SizingError as error:
            refusal = error.refusal
            raise SizingError(
                replace(refusal, why=f"{self.member_of(member)}: {refusal.why}")
            ) from error
        finally:
            member._data_time = held
            self._placing = None
            self._claim = None

    # --- what the book holds -----------------------------------------------------

    def balance(self) -> float:
        """The capital plus every member's realised net profit: what each member's fills
        paid and were paid, with what it holds at its own cost."""
        total = self.book.capital
        for member in self._members:
            member._settle()
            held = fsum(
                float(position.signed_qty)
                * _basis(member, position)
                * member._multiplier_of(position.instrument_id)
                for position in member.cache.positions_open(strategy_id=member.id)
            )
            total += member._cash + held - member.capital
        return total

    def _room(self, exposure: _Exposure) -> float:
        """What the book may still take: `min(capital, balance) × L` less its gross."""
        return min(self.book.capital, self.balance()) * self.book.leverage.rth - exposure.gross

    def _exposure(self) -> _Exposure:
        """Every member's open positions at the mark, its market orders in flight at the price
        they were sized at, and its resting and in-flight limits at their prices for what they
        would add, by name, with the sides each name is held, worked or in flight on. A
        member's one-cancels-other siblings on one side count once, as the one of them that
        leaves the most, since no two of them fill (`kanso.nautilus.facts`)."""
        from nautilus_trader.model.enums import OrderSide, OrderType

        found = _Exposure()
        for member in self._members:
            positions = {
                position.instrument_id.value: position
                for position in member.cache.positions_open(strategy_id=member.id)
            }
            flying: dict[str, list[tuple[float, float]]] = {}
            for order in member._open_markets():
                key = order.instrument_id.value
                leaves = float(order.leaves_qty)
                signed = leaves if order.side == OrderSide.BUY else -leaves
                price = self._sized.get(order.client_order_id) or member._price_now(key) or 0.0
                flying.setdefault(key, []).append((signed, price))
            intended: dict[str, float] = {}
            for key in sorted(set(positions) | set(flying)):
                position = positions.get(key)
                held = 0.0 if position is None else float(position.signed_qty)
                legs = flying.get(key, [])
                now = held + fsum(qty for qty, _ in legs)
                intended[key] = now
                if now == 0.0:
                    continue
                found.sides.setdefault(key, set()).add(1 if now > 0 else -1)
                growing = [(abs(qty), px) for qty, px in legs if (qty > 0) == (now > 0)]
                size = fsum(qty for qty, _ in growing)
                sized_at = fsum(qty * px for qty, px in growing) / size if size else 0.0
                mark = _mark(member, key, position, sized_at)
                if held == 0.0 or (held > 0) != (now > 0):
                    worth = abs(now) * (sized_at or mark)
                elif abs(now) <= abs(held):
                    worth = abs(now) * mark
                else:
                    worth = abs(held) * mark + (abs(now) - abs(held)) * sized_at
                _add(found, key, worth * member._multiplier_of(key))
            resting: dict[tuple[str, int], list[Any]] = {}
            for entry in member._ledger.values():
                order = member._current(entry.order)
                if order.is_closed or order.order_type == OrderType.MARKET:
                    continue
                sign = 1 if order.side == OrderSide.BUY else -1
                resting.setdefault((order.instrument_id.value, sign), []).append(order)
            for (key, sign), orders in resting.items():
                now = intended.get(key, 0.0)
                total = 0.0
                for group in _one_cancels_other(orders):
                    order = max(group, key=lambda sibling: sibling.leaves_qty.as_decimal())
                    before = max(0.0, sign * now + total)
                    total += float(order.leaves_qty)
                    grows = max(0.0, sign * now + total) - before
                    if grows <= 0.0:
                        continue
                    found.sides.setdefault(key, set()).add(sign)
                    price = _order_price(order) or member._price_now(key) or 0.0
                    _add(found, key, grows * price * member._multiplier_of(key))
        found.gross = fsum(found.names.values())
        return found

    def _touch_left(
        self, member: Any, instrument_id: Any, side: Any, limit: float | None
    ) -> float | None:
        """What is left of the size the latest quote displays on a taker's side at this
        instant; `None` for an entry that would rest, or a name with no quote seen."""
        from nautilus_trader.model.enums import OrderSide

        quote = member.cache.quote_tick(instrument_id)
        if quote is None:
            return None
        buy = side == OrderSide.BUY
        if limit is not None and (
            (buy and limit < float(quote.ask_price)) or (not buy and limit > float(quote.bid_price))
        ):
            return None
        shown = float(quote.ask_size if buy else quote.bid_size)
        return shown - self._drawn.get((instrument_id.value, side), 0.0)

    # --- counting ------------------------------------------------------------------

    def _count(self, member: Any, counter: str, session: date | None = None) -> None:
        member_id = self._ids[member.id]
        self.counts[member_id][counter] += 1
        day = self._session if session is None else session
        if day is not None:
            by = self.sessions.setdefault(day, {})
            by.setdefault(member_id, dict.fromkeys(COUNTERS, 0))[counter] += 1

    def _prune(self) -> None:
        """Let go of the orders the book placed that the venue has closed: a market order's
        price is no longer in flight, and a resting entry that filled anything is not
        unfilled, while one closed with nothing filled is counted now, in its own session."""
        cache = self._members[0].cache
        self._sized = {
            client_order_id: price
            for client_order_id, price in self._sized.items()
            if not _closed(cache.order(client_order_id))
        }
        kept = []
        for rested in self._rested:
            _, member, client_order_id, session = rested
            order = cache.order(client_order_id)
            if order is not None and float(order.filled_qty) > 0.0:
                continue
            if _closed(order):
                self._count(member, UNFILLED, session)
                continue
            kept.append(rested)
        self._rested = kept

    def finish(self) -> None:
        """Count every resting entry the book placed that filled nothing by the window's end."""
        for _, member, client_order_id, session in self._rested:
            order = member.cache.order(client_order_id)
            if order is not None and float(order.filled_qty) == 0.0:
                self._count(member, UNFILLED, session)
        self._rested = []


def _closed(order: Any) -> bool:
    """Whether the venue has closed an order the cache holds."""
    return order is not None and bool(order.is_closed)


def _add(found: _Exposure, key: str, worth: float) -> None:
    found.names[key] = found.names.get(key, 0.0) + worth


def _basis(member: Any, position: Any) -> float:
    """A position's own cost a share, in the shares it holds now."""
    applied = tuple(member._restated.get(position.instrument_id.value, ()))
    return float(splits.ledger(splits.moves_of(position, applied)).basis)


def _mark(member: Any, key: str, position: Any, fallback: float) -> float:
    """The latest quote's mid, quoted in the shares held now; a position's own cost where no
    quote has been seen; `fallback` where there is neither."""
    quote = member.cache.quote_tick(_id_of(key))
    if quote is not None:
        mid = (float(quote.bid_price) + float(quote.ask_price)) / 2.0
        return float(mid / member._restating(key, int(quote.ts_event)))
    if position is not None:
        return _basis(member, position)
    return fallback


# --- the runner -------------------------------------------------------------------


@dataclass(frozen=True)
class BookRun:
    """One book over one window: each member's run and the book's.

    `runs` and `intents` are each member's, in the book's order (`members`): its own fills,
    each costed once, extracted as a run of it alone is. `equity` and `returns` are the
    book's at each of its period ends, struck from every member's fills on the marks of the
    whole feed, so with one member they are that member's. `counts` and `sessions` are the
    ledger's refusals per member, and per New York session and member; `cuts` the entries it
    placed smaller than their members' rules asked; `gross` the book's gross after each
    instant at which members asked for entries, whether or not any was placed, and
    `at_room_limit` how many of those left it no room or had the room bind an entry.
    """

    members: tuple[str, ...]
    runs: tuple[CardRun, ...]
    intents: tuple[tuple[tuple[int, str, str, float, str, float | None], ...], ...]
    capital: float
    period_ends_ns: tuple[int, ...]
    returns: tuple[float, ...]
    equity: tuple[float, ...]
    counts: Mapping[str, Mapping[str, int]]
    sessions: Mapping[date, Mapping[str, Mapping[str, int]]]
    cuts: tuple[Cut, ...]
    gross: tuple[float, ...]
    at_room_limit: int

    @property
    def peak_gross(self) -> float | None:
        """The most the book held, rested and had in flight after any instant at which members
        asked for entries."""
        return max(self.gross) if self.gross else None

    @property
    def mean_gross(self) -> float | None:
        """The book's mean gross over the instants at which members asked for entries."""
        return fsum(self.gross) / len(self.gross) if self.gross else None

    @property
    def room_limit_share(self) -> float | None:
        """The share of the instants at which members asked for entries that left the book at
        its room."""
        return self.at_room_limit / len(self.gross) if self.gross else None


def _refuse_members(book: SharedBook, members: Sequence[BookMember]) -> None:
    """What a book cannot hold: no member, a member twice, a member at another capital,
    window or period, a member under a `book` policy, with an attached construct, or holding
    a level-two book, and a window its hypothesis does not declare."""
    from kanso.nautilus.cross_section import BOOK

    if not members:
        raise PreconditionError(
            "book: a book of no member runs nothing", remedy="name the versions the book holds"
        )
    ids = [member.id for member in members]
    if len(set(ids)) != len(ids):
        raise PreconditionError(
            "book: names a member twice", remedy="name each version of the book once"
        )
    first = members[0].request
    for member in members:
        request = member.request
        backtest.stage_of(request.hyp, request.window)
        if request.capital != book.capital:
            raise PreconditionError(
                f"book: {member.id} runs at a capital of {request.capital:,.2f} and the book's "
                f"is {book.capital:,.2f}; every member is run at the book's capital",
                remedy=f"certify {member.id} at the book's capital, or leave it out of the book",
            )
        if (request.window, request.period) != (first.window, first.period):
            raise PreconditionError(
                f"book: {member.id} runs {request.window[0]}..{request.window[1]} by "
                f"{request.period} and {members[0].id} {first.window[0]}..{first.window[1]} "
                f"by {first.period}; a book runs every member over one window and period",
                remedy="run the members over one window",
            )
        refused = (
            "a `book` policy"
            if request.hyp.book is not None
            else "an attached construct"
            if request.modifiers
            else "the level-two book"
            if BOOK in request.hyp.data_requirements
            else None
        )
        if refused is not None:
            raise PreconditionError(
                f"book: {member.id} holds {refused}, which a book does not run in this version",
                remedy=f"leave {member.id} out of the book",
            )


def _seed_of(members: Sequence[BookMember]) -> str:
    """What seeds a book's random sources: its members' snapshots, each once, in order —
    the member's own for a book of one, and the same whatever the members' ranks."""
    return "+".join(sorted({member.request.snapshot_id for member in members}))


def execute_book(
    book: SharedBook,
    members: Sequence[BookMember],
    instruments: Sequence[object],
    groups: Sequence[Sequence[Sequence[object]]],
) -> BookRun:
    """Run a book over its members' window in one engine, and extract each member and the book.

    `groups` holds each member's own points, one type per group, in the order `members` is
    given. The feed is the distinct series among them, each once, so two members of one name
    and grain see one market; it is marked however its instants fall, and the last marker of
    each instant closes it (`kanso.nautilus.cross_section.closed`). Each member's window is
    refused as a run of it alone refuses it (`backtest.checked`), it is fed only the span its
    own request delivers (`deliver_from`) and trades from its window's open (`warm`), and it is
    extracted from its own positions and its own points' marks, as a stage node extracts a
    version.

    The venues are the members', agreed as a stage's are (`kanso.nautilus.node.agree`), each
    opened at the book's capital and at the largest leverage the block declares, so the
    ledger's room binds before the venue's margin.

    The members' sleeves are added to the engine in the order `members` is given, not the
    book's, so the order the engine serves them in is the caller's and decides nothing: the
    tests list a book's members both ways round and find the same runs. The random sources
    are seeded from every member's snapshot (`_seed_of`) and the book's marks are struck on
    the member delivered from the earliest instant, the lowest id at a tie, so neither moves
    when the members' ranks do.

    The whole window is held in this process, as a stage node holds its own.
    """
    from kanso.nautilus.cross_section import closed, deliver_from, ordered, warm
    from kanso.nautilus.node import _Engine, _group_key, _StrategyView, agree
    from kanso.nautilus.venue import venue_config

    _refuse_members(book, members)
    if len(groups) != len(members):
        raise ValidationError(
            f"groups: {len(groups)} sets of points for {len(members)} members; each member is "
            "handed its own"
        )
    own = {
        member.id: tuple(tuple(group) for group in held)
        for member, held in zip(members, groups, strict=True)
    }
    order = ranked(members, book)
    models = {
        member.id: VenueModel.model_validate(dict(member.request.venue_model)) for member in order
    }
    agreed = agree((member.id, member.request.hyp, models[member.id]) for member in order)
    marks = {
        member.id: backtest.checked(member.request, instruments, own[member.id]) for member in order
    }
    seen: dict[tuple[str, str], tuple[object, ...]] = {}
    for member in order:
        for group in own[member.id]:
            if group and len(group) > len(seen.get(_group_key(group), ())):
                seen[_group_key(group)] = group
    points = closed(ordered(tuple(seen[key] for key in sorted(seen)), coincident=True))
    deepest = min(order, key=lambda member: (member.request.delivered[0], member.id))
    measured = replace(
        deepest.request, capital=book.capital, cushion=0.0, settled_ns=None, resumes_ns=None
    )
    union = backtest.Marks(measured, models[deepest.id])
    union.take(points)
    union.check()
    backtest._seed_globals(_seed_of(members))
    engine = backtest.engine_for()
    try:
        for venue, (model, _, held_book) in sorted(agreed.items()):
            backtest.add_venue(
                engine,
                venue_config(venue, model, book.capital, book.leverage.largest, book=held_book),
            )
        for instrument in instruments:
            engine.add_instrument(instrument)
        strategies = []
        for index, member in enumerate(order):
            cls, config = backtest._sleeve(member.request)
            config = type(config)(**{**config.dict(), "order_id_tag": f"{index:03d}"})
            strategy = cls(config=config)
            strategy._hold_until_cross_section = True
            deliver_from(strategy, member.request.delivered[0])
            if member.request.prefix is not None:
                warm(strategy, member.request.bounds[0])
            strategies.append(strategy)
        ledger = BookLedger(
            book,
            [(member.id, strategy) for member, strategy in zip(order, strategies, strict=True)],
        )
        listed = {member.id: strategy for member, strategy in zip(order, strategies, strict=True)}
        for member in members:
            engine.add_strategy(listed[member.id])
        opens, closes = measured.delivered
        backtest._load_stream(engine, points)
        engine.run(start=opens, end=closes - 1, streaming=True)
        engine.end()
        ledger.finish()
        runs = tuple(
            backtest._extract(
                member.request,
                _Engine(_StrategyView(engine.cache, strategy.id.value)),
                marks[member.id],
            )
            for member, strategy in zip(order, strategies, strict=True)
        )
        fills = sorted((fill for run in runs for fill in run.fills), key=lambda fill: fill.ts_ns)
        curve = backtest._equity(
            measured,
            union,
            fills,
            backtest.multipliers_of(engine.cache.instruments()),
            backtest._adjusted(backtest._positions(engine.cache)),
        )
        intents = tuple(
            tuple(
                (i.ts_event, i.instrument_id, i.side, i.qty, i.order_type, i.price)
                for i in strategy.intents
            )
            for strategy in strategies
        )
    finally:
        engine.dispose()
    return BookRun(
        members=tuple(member.id for member in order),
        runs=runs,
        intents=intents,
        capital=book.capital,
        period_ends_ns=curve.ends,
        returns=curve.returns,
        equity=curve.equity,
        counts=ledger.counts,
        sessions=ledger.sessions,
        cuts=tuple(ledger.cuts),
        gross=tuple(ledger.gross),
        at_room_limit=ledger.at_room_limit,
    )


def run_book(book: SharedBook, members: Sequence[BookMember], catalog_path: Path) -> BookRun:
    """Read each member's window from the catalog and run the book over it.

    Each member's window must be one its hypothesis declares, as a run of it alone must,
    and is refused before anything is read; the window is read whole, a member at a time.
    """
    _refuse_members(book, members)
    instruments: dict[str, object] = {}
    groups = []
    for member in members:
        found, held = backtest.window_data(member.request, catalog_path)
        for instrument in found:
            instruments.setdefault(str(getattr(instrument, "id", instrument)), instrument)
        groups.append(held)
    return execute_book(
        book, members, tuple(instruments[name] for name in sorted(instruments)), groups
    )
