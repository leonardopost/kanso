"""The print rule: a resting limit fills only on a later print strictly through its price.

Under `costs.limit_fill: print_through` a resting limit fills only when the venue applies a
print strictly through its price — under a resting buy, over a resting sell — that it applied
after the order reached its book at the price it rests at, and by that print's own size, shared
in the engine's own matching order across the orders it reaches: a buy of 320 resting at 9.50
met by a print of 100 at 9.49 fills 100, and the next print under it another 100. Nothing else
fills it: not a quote however far through its price, not a print at its price, not a bar, not a
print the venue applied before the order landed, and not one it applied before a modify moved
the order to the price it rests at. Under `print_through_whole` a print through fills all that
is left of the order instead, the reading in which a market that traded through a displayed
limit would have taken it first.

Either way a taker — a market order, or a limit marketable when it lands — fills against the
last quote the venue applied while that quote is in force, at its touch and up to the size it
shows, never against a print standing as the book; an IOC limit takes no more than that and
is cancelled for the rest, and a FOK fills whole from it or not at all, so neither ever rests.
A quote is in force until the next quote,
or until a print trades strictly outside it — under a bid or over an ask it shows at a size —
since a market that traded there has left the quote, and a fill on it would be a price nobody
offered. With no quote in force a market order is refused for want of a market, as is one whose
side of the quote shows nothing, and a limit rests at its price. A limit's rest past the size
the quote shows also rests at its own price, so a buy limit never pays above its limit, and a
market order's rest walks one increment past the touch, as the engine walks any market order
larger than its top level. A rest priced through the quote is a resting order like any other:
no quote fills it, and a later print through it fills it as a maker, at its limit. Under
`touch` and `through` nothing here does anything: the venue keeps the engine's own fill model
and `Tape` returns on its first line.

**Two pieces, because the engine hands a fill model too little.** The matching engine asks its
fill model for the fills of every order it has matched — a market order, a limit marketable
when it lands or when a modify lands, and a resting limit a point reached — and fills it from
the book the model answers. It asks with identical arguments for a print, for a resting order
matched again after a command lands and for a quote locked at the price, so the model cannot
tell from them which point it is matching or whether the order was on the book before it.
`Tape`, a simulation module both of kanso's venues load after the corporate actions and
`Availability` (`kanso.nautilus.actions.modules`), tells it: before each point the venue
applies, the point itself, and for a print the orders resting on the instrument's matching
engine before it, each with the price it rested at. A print's record — its price, the side it
hit, what of its size is not yet credited, the orders that rested before it and their prices,
and those it has credited — stands until the instrument's next quote or bar, so an order
matched again later, on either path, is never credited twice by one print, never credited by a
print it was not resting for, and never credited at a price it did not rest at when the print
arrived: a modify that moves an order through the print in hand makes it, for that print, an
order that arrived after it. A print that carries an aggressor reaches only the orders on the
side it hit, as the engine's own match does.

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
bar reaches an instrument that carries quotes (`kanso hyp validate` refuses one) — so
the engine asks the model about every order the quote makes marketable, and the model answers
from the quote alone; with none in force it rests or refuses whatever it is asked about; and a
resting order is credited only by the print's own match, which both paths make alike, the
research path's later matches finding it credited, of the other side or not resting at that
price. The model keeps the last quote and the print in hand for each instrument and the module
none of its own; neither reads a clock nor draws a number, and `prob_fill_on_limit` is one and
`prob_slippage` zero. Both paths build one model per exchange from the same configuration, hand
both pieces the same points in the same order and land the same commands before each, so on a
feed of quotes and prints the venue's rule parts the two nowhere: measured over 5,400 seeded runs
of two names that send entries, market orders, modifies and orders from their fill handlers, with
no cancel, none parted reproducibly, where under `touch` most such runs part. What still parts
them is a sleeve's cancel, mostly on tapes the engine's own rules part as well (`docs/backlog.md`
rows 105, 162 and 163). Nothing is copied, re-stamped or reordered: the sleeve is handed every
point it was, at the same `data_time`.

**A stage's flatten is filled.** A stage node closes every position after its window's last
point, and no point follows to bring a quote: a close refused there would leave the stage
holding a book no restart inherits. So the node tells the model it is closing (`closing`), and
a market order the quote in force cannot fill — none in force after a print outside it, or one
showing nothing on the side the close takes — is answered nothing at all, which the engine
fills from its own book: the last print, or the quote, at that point's price and the rest one
increment past it, exactly as `touch` closes it. Where that book is empty too, the engine
refuses the close under every rule (`docs/backlog.md`).

**A split restates the model's own quote.** The corporate actions restate an instrument's book
in the new share count past the venue's modules, so the module cannot see it; the model's last
quote is restated with it (`restate`), its prices divided by the ratio and its sizes multiplied
by it, rather than handed the book, which after a print is the print on both sides — a taker
sent before the split name's next quote fills at the restated quote, never at a restated print.

Engine facts this module relies on (nautilus_trader 1.231.0). `kanso doctor` checks the first
six on the raw engine (`kanso.nautilus.facts`), and checks this module as kanso loads it, which
also exercises the last two:

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
  `get_matching_engine(instrument_id).get_open_orders()` the orders resting on an instrument.
* `OrderMatchingEngine.cancel_order(order)` cancels an order the engine has accepted and
  reports it, also from inside the fill model's call for that order.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from nautilus_trader.model.objects import Quantity

__all__ = [
    "NAME",
    "WHOLE",
    "PrintThrough",
    "PrintThroughConfig",
    "Tape",
    "closing",
    "observe",
    "restate",
]

NAME: Final = "Tape"
"""What the venue's log calls this module, suffixed with the venue it was loaded into."""

WHOLE: Final = "whole"
"""The `size` under which a print through a resting limit fills all that is left of it."""


class PrintThroughConfig(FillModelConfig, frozen=True):
    """The print rule's configuration: `size` is `print`, a print through a resting limit
    filling it by the print's own size, shared, or `whole`, filling all that is left of it."""

    size: str = "print"


@dataclass(slots=True)
class _Print:
    """The print in hand on one instrument: its price, the side its aggressor hit, what of
    its size is not yet credited (raw), the orders resting before it with the price each
    rested at, and those it credited."""

    price: Any
    hit: Any
    left: int
    resting: Mapping[Any, Any]
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
    """The venue's fill model under the print rules: a resting limit fills from a print
    through it, a taker from the last quote."""

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
        self.exchange: Any = None
        """The exchange the model fills for, set by `observe`: an IOC limit it answers nothing
        is cancelled on that exchange's matching engine."""
        self.closing = False
        """Whether a stage node is flattening after its window's last point (`closing`)."""

    def seen(self, point: Any, resting: Mapping[Any, Any]) -> None:
        """The point the venue is about to apply, and the orders resting before it with the
        price each rests at: a quote becomes the last quote and ends the print in hand, as a
        bar does; a print is in hand until the next of either, and ends the last quote if it
        trades strictly outside it."""
        if isinstance(point, QuoteTick):
            self._quote[point.instrument_id] = point
            self._print[point.instrument_id] = None
        elif isinstance(point, TradeTick):
            self._print[point.instrument_id] = _Print(
                point.price, point.aggressor_side, point.size.raw, resting
            )
            self.traded(point)
        elif isinstance(point, Bar):
            self._print[point.bar_type.instrument_id] = None

    def traded(self, point: TradeTick) -> None:
        """End the last quote of a print's instrument if the print trades strictly outside it:
        the market has left that quote, so no taker is filled on it from here on, one landing
        on this print included."""
        quote = self._quote.get(point.instrument_id)
        if quote is not None and _outside(point.price, quote):
            del self._quote[point.instrument_id]

    def restate(self, instrument: Any, ratio: float, ts_event: int, ts_init: int) -> None:
        """Restate the last quote of an instrument a split applied to in the new count: its
        prices divided by the ratio and its sizes multiplied by it, a size it showed staying
        at least one increment and one it did not at zero. The print in hand ends, as it does
        at any quote; with no quote in force there is nothing to restate."""
        self._print[instrument.id] = None
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
        resting limit, the last quote for a taker — or `None`, the engine's own book, for a
        stage's closing market order the quote cannot fill."""
        if order.liquidity_side == LiquiditySide.MAKER:
            return _Answer(instrument.id, self._rested(instrument, order))
        return self._taken(instrument, order, best_bid, best_ask)

    def _rested(self, instrument: Any, order: Any) -> list[tuple[Any, Any]]:
        nothing = [(order.price, Quantity.zero(instrument.size_precision))]
        hit = self._print.get(instrument.id)
        key = order.client_order_id
        if hit is None or hit.resting.get(key) != order.price or key in hit.credited:
            # A quote, a bar, an order that landed or was moved after the print, a re-match.
            return nothing
        buy = order.side == OrderSide.BUY
        if hit.hit == (AggressorSide.BUYER if buy else AggressorSide.SELLER):
            return nothing  # the engine's own rule: a print reaches the side it hit
        if not (hit.price < order.price if buy else hit.price > order.price):
            return nothing  # at the price, or not through it
        hit.credited.add(key)
        raw = order.leaves_qty.raw if self._whole else min(order.leaves_qty.raw, hit.left)
        if not self._whole:
            hit.left -= raw
        if raw <= 0:
            return nothing
        return [(order.price, Quantity.from_raw(raw, instrument.size_precision)), *nothing]

    def _taken(self, instrument: Any, order: Any, best_bid: Any, best_ask: Any) -> Any:
        zero = Quantity.zero(instrument.size_precision)
        touch = best_ask if order.side == OrderSide.BUY else best_bid
        quote = self._quote.get(instrument.id)
        filled: list[tuple[Any, Any]] = []
        if quote is not None:
            book = OrderBook(instrument.id, BookType.L1_MBP)
            book.update_quote_tick(quote)
            filled = book.simulate_fills(
                order, instrument.price_precision, instrument.size_precision, not order.has_price
            )
        if not order.has_price:
            # A market order: the quote's touch, its rest walking one increment past it — an
            # IOC's cancelled instead; refused with no quote in force or when the side it takes
            # shows nothing — except a stage's flatten, which the engine fills from its own book.
            if self.closing and not filled:
                return None
            return _Answer(instrument.id, filled or [(touch, zero)])
        if order.time_in_force == TimeInForce.IOC:
            # What the quote shows within the limit, and no more: answered without the zero,
            # the engine fills it and cancels the rest. With nothing to take, the order is
            # cancelled here, as the engine cancels an IOC it finds unmarketable on landing.
            if filled:
                return _Answer(instrument.id, filled)
            self.exchange.get_matching_engine(instrument.id).cancel_order(order)
        # A limit: what the quote shows within it, and the rest rests at its price — all of
        # it with no quote in force. A FOK the quote cannot fill whole is cancelled.
        return _Answer(instrument.id, [*filled, (order.price, zero)])


def _outside(price: Any, quote: QuoteTick) -> bool:
    """Whether a print's price is strictly under the quote's bid or over its ask, of a side
    the quote shows at a size."""
    under = quote.bid_size.raw > 0 and price < quote.bid_price
    over = quote.ask_size.raw > 0 and price > quote.ask_price
    return bool(under or over)


def observe(exchange: Any, point: Any) -> None:
    """Tell a venue's fill model the point it is about to apply, if it is a `PrintThrough`:
    for a print, with the orders resting on the instrument before it and their prices."""
    model = exchange.fill_model
    if not isinstance(model, PrintThrough):
        return
    model.exchange = exchange
    resting: dict[Any, Any] = {}
    if isinstance(point, TradeTick):
        engine = exchange.get_matching_engine(point.instrument_id)
        if engine is not None:
            resting = {
                order.client_order_id: order.price if order.has_price else None
                for order in engine.get_open_orders()
            }
    model.seen(point, resting)


def closing(exchange: Any) -> None:
    """Tell a venue's fill model, if it is a `PrintThrough`, that the node is flattening after
    its window's last point: from here a market order the quote in force cannot fill is filled
    from the engine's own book, as under `touch`, rather than refused."""
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
        """Land the commands due by a print — once the print has ended a quote it trades
        outside — then hand the point to the fill model."""
        model = self.exchange.fill_model
        if not isinstance(model, PrintThrough):
            return
        if isinstance(data, TradeTick):
            model.exchange = self.exchange
            model.traded(data)
            self.exchange.process(int(data.ts_init))
        observe(self.exchange, data)

    def process(self, ts_now: int) -> None:
        """Nothing: the module acts on points."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what it decided is in the fills the venue made."""

    def reset(self) -> None:
        """Nothing: the state it feeds is the fill model's."""
