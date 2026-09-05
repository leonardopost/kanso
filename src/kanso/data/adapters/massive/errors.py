"""The four outcomes a Massive request can have, and the exceptions carrying them.

Massive answers several quite different conditions with one byte-identical sentence: the
dataset is not licensed under the plan, the requested range lies wholly outside the
plan's history window, the ticker carries the wrong asset-class prefix, and the key shape
is not one the vendor recognises all produce the same `NOT_ENTITLED` warning. Two of
those are the operator's data plan and two are a malformed request, and telling them
apart matters more here than anywhere else in the adapter: reporting a history floor as
an entitlement failure sends an operator to buy a subscription they already hold.

So the vendor's sentence is never read. The wire is reduced to a `Signal` — did the
source answer with rows, without rows, with a refusal, or by rejecting the request shape
— and meaning is then established by *probing*: asking a second question whose answer
separates the conditions. `kanso.data.adapters.massive.entitlement` does the probing and
this module names the four outcomes it can reach.

* `NOT_ENTITLED` — the plan does not include this dataset for this key. **Non-fatal**: it
  ends one dataset and leaves the rest of the run alone, because a universe usually spans
  several classes and a key rarely entitles them all.
* `BELOW_FLOOR` — the source holds nothing that old. **Non-fatal**: a backfill that
  reaches the floor has finished rather than failed, and clamps to it.
* `EMPTY` — the source is entitled and the range is above the floor, and there is still
  nothing there. Normally a value rather than an exception; raised only where a caller
  required rows, which stops that caller. **Non-fatal**: one silent name in a universe is
  one dataset that yields nothing, and a walk that aborted on it would discard every name
  behind it — including the ones the source serves perfectly.
* `MALFORMED` — kanso or the operator built a request the vendor cannot honour. Fatal:
  every later request of the same shape would fail the same way.

The three non-fatal outcomes are the three that are true of one series and say nothing
about the next. Only a malformed request is fatal, because it is a statement about a shape
rather than about a name, and the next name would be asked the same broken way.

A transport failure — a timeout, a throttle, a gateway error — is none of these four. It
is the absence of an answer rather than an answer, and it carries no outcome.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import ClassVar, Final

from kanso.errors import Exit, KansoError

__all__ = [
    "ERROR_FOR",
    "NON_FATAL",
    "BelowFloorError",
    "EmptyResultError",
    "MalformedRequestError",
    "MassiveError",
    "NotEntitledError",
    "Outcome",
    "TransportError",
]


class Outcome(StrEnum):
    """What a probe established about one request.

    `OK` means the source will serve the range asked for; the other four are the
    conditions the vendor states with a single sentence, separated here by probing.
    """

    OK = "ok"
    NOT_ENTITLED = "not_entitled"
    BELOW_FLOOR = "below_floor"
    EMPTY = "empty"
    MALFORMED = "malformed"


class MassiveError(KansoError):
    """A failure this adapter attributes to a named outcome.

    `outcome` is the outcome the exception carries, or `None` when the failure is not one
    of the four. `fatal` says whether a walk over a universe and a history must stop:
    a dataset the plan excludes and a range older than the source holds are both ordinary
    events during a backfill, and a run that aborted on either would never finish.
    """

    outcome: ClassVar[Outcome | None] = None
    fatal: ClassVar[bool] = True


class NotEntitledError(MassiveError):
    """The plan does not include this dataset for this key, as established by probing."""

    outcome: ClassVar[Outcome | None] = Outcome.NOT_ENTITLED
    fatal: ClassVar[bool] = False

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, Exit.PRECONDITION, remedy)


class BelowFloorError(MassiveError):
    """The requested range lies wholly before the earliest date the source serves.

    `floor` is that date, probed rather than assumed, so the refusal can name it and a
    backfill can clamp to it. A floor is a fact about a source, a series and a day — it
    moves with the calendar where the plan's window rolls — which is why nothing here
    carries one past the day it was measured on.
    """

    outcome: ClassVar[Outcome | None] = Outcome.BELOW_FLOOR
    fatal: ClassVar[bool] = False

    def __init__(self, message: str, remedy: str | None = None, floor: date | None = None) -> None:
        super().__init__(message, Exit.PRECONDITION, remedy)
        self.floor = floor


class EmptyResultError(MassiveError):
    """The source is entitled and above its floor, and served nothing anyway.

    Raised only where a caller required rows. A loader that simply finds no points yields
    none and records the shortfall in its manifest instead: coverage is what was served.

    **Non-fatal**, because it is a fact about one name. A ticker the vendor recognises and
    holds nothing for — a contract that never traded, a listing from yesterday, a name that
    was delisted — is an ordinary member of a large universe, and a walk that stopped at
    the first one would throw away every name behind it to report that one name is quiet.
    """

    outcome: ClassVar[Outcome | None] = Outcome.EMPTY
    fatal: ClassVar[bool] = False

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, Exit.PRECONDITION, remedy)


class MalformedRequestError(MassiveError):
    """The request shape is not one the vendor honours: a wrong prefix, an unknown key."""

    outcome: ClassVar[Outcome | None] = Outcome.MALFORMED

    def __init__(self, message: str, remedy: str | None = None) -> None:
        super().__init__(message, Exit.VALIDATION, remedy)


class TransportError(MassiveError):
    """No answer arrived: a throttle, a gateway error, a timeout, an unreadable body.

    Not one of the four outcomes, because nothing about the data was established. The
    quota is what keeps a throttle rare; there is no retry here, so a caller that sees
    one has a run to repeat rather than a state to reconcile.
    """

    def __init__(self, message: str, remedy: str | None = None, status: int | None = None) -> None:
        super().__init__(message, Exit.ERROR, remedy)
        self.status = status


NON_FATAL: Final[frozenset[Outcome]] = frozenset(
    {Outcome.NOT_ENTITLED, Outcome.BELOW_FLOOR, Outcome.EMPTY}
)
"""The outcomes a walk over a universe and a history survives.

The three that are facts about one series: the plan excludes it, the source is younger than
the range asked for, the source holds nothing for it. Each ends that dataset and no other.
`MALFORMED` is missing because it is a fact about a request shape, and the next series would
be asked the same broken way."""

ERROR_FOR: Final[dict[Outcome, type[MassiveError]]] = {
    Outcome.NOT_ENTITLED: NotEntitledError,
    Outcome.BELOW_FLOOR: BelowFloorError,
    Outcome.EMPTY: EmptyResultError,
    Outcome.MALFORMED: MalformedRequestError,
}
"""The exception each outcome raises as, for a caller that turns one into a failure."""
