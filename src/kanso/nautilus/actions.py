"""Corporate actions, applied by the venue before it matches the point that triggered them.

A split is a bookkeeping change: a thousand shares at four dollars become a hundred at
forty, and nothing is bought, sold or earned. `kanso.nautilus.splits` holds *when* one
happens and *what* it changes; this module holds *where* — inside the simulated exchange,
one call before the ex-date's first point is matched.

**It is not in the strategy, and the reason is measured.** A sleeve handles a data event
only after the exchange has already matched against it: `BacktestEngine._run` sends every
point through `exchange.process_*` before `data_engine.process` publishes it, and a node
subscribes its simulated venue at `sandbox.MARKET_FIRST`, above a strategy's. So a sleeve
that cancels its resting orders on the ex-date is a bar too late. Measured on this window
before this module existed: a sleeve holding 1,005 shares at ten dollars with a take-profit
resting at fifty, taken through a one-for-ten reverse split that restates the price to a
hundred, had the take-profit filled for 1,005 shares at fifty on the ex-date bar and
reported a 40,169.85 profit on a corporate action. A take-profit above the market is the
most ordinary exit there is, and a run optimises exactly that number.

**Where it does belong is the venue.** A split is something the market does, not something
a strategy decides, and cancelling standing orders across a corporate action is what a
broker does rather than what its client does. So both of kanso's simulated venues load this
module — the research path's through `BacktestEngine.add_venue(modules=...)`, the node
path's through `kanso.nautilus.sandbox` — and a deployment against a real broker loads
none, because there the broker has already done it. One mechanism, one instant, and
`kanso replay parity` compares the two paths at a tolerance of zero across a split.

**The account is left as the engine computed it.** A split moves no money, but the engine's
own P&L does move across one, and nothing in strategy or module code can stop it:
`Position.avg_px_open` is `cdef readonly` and `apply_adjustment` does not rescale it, so
from the closing fill onwards the account is credited `(fill_px - pre_split_avg) x qty`.
Measured under kanso's own venue and instruments, a $10,500 account reads $19,500 the bar
after the position closes — and reads $10,500 correctly at every bar up to and including
the ex-date, so there is nothing to repair *at* the action. Writing the balance back here
would repair nothing and hide that; kanso instead reads none of those numbers (the runner's
extraction computes a trade from its fills and this module's adjustments) and
`criteria.integrity` denies a researched `strategy.py` the account and everything derived
from a position's opening basis.

Engine facts this module relies on (nautilus_trader 1.231.0):

* `SimulatedExchange.process_bar`, `process_quote_tick`, `process_trade_tick`, the three
  order-book variants, `process_instrument_status` and `process_instrument_close` each call
  `module.pre_process(data)` for every loaded module *before* handing the point to the
  matching engine. `process(ts_now)` calls `module.process(ts_now)` after draining the
  command queue.
* `SimulatedExchange.__init__` calls `module.register_base(portfolio, msgbus, cache, clock)`
  and then `module.register_venue(self)`, so a module holds the kernel's portfolio and
  cache and the exchange itself. `SimulationModule.process`, `log_diagnostics` and `reset`
  raise `NotImplementedError` unless overridden; `pre_process` does not.
* `SimulatedExchange.send` processes a command in the same call when `use_message_queue` is
  off, which is what a stage or replay venue sets unless its venue model states a latency,
  and queues it when it is on, which is the research venue's default and the stage's under a
  stated latency; `process(ts_now)` drains that queue. Both are called here, so
  a cancel raised from `pre_process` is applied on either path before the point that
  raised it reaches the matching engine.
* `SimulatedExchange.instruments` is the venue's own instrument map, populated by
  `add_instrument` before any point moves the market on both paths.
* `MessageBus.publish(topic, msg)` calls every handler subscribed to `topic` before it
  returns, so a sleeve subscribed to `TOPIC` has taken in a split — restated what it holds
  and booked the payment in lieu — before the point that applied the split reaches it.
* `SimulatedExchange.get_matching_engine(instrument_id)` returns the instrument's
  `OrderMatchingEngine` on both venues. Its `get_book()` is the book the venue declares — L1
  unless the hypothesis requires `book`, then L2 by price level — and on an L1 book
  `process_quote_tick` sets the top level from a quote, skipping one older than its last
  update — which is why a restatement is admitted first (`kanso.nautilus.availability`): a
  bar's walk can stamp the book past the instant of the point that applies the split. A
  side a quote shows at size zero is left empty, and `best_bid_price` or `best_ask_price`
  returns `None` for it. A market order is matched against that top level.
* On that book `process_quote_tick` also keeps the quote's bid and ask prices whatever their
  sizes, and `process_trade_tick`, once a print has been matched, sets the matching engine's
  bid back to the kept bid after a buyer's print and its ask back to the kept ask after a
  seller's, where they stay until a later point or a landing command sets them from the
  book's own side.
* `Position` keeps no last price of its own: its prices are `avg_px_open` and
  `avg_px_close`, and its last fill is `last_event`, an `OrderFilled` carrying `last_px`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId, StrategyId

from kanso.nautilus import availability, splits
from kanso.nautilus.availability import Availability
from kanso.nautilus.splits import Split

__all__ = ["NAME", "TOPIC", "CorporateActions", "Restated", "last_price", "modules"]

NAME: Final = "CorporateActions"
"""What the venue's log calls this module, suffixed with the venue it was loaded into."""

TOPIC: Final = "kanso.restated"
"""The bus topic a venue announces a split on, the moment it applies it and never before."""


@dataclass(frozen=True, slots=True)
class Restated:
    """One split, announced as the venue applies it: the instrument, the split, and the
    adjustment it made to each position it found open, carrying what it paid in lieu.

    It is how a sleeve learns of a split without holding a schedule: a schedule names every
    split of the instrument's life, the certification window's among them, and anything the
    strategy base holds a researched `strategy.py` can read. An announcement names only a
    split that has happened.
    """

    instrument_id: str
    split: Split
    adjustments: tuple[Any, ...]


class CorporateActions(SimulationModule):  # type: ignore[misc]
    """The corporate actions one venue's instruments declare, applied as they fall due.

    Held as a list of `(effective_ns, instrument, split)` in ex-date order and popped as
    the market's reference time reaches each one, so a split takes effect at the open of
    its ex-date whether or not that instrument printed first — a venue sees one stream in
    one order, and every point in it moves the clock.
    """

    def __init__(self, config: Any = None) -> None:
        super().__init__(config if config is not None else SimulationModuleConfig())
        self._due: list[tuple[int, str, Split]] = []
        self._applied: set[tuple[str, date]] = set()
        self._known = -1
        self._quoted: dict[InstrumentId, tuple[Any, Any]] = {}

    # --- what the exchange calls ---------------------------------------------

    def pre_process(self, data: Any) -> None:
        """Apply everything this point's reference time has reached, before it is matched,
        then keep a quote's bid and ask prices, whatever their sizes: the matching engine
        keeps them too once it applies the quote, which `Availability` sees that it does, and a
        restatement prices an empty side from them."""
        self.apply_through(int(data.ts_event), int(data.ts_init))
        if isinstance(data, QuoteTick):
            self._quoted[data.instrument_id] = (data.bid_price, data.ask_price)

    def process(self, ts_now: int) -> None:
        """Nothing: a corporate action is applied by the point that carries the market past
        it, and a venue advanced to an instant with no point has matched nothing."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what this module did is in the positions' own adjustment ledgers, which
        is where the runner's extraction reads it."""

    def reset(self) -> None:
        """Forget what has been applied and quoted, so a reused exchange re-reads its
        schedules."""
        self._due = []
        self._applied = set()
        self._known = -1
        self._quoted = {}

    # --- the action ----------------------------------------------------------

    def apply_through(self, ts_event: int, ts_init: int) -> None:
        """Apply every scheduled action effective at or before `ts_event`, in ex-date order."""
        self._refresh()
        while self._due and self._due[0][0] <= ts_event:
            _effective, name, split = self._due.pop(0)
            self._apply(InstrumentId.from_str(name), split, ts_event, ts_init)

    def _refresh(self) -> None:
        """Re-read the venue's schedules when its instrument map has changed, and only then.

        Both paths add every instrument before the first point moves the market, so this
        reads once in practice; it is guarded by the count rather than by a flag so that an
        instrument arriving mid-run brings its schedule with it instead of being skipped.
        """
        instruments = self.exchange.instruments
        if len(instruments) == self._known:
            return
        self._known = len(instruments)
        self._due = sorted(
            (split.effective_ns, str(instrument_id), split)
            for instrument_id, held in instruments.items()
            for split in splits.schedule_of(held)
            if (str(instrument_id), split.ex_date) not in self._applied
        )

    def _apply(
        self, instrument_id: InstrumentId, split: Split, ts_event: int, ts_init: int
    ) -> None:
        """Cancel, then adjust, then resync, then restate the book, then announce — in that
        order, and each part earns its place.

        **Cancel**, because a resting order is priced and sized in shares that no longer
        exist and the exchange is about to match it against restated prices. **Adjust**
        every open position in the instrument, whichever sleeve holds it, because a split
        reaches every holder, and pay out the fraction it leaves at the last price the book
        quotes in the old count — before the restatement below replaces it; a position the
        payout leaves flat is re-indexed as closed. **Resync**, because `Portfolio` caches a
        net position per instrument and would otherwise keep the pre-split count, so the
        next exit would be sized against shares nobody holds — measured, 1,005 sold against
        100 held. **Restate the book**, because the matching engine quotes the instrument at
        its last print until it prints again, and the point that applied the split may be
        another instrument's: an order that name's handler sends into this one would fill at
        the pre-split price — measured, 50 restated shares sold at 20.00 against 200.00, a
        9,005.50 loss on a card whose baseline lost the cost alone. Last, **announce** it on
        `TOPIC`: that is how a sleeve learns of the split, without holding a schedule, and
        books the payment in lieu.
        """
        self._cancel(instrument_id, ts_init)
        instrument = self.exchange.instruments[instrument_id]
        lot = float(instrument.lot_size or instrument.size_increment)
        multiplier = float(instrument.multiplier)
        book = self.exchange.get_matching_engine(instrument_id).get_book()
        made: list[Any] = []
        for position in sorted(
            self.cache.positions_open(instrument_id=instrument_id),
            key=lambda held: str(held.id),
        ):
            price = last_price(book, position)
            made.append(splits.apply_to(position, split, lot, ts_event, price, multiplier))
            self.cache.update_position(position)
        self.portfolio.initialize_positions()
        self._applied.add((str(instrument_id), split.ex_date))
        self._restate_book(instrument_id, split, ts_event, ts_init)
        self.msgbus.publish(topic=TOPIC, msg=Restated(str(instrument_id), split, tuple(made)))

    def _restate_book(
        self, instrument_id: InstrumentId, split: Split, ts_event: int, ts_init: int
    ) -> None:
        """Quote the matching engine's book in the restated shares: the last bid and ask
        divided by the ratio, their sizes multiplied by it. Nothing rests in it — the cancel
        ran first — so the quote moves prices and matches nothing.

        A side the book holds empty — a quote showed it at size zero — is restated empty, at
        size zero, which a top-of-book book applies as no level. Restating only a book that
        holds both sides left the other side quoting the old count: measured, a book whose
        last quote showed no bid kept its ask at ten dollars, and an order another name's
        handler sent into it filled there against a restated hundred. The empty side's price
        matters although it shows nothing: the matching engine keeps the last quote's bid
        and ask prices whatever their sizes, and after every print on a top-of-book book it
        puts the side the print's aggressor did not trade against back to that price, until
        a later point or a landing command sets it again. So the empty side is priced at
        what the last quote showed there, divided by the ratio — what that quote would have
        shown in the new count. Priced at the other side's restated price, as it was, a sell
        resting at the restated ask was filled there by a buyer's print below it: measured,
        an empty bid restated at an ask of 100.30, a buyer's print at 100.20 put the
        engine's bid at 100.30, and a second at 100.10 filled a sell of 100 at 100.30 that
        no buyer paid. A book no point has reached has nothing to restate.

        The quote carries the instant of the point that applied the split, and a bar of this
        instrument published after that instant has already stamped the book past it, so it
        is admitted first rather than skipped: measured, a bar of the eve published thirty
        seconds into the ex-date left the book quoting ten dollars, and an order another
        name's handler sent into it filled there against a restated hundred."""
        engine = self.exchange.get_matching_engine(instrument_id)
        book = engine.get_book()
        bid, ask = book.best_bid_price(), book.best_ask_price()
        # Only a quote empties a side, and every quote is kept, so a book with an empty side
        # has a quote to price it from; a book with no quote kept holds both sides, set by a
        # print or a bar, or nothing at all.
        quoted_bid, quoted_ask = self._quoted.get(instrument_id, (bid, ask))
        if quoted_bid is None:
            return
        instrument = self.exchange.instruments[instrument_id]
        step = float(instrument.size_increment)

        def side(price: Any, size: Any, quoted: Any) -> tuple[Any, Any]:
            if price is None:
                return instrument.make_price(float(quoted) / split.ratio), instrument.make_qty(0)
            restated_size = max(float(size) * split.ratio, step)
            return instrument.make_price(float(price) / split.ratio), instrument.make_qty(
                restated_size
            )

        bid_price, bid_size = side(bid, book.best_bid_size(), quoted_bid)
        ask_price, ask_size = side(ask, book.best_ask_size(), quoted_ask)
        restated = QuoteTick(
            instrument_id, bid_price, ask_price, bid_size, ask_size, ts_event, ts_init
        )
        availability.admit(engine, ts_event)
        engine.process_quote_tick(restated)
        self._quoted[instrument_id] = (bid_price, ask_price)

    def _cancel(self, instrument_id: InstrumentId, ts_init: int) -> None:
        """Cancel every resting order in this instrument, one command per sleeve holding one."""
        resting = self.cache.orders_open(instrument_id=instrument_id)
        if not resting:
            return
        for strategy_id in sorted({str(order.strategy_id) for order in resting}):
            self.exchange.send(
                CancelAllOrders(
                    trader_id=resting[0].trader_id,
                    strategy_id=StrategyId(strategy_id),
                    instrument_id=instrument_id,
                    order_side=OrderSide.NO_ORDER_SIDE,
                    command_id=UUID4(),
                    ts_init=ts_init,
                )
            )
        self.exchange.process(ts_init)


def last_price(book: Any, position: Any) -> float:
    """An instrument's last price in the old share count, before a split restates it: the
    midpoint its book still quotes, which is the close before the ex-date; the one side it
    quotes when a quote showed the other at size zero; or, when it quotes neither, the price
    of the position's own last fill.

    The position's last fill is its `last_event`: nautilus_trader 1.231.0's `Position` keeps
    no `last_px` of its own — its prices are `avg_px_open` and `avg_px_close` — and reading
    one ended the run with `AttributeError` the first time a book with an empty side met a
    split with a position open."""
    bid, ask = book.best_bid_price(), book.best_ask_price()
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    if bid is not None or ask is not None:
        return float(bid if bid is not None else ask)
    return float(position.last_event.last_px)


def modules(venue: str) -> list[SimulationModule]:
    """The simulation modules one venue loads, in the order it runs them, each named for the
    venue it serves: the corporate actions first, which read the book a split restates and
    pays in lieu from, then `Availability`, which may empty a top-of-book book before the
    point is applied. The other way round, a split that the instrument's own point triggers,
    stamped before the book's last update, would find the book already emptied: nothing to
    restate, and no quote to value the fraction at."""
    return [
        CorporateActions(SimulationModuleConfig(component_id=f"{NAME}-{venue}")),
        Availability(SimulationModuleConfig(component_id=f"{availability.NAME}-{venue}")),
    ]
