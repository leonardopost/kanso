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
record: the balance a sleeve sizes against is the equity the runner strikes on every path. A
period target's record of a stopped week reads each of its fills off it too
(`KansoStrategy._target_filled`), dated spread or none: a resting order the venue fills while
it applies a point is struck at that point, which the sleeve has yet to be handed.

**It also says where each split fell among the fills.** The venue applies a split at the
first point of its venue printed past it, at the instant that point was published, and a fill
it matched at that instant before it is struck in the old shares: an order in flight lands
behind the instant's first point, another venue's, say, and a point published then can come
before the one that applies the split, printed before it. Which fills came first is the
venue's order again, and no instant can say it, so the record numbers each fill the bus
carries and keeps, for each split the venue announces (`kanso.nautilus.actions.TOPIC`), how
many had been heard when it did — what the runner's extraction orders a split among the fills
of its instant by (`kanso.nautilus.backtest._adjusted`, `kanso.criteria.Adjustment`).

`Struck` is the record. Its venue half is a part of the one module a kanso venue loads
(`kanso.nautilus.actions.parts`), loaded only where a dated spread is charged, a period
target declared or a split declared, and placed last, so it is handed each point after the
parts that may land commands before the point is applied (`kanso.nautilus.tape`) and before
the matching engine applies it: it keeps each name's last point the engine applies, at that
point's `ts_event` — a bar's close, a print's or a quote's own stamp, never the instant it
was published at. Its other half hears each fill on the bus as the execution engine applies
it, before the sleeve is handed it, and keeps the name's instant for it, by the fill's
instrument and trade id, and each split the venue announces. Those are what every event of
one fill shares:
a fill that flips a net position reaches the positions the extraction reads as two events, the
second under an event id the bus never carried, and both keep the fill's instrument and trade
id, which the harness is handed whole. A fill of a name the venue had applied nothing of is not
kept, and is read at its own instant. A level-two venue strikes every fill at the touch, which
pays no half-spread, so what is kept for one charges nothing.

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
* `kanso.nautilus.actions` announces a split on `TOPIC` while the venue applies it, after it
  has adjusted every position and before the point that carried the market past it is
  matched, so no fill is made between the two; and a researched strategy, which cannot reach
  the bus (`strategy_integrity`), cannot send one from a handler of the announcement.
* `ExecutionEngine._flip_position` splits a fill that flips a position into one that closes
  it, under the fill's event id, and one that opens the other side, under a new `UUID4`; both
  copy the fill's `instrument_id` and `trade_id`, and the matching engine gives each fill of an
  instrument a trade id of its own (`_generate_trade_id`, a hash of the venue, the matching
  engine and the instant, and a count of every trade id it has made).

`kanso doctor` re-checks, in `kanso.nautilus.facts`, the first fact ("a simulation module that
calls its exchange's process from pre_process lands every command due by then before the
matching engine applies the point"), the second's finest grain ("the exchange matches against
the finest bar type it has seen for an instrument"), and the third's order of the two topics
with the fourth ("a fill is published on its instrument's fills topic before its strategy is
handed it, and a fill that flips a NETTING position reaches the positions as two that keep its
instrument and trade id ..."). That a bar is applied only on a top-of-book venue with bar
execution, never one aggregated `INTERNAL`, and that a node's venue hands its events over
synchronously, are read from the engine's source alone.
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
    at: the `ts_event` of its name's last point the venue had applied when it filled; and the
    order the bus carried the fills and the venue's splits in."""

    def __init__(self) -> None:
        self._last: dict[Any, int] = {}
        """Each instrument's last point the venue applied, at its `ts_event`."""
        self._grain: dict[Any, tuple[Any, Any]] = {}
        """Each instrument's execution bar type and its step, as the matching engine holds it."""
        self._at: dict[tuple[str, str], int] = {}
        """The instant each fill's price was struck at, by its instrument and trade id."""
        self._heard: dict[tuple[str, str], int] = {}
        """Each fill heard, by its instrument and trade id, numbered from one in the order the
        bus carried it."""
        self._split: dict[str, int] = {}
        """Each split adjustment the venue announced, by its event id: how many fills had been
        heard when it was."""

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
        """A fill the execution engine has applied: number it, and keep its name's last
        applied instant."""
        key = _fill_key(event)
        self._heard.setdefault(key, len(self._heard) + 1)
        at = self._last.get(event.instrument_id)
        if at is not None:
            self._at[key] = at

    def restated(self, restated: Any) -> None:
        """A split the venue has just applied, as `kanso.nautilus.actions` announces it: keep,
        for each position it adjusted, how many fills had been heard."""
        for event in restated.adjustments:
            self._split[str(event.id)] = len(self._heard)

    def heard(self, event: Any) -> int:
        """Where a fill falls among those the bus carried — the number `filled` gave it, or
        none, `0`, for a fill not heard — so it follows a split the venue had announced
        when fewer fills had been heard (`split`)."""
        return self._heard.get(_fill_key(event), 0)

    def split(self, event: Any) -> int:
        """How many fills had been heard when the venue announced the split a position's
        adjustment event applied, or `-1` for one it did not announce, which every fill of
        its instant then follows."""
        return self._split.get(str(event.id), -1)

    def at(self, event: Any) -> int | None:
        """The instant a fill's price was struck at, or `None` for a fill not heard: the fill
        the bus carried, or either event the engine split it into on a flip."""
        return self._at.get(_fill_key(event))

    def listen(self, msgbus: Any) -> None:
        """Hear every fill the execution engine on `msgbus` applies, and every split its venues
        announce."""
        from kanso.nautilus.actions import TOPIC

        msgbus.subscribe(topic=FILLS, handler=self.filled)
        msgbus.subscribe(topic=TOPIC, handler=self.restated)


def _fill_key(event: Any) -> tuple[str, str]:
    """What every event of one fill shares: its instrument and the trade id its matching engine
    gave it, which a flip's two halves both copy and no other fill of the name carries."""
    return event.instrument_id.value, event.trade_id.value


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
