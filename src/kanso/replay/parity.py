"""Whether the two code paths agree: one target, one range, two sessions, one comparison.

Parity is the claim the whole design rests on — that the strategy a node runs is the strategy
the research runner measured. It is checked by running the same target over the same days on
both paths and comparing the order intents element by element: instant, instrument, side,
quantity, order type and, for an order that names one, price. The first difference is
reported with its index and its field, because a divergence is a bug with a location and
"they differ" is not a place to start looking.

The instant is compared within a tolerance and the rest exactly. Everything a strategy
decides is a function of the data it has seen, so a quantity or a side that differs is a
different decision and there is no tolerance that makes it the same one. The instant is the
one field where a tolerance is meaningful at all, and even that only because an intent is
stamped with the data event's time: the tolerance is there to be set to zero and to say so.

**What each path was fed is compared before what it decided.** Both are handed the window's
points from the catalog, and each session records how many it was released and the digest
of their stream. Two paths released different points — a feed that stopped short, a catalog
that changed between the two runs — can agree or disagree about their orders and neither
says anything about the code, so a difference there is the first divergence, reported
against the stream rather than an intent, and no tolerance applies to it. The digest is what
keeps that check honest without the stream itself: the stream is never written, and two
streams that differ in any point have different digests.

A run whose intents are empty on both paths is identical and says nothing. Parity therefore
reports how many intents it compared, so a gate reading it can tell agreement from silence.

No divergence carries an explanation. One used to: while the node executed against the
engine's own convenience venue, an order larger than a single book state was filled once and
its remainder never revisited, so a quantity lower on the node path had a known cause and was
reported with it. kanso now assembles its own exchange and the two paths fill that order
identically, so the hint would name a cause that no longer exists — which is worse than
silence, because it sends a reader to check the one thing that is no longer wrong. A
divergence is reported as what it is: an index, a field and the two values.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Final

from kanso.replay import record
from kanso.replay import run as runner
from kanso.replay.record import Intent, Session

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["FIELDS", "RELEASED", "STREAM", "Divergence", "Parity", "compare", "parity"]

FIELDS: Final = ("ts_event", "instrument", "side", "qty", "order_type", "price")
"""The order intent, field by field, in the order they are compared."""

RELEASED: Final = "released"
STREAM: Final = "stream_sha256"
"""The two fields of a session that say what its path was fed, compared before any order."""

MISSING: Final = "missing"
"""The field a divergence names when one path stopped submitting and the other did not."""


@dataclass(frozen=True)
class Divergence:
    """The first place two sessions stopped agreeing: in what they were fed, or in an order.

    `index` is the order's position in submission order, and `None` for a divergence in
    the stream, which is one record per session rather than a sequence to index.
    """

    index: int | None
    field: str
    node: object | None
    engine: object | None

    def render(self) -> str:
        """One line naming where the paths parted and what each said."""
        where = "stream" if self.index is None else f"intent {self.index}"
        if self.field == MISSING:
            longer = "node" if self.engine is None else "engine"
            return f"{where}: only the {longer} path submitted one"
        return (
            f"{where}: {self.field} is {self.node!r} on the node path "
            f"and {self.engine!r} on the engine path"
        )


@dataclass(frozen=True)
class Parity:
    """What comparing two sessions found, and everything it compared.

    The sequences travel with the verdict so a reader with a tolerance of its own — a gate
    whose plan chose one — can ask the same question again without running anything: `at`
    re-compares what is already here. What each path was released travels too, as a count
    and a digest; a session written before kanso kept the digest has none, and then the
    count is all there is to compare.
    """

    node: str
    engine: str
    ts_ns: int
    node_orders: tuple[Intent, ...]
    engine_orders: tuple[Intent, ...]
    max_ts_delta_ns: int
    divergence: Divergence | None = None
    node_released: int = 0
    engine_released: int = 0
    node_stream: str | None = None
    engine_stream: str | None = None

    @property
    def identical(self) -> bool:
        """Whether the two paths were fed the same points and submitted the same orders."""
        return self.divergence is None

    @property
    def fed(self) -> Divergence | None:
        """Where the two streams differed, or `None` when both paths were released the same."""
        if self.node_released != self.engine_released:
            return Divergence(None, RELEASED, self.node_released, self.engine_released)
        digests = (self.node_stream, self.engine_stream)
        if None not in digests and self.node_stream != self.engine_stream:
            return Divergence(None, STREAM, self.node_stream, self.engine_stream)
        return None

    @property
    def compared(self) -> int:
        """How many orders the two sequences had in common to compare at all."""
        return min(len(self.node_orders), len(self.engine_orders))

    def at(self, ts_ns: int) -> Parity:
        """The same two sessions judged at another instant tolerance."""
        divergence, widest = compare(self.node_orders, self.engine_orders, ts_ns=ts_ns)
        return replace(self, ts_ns=ts_ns, max_ts_delta_ns=widest, divergence=self.fed or divergence)

    def payload(self) -> dict[str, object]:
        """The result as one JSON object."""
        return {
            "node": self.node,
            "engine": self.engine,
            "ts_ns": self.ts_ns,
            "identical": self.identical,
            "compared": self.compared,
            "node_intents": len(self.node_orders),
            "engine_intents": len(self.engine_orders),
            "node_released": self.node_released,
            "engine_released": self.engine_released,
            "node_stream": self.node_stream,
            "engine_stream": self.engine_stream,
            "max_ts_delta_ns": self.max_ts_delta_ns,
            "divergence": None if self.divergence is None else self.divergence.render(),
        }


def compare(
    node: Sequence[Intent], engine: Sequence[Intent], *, ts_ns: int = 0
) -> tuple[Divergence | None, int]:
    """The first divergence between two intent sequences, and the widest instant apart.

    Compared in submission order: the nth order one path sent is the nth the other must
    have sent. A path that sent fewer diverges at the first one it did not send.
    """
    widest = 0
    for index in range(max(len(node), len(engine))):
        left = node[index] if index < len(node) else None
        right = engine[index] if index < len(engine) else None
        if left is None or right is None:
            return Divergence(index, MISSING, left, right), widest
        delta = abs(left.ts_event - right.ts_event)
        widest = max(widest, delta)
        if delta > ts_ns:
            return Divergence(index, "ts_event", left.ts_event, right.ts_event), widest
        for field in FIELDS[1:]:
            mine, theirs = getattr(left, field), getattr(right, field)
            if mine != theirs:
                return Divergence(index, field, mine, theirs), widest
    return None, widest


def parity(
    ws: Workspace,
    store: StateStore,
    *,
    strategy: str | None = None,
    version: int | None = None,
    hyp: str | None = None,
    sha: str | None = None,
    start: date | None = None,
    end: date | None = None,
    speed: float = 0.0,
    ts_ns: int = 0,
) -> Parity:
    """Replay a target on the node path, then on the engine path, and compare the two.

    The engine run takes its range from the node session that was just written rather than
    resolving it again, so the second path runs over the days the first one actually
    covered even if the catalog grew between them.
    """
    node = runner.run(
        ws,
        store,
        strategy=strategy,
        version=version,
        hyp=hyp,
        sha=sha,
        start=start,
        end=end,
        speed=speed,
        mode=runner.NODE,
    )
    engine = runner.run(
        ws,
        store,
        strategy=strategy,
        version=version,
        hyp=hyp,
        sha=sha,
        start=node.from_,
        end=node.to,
        speed=speed,
        mode=runner.ENGINE,
    )
    return of_sessions(ws, node, engine, ts_ns=ts_ns)


def of_sessions(ws: Workspace, node: Session, engine: Session, *, ts_ns: int = 0) -> Parity:
    """Compare two persisted sessions without running anything: the streams, then the orders."""
    return Parity(
        node=node.session_id,
        engine=engine.session_id,
        ts_ns=ts_ns,
        node_orders=record.intents_of(ws, node.session_id),
        engine_orders=record.intents_of(ws, engine.session_id),
        max_ts_delta_ns=0,
        node_released=node.released,
        engine_released=engine.released,
        node_stream=node.stream_sha256,
        engine_stream=engine.stream_sha256,
    ).at(ts_ns)
