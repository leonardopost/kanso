"""The vocabulary an attached construct speaks, and the registry the sleeve reads it from.

A sleeve is a whole strategy. A *modifier* is a construct attached to one — a filter, an
overlay or an exit rule — that changes what the sleeve does without editing it. The two
halves meet here:

* `HookContext` is everything a modifier is told about the moment it is consulted: the
  order under consideration, the position it would change, the capital behind it, and the
  last data event of each kind. It carries `ts_event`, never a wall clock;
* `Decision` is everything a modifier may say back. Each construct owns exactly one part
  of it — a filter answers `allow`, an overlay answers `scale` and `hedges`, an exit rule
  answers `exit` — and `Decision.neutral(construct)` is the identity for that part: the
  answer that leaves the host exactly as it was;
* the registry maps a host strategy to the modifiers attached to it, so the sleeve can
  consult them synchronously, inside the call that is about to place an order, rather than
  through the message bus, which would answer after the fact.

Nothing here imports the engine. The registry is keyed by the identity of the message bus
the components share, because there is exactly one of those per backtest engine or trading
node and every component of that engine holds a reference to it: two engines alive in one
process therefore keep separate registries, and a key cannot outlive the components that
would answer under it. Entries are weak, so a modifier that is dropped without stopping
leaves nothing behind.
"""

from __future__ import annotations

import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable

from kanso.errors import ValidationError

FILTER: Final = "filter"
"""A conditioning rule that gates a host's entries."""

OVERLAY: Final = "overlay"
"""An exposure modification: scaling, hedge legs, or both."""

EXIT: Final = "exit"
"""Exit logic added to a host's: stops, targets, time exits, trailing rules."""

MODIFIER_CONSTRUCTS: Final = (FILTER, OVERLAY, EXIT)
"""The constructs that attach to a running sleeve as an engine actor."""

_ANSWERS: Final[dict[str, tuple[str, ...]]] = {
    FILTER: ("allow",),
    OVERLAY: ("scale", "hedges"),
    EXIT: ("exit",),
}
"""The `Decision` fields each construct owns; every other field must be left unset."""


@dataclass(frozen=True)
class Hedge:
    """One hedge leg an overlay asks for: an instrument and a signed quantity.

    A positive quantity buys, a negative quantity sells. The instrument is written the way
    the engine writes it, `SYMBOL.VENUE`, and must already be known to the host's cache.
    """

    instrument: str
    qty: float


@dataclass(frozen=True)
class HookContext:
    """What a hook and `evaluate` are told about the moment they are consulted.

    `ts_event` is the economic reference time of the last data event the host handled —
    the only clock a strategy or a modifier may read, because the engine runs a test clock
    in a backtest and a live clock in a node, so anything else breaks replay parity.
    """

    instrument_id: str
    ts_event: int
    side: str
    qty: float
    price: float | None
    order_type: str
    position_qty: float
    capital: float
    last_bar: object | None
    last_quote: object | None
    last_trade: object | None
    host_strategy_id: str | None
    cache: object


@dataclass(frozen=True)
class Decision:
    """A modifier's answer. Each construct sets its own fields and leaves the rest unset.

    `allow` belongs to a filter, `scale` and `hedges` to an overlay, `exit` to an exit
    rule. An unset field is silence, not a "no": the sleeve composes several modifiers by
    taking every filter's `allow`, the product of every overlay's `scale`, the union of
    their `hedges`, and whether any exit rule says `exit`.
    """

    allow: bool | None = None
    scale: float | None = None
    hedges: tuple[Hedge, ...] | None = None
    exit: bool | None = None

    def __post_init__(self) -> None:
        if self.hedges is not None and not isinstance(self.hedges, tuple):
            object.__setattr__(self, "hedges", tuple(self.hedges))

    @classmethod
    def neutral(cls, construct: str) -> Decision:
        """The identity for a construct: the answer that changes nothing about the host."""
        if construct == FILTER:
            return cls(allow=True)
        if construct == OVERLAY:
            return cls(scale=1.0, hedges=())
        if construct == EXIT:
            return cls(exit=False)
        raise ValidationError(
            f"construct: {construct!r} has no attachable decision; "
            f"one of {', '.join(MODIFIER_CONSTRUCTS)} was expected"
        )

    def check(self, construct: str) -> Decision:
        """Refuse an answer to a question the construct was not asked.

        A filter that returns a `scale`, or an overlay that returns an `exit`, is a
        modifier wired to the wrong construct; letting it through would quietly change a
        host in a way the construct's objective never measures.
        """
        answers = _ANSWERS.get(construct)
        if answers is None:
            raise ValidationError(
                f"construct: {construct!r} has no attachable decision; "
                f"one of {', '.join(MODIFIER_CONSTRUCTS)} was expected"
            )
        foreign = [
            name
            for name in ("allow", "scale", "hedges", "exit")
            if name not in answers and getattr(self, name) is not None
        ]
        if foreign:
            raise ValidationError(
                f"decision: a {construct} construct answers "
                f"{', '.join(answers)} and set {', '.join(foreign)} as well"
            )
        if self.scale is not None and not 0.0 <= self.scale <= 1.0:
            raise ValidationError(f"decision.scale: {self.scale} is outside [0, 1]")
        return self


@runtime_checkable
class Modifier(Protocol):
    """What the sleeve needs of an attached construct: its kind and its answer."""

    construct: str

    def evaluate(self, ctx: HookContext) -> Decision:
        """The construct's decision for this moment."""
        ...


@dataclass
class _Attached:
    """The modifiers attached to one host strategy, held weakly."""

    refs: list[weakref.ReferenceType[Modifier]] = field(default_factory=list)

    def live(self) -> tuple[Modifier, ...]:
        alive: list[Modifier] = []
        keep: list[weakref.ReferenceType[Modifier]] = []
        for ref in self.refs:
            modifier = ref()
            if modifier is not None:
                alive.append(modifier)
                keep.append(ref)
        self.refs = keep
        return tuple(alive)


_REGISTRY: dict[tuple[int, str], _Attached] = {}


def _key(bus: object, host: str) -> tuple[int, str]:
    return (id(bus), host)


def register_modifier(bus: object, host: str, modifier: Modifier) -> None:
    """Attach a modifier to a host strategy for the lifetime of one engine.

    `bus` is the message bus the host and the modifier share; `host` is the host's
    `StrategyId`. Registering the same modifier twice is a no-op, so a restarted component
    does not answer twice.
    """
    attached = _REGISTRY.setdefault(_key(bus, host), _Attached())
    if any(existing is modifier for existing in attached.live()):
        return
    attached.refs.append(weakref.ref(modifier))


def deregister_modifier(bus: object, host: str, modifier: Modifier) -> None:
    """Detach a modifier. Detaching one that is not attached is a no-op."""
    key = _key(bus, host)
    attached = _REGISTRY.get(key)
    if attached is None:
        return
    attached.refs = [ref for ref in attached.refs if ref() is not None and ref() is not modifier]
    if not attached.refs:
        del _REGISTRY[key]


def modifiers_for(
    bus: object, hosts: str | Sequence[str], construct: str | None = None
) -> tuple[Modifier, ...]:
    """The modifiers attached to a host, in attachment order, optionally of one construct.

    Several names may be given for the same host, because a sleeve answers to more than
    one: the engine rewrites a strategy's `StrategyId` when it is added to a trader unless
    the config pins an `order_id_tag`, so a modifier composed against the sleeve's class
    name and one composed against its final id must both find it.
    """
    names = (hosts,) if isinstance(hosts, str) else tuple(hosts)
    found: list[Modifier] = []
    for host in names:
        key = _key(bus, host)
        attached = _REGISTRY.get(key)
        if attached is None:
            continue
        live = attached.live()
        if not attached.refs:
            del _REGISTRY[key]
        for modifier in live:
            if construct is not None and modifier.construct != construct:
                continue
            if not any(modifier is seen for seen in found):
                found.append(modifier)
    return tuple(found)
