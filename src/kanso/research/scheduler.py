"""The queue: which hypothesis a free lane takes next, and what a stalled run does.

Research is indefinite, so the queue is not a backlog that drains. A hypothesis enters it
and leaves it only by dying — `failed` or `retired` — and everything else that can happen
to one puts it back. A run that stalls goes back. A certificate that fails goes back. A
certificate that *passes* goes back too, because a certificate is a milestone in a
hypothesis's life rather than the end of it: there is always a better version of an idea
that already works.

Order is priority descending, then first in. That is the whole policy, and the priorities
are the whole of its tuning: a hypothesis nobody has touched enters at 0, a stalled one
returns at −1, and one whose baseline would not run returns at −2. So a stall is a demotion
rather than an exclusion, and repeated stalling costs a hypothesis its place at the front
without ever costing it its place in the queue. That decay, the keep rule's noise floor
and the refusal to re-certify unchanged bytes are the three things that bound churn.

Two rows are skipped rather than served: a hypothesis already being researched somewhere,
because one run per hypothesis is the invariant the run table enforces, and a dead one,
which is dropped on sight. Lane `op` is a lane like any other to this module, which is
what keeps an operator working by hand from blocking a daemon lane: their hypothesis is
simply not available to be taken.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from kanso.errors import PreconditionError
from kanso.hyp import active_run, set_status
from kanso.research import records

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "BASELINE_PRIORITY",
    "CERTIFIABLE",
    "DEAD",
    "QUEUED",
    "STALLED",
    "STALL_PRIORITY",
    "QueueItem",
    "Stall",
    "dequeue",
    "drop",
    "enqueue",
    "on_baseline_failed",
    "on_stall",
    "queued",
    "requeue",
]

STALL_PRIORITY: Final = -1
"""Where a stalled run's hypothesis returns to the queue."""

BASELINE_PRIORITY: Final = -2
"""Where a hypothesis whose baseline would not run returns: behind a stalled one, since
its starting point needs an operator rather than another lane."""

DEAD: Final = frozenset({"failed", "retired"})
"""The two statuses that leave the queue. Every other one comes back."""

QUEUED: Final = "queued"
STALLED: Final = "stalled"
CERTIFIABLE: Final = "certifiable"
"""The events this module appends, under the hypothesis id as subject."""


@dataclass(frozen=True)
class QueueItem:
    """One waiting hypothesis, in serving order."""

    hyp_id: str
    priority: int
    enqueued_at: str

    def payload(self) -> dict[str, object]:
        return {"id": self.hyp_id, "priority": self.priority, "enqueued_at": self.enqueued_at}


@dataclass(frozen=True)
class Stall:
    """What a stalled run left behind: a subject worth certifying, or nothing yet."""

    hyp_id: str
    best_sha: str | None
    certifiable: bool
    priority: int

    def payload(self) -> dict[str, object]:
        return {
            "id": self.hyp_id,
            "best_sha": self.best_sha,
            "certifiable": self.certifiable,
            "priority": self.priority,
        }


def enqueue(store: StateStore, hyp_id: str, priority: int = 0) -> QueueItem:
    """Put a hypothesis in the queue, or raise the priority of one already in it.

    Idempotent by design: enqueueing twice keeps the first arrival's place, because a
    second request is an operator saying "this one matters", not "this one is new".
    """
    _alive(store, hyp_id)
    held = _row(store, hyp_id)
    if held is not None:
        if priority > held.priority:
            store.connection.execute(
                "UPDATE queue SET priority = ? WHERE hyp_id = ?", (priority, hyp_id)
            )
            return QueueItem(hyp_id, priority, held.enqueued_at)
        return held
    now = datetime.now(tz=UTC).isoformat()
    store.connection.execute(
        "INSERT INTO queue (hyp_id, priority, enqueued_at) VALUES (?, ?, ?)",
        (hyp_id, priority, now),
    )
    store.event(QUEUED, hyp_id, {"priority": priority})
    return QueueItem(hyp_id, priority, now)


def requeue(store: StateStore, hyp_id: str, priority: int) -> QueueItem:
    """Return a hypothesis to the queue at `priority`, behind everything in that band."""
    drop(store, hyp_id)
    _alive(store, hyp_id)
    now = datetime.now(tz=UTC).isoformat()
    store.connection.execute(
        "INSERT INTO queue (hyp_id, priority, enqueued_at) VALUES (?, ?, ?)",
        (hyp_id, priority, now),
    )
    store.event(QUEUED, hyp_id, {"priority": priority})
    return QueueItem(hyp_id, priority, now)


def drop(store: StateStore, hyp_id: str) -> None:
    """Take a hypothesis out of the queue. Idempotent."""
    store.connection.execute("DELETE FROM queue WHERE hyp_id = ?", (hyp_id,))


def queued(store: StateStore) -> list[QueueItem]:
    """Everything waiting, in the order it will be served."""
    rows = store.connection.execute(
        "SELECT hyp_id, priority, enqueued_at FROM queue ORDER BY priority DESC, queue_id"
    ).fetchall()
    return [QueueItem(str(r["hyp_id"]), int(r["priority"]), str(r["enqueued_at"])) for r in rows]


def dequeue(store: StateStore) -> str | None:
    """The next hypothesis to research, removed from the queue, or `None`.

    A dead hypothesis is dropped on sight and one already being researched is passed
    over and left where it is, so the lane that finishes it finds its place unchanged.
    """
    for item in queued(store):
        status = _status(store, item.hyp_id)
        if status in DEAD:
            drop(store, item.hyp_id)
            continue
        if active_run(store, item.hyp_id) is not None:
            continue
        drop(store, item.hyp_id)
        return item.hyp_id
    return None


def on_stall(ws: Workspace, store: StateStore, hyp_id: str) -> Stall:
    """What happens when a run ends on `stall_k` consecutive non-keeps.

    A `best` this hypothesis has not certified makes it a candidate, and the certification
    run is the next step; either way it is requeued at −1, because a hypothesis leaves the
    queue only by dying. `ws` names the workspace the certification will be run in.
    """
    best, _ = records.best_of(store, hyp_id)
    certifiable = best is not None and best != _certified_sha(store, hyp_id)
    if certifiable:
        set_status(store, hyp_id, "candidate")
        store.event(CERTIFIABLE, hyp_id, {"strategy_sha": best})
        # The certification run belongs here, on this subject, before the requeue.
    store.event(STALLED, hyp_id, {"best_sha": best, "certifiable": certifiable})
    requeue(store, hyp_id, STALL_PRIORITY)
    return Stall(hyp_id, best, certifiable, STALL_PRIORITY)


def on_baseline_failed(store: StateStore, hyp_id: str) -> QueueItem:
    """Requeue a hypothesis whose run could not begin, behind the stalled ones."""
    return requeue(store, hyp_id, BASELINE_PRIORITY)


def _row(store: StateStore, hyp_id: str) -> QueueItem | None:
    found = store.connection.execute(
        "SELECT hyp_id, priority, enqueued_at FROM queue WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    if found is None:
        return None
    return QueueItem(str(found["hyp_id"]), int(found["priority"]), str(found["enqueued_at"]))


def _status(store: StateStore, hyp_id: str) -> str:
    row = store.connection.execute(
        "SELECT status FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    return "" if row is None else str(row["status"])


def _alive(store: StateStore, hyp_id: str) -> None:
    """Refuse to queue something that is not a hypothesis, or is one that is over."""
    status = _status(store, hyp_id)
    if not status:
        raise PreconditionError(
            f"{hyp_id!r} is not a registered hypothesis, so it cannot be queued",
            remedy=f"run `kanso hyp add hypotheses/{hyp_id}/hypothesis.yaml`",
        )
    if status in DEAD:
        raise PreconditionError(
            f"{hyp_id} is {status}, and research does not resume a hypothesis that is over",
            remedy="register a new hypothesis for the idea",
        )


def _certified_sha(store: StateStore, hyp_id: str) -> str | None:
    """The subject of this hypothesis's newest certificate, whatever its verdict."""
    row = store.connection.execute(
        "SELECT strategy_sha FROM certificates WHERE hyp_id = ? ORDER BY created_at DESC LIMIT 1",
        (hyp_id,),
    ).fetchone()
    return None if row is None else str(row["strategy_sha"])
