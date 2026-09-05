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

A run whose intents are empty on both paths is identical and says nothing. Parity therefore
reports how many intents it compared, so a gate reading it can tell agreement from silence.

One divergence has a known cause and says so. The venue a node executes against matches an
order once, against the single book state a bar left behind, and never revisits what did not
fit; a backtest matches a working order against the whole sequence of prices it replays that
bar as, so an order too large for one state completes there and not on the node. The node
then holds less than the engine does, and the next exit — which closes the position actually
held — is the smaller of the two. That is the shape this module recognises: a quantity lower
on the node path than on the engine path. It is a hint and not a verdict, because another
fault could produce the same shape, so it names the check that tells them apart rather than
closing the question.
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

__all__ = ["FIELDS", "Divergence", "Parity", "compare", "parity"]

FIELDS: Final = ("ts_event", "instrument", "side", "qty", "order_type", "price")
"""The order intent, field by field, in the order they are compared."""

MISSING: Final = "missing"
"""The field a divergence names when one path stopped submitting and the other did not."""

UNDER_FILLED: Final = (
    "a lower quantity on the node path is usually an order the simulated venue filled only in "
    "part: it matches once, against the one book state a bar left behind, and does not revisit "
    "the remainder, where a backtest matches against every price it replays that bar as. It "
    "shows only where one order is large against the depth a single state offers, so compare "
    "this order's size with the volume of the bar it was sent on before suspecting the strategy."
)
"""What a quantity lower on the node path than on the engine path usually means."""


@dataclass(frozen=True)
class Divergence:
    """The first place two intent sequences stopped agreeing."""

    index: int
    field: str
    node: object | None
    engine: object | None

    @property
    def likely_cause(self) -> str | None:
        """The known cause this divergence has the shape of, or `None` for any other shape.

        Only a quantity lower on the node path is recognised, because that is the shape the
        one known difference between the venues produces. Anything else — a side, an
        instrument, an instant, or a quantity that is *higher* on the node — is left
        unexplained rather than given a cause that may not be its own.
        """
        if self.field != "qty":
            return None
        if not isinstance(self.node, int | float) or not isinstance(self.engine, int | float):
            return None
        return UNDER_FILLED if self.node < self.engine else None

    def render(self) -> str:
        """One line naming where the paths parted and what each said."""
        if self.field == MISSING:
            longer = "node" if self.engine is None else "engine"
            return f"intent {self.index}: only the {longer} path submitted one"
        return (
            f"intent {self.index}: {self.field} is {self.node!r} on the node path "
            f"and {self.engine!r} on the engine path"
        )


@dataclass(frozen=True)
class Parity:
    """What comparing two sessions' order intents found, and both sequences it compared.

    The sequences travel with the verdict so a reader with a tolerance of its own — a gate
    whose plan chose one — can ask the same question again without running anything: `at`
    re-compares what is already here.
    """

    node: str
    engine: str
    ts_ns: int
    node_orders: tuple[Intent, ...]
    engine_orders: tuple[Intent, ...]
    max_ts_delta_ns: int
    divergence: Divergence | None = None

    @property
    def identical(self) -> bool:
        """Whether the two paths submitted the same orders, within the tolerance."""
        return self.divergence is None

    @property
    def compared(self) -> int:
        """How many orders the two sequences had in common to compare at all."""
        return min(len(self.node_orders), len(self.engine_orders))

    def at(self, ts_ns: int) -> Parity:
        """The same two sequences judged at another instant tolerance."""
        divergence, widest = compare(self.node_orders, self.engine_orders, ts_ns=ts_ns)
        return replace(self, ts_ns=ts_ns, max_ts_delta_ns=widest, divergence=divergence)

    def payload(self) -> dict[str, object]:
        """The result as one JSON object.

        `likely_cause` is present only when there is one, so a passing certificate does not
        carry an empty explanation for a divergence it never had.
        """
        document: dict[str, object] = {
            "node": self.node,
            "engine": self.engine,
            "ts_ns": self.ts_ns,
            "identical": self.identical,
            "compared": self.compared,
            "node_intents": len(self.node_orders),
            "engine_intents": len(self.engine_orders),
            "max_ts_delta_ns": self.max_ts_delta_ns,
            "divergence": None if self.divergence is None else self.divergence.render(),
        }
        cause = None if self.divergence is None else self.divergence.likely_cause
        if cause is not None:
            document["likely_cause"] = cause
        return document


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
    """Compare two persisted sessions without running anything."""
    left = record.intents_of(ws, node.session_id)
    right = record.intents_of(ws, engine.session_id)
    divergence, widest = compare(left, right, ts_ns=ts_ns)
    return Parity(
        node=node.session_id,
        engine=engine.session_id,
        ts_ns=ts_ns,
        node_orders=left,
        engine_orders=right,
        max_ts_delta_ns=widest,
        divergence=divergence,
    )
