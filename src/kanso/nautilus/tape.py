"""The print rule: a resting limit fills only on a later print strictly through its price.

Under `costs.limit_fill: print_through` a resting limit fills only when the venue applies a
print strictly through its price — under a resting buy, over a resting sell — that it applied
after the order reached its book, and by that print's own size, shared in the engine's own
matching order across the orders it reaches: a buy of 320 resting at 9.50 met by a print of
100 at 9.49 fills 100, and the next print under it another 100. Nothing else fills it: not a
quote however far through its price, not a print at its price, not a bar, and not a print the
venue applied before the order landed. Under `print_through_whole` a print through fills all
that is left of the order instead, the reading in which a market that traded through a
displayed limit would have taken it first. Either way a taker — a market order, or a limit
marketable when it lands — fills against the last quote the venue applied, at its touch and up
to the size it shows, never against a print standing as the book: a limit's rest stays on the
book at its own price, so a buy limit never pays above its limit, and a market order's rest
walks one increment past the touch, as the engine walks any market order larger than its top
level. A market order with no quote applied, or one whose side of the quote shows nothing, is
refused for want of a market. Under `touch` and `through` nothing here does anything: the venue
keeps the engine's own fill model and `Tape` returns on its first line.

**Two pieces, because the engine hands a fill model too little.** The matching engine asks its
fill model for the fills of every order it has matched — a market order, a limit marketable
when it lands, and a resting limit a point reached — and fills it from the book the model
answers. It asks with identical arguments for a print, for a resting order matched again after
a command lands and for a quote locked at the price, so the model cannot tell from them which
point it is matching or whether the order was on the book before it. `Tape`, a simulation
module both of kanso's venues load after the corporate actions and `Availability`
(`kanso.nautilus.actions.modules`), tells it: before each point the venue applies, the point
itself, and for a print the orders resting on the instrument's matching engine before it. A
print's record — its price, the side it hit, what of its size is not yet credited, the orders
that rested before it and those it has credited — stands until the instrument's next quote or
bar, so an order matched again later, on either path, is never credited twice by one print and
never credited by a print it was not resting for. A print that carries an aggressor reaches only
the orders on the side it hit, as the engine's own match does; without that, the research path,
which matches every resting order again once a command lands, credited a buyer's print under a
resting buy that the node never credited.

**The fill the model answers is the whole fill.** `PrintThrough` answers a book whose
`simulate_fills` returns exactly the fills the rule allows, ended by a fill of zero quantity:
the engine applies the fills in order and returns at the zero, before it cancels an IOC
remainder, walks a market order's rest or fills a resting limit's remainder whole on the
top-of-book venue. A lone zero answered for a market order is the engine's refusal for want of
a market; for a limit it leaves the order resting at its price.

**A command due by a print lands before it.** Under a stated latency the venue lands a command
at the first point after its delay, and after matching that point. Before a print `Tape` lands
every command due by the print's instant first (`SimulatedExchange.process`), so an order that
reached the venue in time is on the book when the print arrives, and a cancel that reached it
in time has taken the order off; one due at a quote still lands after the quote, so a taker
fills on the first quote at or after its delay — unless a print comes first, when it fills on
the quote before.

**It holds state and decides nothing at random.** The fill model keeps the last quote and the
print in hand for each instrument and the module none of its own; neither reads a clock nor
draws a number, and `prob_fill_on_limit` is one and `prob_slippage` zero. Both paths build one
model per exchange from the same configuration, hand both pieces the same points in the same
order and land the same commands before each, so the two hold the same state at every point,
and a card and a stage fill alike — every fill, not only every intent. Nothing is copied,
re-stamped or reordered: the sleeve is handed every point it was, at the same `data_time`.

Engine facts this module relies on (nautilus_trader 1.231.0). `kanso doctor` checks the first
five on the raw engine (`kanso.nautilus.facts`), and checks this module as kanso loads it, which
also exercises the last:

* The matching engine asks `FillModel.get_orderbook_for_fill_simulation(instrument, order,
  best_bid, best_ask)` for the fills of an order it has matched — a market order, a limit on
  landing and a resting limit a point reached — and takes its answer's `simulate_fills` in
  place of its own book; a resting limit it has matched is marked `MAKER` first, and a market
  order or a limit marketable on landing `TAKER`.
* `apply_fills` returns at the first fill of zero quantity, before the IOC cancel, the market
  order's one-increment walk and the limit's whole fill on exhausted top-of-book volume; it
  rejects a market order still `SUBMITTED` whose only fill is that zero ("no market"), and
  leaves a limit open.
* A market order's fills that the answered book does not cover walk one increment past the
  last of them on a top-of-book venue.
* Once a command lands, the research engine matches every resting order again and asks the
  fill model with exactly the arguments of the point's own match.
* `SimulatedExchange.process(ts_now)` lands every command in flight due by `ts_now`, and a
  module may call it from `pre_process`, before the matching engine applies the point.
* `SimulatedExchange.fill_model` is the model the exchange was built with, and
  `get_matching_engine(instrument_id).get_open_orders()` the orders resting on an instrument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from nautilus_trader.backtest.config import FillModelConfig
from nautilus_trader.backtest.models import FillModel
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggressorSide, BookType, LiquiditySide, OrderSide
from nautilus_trader.model.objects import Quantity

__all__ = ["NAME", "WHOLE", "PrintThrough", "PrintThroughConfig", "Tape", "observe"]

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
    its size is not yet credited (raw), the orders resting before it, and those it credited."""

    price: Any
    hit: Any
    left: int
    resting: frozenset[Any]
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

    def seen(self, point: Any, resting: frozenset[Any]) -> None:
        """The point the venue is about to apply, and the orders resting before it: a quote
        becomes the last quote and ends the print in hand, as a bar does; a print is in hand
        until the next of either."""
        if isinstance(point, QuoteTick):
            self._quote[point.instrument_id] = point
            self._print[point.instrument_id] = None
        elif isinstance(point, TradeTick):
            self._print[point.instrument_id] = _Print(
                point.price, point.aggressor_side, point.size.raw, resting
            )
        elif isinstance(point, Bar):
            self._print[point.bar_type.instrument_id] = None

    def get_orderbook_for_fill_simulation(
        self, instrument: Any, order: Any, best_bid: Any, best_ask: Any
    ) -> Any:
        """The book an order the engine has matched is filled from: the rule's fills for a
        resting limit, the last quote for a taker."""
        if order.liquidity_side == LiquiditySide.MAKER:
            return _Answer(instrument.id, self._rested(instrument, order))
        return self._taken(instrument, order, best_bid, best_ask)

    def _rested(self, instrument: Any, order: Any) -> list[tuple[Any, Any]]:
        nothing = [(order.price, Quantity.zero(instrument.size_precision))]
        hit = self._print.get(instrument.id)
        key = order.client_order_id
        if hit is None or key not in hit.resting or key in hit.credited:
            return nothing  # a quote, a bar, an order that landed after the print, a re-match
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
        if quote is None:
            # No quote applied: a market order is refused, a limit rests at its price.
            return _Answer(instrument.id, [(order.price if order.has_price else touch, zero)])
        book = OrderBook(instrument.id, BookType.L1_MBP)
        book.update_quote_tick(quote)
        filled = book.simulate_fills(
            order, instrument.price_precision, instrument.size_precision, not order.has_price
        )
        if order.has_price:
            return _Answer(instrument.id, [*filled, (order.price, zero)])  # the rest rests
        # A market order: the quote's touch, its rest walking one increment past it; refused
        # when the side it takes shows nothing.
        return _Answer(instrument.id, filled or [(touch, zero)])


def observe(exchange: Any, point: Any) -> None:
    """Tell a venue's fill model the point it is about to apply, if it is a `PrintThrough`:
    for a print, with the orders resting on the instrument before it."""
    model = exchange.fill_model
    if not isinstance(model, PrintThrough):
        return
    resting: frozenset[Any] = frozenset()
    if isinstance(point, TradeTick):
        engine = exchange.get_matching_engine(point.instrument_id)
        if engine is not None:
            resting = frozenset(order.client_order_id for order in engine.get_open_orders())
    model.seen(point, resting)


class Tape(SimulationModule):  # type: ignore[misc]
    """Before every point under the print rules: land what is due by a print, then tell the
    fill model the point in hand. Under any other rule, nothing."""

    def pre_process(self, data: Any) -> None:
        """Land the commands due by a print, then hand the point to the fill model."""
        if not isinstance(self.exchange.fill_model, PrintThrough):
            return
        if isinstance(data, TradeTick):
            self.exchange.process(int(data.ts_init))
        observe(self.exchange, data)

    def process(self, ts_now: int) -> None:
        """Nothing: the module acts on points."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what it decided is in the fills the venue made."""

    def reset(self) -> None:
        """Nothing: the state it feeds is the fill model's."""
