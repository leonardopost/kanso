"""The queue: which hypothesis a free lane takes next, and what a stalled run does.

Research is indefinite, so the queue is not a backlog that drains. A hypothesis enters it
and leaves it only when an operator retires it, and everything else that can happen to one
puts it back. A run that stalls goes back. A certificate that fails goes back. A
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

Between the queue and a run there is a gap: a lane that took a hypothesis holds it, with no
row to show for it, until its baseline has run and the run row exists — and again between
a stall, which ends the run, and this module's decision on where the hypothesis goes. The
queue records the passage — `queued`, `claimed`, `removed`, and the run's own `run_begun` —
so both gaps can be read back: what a dead lane was holding is put back at the next start,
at the priority it held; what an operator took out while a lane held it stays out when that
lane fails, and the lane does not begin the run it was about to; and a hypothesis retired
in a lane's hands has its claim closed, so resuming it later does not revive the claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from kanso.errors import PreconditionError
from kanso.hyp import active_run, set_status
from kanso.research import records
from kanso.research.lanes import DEFAULT_LANE
from kanso.research.passages import (
    BEGUN,
    CLAIMED,
    QUEUED,
    REMOVED,
    Passage,
    last_passage,
    taken,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "BASELINE_PRIORITY",
    "BEGUN",
    "CERTIFIABLE",
    "CLAIMED",
    "DEAD",
    "QUEUED",
    "REMOVED",
    "STALLED",
    "STALL_PRIORITY",
    "QueueItem",
    "Stall",
    "claimed",
    "dequeue",
    "drop",
    "enqueue",
    "hold",
    "on_baseline_failed",
    "on_host_composed",
    "on_stall",
    "put_back",
    "queued",
    "recover",
    "remove",
    "requeue",
    "taken",
]

STALL_PRIORITY: Final = -1
"""Where a stalled run's hypothesis returns to the queue."""

BASELINE_PRIORITY: Final = -2
"""Where a hypothesis whose baseline would not run returns: behind a stalled one, since
its starting point needs an operator rather than another lane."""

WAKEABLE: Final = frozenset({"classified", "researching", "certified"})
"""The statuses a hypothesis attached to a host may be in and be woken from when the host
composes a new version: not yet begun, in research, or certified against the old one."""

DEAD: Final = frozenset({"retired"})
"""The one status that leaves the queue, and only an operator writes it.

`failed` was here, and the framework wrote it: `n_fail` consecutive failing certificates
ended a hypothesis and no command could bring one back. Whether an idea is worth another
lane is the operator's call, so a failing certificate now escalates and the hypothesis
returns to the queue. The status survives on hypotheses ended under an older version and
means only that: their last certificate failed, and `kanso research queue add` takes them
back."""

STALLED: Final = "stalled"
CERTIFIABLE: Final = "certifiable"
HOST_COMPOSED: Final = "host_composed"
"""The events this module appends beside the passages `research/passages.py` defines —
`QUEUED`, `CLAIMED`, `REMOVED` and the run's `BEGUN` — under the hypothesis id as subject."""


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
    priority: int | None
    verdict: str | None = None
    """`None` where nothing was certified; otherwise the verdict of the certificate the
    stall produced. A `priority` of `None` means the hypothesis did not come back: the
    only way out of the queue is death, and the failure run is the one way to it."""

    def payload(self) -> dict[str, object]:
        return {
            "id": self.hyp_id,
            "best_sha": self.best_sha,
            "certifiable": self.certifiable,
            "priority": self.priority,
            "verdict": self.verdict,
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


def drop(store: StateStore, hyp_id: str) -> bool:
    """Take a hypothesis out of the queue. Idempotent; true only if this call removed it."""
    cursor = store.connection.execute("DELETE FROM queue WHERE hyp_id = ?", (hyp_id,))
    return cursor.rowcount == 1


def queued(store: StateStore) -> list[QueueItem]:
    """Everything waiting, in the order it will be served."""
    rows = store.connection.execute(
        "SELECT hyp_id, priority, enqueued_at FROM queue ORDER BY priority DESC, queue_id"
    ).fetchall()
    return [QueueItem(str(r["hyp_id"]), int(r["priority"]), str(r["enqueued_at"])) for r in rows]


def recover(store: StateStore) -> list[str]:
    """Re-queue every hypothesis a lane claimed and never began, or held after a stall.

    That is what a lane leaves behind when it dies between taking a hypothesis and
    recording its run, or between a stall and the scheduler's decision: no run, no place,
    and the queue's last word on it a claim. Each returns at the priority the claim
    recorded — where it stood in the queue, or where the stall would have put it — in the
    order the claims were made. Nothing else with no run and no place is touched: a
    hypothesis whose run the operator ended looks the same from the tables and is left
    alone, and so is one the operator took out of the queue.
    """
    rows = store.connection.execute(
        "SELECT subject, MAX(event_id) AS last FROM events WHERE kind = ?"
        " GROUP BY subject ORDER BY last",
        (CLAIMED,),
    ).fetchall()
    found: list[str] = []
    for row in rows:
        hyp_id = str(row["subject"])
        passage = last_passage(store, hyp_id)
        if passage is None or not claimed(store, hyp_id) or _row(store, hyp_id) is not None:
            continue
        enqueue(store, hyp_id, _priority_of(passage))
        found.append(hyp_id)
    return found


def claimed(store: StateStore, hyp_id: str) -> bool:
    """Whether a lane holds `hyp_id` with no run to show for it.

    True when the last passage recorded for the hypothesis is a claim — not a run
    beginning, not a return to the queue, not the operator taking it out — and no run is
    active. A retired hypothesis has no claim outstanding whatever the record says.
    """
    if _status(store, hyp_id) in DEAD or active_run(store, hyp_id) is not None:
        return False
    return _kind(last_passage(store, hyp_id)) == CLAIMED


def hold(store: StateStore, hyp_id: str, lane: str, priority: int = STALL_PRIORITY) -> None:
    """Record that `lane` holds `hyp_id` with no run: a stall has just ended the run.

    Between the run's end and `on_stall`'s decision the hypothesis is in no table. A claim
    recorded here means a lane that fails or dies in that gap owes the queue a return, and
    `put_back` and `recover` pay it — `recover` at `priority`, where the stall would have
    put it.
    """
    store.event(CLAIMED, hyp_id, {"lane": lane, "priority": priority})


def remove(store: StateStore, hyp_id: str) -> str:
    """Take a hypothesis out of the queue, or out of a lane's hands before its run begins.

    Returns where it came from: `"queue"` when a row was removed, `"lane"` when none was
    but a lane holds it. That lane refuses to begin the run — a baseline in flight is
    discarded — and its failure does not bring the hypothesis back; a run that had already
    begun is ended with `research end`. Refuses a hypothesis that is neither queued nor
    held.
    """
    passage = last_passage(store, hyp_id)
    if drop(store, hyp_id):
        detail: dict[str, object] = {"from": "queue"}
    elif claimed(store, hyp_id) and passage is not None:
        detail = {"from": "lane", "lane": passage[1].get("lane")}
    else:
        raise PreconditionError(
            f"{hyp_id} is not in the queue, and no lane holds it short of a run",
            remedy="`kanso research status` lists what waits and what runs; a run ends with "
            f"`kanso research end {hyp_id}`",
        )
    store.event(REMOVED, hyp_id, detail)
    return str(detail["from"])


def put_back(store: StateStore, hyp_id: str) -> QueueItem | None:
    """Return a failed lane's hypothesis to the queue, or say why not with `None`.

    Beside the stalled ones when its run is still open, behind them when the lane held it
    without one — a baseline that would not run, or a stall whose certification could not.
    Nothing comes back that was retired, or that the operator took out of the queue while
    the lane held it: the lane's failure is not a reason to overrule either, and a retire
    closes the claim so that `hyp resume` does not revive it.
    """
    if _status(store, hyp_id) in DEAD:
        _release(store, hyp_id, "retired")
        return None
    if active_run(store, hyp_id) is not None:
        return requeue(store, hyp_id, STALL_PRIORITY)
    if claimed(store, hyp_id):
        return on_baseline_failed(store, hyp_id)
    return None


def dequeue(store: StateStore, lane: str = DEFAULT_LANE) -> str | None:
    """The next hypothesis to research, removed from the queue, or `None`.

    A dead hypothesis is dropped on sight and one already being researched is passed
    over and left where it is, so the lane that finishes it finds its place unchanged.

    The removal is the claim, and it is recorded under the lane that made it, with the
    priority the row held, in the one transaction: a lane killed mid-claim leaves either
    the row or the record, never neither. Lanes poll in step, so two of them read the
    same head at once; the one whose delete removed the row has it, and the other, whose
    delete removed nothing, moves on to the next row rather than starting a run that the
    first lane's run would then refuse.
    """
    for item in queued(store):
        status = _status(store, item.hyp_id)
        if status in DEAD:
            drop(store, item.hyp_id)
            continue
        if active_run(store, item.hyp_id) is not None:
            continue
        with store.transaction():
            if not drop(store, item.hyp_id):
                continue
            store.event(CLAIMED, item.hyp_id, {"lane": lane, "priority": item.priority})
        return item.hyp_id
    return None


def on_stall(ws: Workspace, store: StateStore, hyp_id: str, lane: str = DEFAULT_LANE) -> Stall:
    """What happens when a run ends on `stall_k` consecutive non-keeps.

    A `best` this hypothesis has not certified makes it a candidate, and certification is
    what happens next — here, on those bytes, before anything else takes a lane. The
    verdict says where the hypothesis stands and ends nothing: a pass certifies it and a
    fail returns it to research, and either way it is requeued at −1, because the queue is
    left only when an operator retires something.

    The retire is checked twice, before the certification and after it, because a run ends
    before this is called and an operator is free to retire in the seconds a certification
    takes. Certification refuses a retired subject outright, so without the first check
    this raises; without the second, a retire that landed mid-certification would be lost.
    `ws` names the workspace the certification runs in and `lane` is the lane its model
    call is billed to.

    A certification that cannot run at all — no model on the planner's tier, a plan this
    version can no longer honour — raises out of here rather than being swallowed. The
    lane that called it records the failure and puts the hypothesis back itself, and an
    operator driving the loop by hand sees the reason instead of a silent stall.

    A `queue remove` that lands during the certification is honoured like the retire: the
    hypothesis returns nowhere. Either way an open claim on it is closed.
    """
    # Certification reads research; research schedules certification. The import is
    # deferred so the cycle exists only while this function runs.
    from kanso.certify.run import certify

    if _status(store, hyp_id) in DEAD:
        drop(store, hyp_id)
        _release(store, hyp_id, "retired")
        return Stall(hyp_id, None, False, None, None)
    best, _ = records.best_of(store, hyp_id)
    certifiable = best is not None and best != _certified_sha(store, hyp_id)
    verdict: str | None = None
    if certifiable:
        set_status(store, hyp_id, "candidate")
        store.event(CERTIFIABLE, hyp_id, {"strategy_sha": best})
        verdict = certify(ws, store, hyp_id, sha=best, lane=lane).verdict
    store.event(STALLED, hyp_id, {"best_sha": best, "certifiable": certifiable, "verdict": verdict})
    if _status(store, hyp_id) in DEAD:
        drop(store, hyp_id)
        _release(store, hyp_id, "retired")
        return Stall(hyp_id, best, certifiable, None, verdict)
    if _kind(last_passage(store, hyp_id)) == REMOVED:
        return Stall(hyp_id, best, certifiable, None, verdict)
    requeue(store, hyp_id, STALL_PRIORITY)
    return Stall(hyp_id, best, certifiable, STALL_PRIORITY, verdict)


def on_host_composed(ws: Workspace, store: StateStore, host_id: str, version: int) -> list[str]:
    """Put back in the queue every idle hypothesis attached to a host that just composed.

    A host that gains a version is a host its attached constructs have not been measured
    against: their next run pins the new version, so the ones that are idle — neither
    running, nor queued, nor held by a lane — go back in the queue now rather than when an
    operator notices. A run in flight keeps the version it pinned (`docs/constructs.md`),
    and a retired or failed hypothesis is left alone. Returns the ids woken, in id order.
    """
    from kanso.hyp import hypothesis_of

    woken: list[str] = []
    rows = store.connection.execute(
        "SELECT hyp_id, status FROM hypotheses ORDER BY hyp_id"
    ).fetchall()
    for row in rows:
        hyp_id = str(row["hyp_id"])
        if hyp_id == host_id or str(row["status"]) not in WAKEABLE:
            continue
        hyp = hypothesis_of(ws, store, hyp_id)
        if hyp.construct is None or hyp.construct.host != host_id:
            continue
        if (
            active_run(store, hyp_id) is not None
            or _row(store, hyp_id) is not None
            or claimed(store, hyp_id)
        ):
            continue
        store.event(HOST_COMPOSED, hyp_id, {"host": host_id, "version": version})
        enqueue(store, hyp_id)
        woken.append(hyp_id)
    return woken


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


def _kind(passage: Passage | None) -> str | None:
    return None if passage is None else passage[0]


def _priority_of(passage: Passage) -> int:
    """The priority a claim recorded, or the queue's entry priority when it recorded none."""
    value = passage[1].get("priority", 0)
    return value if isinstance(value, int) else 0


def _release(store: StateStore, hyp_id: str, because: str) -> None:
    """Close an open claim on a hypothesis that is not coming back to the queue."""
    passage = last_passage(store, hyp_id)
    if _kind(passage) == CLAIMED and passage is not None:
        lane = passage[1].get("lane")
        store.event(REMOVED, hyp_id, {"from": "lane", "lane": lane, "because": because})


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
            f"{hyp_id} is retired, and research resumes it only when you say so",
            remedy=f"run `kanso hyp resume {hyp_id}`",
        )


def _certified_sha(store: StateStore, hyp_id: str) -> str | None:
    """The subject of this hypothesis's newest certificate, whatever its verdict."""
    row = store.connection.execute(
        "SELECT strategy_sha FROM certificates WHERE hyp_id = ? ORDER BY created_at DESC LIMIT 1",
        (hyp_id,),
    ).fetchone()
    return None if row is None else str(row["strategy_sha"])
