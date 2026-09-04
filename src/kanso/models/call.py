"""What one model call is, what it answers with, and the contract a client meets.

A `Call` is already routed: the tier, the thinking effort and the output cap have been
decided, the prompt is split into the half that never changes for a subject and the half
that does, and the answer's shape is fixed. A client's whole job is to put that on its
protocol's wire and bring back an `Answer`; deciding *what* to send, *whether* the answer
is usable and *what to do* when it is not belongs to the router, so a second protocol is
a file that speaks a wire and nothing more.

The split between `system` and `user` is load-bearing rather than stylistic. Providers
cache prompt prefixes by exact bytes, so the stable half is emitted from sorted keys and
carries no clock, no identifier that varies per call and no result of a previous call;
everything that moves goes in the user turn, after the cache breakpoint. A retry reuses
the same `Call` with the validation errors appended to the user turn alone, so the cached
prefix survives the retry that most needs it.

`CallInputs` is what a calling step hands the router. `check` is how a step that knows
more than the schema does — that a construct id must exist in the catalogue, that a
parameter must sit in its range — puts that knowledge on the same retry ladder as a
malformed answer, rather than raising past it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar, Protocol

from kanso.schemas.models import ModelSpec

__all__ = ["Answer", "Call", "CallInputs", "Client"]


@dataclass(frozen=True)
class Call:
    """One fully routed request: what to ask, how hard to think, what shape to answer in."""

    task_class: str
    tier: str
    effort: str
    max_output: int
    system: str
    user: str
    schema: Mapping[str, object]

    def retrying(self, complaints: Sequence[str]) -> Call:
        """The same call with the complaints appended to the user turn.

        The system turn is untouched, so the provider cache that hit on the first attempt
        hits again on the retry.
        """
        listed = "\n".join(f"- {complaint}" for complaint in complaints)
        return replace(
            self,
            user=(
                f"{self.user}\n\n"
                "Your previous answer was rejected. Correct exactly these problems and "
                "answer again with the whole object:\n"
                f"{listed}"
            ),
        )

    def at(self, tier: str) -> Call:
        """The same call escalated to another tier, at the same effort and cap."""
        return replace(self, tier=tier)


@dataclass(frozen=True)
class Answer:
    """One model's reply: the parsed object, what it cost and whether the cache hit.

    `data` is an empty mapping when the reply was not a JSON object at all, which is not
    a special case — an empty object fails every task class's schema and takes the same
    ladder a wrong object takes. `cache_hit` is `None` when the protocol says nothing
    about caching, which is different from saying the cache missed.
    """

    data: Mapping[str, object]
    model: str
    tier: str
    tokens_in: int
    tokens_out: int
    cost: float
    cache_hit: bool | None


@dataclass(frozen=True)
class CallInputs:
    """What a calling step hands the router.

    `subject` is what the call is about — a hypothesis id — and is what "byte-stable for
    a given subject" is stated against. `stable` becomes the system turn and must hold
    only facts that do not change between the calls of one subject; `dynamic` becomes the
    user turn. `check` runs after the schema and returns one complaint per problem, empty
    when the answer is usable.
    """

    subject: str
    stable: Mapping[str, object] = field(default_factory=dict)
    dynamic: Mapping[str, object] = field(default_factory=dict)
    check: Callable[[Mapping[str, object]], Sequence[str]] | None = None


class Client(Protocol):
    """One wire protocol.

    `complete` makes exactly one request and returns what came back. It never retries,
    never escalates and never judges the answer: a reply that arrived is an `Answer`
    whatever it says, and only a request that could not be answered at all raises.
    """

    protocol: ClassVar[str]

    def complete(self, spec: ModelSpec, call: Call) -> Answer: ...
