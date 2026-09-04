"""Whether the code path that will trade is the code path that was measured.

Everything the framework claims about a strategy is claimed from the research path: one
backtest engine, one window, one ordered set of points. What eventually trades is a node —
an asynchronous live path with a data client, a live clock and an execution client of its
own. The two run the same generated source, but "the same source" is an assumption until
something compares them, and this gate is where the assumption is discharged, before a
certificate is written rather than after a deployment behaves unexpectedly.

**Order intents are what is compared, not results.** A fill is a property of the venue and
a P&L is a property of the fills, so two paths could differ in either without the strategy
having decided anything differently. What the strategy decided is the sequence of orders it
submitted — instant, instrument, side, quantity, order type and, for an order that names
one, price — and a difference anywhere in that sequence is a different decision, at a known
index, in a named field.

**Only the instant carries a tolerance**, in nanoseconds, and the plan chooses it. An
intent is stamped with the data event's time rather than with a clock, because the node
runs on a live clock and the engine on a test clock and a wall-clock stamp could never
match; agreement to the nanosecond is therefore the expected case and the tolerance exists
to be set to zero and to say so. Every other field is compared exactly: there is no
tolerance under which a different quantity is the same quantity.

**The gate runs nothing.** Replaying one target twice over the certification window is the
runner's work; what arrives here is the comparison it produced, re-judged at the tolerance
this plan chose. Two empty sequences are not agreement — a window in which neither path
submitted an order says nothing about whether they would have agreed — so that is recorded
as a skip and no verdict rests on it.
"""

from __future__ import annotations

from typing import ClassVar, Final

from kanso.criteria.context import Gate, GateContext, count, skipped, verdict
from kanso.replay.parity import Parity
from kanso.schemas import GateResult

NO_TOLERANCE: Final = "no instant tolerance was chosen, so no comparison was required"
NO_PARITY: Final = "no replay of the two code paths was supplied, so nothing was compared"
NO_INTENTS: Final = "neither code path submitted an order, so there was nothing to compare"


class _ParityReplay:
    """The live code path and the research code path submitted the same orders."""

    id: ClassVar[str] = "parity_replay"

    def evaluate(self, ctx: GateContext) -> GateResult:
        tolerance = count(ctx, "ts_ns")
        if tolerance is None:
            return skipped(self.id, NO_TOLERANCE)
        if not isinstance(ctx.session, Parity):
            return skipped(self.id, NO_PARITY)
        judged = ctx.session.at(tolerance)
        if not judged.node_orders and not judged.engine_orders:
            return skipped(self.id, NO_INTENTS)
        return verdict(self.id, judged.identical, judged.payload())


gate: Final[Gate] = _ParityReplay()
"""What the toolbox entry `parity_replay` resolves to."""
