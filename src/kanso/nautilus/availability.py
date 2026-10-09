"""Every quote and print the sleeve is handed reaches the venue's top-of-book book.

A market point carries two instants: `ts_init`, when it became public, by which the engine
delivers it, and `ts_event`, its participant's reference time. A tape can stamp them apart —
a consolidated tape takes `ts_init` from itself and `ts_event` from the participant, a few
hundred microseconds earlier — so a point delivered after another can carry the earlier
`ts_event`. The engine's top-of-book matching engine ignores such a point: a quote or a print
stamped before its book's last update advances the venue's clock and matches the resting
orders against the book the venue already held — which can credit a print standing as that
book a second time — and leaves the book and the last price as they were. The sleeve is
handed it all the same, so the venue matched against a market the sleeve was no longer
looking at. Measured on 85 sessions each of two Nasdaq names' quotes and lit prints, about
12 % of the quotes and 53 % of the prints the sleeve was handed never reached the venue: a
resting buy a later quote went through stayed unfilled, and a market order sent on a quote
the venue had ignored filled against the book before it.

`Availability` is a simulation module both of kanso's venues load, after the corporate
actions (`kanso.nautilus.actions.modules`). Handed a quote or a print before the matching
engine sees it, it empties that instrument's top-of-book book when the engine would
otherwise skip the point, and only then; the engine then applies the point at its
`ts_init`, as the sleeve is handed it. A top-of-book book holds one level a side, which the
point sets whole, so emptying it first loses nothing the point does not replace. The module
copies, re-stamps and reorders nothing: the data engine, the cache and the sleeve get the
point they got before, at the same `data_time` and under the same stream digest. What moves
is what the venue fills, and with it every order a sleeve sends because of a fill or of the
position a fill leaves. It holds no state, reads no clock and draws nothing, so the two code
paths, handed the same points in the same order, hold the same book after each.

It leaves two things alone. A level-two book has no such filter — the engine applies a
change whatever its `ts_event` — and holds depth a reset would delete. A bar is never
filtered: the venue walks its open, high, low and close as prints stamped at the bar's
`ts_init`, straight into the book.

Engine facts this module relies on (nautilus_trader 1.231.0). `kanso doctor` checks the first
two as engine facts (`kanso.nautilus.facts`), the second by loading this module alone on a
venue, which also exercises the third and shows a module handed a quote and a print before
the matching engine sees them. That a venue calls several modules in the order loaded is
read from the engine's source; the order kanso loads them in is pinned by a test of
`kanso.nautilus.actions.modules`.

* A top-of-book (`L1_MBP`) matching engine's `process_quote_tick` and `process_trade_tick`
  return before they touch the book when the point's `ts_event` is earlier than the book's
  `ts_last` — the running maximum of the `ts_event` of every quote and print applied, and of
  the `ts_init` a walked bar stamps its prints with — having matched the resting orders
  against the book they held at the point's `ts_init`, and apply one stamped at `ts_last`.
* `OrderBook.reset()` empties both sides and zeroes `ts_last`, and a quote or a print applied
  to a top-of-book book after it leaves the book exactly as that point alone sets it.
* `SimulatedExchange.get_matching_engine(instrument_id)` returns the instrument's matching
  engine, or `None` before the venue has built one; `OrderMatchingEngine.get_book()` returns
  the book it matches against, and its `book_type` the venue's.
* `SimulatedExchange.process_quote_tick` and `process_trade_tick` call
  `module.pre_process(point)` for every loaded module, in the order loaded, before the
  matching engine sees the point.
"""

from __future__ import annotations

from typing import Any, Final

from nautilus_trader.backtest.modules import SimulationModule
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import BookType

__all__ = ["NAME", "Availability", "admit"]

NAME: Final = "Availability"
"""What the venue's log calls this module, suffixed with the venue it was loaded into."""


def admit(engine: Any, ts_event: int) -> None:
    """Empty a top-of-book engine's book when it has applied a point stamped later than
    `ts_event`, so that a point stamped `ts_event` is applied rather than skipped. A level-two
    book, which filters nothing by `ts_event`, is never touched: a reset would delete its
    depth."""
    if engine is None or engine.book_type != BookType.L1_MBP:
        return
    book = engine.get_book()
    if ts_event < book.ts_last:
        book.reset()


class Availability(SimulationModule):  # type: ignore[misc]
    """Every quote and print the sleeve is handed reaches the top-of-book venue's book."""

    def pre_process(self, data: Any) -> None:
        """Admit a quote or a print the matching engine would otherwise skip."""
        if isinstance(data, QuoteTick | TradeTick):
            admit(self.exchange.get_matching_engine(data.instrument_id), int(data.ts_event))

    def process(self, ts_now: int) -> None:
        """Nothing: the module acts on points, and an instant with no point applied none."""

    def log_diagnostics(self, logger: Any) -> None:
        """Nothing: what it did is in the fills the venue made."""

    def reset(self) -> None:
        """Nothing: it holds no state."""
