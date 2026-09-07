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
  off, which is what the node's venue sets, and queues it when it is on, which is the
  research venue's default; `process(ts_now)` drains that queue. Both are called here, so
  a cancel raised from `pre_process` is applied on either path before the point that
  raised it reaches the matching engine.
* `SimulatedExchange.instruments` is the venue's own instrument map, populated by
  `add_instrument` before any point moves the market on both paths.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Final

from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import CancelAllOrders
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId, StrategyId

from kanso.nautilus import splits
from kanso.nautilus.splits import Split

__all__ = ["NAME", "CorporateActions", "modules"]

NAME: Final = "CorporateActions"
"""What the venue's log calls this module, suffixed with the venue it was loaded into."""


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

    # --- what the exchange calls ---------------------------------------------

    def pre_process(self, data: Any) -> None:
        """Apply everything this point's reference time has reached, before it is matched."""
        self.apply_through(int(data.ts_event), int(data.ts_init))

    def process(self, ts_now: int) -> None:
        """Nothing: a corporate action is applied by the point that carries the market past
        it, and a venue advanced to an instant with no point has matched nothing."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what this module did is in the positions' own adjustment ledgers, which
        is where the runner's extraction reads it."""

    def reset(self) -> None:
        """Forget what has been applied, so a reused exchange re-reads its schedules."""
        self._due = []
        self._applied = set()
        self._known = -1

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
        """Cancel, then adjust, then resync — in that order, and each part earns its place.

        **Cancel**, because a resting order is priced and sized in shares that no longer
        exist and the exchange is about to match it against restated prices. **Adjust**
        every open position in the instrument, whichever sleeve holds it, because a split
        reaches every holder. **Resync**, because `Portfolio` caches a net position per
        instrument and would otherwise keep the pre-split count, so the next exit would be
        sized against shares nobody holds — measured, 1,005 sold against 100 held.
        """
        self._cancel(instrument_id, ts_init)
        instrument = self.exchange.instruments[instrument_id]
        lot = float(instrument.lot_size or instrument.size_increment)
        for position in sorted(
            self.cache.positions_open(instrument_id=instrument_id),
            key=lambda held: str(held.id),
        ):
            splits.apply_to(position, split, lot, ts_event)
        self.portfolio.initialize_positions()
        self._applied.add((str(instrument_id), split.ex_date))

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


def modules(venue: str) -> list[CorporateActions]:
    """The simulation modules one venue loads: this one, named for the venue it serves."""
    return [CorporateActions(SimulationModuleConfig(component_id=f"{NAME}-{venue}"))]
