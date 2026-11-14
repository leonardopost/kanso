"""The instant each fill's price was struck at, as the venue struck it.

A dated spread charges a fill the row and the part of the session its price was struck in
(`kanso.nautilus.costs.dated_half_spread`), and which instant that was is the venue's to say:
the price of a fill is the market its own name's matching engine last applied when the order
filled — the point the engine was applying when a resting order filled, and the last point it
had applied when an order landed. Under a latency an order lands after the first point of an
instant, whichever name's point that is, so a name whose bar of the instant comes later fills
at its bar before it; with none, an order a fill handler sends in the middle of an instant
fills before the venue applies the rest of it; a bar published after its close prices at the
minute it covers; and a name with no bars prices at its prints. Reconstructing that from the
stream a sleeve or an extraction is handed means re-deriving the venue's order of operations,
which differs between a card, a node and a stage that shares its venue with other versions.
So it is read here, off the venue, once, and both the extraction (`kanso.nautilus.backtest.
Marks.priced`) and the harness (`kanso.nautilus.strategy.KansoStrategy._half_of`) read the one
record: the balance a sleeve sizes against is the equity the runner strikes on every path.

`Struck` is the record. Its venue half is a part of the one module a kanso venue loads
(`kanso.nautilus.actions.parts`), loaded only where a dated spread is charged, and placed last,
so it is handed each point after the parts that may land commands before the point is applied
(`kanso.nautilus.tape`) and before the matching engine applies it: it keeps each name's last
point the engine applies, at that point's `ts_event` — a bar's close, a print's or a quote's
own stamp, never the instant it was published at. Its other half hears each fill on the bus as
the execution engine applies it, before the sleeve is handed it, and keeps the name's instant
for it, by the fill's event id. A fill of a name the venue had applied nothing of is not kept,
and is read at its own instant. A level-two venue strikes every fill at the touch, which pays
no half-spread, so what is kept for one charges nothing.

Engine facts this module relies on (nautilus_trader 1.231.0):

* `SimulatedExchange.process_bar`, `process_quote_tick` and `process_trade_tick` call
  `module.pre_process(point)` for every module loaded, in the order loaded, before the
  matching engine sees the point, and the matching engine fills a resting order it matches
  against the point inside that same call (`kanso.nautilus.actions`).
* `OrderMatchingEngine.process_bar` applies a bar only on a venue with bar execution and a
  top-of-book (`L1_MBP`) book, never one aggregated `INTERNAL`, and of an instrument's bar
  types only its execution type: the first it is handed, replaced by a type whose step is no
  longer than the one it holds, a bar of a longer step being skipped. On that book a quote
  and a print, once `kanso.nautilus.availability` has admitted it, are applied.
* `ExecutionEngine._handle_event` applies a fill to its order and publishes it on
  `events.fills.<instrument_id>` before it publishes it on `events.order.<strategy_id>`, the
  topic a strategy is handed its order events on, and `MessageBus.publish` calls every
  handler before it returns. On a node kanso's venue hands its events to the execution
  engine's synchronous implementation (`kanso.nautilus.sandbox`), so a fill is heard there
  before the venue applies another point, as it is in a backtest.
* An `OrderFilled`'s `id` is its own `UUID4`, and the cache and a position keep the event
  object the engine published.
"""

from __future__ import annotations

from typing import Any, Final

from nautilus_trader.backtest.config import SimulationModuleConfig
from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.model.data import Bar, QuoteTick, TradeTick
from nautilus_trader.model.enums import AggregationSource, BookType

__all__ = ["FILLS", "NAME", "Struck", "StruckPart"]

NAME: Final = "Struck"
"""What the venue's log calls the part, suffixed with the venue it was loaded into."""

FILLS: Final = "events.fills.*"
"""The bus topics the execution engine publishes each fill on, one per instrument."""


class Struck:
    """Each name's last point the venue applied, and the instant each fill's price was struck
    at: the `ts_event` of its name's last point the venue had applied when it filled."""

    def __init__(self) -> None:
        self._last: dict[Any, int] = {}
        """Each instrument's last point the venue applied, at its `ts_event`."""
        self._grain: dict[Any, tuple[Any, Any]] = {}
        """Each instrument's execution bar type and its step, as the matching engine holds it."""
        self._at: dict[Any, int] = {}
        """The instant each fill's price was struck at, by the fill's event id."""

    def bar(self, bar: Bar) -> None:
        """A bar the venue is about to be handed: kept where the matching engine applies it."""
        bar_type = bar.bar_type
        if bar_type.aggregation_source == AggregationSource.INTERNAL:
            return
        instrument_id = bar_type.instrument_id
        held = self._grain.get(instrument_id)
        if held is None or held[0] != bar_type:
            step = bar_type.spec.timedelta
            if held is not None and held[1] < step:
                return
            self._grain[instrument_id] = (bar_type, step)
        self._last[instrument_id] = int(bar.ts_event)

    def point(self, data: QuoteTick | TradeTick, instrument_id: Any) -> None:
        """A quote or a print the venue is about to apply."""
        self._last[instrument_id] = int(data.ts_event)

    def filled(self, event: Any) -> None:
        """A fill the execution engine has applied: keep its name's last applied instant."""
        at = self._last.get(event.instrument_id)
        if at is not None:
            self._at[event.id] = at

    def at(self, event: Any) -> int | None:
        """The instant a fill's price was struck at, or `None` for a fill not heard."""
        return self._at.get(event.id)

    def listen(self, msgbus: Any) -> None:
        """Hear every fill the execution engine on `msgbus` applies."""
        msgbus.subscribe(topic=FILLS, handler=self.filled)


class StruckPart(SimulationModule):  # type: ignore[misc]
    """The venue half of a `Struck`: handed each point before the matching engine applies it,
    a bar only where the venue applies bars."""

    def __init__(self, venue: str, struck: Struck) -> None:
        super().__init__(SimulationModuleConfig(component_id=f"{NAME}-{venue}"))
        self.struck = struck
        self._bars = True

    def register_venue(self, exchange: Any) -> None:
        """Hold the exchange, and read once whether it applies a bar at all."""
        super().register_venue(exchange)
        self._bars = bool(exchange.bar_execution) and exchange.book_type == BookType.L1_MBP

    def pre_process(self, data: Any) -> None:
        """Keep a bar the venue applies."""
        if self._bars and type(data) is Bar:
            self.struck.bar(data)

    def point(self, data: QuoteTick | TradeTick, instrument_id: Any) -> None:
        """Keep a quote or a print, whose id `KansoModules` read once."""
        self.struck.point(data, instrument_id)

    def process(self, ts_now: int) -> None:
        """Nothing: the part acts on points, and an instant with no point applied none."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what it kept is on the fills the run records."""

    def reset(self) -> None:
        """Nothing: the record is the run's, read after its engine has ended."""
