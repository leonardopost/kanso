"""The print rules: a resting limit fills from the prints that reach it, once the size ahead of
it has traded.

Under `costs.limit_fill: print_through` a resting limit is filled only by prints that the venue
applied after the order took its place at the price it rests at, from the side that can trade
with it, and that the data does not mark `Unlit`. A print strictly through its price — under a
resting buy, over a resting sell — fills it by that print's own size, shared across the orders it
reaches in price priority and then time at the price: a buy of 320 resting at 9.50 met by a print
of 100 at 9.49 fills 100, and the next print under it another 100; of two buys it reaches, the
higher is filled first. A print at its price fills it only once the size ahead of it there has
traded, and by what of the print is left past that size: a buy of 300 joining a bid of 9.50
showing 500 is filled by sellers' prints of 100 at 9.50 at the sixth, seventh and eighth. An
order takes its place at its price when the venue accepts it, when its trigger puts a stop's
limit on the book, or when the venue last modifies it, whichever came last, so a modify sends an
order to the back of its price, as the engine's own queue model does, and a partial fill does
not. Orders that took their places at one instant keep the order the engine holds them in,
which a modify does not re-sort, rather than the order they landed in: at that instant an order
modified to a price can stay ahead of one that landed there before the modify did. Nothing else
fills it: not a quote however far through its price, not a bar, not a print the venue applied
before the order landed, not one it applied before a modify moved the order to the price it
rests at, and not an `Unlit` print. Under `print_through_whole` a print through fills all that is
left of the order instead, the reading in which a market that traded through a displayed limit
would have taken it first; a print at its price fills what of it is left past the size ahead
under either rule.

**The size ahead is estimated from the top of the book.** When an order takes its place it is
the size the quote in force shows at the order's price, less what the sleeve's own takers have
taken there since that quote; nothing, when the order's price is better than the quote's side
of the book, as an order inside the spread is alone at its price; and unknown, when the price is
behind the touch or no quote is in force, and then, until a quote shows the price, only a print
through the order fills it. Each later quote cuts the estimate to the size it shows at the
order's price, so a size cancelled there is taken from behind the order first and the estimate
never exceeds what the price shows:
a quote whose side has moved away from the order's price — a bid under a resting buy — shows
nothing there and leaves nothing ahead, one whose side is better than the order's price leaves
the estimate as it was, and one whose side is at a price whose size ahead was unknown starts it
at the size shown. A print at the price cuts it by what of the print is left once the orders it
is through have taken their share, a sleeve's own taker filled at the touch cuts it by what the
taker took there, and a print through the price leaves nothing ahead. A sleeve's own orders at
one price queue among themselves by the time each took its place: a later one fills only past
all that is left of an earlier one, which counts as ahead of it beside the estimate. The
estimate is the model's own (`PrintThrough`) and not the engine's queue model, which kanso's
top-of-book venues leave off: that snapshots the size ahead off the matching engine's own book,
which a print sets to the print's price and size, cuts it on every print the venue applies,
whatever the data says of it, and cuts it on a quote only when the quote's price moves.

**Which prints fill is the data's to say.** A print the runner hands the venue as an `Unlit` —
one its print terms in the catalog mark traded off an exchange's displayed book or outside the
regular way: off-exchange, odd-lot, late, out of sequence, a correction or another sale
condition that may not set the last price (`kanso.data.types.print_terms`) — neither fills a
resting order nor cuts the size ahead of one, and still ends the quote in force when it trades
strictly outside it, as any print does, so a taker is decided alike whatever a print's terms. A
print with no terms held is read as lit: it fills and moves the queue as a lit one does.

Either way a taker — a market order, or a limit marketable when it lands — fills against the
last quote the venue applied while that quote is in force, at its touch and up to the size it
shows less what the sleeve's own takers have taken from it since, never against a print standing
as the book: three market buys of 100 sent while one quote shows 100 at the ask take those 100
once, and the next two take theirs one increment past it. An IOC limit takes no more than that
and is cancelled for the rest, and a FOK fills whole from it or not at all, so neither ever
rests. A quote is in force until the next quote,
or until a print trades strictly outside it — under a bid or over an ask it shows at a size —
since a market that traded there has left the quote, and a fill on it would be a price nobody
offered. With no quote in force a market order is refused for want of a market, as is one whose
side of the quote shows nothing, and a limit rests at its price. A limit's rest past the size
the quote shows also rests at its own price, so a buy limit never pays above its limit, and a
market order's rest walks one increment past the touch, as the engine walks any market order
larger than its top level — the whole order, when the sleeve's own takers have taken all the
quote showed. A rest priced through the quote is a resting order like any other: no quote fills
it, and a later print at or through it fills it as a maker, at its limit. Under `touch` and
`through` nothing here does anything: the venue keeps the engine's own fill model and `Tape`
returns on its first line.

**Two pieces, because the engine hands a fill model too little.** The matching engine asks its
fill model for the fills of every order it has matched — a market order, a limit marketable
when it lands or when a modify lands, and a resting limit a point reached — and fills it from
the book the model answers. It asks with identical arguments for a print, for a resting order
matched again after a command lands and for a quote locked at the price, so the model cannot
tell from them which point it is matching or whether the order was on the book before it.
`Tape`, a simulation module both of kanso's venues load after the corporate actions and
`Availability` (`kanso.nautilus.actions.parts`), tells it: before each point the venue
applies, the point itself, and for a print the orders resting on the instrument's matching
engine before it as limits, each with the price it rested at. A print's record — its price, the
side it hit, the orders that rested before it with their prices and the share of its size each
is due, and those it has credited — stands until the instrument's next quote or bar, so an order
matched again later, on either path, is never credited twice by one print, never credited by a
print it was not resting for, and never credited at a price it did not rest at when the print
arrived: a modify that moves an order through the print in hand makes it, for that print, an
order that arrived after it. A print that carries an aggressor reaches only the orders on the
side it hit, as the engine's own match does. The shares are struck when the print arrives, from
the orders resting then, and not in the order the engine asks: the engine walks its bids, then
its asks, in the order each reached its price list, which a modify does not re-sort, so an
order modified to a better price would otherwise be asked after one it has gone ahead of.

**The fill the model answers is the whole fill.** `PrintThrough` answers a book whose
`simulate_fills` returns exactly the fills the rule allows, ended by a fill of zero quantity:
the engine applies the fills in order and returns at the zero, before it cancels an IOC
remainder, walks a market order's rest or fills a resting limit's remainder whole on the
top-of-book venue. A lone zero answered for a market order is the engine's refusal for want of
a market — for an order still `SUBMITTED`, which kanso never moves on from there with a cancel
(`KansoStrategy.cancel_order`); for a limit it leaves the order resting at its price. An IOC
limit is answered what the quote gives it and no zero, so the engine fills that and cancels the
rest; one the quote gives nothing the model cancels on the matching engine itself, as the
engine cancels an IOC it finds unmarketable on landing.

**A command due by a print lands before it.** Under a stated latency the venue lands a command
at the first point after its delay, and after matching that point. Before a print `Tape` lands
every command due by the print's instant first (`SimulatedExchange.process`), so an order that
reached the venue in time is on the book when the print arrives, and a cancel that reached it
in time has taken the order off; one due at a quote still lands after the quote, so a taker
fills on the first quote at or after its delay — unless a print comes first, when it fills on
the quote before, if neither that print nor one since traded outside it: a print outside the
quote ends it before what is due by the print lands, so a taker in flight across a gap or a
jump is refused or rests, as one sent on that print is.

**The two paths fill alike because the model decides.** The engine decides whether an order is
marketable from its own bid and ask, and the two paths can hold those differently: once a
command lands the research engine matches every resting order again and re-reads its bid and
ask from its book, which a print sets to the print's price on both sides, while the node's
venue keeps the side the engine put back to the last quote after a print with an aggressor.
While a quote is in force neither path's bid is under its bid nor its ask over its ask — a
print inside the quote moves them only towards each other, one outside ends the quote, and no
bar reaches an instrument that carries quotes (`kanso hyp validate` refuses a hypothesis that
asks one for both, and a stage's `deploy` versions that do between them) — so
the engine asks the model about every order the quote makes marketable, and the model answers
from the quote alone; with none in force it rests or refuses whatever it is asked about; and a
resting order is credited only by the print's own match, which both paths make alike, the
research path's later matches finding it credited, of the other side or not resting at that
price. The model keeps the last quote and the print in hand for each instrument and the module
none of its own; neither reads a clock nor draws a number, and `prob_fill_on_limit` is one and
`prob_slippage` zero. Both paths build one model per exchange from the same configuration, hand
both pieces the same points in the same order and land the same commands before each, so on a
feed of quotes and prints, where a sleeve sends no cancel, the venue's rule parted the two on
none of 5,400 seeded runs of two names that send entries, market orders, modifies and orders
from their fill handlers, where under `touch` most such runs part. They still part on a few
tapes: where a sleeve cancels, most of them tapes the engine's own rules part as well; on one
tape of a generator with a split and an exit that cancels what rests, whose script sends no
cancel; and on some the two record one instant's fills in another order (`docs/backlog.md`
rows 105, 162 and 163). Nothing is copied, re-stamped or reordered: the sleeve is handed every
point it was, at the same `data_time`.

**A stage's flatten is filled.** A stage node closes every position after its window's last
point, and no point follows to bring a quote: a close refused there would leave the stage
holding a book no restart inherits. So the node first lands what the sleeves sent on that
point and still had in flight, which the rule answers as on any point — a market order with
no quote in force refused — and only then tells the model it is closing (`closing`). From
there a market order the quote in force cannot fill — none in force after a print outside it,
or one showing nothing on the side the close takes — is answered nothing at all, which the
engine fills from its own book: the last print, or the quote, at that point's price and the
rest one increment past it, exactly as `touch` closes it. Where that book is empty too, the engine
refuses the close under every rule (`docs/backlog.md`).

**A split restates the model's own quote.** The corporate actions restate an instrument's book
in the new share count past the venue's modules, so the module cannot see it; the model's last
quote is restated with it (`restate`), its prices divided by the ratio and its sizes multiplied
by it, rather than handed the book, which after a print is the print on both sides — a taker
sent before the split name's next quote fills at the restated quote, never at a restated print.

Engine facts this module relies on (nautilus_trader 1.231.0). `kanso doctor` checks the first
six on the raw engine (`kanso.nautilus.facts`), and checks this module as kanso loads it, which
also exercises the seventh and eighth; the suite pins the last six, through a modify that
moves an order ahead of another, through a stage's flatten, through a print at a resting
order's price, through FOK limits, through an order filled in parts and through `Unlit` prints
on both paths:

* The matching engine asks `FillModel.get_orderbook_for_fill_simulation(instrument, order,
  best_bid, best_ask)` for the fills of an order it has matched — a market order, a limit on
  landing and a resting limit a point reached — and takes its answer's `simulate_fills` in
  place of its own book; a resting limit it has matched is marked `MAKER` first, and a market
  order or a limit marketable on landing `TAKER`.
* `apply_fills` returns at the first fill of zero quantity, before the IOC cancel, the market
  order's one-increment walk and the limit's whole fill on exhausted top-of-book volume; it
  rejects a market order still `SUBMITTED` whose only fill is that zero ("no market"), and
  leaves a limit open. With no zero it cancels an IOC's rest after the fills, before either.
* A market order's fills that the answered book does not cover walk one increment past the
  last of them on a top-of-book venue.
* Once a command lands, the research engine matches every resting order again and asks the
  fill model with exactly the arguments of the point's own match.
* `SimulatedExchange.process(ts_now)` lands every command in flight due by `ts_now`, and a
  module may call it from `pre_process`, before the matching engine applies the point.
* A top-of-book matching engine keeps a bid and an ask of its own, from which it judges whether
  an order is marketable: a quote sets both; a print with no aggressor sets both to its price;
  a buyer's print raises the ask to its price when it trades over it and puts the bid back to
  the last quote's, a seller's the other way round; and the re-match after a command lands
  reads both from the book, which a print sets to its price on both sides. After a print
  inside the last quote, re-matched or not, neither is outside that quote: the bid is never
  under its bid nor the ask over its ask.
* `SimulatedExchange.fill_model` is the model the exchange was built with, and
  `get_matching_engine(instrument_id).get_open_orders()` the orders resting on an instrument,
  `get_open_bid_orders()` and `get_open_ask_orders()` the engine's own two lists of them.
* `OrderMatchingEngine.cancel_order(order)` cancels an order the engine has accepted and
  reports it, also from inside the fill model's call for that order.
* A modify changes a resting order's price in place, without re-sorting the lists the engine
  walks, and the venue reports it with an `OrderUpdated` stamped (`ts_event`) at the instant
  it lands, as it stamps the order's `ts_accepted`, and a stop's trigger with an
  `OrderTriggered` stamped at the instant it triggers; both paths apply each event to the order
  before the next point reaches the venue.
* A market order whose fill model answers `None`, or a book that fills nothing, is filled from
  the engine's own book (`determine_market_fills_with_simulation`) — which is why a refusal is
  answered a fill of zero rather than an empty book.
* A print at a resting limit's price is a match the engine asks the fill model about, as a print
  through it is: a print with no aggressor sets the matching engine's bid and ask to its price,
  a seller's its ask, a buyer's its bid, and the limit probability is one.
* `apply_fills` cancels a FOK whose answered fills come to less than what is left of it before
  it applies any of them, so what a FOK is answered is taken only when it fills whole.
* An order's `event_count` grows with every event applied to it — its acceptance, each fill, a
  modify, a trigger — so an order whose count has not moved has not moved its place.
* A subclass of `TradeTick` is a print to both paths: the backtest loop, the live data engine
  and a venue's own subscription dispatch a point by `isinstance`, so an `Unlit` reaches
  `process_trade_tick`, the modules and a sleeve's `on_trade_tick` as any print does, and it
  pickles by the engine's own state with no reducer of its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from nautilus_trader.backtest.config import FillModelConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.enums import (
    AggressorSide,
    BookType,
    LiquiditySide,
    OrderSide,
    TimeInForce,
)
from nautilus_trader.model.events import OrderTriggered, OrderUpdated
from nautilus_trader.model.objects import Price, Quantity

__all__ = [
    "NAME",
    "WHOLE",
    "PrintThrough",
    "PrintThroughConfig",
    "Rested",
    "Tape",
    "Unlit",
    "closing",
    "observe",
    "restate",
]

NAME: Final = "Tape"
"""What the venue's log calls this module, suffixed with the venue it was loaded into."""

WHOLE: Final = "whole"
"""The `size` under which a print through a resting limit fills all that is left of it."""


class Unlit(TradeTick):  # type: ignore[misc]
    """A print its terms mark not lit and eligible: traded off an exchange's displayed book or
    outside the regular way (`kanso.data.types.print_terms`). Every field is the print's own;
    under the print rules it fills no resting order and moves no queue, and it is a print to
    everything else — the venue's quote, a sleeve's handlers, the marks."""

    __slots__ = ()


class PrintThroughConfig(FillModelConfig, frozen=True):
    """The print rule's configuration: `size` is `print`, a print through a resting limit
    filling it by the print's own size, shared, or `whole`, filling all that is left of it."""

    size: str = "print"


@dataclass(frozen=True, slots=True)
class Rested:
    """A limit resting on the book when a print arrives: its client order id, whether it
    buys, its price, what is left of it (raw), and the instant it took its place at that
    price — when the venue accepted it, triggered it or last modified it, whichever came
    last."""

    key: Any
    buy: bool
    price: Any
    leaves: int
    since: int


@dataclass(slots=True)
class _Place:
    """A resting limit's place at its price: the price, whether it buys, the instant it took
    the place, the count of the order's events when that was read, and the size estimated
    ahead of it there (raw), `None` while it is unknown."""

    price: Any
    buy: bool
    since: int
    events: int
    ahead: int | None


@dataclass(slots=True)
class _Print:
    """The print in hand on one instrument: its price, the side its aggressor hit, the
    orders resting before it with the price each rested at and the share of its size (raw)
    each is due, and those it credited."""

    price: Any
    hit: Any
    resting: Mapping[Any, Any]
    shares: Mapping[Any, int]
    credited: set[Any] = field(default_factory=set)


class _Answer(OrderBook):  # type: ignore[misc]
    """A book whose fills are exactly those it was built with (`simulate_fills` is cpdef)."""

    def __init__(self, instrument_id: Any, fills: list[tuple[Any, Any]]) -> None:
        super().__init__(instrument_id, BookType.L2_MBP)
        self._fills = fills

    def simulate_fills(
        self, order: Any, price_prec: int, size_prec: int, is_aggressive: bool
    ) -> list[tuple[Any, Any]]:
        """The fills the rule allows, whatever the order and the book's own levels."""
        return list(self._fills)


class PrintThrough(FillModel):  # type: ignore[misc]
    """The venue's fill model under the print rules: a resting limit fills from a print at
    its price past the size ahead of it or from one through it, a taker from the last quote,
    less what the sleeve's own takers have taken from it."""

    def __init__(
        self,
        prob_fill_on_limit: float = 1.0,
        prob_slippage: float = 0.0,
        random_seed: int | None = None,
        config: Any = None,
    ) -> None:
        super().__init__(prob_fill_on_limit, prob_slippage, random_seed, config)
        self._whole = getattr(config, "size", "print") == WHOLE
        self._quote: dict[Any, QuoteTick] = {}
        self._print: dict[Any, _Print | None] = {}
        self._places: dict[Any, dict[Any, _Place]] = {}
        self._taken: dict[Any, list[int]] = {}
        """What the sleeve's own takers have taken from the quote in force, raw: from its bid
        and from its ask."""
        self.exchange: Any = None
        """The exchange the model fills for, set by `observe`: an IOC limit it answers nothing
        is cancelled on that exchange's matching engine."""
        self.closing = False
        """Whether a stage node is flattening after its window's last point (`closing`)."""

    def seen(self, point: Any, resting: Sequence[Any]) -> None:
        """The point the venue is about to apply, and the limits resting before it: a quote
        cuts the size ahead of each to what it shows, becomes the last quote and ends the
        print in hand, as a bar ends it; a print is in hand until the next of either, its size
        shared across the orders it reaches when it arrives, and ends the last quote if it
        trades strictly outside it. `resting` is the limits as `placed` takes them: as the
        engine holds them, or as `Rested`."""
        if isinstance(point, QuoteTick):
            instrument_id = point.instrument_id  # one object, so its name is formatted once
            self.placed(instrument_id, resting)
            self.quoted(point, instrument_id)
        elif isinstance(point, TradeTick):
            instrument_id = point.instrument_id
            self._in_hand(point, instrument_id, self.placed(instrument_id, resting))
            self.traded(point, instrument_id)
        elif isinstance(point, Bar):
            self._print[point.bar_type.instrument_id] = None

    def placed(self, instrument_id: Any, orders: Sequence[Any]) -> list[Rested]:
        """The limits resting on an instrument, as a print finds them, each in its place: an
        order that has taken a place since the model last looked — landed, triggered or
        modified — is given the size ahead of it from the quote in force now, and one that has
        not keeps what its place had come to. Orders no longer resting are let go. `orders` are
        the engine's, in the order it holds them, or `Rested` as a caller states them."""
        held = self._places.get(instrument_id)
        if not orders:
            if held:
                del self._places[instrument_id]
            return []
        quote = self._quote.get(instrument_id)
        taken = self._taken.get(instrument_id)
        places: dict[Any, _Place] = {}
        rested: list[Rested] = []
        for order in orders:
            if type(order) is Rested:  # as a caller that asks the model directly states it
                key = order.key
                place = None if held is None else held.get(key)
                if place is None or place.since != order.since or place.price != order.price:
                    ahead = _ahead(quote, taken, order.price, order.buy)
                    place = _Place(order.price, order.buy, order.since, -1, ahead)
                places[key] = place
                rested.append(order)
                continue
            key = order.client_order_id
            place = None if held is None else held.get(key)
            count = order.event_count
            if place is None or place.events != count:
                since = _since(order)
                price = order.price
                if place is None or place.since != since or place.price != price:
                    buy = order.side == OrderSide.BUY
                    place = _Place(price, buy, since, count, _ahead(quote, taken, price, buy))
                else:
                    place.events = count
            places[key] = place
            rested.append(Rested(key, place.buy, place.price, order.leaves_qty.raw, place.since))
        self._places[instrument_id] = places
        return rested

    def quoted(self, quote: QuoteTick, instrument_id: Any) -> None:
        """A quote the venue is about to apply: it cuts the size ahead of every order resting
        on its instrument to what it shows at the order's price, starts the size ahead of one
        it shows that price for at that size, becomes the quote in force with nothing taken
        from it, and ends the print in hand."""
        places = self._places.get(instrument_id)
        if places:
            for place in places.values():
                shown = _shown(quote, place.price, place.buy)
                if shown is not None and (place.ahead is None or shown < place.ahead):
                    place.ahead = shown
        self._quote[instrument_id] = quote
        self._print[instrument_id] = None
        self._taken.pop(instrument_id, None)

    def _in_hand(self, point: TradeTick, instrument_id: Any, resting: Sequence[Rested]) -> None:
        """Hold a print as the one in hand on its instrument, its size shared across the orders
        resting before it that it reaches — or nothing in hand, for an `Unlit` print."""
        if type(point) is Unlit:
            self._print[instrument_id] = None
            return
        self._print[instrument_id] = _Print(
            point.price,
            point.aggressor_side,
            {each.key: each.price for each in resting},
            _shares(point, resting, self._places.get(instrument_id, {})),
        )

    def traded(self, point: TradeTick, instrument_id: Any = None) -> None:
        """End the last quote of a print's instrument if the print trades strictly outside it:
        the market has left that quote, so no taker is filled on it from here on, one landing
        on this print included. Asked again for the same print it ends nothing more: the
        quote is gone, or the print is still not outside it. `instrument_id` is the print's
        own, when the caller has read it already."""
        if instrument_id is None:
            instrument_id = point.instrument_id
        quote = self._quote.get(instrument_id)
        if quote is not None and _outside(point.price, quote):
            del self._quote[instrument_id]

    def restate(self, instrument: Any, ratio: float, ts_event: int, ts_init: int) -> None:
        """Restate the last quote of an instrument a split applied to in the new count: its
        prices divided by the ratio and its sizes multiplied by it, a size it showed staying
        at least one increment and one it did not at zero. The print in hand ends, as it does
        at any quote, and nothing is taken from the restated quote; the split cancels every
        resting order, so no place is kept. With no quote in force there is nothing to
        restate."""
        self._print[instrument.id] = None
        self._places.pop(instrument.id, None)
        self._taken.pop(instrument.id, None)
        quote = self._quote.get(instrument.id)
        if quote is None:
            return
        step = float(instrument.size_increment)

        def size(shown: Any) -> Any:
            if shown.raw == 0:
                return instrument.make_qty(0)
            return instrument.make_qty(max(float(shown) * ratio, step))

        self._quote[instrument.id] = QuoteTick(
            instrument.id,
            instrument.make_price(float(quote.bid_price) / ratio),
            instrument.make_price(float(quote.ask_price) / ratio),
            size(quote.bid_size),
            size(quote.ask_size),
            ts_event,
            ts_init,
        )

    def get_orderbook_for_fill_simulation(
        self, instrument: Any, order: Any, best_bid: Any, best_ask: Any
    ) -> Any:
        """The book an order the engine has matched is filled from: the rule's fills for a
        resting limit, the last quote less what the sleeve's takers took from it for a taker —
        or `None`, the engine's own book, for a stage's closing market order the quote cannot
        fill."""
        if order.liquidity_side == LiquiditySide.MAKER:
            return _Answer(instrument.id, self._rested(instrument, order))
        return self._taken_by(instrument, order, best_bid, best_ask)

    def _rested(self, instrument: Any, order: Any) -> list[tuple[Any, Any]]:
        nothing = [(order.price, Quantity.zero(instrument.size_precision))]
        hit = self._print.get(instrument.id)
        key = order.client_order_id
        if hit is None or hit.resting.get(key) != order.price or key in hit.credited:
            # A quote, a bar, an `Unlit` print, an order that landed or was moved after the
            # print, a re-match.
            return nothing
        buy = order.side == OrderSide.BUY
        if hit.hit == (AggressorSide.BUYER if buy else AggressorSide.SELLER):
            return nothing
        through = bool(hit.price < order.price if buy else hit.price > order.price)
        if not through and hit.price != order.price:
            return nothing
        hit.credited.add(key)
        raw = order.leaves_qty.raw
        if not (through and self._whole):
            raw = min(raw, hit.shares.get(key, 0))
        if raw <= 0:
            return nothing
        return [(order.price, Quantity.from_raw(raw, instrument.size_precision)), *nothing]

    def _taken_by(self, instrument: Any, order: Any, best_bid: Any, best_ask: Any) -> Any:
        zero = Quantity.zero(instrument.size_precision)
        buy = order.side == OrderSide.BUY
        touch = best_ask if buy else best_bid
        quote = self._quote.get(instrument.id)
        filled: list[tuple[Any, Any]] = []
        spent = False
        if quote is not None:
            shown, spent = _less(quote, self._taken.get(instrument.id), buy, instrument)
            book = OrderBook(instrument.id, BookType.L1_MBP)
            book.update_quote_tick(shown)
            filled = book.simulate_fills(
                order, instrument.price_precision, instrument.size_precision, not order.has_price
            )
        if not order.has_price:
            # A market order: the quote's touch, its rest walking one increment past it — the
            # whole order once the sleeve's takers have taken all the quote showed there — an
            # IOC's cancelled instead; refused with no quote in force or when the side it takes
            # shows nothing — except a stage's flatten, which the engine fills from its own book.
            if spent and not filled:
                return _Answer(instrument.id, [(_past(quote, buy, instrument), order.leaves_qty)])
            if self.closing and not filled:
                return None
            self._took(instrument.id, buy, filled)
            return _Answer(instrument.id, filled or [(touch, zero)])
        if order.time_in_force == TimeInForce.IOC:
            # What the quote shows within the limit, and no more: answered without the zero,
            # the engine fills it and cancels the rest. With nothing to take, the order is
            # cancelled here, as the engine cancels an IOC it finds unmarketable on landing.
            if filled:
                self._took(instrument.id, buy, filled)
                return _Answer(instrument.id, filled)
            self.exchange.get_matching_engine(instrument.id).cancel_order(order)
        # A limit: what the quote shows within it, and the rest rests at its price — all of
        # it with no quote in force. A FOK the quote cannot fill whole is cancelled, and takes
        # nothing.
        if order.time_in_force != TimeInForce.FOK or (
            sum(qty.raw for _, qty in filled) >= order.leaves_qty.raw
        ):
            self._took(instrument.id, buy, filled)
        return _Answer(instrument.id, [*filled, (order.price, zero)])

    def _took(self, instrument_id: Any, buy: bool, filled: Sequence[tuple[Any, Any]]) -> None:
        """Count what a taker was answered at the touch against the quote in force: a buy from
        its ask, a sale from its bid, and against the size ahead of each of the sleeve's own
        orders resting at that price on that side."""
        raw = sum(qty.raw for _, qty in filled)
        if not raw:
            return
        taken = self._taken.setdefault(instrument_id, [0, 0])
        taken[1 if buy else 0] += raw
        price = filled[0][0]
        for place in self._places.get(instrument_id, {}).values():
            if place.buy != buy and place.price == price and place.ahead:
                place.ahead = max(place.ahead - raw, 0)


def _since(order: Any) -> int:
    """The instant a resting limit took its place at its price: when the venue accepted it or,
    if later, when a stop's trigger put its limit on the book or the venue last modified it."""
    return int(
        max(
            [
                order.ts_accepted,
                *(
                    e.ts_event
                    for e in order.events
                    if isinstance(e, (OrderTriggered, OrderUpdated))
                ),
            ]
        )
    )


def _ahead(
    quote: QuoteTick | None, taken: Sequence[int] | None, price: Any, buy: bool
) -> int | None:
    """The size estimated ahead of an order taking its place at `price`: what the quote in force
    shows there less what the sleeve's takers have taken from it, nothing when the price is
    better than the quote's side, and unknown behind the touch or with no quote in force."""
    if quote is None:
        return None
    touch = quote.bid_price if buy else quote.ask_price
    if price == touch:
        shown = int((quote.bid_size if buy else quote.ask_size).raw)
        return max(shown - (taken[0 if buy else 1] if taken else 0), 0)
    better = price > touch if buy else price < touch
    return 0 if better else None


def _shown(quote: QuoteTick, price: Any, buy: bool) -> int | None:
    """What a quote shows at an order's price on the order's side: its size there, nothing when
    its side has moved away from the price, and unknown when its side is better than it."""
    touch = quote.bid_price if buy else quote.ask_price
    if price == touch:
        return int((quote.bid_size if buy else quote.ask_size).raw)
    away = touch < price if buy else touch > price
    return 0 if away else None


def _less(
    quote: QuoteTick, taken: Sequence[int] | None, buy: bool, instrument: Any
) -> tuple[QuoteTick, bool]:
    """The quote in force less what the sleeve's takers have taken from it, and whether the
    side a taker takes from showed a size they have taken all of."""
    if not taken or not (taken[1] if buy else taken[0]):
        return quote, False
    bid = max(quote.bid_size.raw - taken[0], 0)
    ask = max(quote.ask_size.raw - taken[1], 0)
    spent = (quote.ask_size.raw > 0 and ask == 0) if buy else (quote.bid_size.raw > 0 and bid == 0)
    precision = instrument.size_precision
    return (
        QuoteTick(
            quote.instrument_id,
            quote.bid_price,
            quote.ask_price,
            Quantity.from_raw(bid, precision),
            Quantity.from_raw(ask, precision),
            quote.ts_event,
            quote.ts_init,
        ),
        spent,
    )


def _past(quote: QuoteTick, buy: bool, instrument: Any) -> Any:
    """One increment past the quote's touch on the side a taker takes from."""
    step = instrument.price_increment.raw
    touch = quote.ask_price if buy else quote.bid_price
    return Price.from_raw(touch.raw + step if buy else touch.raw - step, instrument.price_precision)


def _reaches(price: Any, hit: Any, buy: bool, limit: Any) -> bool:
    """Whether a print at `price` whose aggressor hit `hit` is through a resting limit at
    `limit` — under a buy, over a sell — from the side that can trade with it, as the engine's
    own match reaches only the side a print hit."""
    if hit == (AggressorSide.BUYER if buy else AggressorSide.SELLER):
        return False
    return bool(price < limit if buy else price > limit)


def _shares(
    point: TradeTick, resting: Sequence[Rested], places: Mapping[Any, _Place]
) -> dict[Any, int]:
    """A print's size shared across the resting limits it reaches: the bids from the highest
    down, then the asks from the lowest up, and at one price the one that took its place there
    first first. An order the print is through takes what is left of it or of the print,
    whichever is less, and has nothing ahead of it from then on; at the print's own price what
    of the print the orders through it left first trades the size ahead of each order — the
    estimate, and all that is left of the sleeve's own orders ahead of it there — and the order
    takes what is left past that, and the estimate is cut by what was left of the print. Orders
    that took their places at one instant keep the order the engine holds them in."""
    left = point.size.raw
    price = point.price
    hit = point.aggressor_side
    shares: dict[Any, int] = {}
    side: bool | None = None
    reached = 0
    owned = 0
    ranked = sorted(
        resting,
        key=lambda each: (
            not each.buy,
            -each.price.raw if each.buy else each.price.raw,
            each.since,
        ),
    )
    for each in ranked:
        if hit == (AggressorSide.BUYER if each.buy else AggressorSide.SELLER):
            continue
        if each.buy is not side:
            side, reached, owned = each.buy, -1, 0
        place = places.get(each.key)
        if price < each.price if each.buy else price > each.price:
            shares[each.key] = share = min(each.leaves, left)
            left -= share
            if place is not None:
                place.ahead = 0
        elif price == each.price:
            if reached < 0:
                reached = left
            if place is not None and place.ahead is not None:
                share = min(each.leaves, max(reached - place.ahead - owned, 0))
                if share:
                    shares[each.key] = share
                    left -= share
                place.ahead = max(place.ahead - reached, 0)
            owned += each.leaves
    return shares


def _outside(price: Any, quote: QuoteTick) -> bool:
    """Whether a print's price is strictly under the quote's bid or over its ask, of a side
    the quote shows at a size."""
    under = quote.bid_size.raw > 0 and price < quote.bid_price
    over = quote.ask_size.raw > 0 and price > quote.ask_price
    return bool(under or over)


def _limits(engine: Any) -> list[Any]:
    """The limits resting on a matching engine, in the order it holds them: a stop's limit
    only once it has triggered."""
    if engine is None:
        return []
    return [
        order
        for order in engine.get_open_orders()
        if order.has_price and (not order.has_trigger_price or order.is_triggered)
    ]


def _any_resting(engine: Any) -> bool:
    """Whether any order rests on a matching engine, read off its own lists, unbuilt."""
    return engine is not None and bool(engine.get_open_bid_orders() or engine.get_open_ask_orders())


def observe(exchange: Any, point: Any) -> None:
    """Tell a venue's fill model the point it is about to apply, if it is a `PrintThrough`,
    with the limits resting on the instrument before it — a stop's limit only once it has
    triggered — in the order the engine holds them."""
    model = exchange.fill_model
    if not isinstance(model, PrintThrough):
        return
    model.exchange = exchange
    orders: list[Any] = []
    if isinstance(point, (QuoteTick, TradeTick)):
        orders = _limits(exchange.get_matching_engine(point.instrument_id))
    model.seen(point, orders)


def closing(exchange: Any) -> None:
    """Tell a venue's fill model, if it is a `PrintThrough`, that the node is flattening after
    its window's last point, once what the sleeves sent on that point has landed: from here a
    market order the quote in force cannot fill is filled from the engine's own book, as
    under `touch`, rather than refused."""
    model = exchange.fill_model
    if isinstance(model, PrintThrough):
        model.closing = True


def restate(exchange: Any, instrument_id: Any, ratio: float, ts_event: int, ts_init: int) -> None:
    """Restate a venue's fill model's last quote of an instrument in a split's new count, if
    the model is a `PrintThrough` (`PrintThrough.restate`)."""
    model = exchange.fill_model
    if isinstance(model, PrintThrough):
        model.restate(exchange.instruments[instrument_id], ratio, ts_event, ts_init)


class Tape(SimulationModule):  # type: ignore[misc]
    """Before every point under the print rules: land what is due by a print, then tell the
    fill model the point in hand. Under any other rule, nothing."""

    def pre_process(self, data: Any) -> None:
        """`point` for a quote or a print, and the point handed to the fill model for any
        other."""
        model = self.exchange.fill_model
        if not isinstance(model, PrintThrough):
            return
        if isinstance(data, (QuoteTick, TradeTick)):
            self.point(data, data.instrument_id)
            return
        observe(self.exchange, data)

    def point(self, data: QuoteTick | TradeTick, instrument_id: Any) -> None:
        """A quote or a print whose id the venue's modules read once. A quote places every
        order that has taken a place since the last point under the quote before it, then
        cuts the size ahead of each and becomes the quote in force. A print places them too —
        under the quote it is about to end, when it trades outside it — then ends a quote it
        trades outside, lands the commands due by it, places what they put on the book, and is
        held with the limits resting before it. Nothing between the two looks at the orders
        moves the model's quotes: landing commands asks the model for fills, which reads them,
        and only a split restates one (`kanso.nautilus.actions`), applied before this part is
        handed the point."""
        exchange = self.exchange
        model = exchange.fill_model
        if not isinstance(model, PrintThrough):
            return
        model.exchange = exchange
        engine = exchange.get_matching_engine(instrument_id)
        if type(data) is QuoteTick:
            if model._places.get(instrument_id) or _any_resting(engine):
                model.placed(instrument_id, _limits(engine))
            model.quoted(data, instrument_id)
            return
        quote = model._quote.get(instrument_id)
        if quote is not None and _outside(data.price, quote):
            model.placed(instrument_id, _limits(engine))
        model.traded(data, instrument_id)
        exchange.process(int(data.ts_init))
        model._in_hand(data, instrument_id, model.placed(instrument_id, _limits(engine)))

    def process(self, ts_now: int) -> None:
        """Nothing: the module acts on points."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what it decided is in the fills the venue made."""

    def reset(self) -> None:
        """Nothing: the state it feeds is the fill model's."""
