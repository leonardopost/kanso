"""The queue: which hypothesis a free lane takes next, and what a stalled run does.

Research is indefinite, so the queue is not a backlog that drains. A hypothesis enters it
and leaves it only when an operator retires it, and everything else that can happen to one
puts it back. A run that stalls goes back. A certificate that fails goes back. A
certificate that *passes* goes back too, because a certificate is a milestone in a
hypothesis's life rather than the end of it: there is always a better version of an idea
that already works.

Order is priority descending, then first in. That is the whole policy, and the priorities
are the whole of its tuning: a hypothesis nobody has touched enters at 0, a stalled one
returns at −1, one whose baseline would not run returns at −2, and one parked returns at
−3. So a stall is a demotion rather than an exclusion, and repeated stalling costs a
hypothesis its place at the front without ever costing it its place in the queue. That
decay, the keep rule's noise floor and the refusal to re-certify unchanged bytes are the
three things that bound churn.

A park is `[research] park_after_stalls` stalls in a row on one best, however the runs
between them were re-seeded or explored (0, the template's, is never), and counted since the
best was last cleared. It is recorded on the stall that completes the spell, and it is derived
again in `requeue` (`parked`) from the newest `stalled` event — parked, on the best the
hypothesis still holds, and no `best_cleared` since — rather than written once there, because
every return to the queue goes through `requeue` and most are not stalls: a lane failing
mid-run puts a hypothesis back at −1, a baseline that would not run at −2, `recover` at
whatever the claim recorded, and a band written only at the stall would be lifted by the next
of them (`docs/backlog.md` row 62). So every return agrees with the stall until a keep moves
the best, a re-pin or `research begin --from-workspace` clears it — a cleared best re-kept on
the same bytes is a new spell — or a stall records no park, under a key of 0 or one above its
spell. A park only reorders: the row stays in the queue, its status, run and best are
untouched, and a lane serves it as soon as nothing ranks above it. Nor does it raise a return
asked for lower still: `recover` puts a claim on a row an operator queued below −3 back where
it stood. `enqueue` is not held to it, so an operator's `queue add --priority` raises a parked
row, or queues one out of the queue at the priority asked, until its next return. A host
composing wakes only a hypothesis in no row (`on_host_composed`) — at 0, until its next
return — so a parked row already in the queue stays at −3.

Three rows are skipped rather than served: a hypothesis already being researched somewhere,
because one run per hypothesis is the invariant the run table enforces; a dead one, which is
dropped on sight; and one the caller says to pass over — a daemon lane's, a baseline killed
for its clock on what a baseline would run on now (`kanso.research.daemon.baseline_unmoved`),
which is left where it is. Lane `op` is a lane like any other to this module, which is
what keeps an operator working by hand from blocking a daemon lane: their hypothesis is
simply not available to be taken.

A stall is also where the search's memory is read. `reseed_after_stalls` consecutive
stalls on the same best say the best is a ridge the climb cannot leave, so the next run
starts from the highest-scoring other keep still aligned under the same pins, `program.md`
among them — or from the stalled run's own base — and the decision rides on the `queued`
passage, where `put_back` and `recover` keep it, rather than on the stall's own event,
where they would not. The best is not touched: a re-seeded run climbs its own ancestry and
replaces the hypothesis's best only by beating it (`records.set_best`).

Between the queue and a run there is a gap: a lane that took a hypothesis holds it, with no
row to show for it, until its baseline has run and the run row exists — and again between
a stall, which ends the run, and this module's decision on where the hypothesis goes. The
queue records the passage — `queued`, `claimed`, `removed`, and the run's own `run_begun` —
so both gaps can be read back: what a dead lane was holding is put back at the priority it
held, at the next start or as soon as a running daemon's supervisor finds that lane dead;
what an operator took out while a lane held it stays out when that
lane fails, and the lane does not begin the run it was about to; and a hypothesis retired
in a lane's hands has its claim closed, so resuming it later does not revive the claim.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, cast

from kanso.errors import PreconditionError
from kanso.hyp import active_run
from kanso.hyp.registry import BEST_CLEARED
from kanso.research import records
from kanso.research.lanes import DEFAULT_LANE
from kanso.research.passages import (
    BEGUN,
    CLAIMED,
    QUEUED,
    REMOVED,
    RESEED_FROM,
    Passage,
    last_passage,
    reseed_of,
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
    "PARK_PRIORITY",
    "QUEUED",
    "REMOVED",
    "RESEED",
    "STALLED",
    "STALL_PRIORITY",
    "QueueItem",
    "Stall",
    "claimed",
    "dequeue",
    "drop",
    "enqueue",
    "hold",
    "last_stall",
    "on_baseline_failed",
    "on_host_composed",
    "on_stall",
    "parked",
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

PARK_PRIORITY: Final = -3
"""Where a parked hypothesis returns, by any path, while its best is the one it parked on:
behind 0, −1 and −2, every band kanso itself returns a hypothesis to, and served as soon as
nothing ranked above it waits."""

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
RESEED: Final = "reseed"
"""The events a stall and a host's new version append beside the passages
`research/passages.py` defines — `QUEUED`, `CLAIMED`, `REMOVED` and the run's `BEGUN` — under
the hypothesis id as subject. `CERTIFIABLE` is the stall's certification child's, appended once
no research gate has refused the best on research data — each passed, or judged nothing — and
the certification window is next."""


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
    reseed_from: str | None = None
    """The sha the next run was told to start from, when this stall decided one."""
    precheck: dict[str, object] | None = None
    """The best's research gates as judged on research data before any certification window
    was read (`kanso.certify.run.Precheck.payload`): the deciding reading's keys, and every
    reading under `gates` — the certification child's wherever one was started; `None` where
    nothing was worth certifying before it."""
    parked: bool = False
    """Whether this stall completed `[research] park_after_stalls` stalls on its best, as its
    `stalled` event records it: the hypothesis returned at `PARK_PRIORITY`, or, where it
    returned nowhere — retired or taken out of the queue while the stall certified — returns
    there once it is queued again and a lane puts it back."""
    stalls_on_best: int | None = None
    """The stalls in a row on this best since it was last cleared, this one included, however
    re-seeded or explored; 0 where the best moved while the stall was decided — a re-pin
    cleared it during the certification — so the stall stands in no spell and parks nothing;
    `None` where the stall recorded no `stalled` event — a hypothesis retired before it."""

    def payload(self) -> dict[str, object]:
        return {
            "id": self.hyp_id,
            "best_sha": self.best_sha,
            "certifiable": self.certifiable,
            "priority": self.priority,
            "verdict": self.verdict,
            "reseed_from": self.reseed_from,
            "precheck": self.precheck,
            "parked": self.parked,
            "stalls_on_best": self.stalls_on_best,
        }


def enqueue(store: StateStore, hyp_id: str, priority: int = 0) -> QueueItem:
    """Put a hypothesis in the queue, or raise the priority of one already in it.

    One a lane has claimed and not yet begun is refused rather than queued behind itself:
    a second row would be claimed by a second lane in the minutes the baseline takes, and a
    hypothesis has one run open at a time. One with a run open is queued and waits — the
    lane that finishes the run finds its place, and a daemon lane passes over it until then,
    which is how an interactive run's hypothesis is handed to the daemon.

    Idempotent by design: enqueueing twice keeps the first arrival's place, because a
    second request is an operator saying "this one matters", not "this one is new".
    """
    _alive(store, hyp_id)
    _unheld(store, hyp_id)
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


def requeue(
    store: StateStore, hyp_id: str, priority: int, *, reseed_from: str | None = None
) -> QueueItem:
    """Return a hypothesis to the queue at `priority`, behind everything in that band — or at
    `PARK_PRIORITY` while it is parked, unless the caller asked for less.

    Parked is the newest `stalled` event's word, read here rather than at the stall
    (`parked`): it recorded a park, the hypothesis's best is still the one it recorded, and
    no `best_cleared` came after it. Every return goes through here — a stall, a lane's
    failure mid-run, a baseline that would not run, a claim recovered — so all of them agree,
    and a keep that moves the best, or a re-pin that clears it, ends the park at the next.
    The `queued` passage records the priority the row was given.

    `reseed_from` rides on the passage: the sha the next run starts from instead of the
    best, read back by `research/passages.py` when that run begins.
    """
    drop(store, hyp_id)
    _alive(store, hyp_id)
    if priority > PARK_PRIORITY and parked(store, hyp_id):
        priority = PARK_PRIORITY
    now = datetime.now(tz=UTC).isoformat()
    store.connection.execute(
        "INSERT INTO queue (hyp_id, priority, enqueued_at) VALUES (?, ?, ?)",
        (hyp_id, priority, now),
    )
    detail: dict[str, object] = {"priority": priority}
    if reseed_from is not None:
        detail[RESEED_FROM] = reseed_from
    store.event(QUEUED, hyp_id, detail)
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


def recover(store: StateStore, lane: str | None = None) -> list[str]:
    """Re-queue every hypothesis a lane claimed and never began, or held after a stall.

    That is what a lane leaves behind when it dies between taking a hypothesis and
    recording its run, or between a stall and the scheduler's decision: no run, no place,
    and the queue's last word on it a claim. Each returns at the priority the claim
    recorded — where it stood in the queue, or where the stall would have put it — or at −3
    while it is parked where it recorded more (`requeue`), in the order the claims were
    made. Nothing else with no run and no place is touched: a hypothesis whose run the
    operator ended looks the same from the tables and is left alone, and so is one the
    operator took out of the queue. A reseed the dead lane was handed and never consumed —
    no run began — rides on to the new passage.

    With `lane`, only what that lane held: what the supervisor puts back when one lane
    dies under a running daemon, where every other lane is alive and working what it
    claimed, and a claim of theirs put back would be taken a second time.
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
        if lane is not None and passage[1].get("lane") != lane:
            continue
        requeue(store, hyp_id, _priority_of(passage), reseed_from=reseed_of(store, hyp_id))
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
    but a lane holds it. That lane lets it go at its next catalog read, or within a second
    when the baseline card is already running — the card is killed — and begins no run
    (`loop.begin`); its failure does not bring the hypothesis back either. A run that had
    already begun is ended with `research end`. Refuses a hypothesis that is neither
    queued nor held.
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


def put_back(store: StateStore, hyp_id: str, lane: str | None = None) -> QueueItem | None:
    """Return a failed lane's hypothesis to the queue, or say why not with `None`.

    Beside the stalled ones when its run is still open, behind them when the lane held it
    without one — a baseline that would not run, or a stall whose certification could not —
    and at −3, behind both, while it is parked: a lane's failure is no reason to lift a park
    (`requeue`).
    Nothing comes back that was retired, or that the operator took out of the queue while
    the lane held it: the lane's failure is not a reason to overrule either, and a retire
    closes the claim so that `hyp resume` does not revive it. Nor does anything come back
    that another lane holds by now: with `lane`, a claim recorded under a different lane is
    that lane's to answer for — the operator took the hypothesis out from under this one,
    added it again and a second lane claimed it while this lane's card was still running,
    and a return here would have the two lanes run it at once.
    """
    if _status(store, hyp_id) in DEAD:
        _release(store, hyp_id, "retired")
        return None
    if active_run(store, hyp_id) is not None:
        return requeue(store, hyp_id, STALL_PRIORITY)
    if claimed(store, hyp_id):
        passage = last_passage(store, hyp_id)
        if lane is not None and passage is not None and passage[1].get("lane") != lane:
            return None
        return on_baseline_failed(store, hyp_id)
    return None


def dequeue(
    store: StateStore,
    lane: str = DEFAULT_LANE,
    passed_over: Callable[[str], bool] | None = None,
) -> str | None:
    """The next hypothesis to research, removed from the queue, or `None`.

    A dead hypothesis is dropped on sight, and one already being researched — a run open,
    or a claim a lane holds with its run still to begin — is passed over and left where it
    is, so the lane that finishes it finds its place unchanged; so is one `passed_over` says
    a lane is not to begin yet. Measured before the second
    reading: a lane claimed a hypothesis, the operator queued it again in the minutes its
    baseline card took, a second lane claimed it too, and the two runs collided on the
    one row a hypothesis may have open.

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
        if active_run(store, item.hyp_id) is not None or claimed(store, item.hyp_id):
            continue
        if passed_over is not None and passed_over(item.hyp_id):
            continue
        with store.transaction():
            if not drop(store, item.hyp_id):
                continue
            store.event(CLAIMED, item.hyp_id, {"lane": lane, "priority": item.priority})
        return item.hyp_id
    return None


def on_stall(ws: Workspace, store: StateStore, hyp_id: str, lane: str = DEFAULT_LANE) -> Stall:
    """What happens when a run ends on `stall_k` consecutive non-keeps.

    A `best` that scored above zero on its objective and has no certificate under the pinned
    plan and the installed engine makes it a candidate, and certification is what happens
    next — here, on those bytes, before anything else takes a lane. A best certified before
    other bytes were is not certified again: the repeat would be refused, and the lane with
    it. Nor is a best at or below zero: every objective in the library measures an edge whose
    zero is none, so research showed no edge and no certificate can show one, and the plan's
    model call and the certification window's backtests would buy a verdict already known.
    The `stalled` event records what the best scored, so the reason is on the record. Nor is
    a best whose search cannot be priced in its objective's units — fewer than two cards
    scored under it behind two or more draws, the cards the family has run and the settings
    it declares tried outside kanso (`kanso.certify.run.unpriced`) —
    which certification would refuse, and the lane with it; the run goes on drawing
    the cards that make it priceable.

    Nor, for the zero rule's reason, is a best a research gate refuses on research data. The
    plan's research gates — the search correction, the gate whose library file `deflates` the
    best's objective; `walk_forward_consistency`; `overfit_probability`, where the plan carries
    it; and `research_plateau`, each at the values the planner chose — read the research
    window, the trials and the measurements their cards kept alone, and a certificate fails
    when any gate that judged fails, so one that refuses the best on research data has decided
    the certificate before the certification window is read. The plan is therefore made here,
    in the lane — the planner's call when none is pinned, the refusal of a pinned plan this
    version cannot run — and the correction and the folds judged on the measurement the best's
    newest card kept, and `overfit_probability` on the trials' stored measurements on that
    card's grid, running nothing, so the plateau, which re-runs the research window once per
    move, is left to the child; both on the workspace as the child would open
    it, `kanso.toml` read again rather than as the lane read it when it started
    (`kanso.certify.run.precheck_at_stall`). A refusal spares the stall: no child is started,
    no certification window is read, the hypothesis stays where it was and goes back to the
    queue at −1, or −3 while parked (`[research] park_after_stalls`). A measurement struck
    under another `[research]` reading than the file's now or another criteria version
    judges nothing here but `overfit_probability`, which reads the trials' stored
    measurements and none of those settings; none at all judges nothing, and nor does a
    snapshot that cannot support a claim, whose failing certificate is how it reaches the
    operator.
    Whatever the lane read, a child it starts runs the research window first and judges every
    research gate again on that run, the plateau last, before it reads the certification
    window, and spares the stall itself when one refuses
    (`kanso.certify.run.certify_at_stall`): that run is the certificate's own, so its readings
    are the ones a certificate records. On a snapshot that cannot support a claim the child
    runs no window at all and writes its failing certificate, and its precheck judges nothing
    and says why. The `stalled` event records the lane's precheck where no child was started
    — a spare, the only lane precheck that keeps a child from starting — and the child's
    wherever one was. A research gate that judged nothing spares nothing. A stall the child
    spares writes no certificate, so the best is not `judged` and a later stall of it starts
    a child again; where it was the plateau that refused, that child runs the research window
    and its moves again to the same refusal (`docs/backlog.md` row 343).

    The verdict says where the hypothesis stands and ends nothing: a pass certifies it and a
    fail returns it to research, and either way it is requeued at −1, or −3 while parked
    (`[research] park_after_stalls`), because the queue is left only when an operator retires
    something.

    The retire is checked twice, before the certification and after it, because a run ends
    before this is called and an operator is free to retire in the seconds a certification
    takes. Certification refuses a retired subject outright, so without the first check
    this raises; without the second, a retire that landed mid-certification would be lost.
    `ws` names the workspace the certification runs in and `lane` is the lane its model
    call is billed to.

    A certification that cannot run at all — no model on the planner's tier, a plan this
    version can no longer honour, both now met in the lane before the precheck, and a fault
    in planning, raised as the refusal the child would have reported — raises out of here
    rather than being swallowed; a fault reading the card judges nothing and leaves the stall
    to the child. The lane that called it records the failure and puts the hypothesis back
    itself, and an operator driving the loop by hand sees the reason instead of a silent
    stall.

    A `queue remove` that lands during the certification is honoured like the retire: the
    hypothesis returns nowhere. Either way an open claim on it is closed.

    `[research] reseed_after_stalls` consecutive stalls on the same best — counted since
    the last reseed — decide that the next run starts elsewhere: from the highest-scoring
    other keep still aligned under the stalled run's pins, `program.md` among them, else
    from that run's base when it differs from the best. The decision is a `reseed` event
    and rides on the `queued` passage.

    `[research] park_after_stalls` stalls in a row on the same best — counted newest first
    over the `stalled` events, this one included, and broken only by a stall on another best,
    a `best_cleared` or a stall decided across one, so neither a reseed nor an exploration
    starts it over, and a stall spared or certified counts like any other — park the
    hypothesis: the `stalled` event records `parked` and `stalls_on_best`, and the hypothesis
    returns at −3, where `requeue` keeps every later return until a keep moves that best, a
    re-pin clears it, or a later stall parks nothing — under a key of 0, or one above its
    spell. 0 is never. A park ends nothing: the status, the best and the row are untouched,
    and a lane serves it once nothing ranked above it waits. A retire or a `queue remove`
    during the certification leaves the park recorded, and the `Stall` says so: the
    hypothesis returns nowhere now, and at −3 once it is queued again and a lane puts it
    back. A re-pin is not refused while this decides — the run it would wait on has ended —
    so the best is read again before the `stalled` event is written, and where it moved or
    was cleared since it was read the stall records `stalls_on_best` 0 and parks nothing: it
    was measured under the scope the re-pin replaced, so it belongs to no spell, and the next
    stall's spell starts after it.
    """
    # Certification reads research; research schedules certification. The import is
    # deferred so the cycle exists only while this function runs.
    from kanso.certify.certificate import judged
    from kanso.certify.child import certify_in_child
    from kanso.certify.plan import read_plan
    from kanso.certify.run import Precheck, pinned_sha, precheck_at_stall, unpriced
    from kanso.env.envelope import engine_version

    if _status(store, hyp_id) in DEAD:
        drop(store, hyp_id)
        _release(store, hyp_id, "retired")
        return Stall(hyp_id, None, False, None, None)
    best, scored = records.best_of(store, hyp_id)
    cleared = _cleared(store, hyp_id)
    certifiable = False
    if best is not None and scored is not None and scored > 0:
        pinned = read_plan(ws, hyp_id)
        open_id = active_run(store, hyp_id)
        stalled = next((r for r in records.runs_of(store, hyp_id) if r.run_id == open_id), None)
        pin = pinned_sha(store, hyp_id, best) if stalled is None else stalled.hypothesis_sha
        objective = records.objective_of(store, pin)
        certifiable = not judged(
            ws,
            store,
            hyp_id,
            strategy_sha=best,
            hypothesis_sha=pin,
            plan_version=None if pinned is None else pinned.plan_version,
            nautilus_version=engine_version(),
        ) and (
            objective is None
            or unpriced(store, hyp_id, objective, records.n_trials(store, hyp_id)) is None
        )
    checked: Precheck | None = None
    if certifiable:
        checked = precheck_at_stall(ws.root, store, hyp_id, cast("str", best), lane=lane)
        certifiable = not checked.spared
    verdict: str | None = None
    cost: dict[str, object] = {}
    if certifiable:
        made = certify_in_child(ws, store, hyp_id, sha=cast("str", best), lane=lane)
        checked = made.precheck
        certifiable = made.certificate is not None
        verdict = None if made.certificate is None else made.certificate.verdict
        cost = {
            "cert_peak_mem_gb": round(made.peak_mem_gb, 3),
            "cert_wall_s": round(made.wall_s, 3),
        }
    checks = None if checked is None else checked.payload()
    after = ws.config.research.park_after_stalls
    held = records.best_of(store, hyp_id)[0] == best and _cleared(store, hyp_id) == cleared
    stalls_on_best = _spell(store, hyp_id, best) + 1 if held else 0
    parks = after > 0 and stalls_on_best >= after
    store.event(
        STALLED,
        hyp_id,
        {
            "best_sha": best,
            "best_metric": scored,
            "certifiable": certifiable,
            "verdict": verdict,
            "precheck": checks,
            "parked": parks,
            "stalls_on_best": stalls_on_best,
            **cost,
        },
    )
    if _status(store, hyp_id) in DEAD:
        drop(store, hyp_id)
        _release(store, hyp_id, "retired")
        return Stall(hyp_id, best, certifiable, None, verdict, None, checks, parks, stalls_on_best)
    if _kind(last_passage(store, hyp_id)) == REMOVED:
        return Stall(hyp_id, best, certifiable, None, verdict, None, checks, parks, stalls_on_best)
    reseed = _reseed(store, hyp_id, best, ws.config.research.reseed_after_stalls)
    item = requeue(store, hyp_id, STALL_PRIORITY, reseed_from=reseed)
    return Stall(
        hyp_id, best, certifiable, item.priority, verdict, reseed, checks, parks, stalls_on_best
    )


def last_stall(store: StateStore, hyp_id: str) -> dict[str, object] | None:
    """The newest stall of a hypothesis as `kanso status` shows it: when, on which best,
    whether a certification followed, its verdict and the precheck that decided it, whether
    it parked the hypothesis and how many stalls in a row on that best it was — `None`
    before the first stall. A `precheck` of `None` is a stall that had nothing worth
    certifying — a best at or below zero, one already certified under the pinned plan and
    the installed engine, a search not yet priceable — or one recorded before stalls judged
    one; a `stalls_on_best` of `None` is one recorded before stalls counted them, which
    parked nothing, and of 0 one whose best moved while it was decided, which stands in no
    spell. `parked` here is what the stall recorded, and stays so after a keep moves
    the best or a re-pin clears it; whether the park still holds is `parked(store, hyp_id)`."""
    row = _newest_stall(store, hyp_id)
    if row is None:
        return None
    detail = json.loads(str(row["detail"]))
    return {
        "at": str(row["ts"]),
        "best_sha": detail.get("best_sha"),
        "certifiable": detail.get("certifiable"),
        "verdict": detail.get("verdict"),
        "precheck": detail.get("precheck"),
        "parked": detail.get("parked") is True,
        "stalls_on_best": detail.get("stalls_on_best"),
    }


def _newest_stall(store: StateStore, hyp_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = store.connection.execute(
        "SELECT ts, detail FROM events WHERE kind = ? AND subject = ?"
        " ORDER BY event_id DESC LIMIT 1",
        (STALLED, hyp_id),
    ).fetchone()
    return row


def parked(store: StateStore, hyp_id: str) -> bool:
    """Whether the hypothesis's returns to the queue go to `PARK_PRIORITY` now — the one
    predicate `requeue` applies and `kanso status` reports: its newest stall since its best
    was last cleared recorded a park, on the best it still holds.

    So a keep that moves the best lifts it, and so does a `best_cleared` — a re-pin whose
    scope or card gates moved, or `research begin --from-workspace` — even when the next
    baseline keeps the same bytes again, since the stalls behind it were measured under the
    scope it replaced. A retired hypothesis is answered like any other: it returns nowhere
    until it is resumed and queued, and then here. It is read at a return and moves no row:
    one already waiting at −3 when a re-pin lifts the park keeps that place until a lane
    serves it or `enqueue` raises it, since `kanso.hyp` re-pins without the scheduler.
    """
    newest = next(_stalls_since_cleared(store, hyp_id), None)
    if newest is None:
        return False
    best = records.best_of(store, hyp_id)[0]
    return newest.get("parked") is True and newest.get("best_sha") == best


def _spell(store: StateStore, hyp_id: str, best: str | None) -> int:
    """The stalls in a row on `best` already recorded: the hypothesis's `stalled` events
    since its best was last cleared, counted newest first while each is on `best`. Only
    another best, a `best_cleared` or a stall decided across a move of the best — recorded
    at 0 — breaks the spell — a `reseed` or an exploration between them does not — and a
    best of `None` is one best."""
    spell = 0
    for detail in _stalls_since_cleared(store, hyp_id):
        if detail.get("best_sha") != best or detail.get("stalls_on_best") == 0:
            break
        spell += 1
    return spell


def _stalls_since_cleared(store: StateStore, hyp_id: str) -> Iterator[dict[str, object]]:
    """The details of the hypothesis's `stalled` events, newest first, back to its newest
    `best_cleared` (`kanso.hyp.registry.BEST_CLEARED`, which `research begin
    --from-workspace` writes too)."""
    rows = store.connection.execute(
        "SELECT kind, detail FROM events WHERE subject = ? AND kind IN (?, ?)"
        " ORDER BY event_id DESC",
        (hyp_id, STALLED, BEST_CLEARED),
    )
    for row in rows:
        if str(row["kind"]) != STALLED:
            return
        detail: dict[str, object] = json.loads(str(row["detail"]))
        yield detail


def _cleared(store: StateStore, hyp_id: str) -> int:
    """The event id of the hypothesis's newest `best_cleared`, 0 before its first."""
    row = store.connection.execute(
        "SELECT MAX(event_id) FROM events WHERE kind = ? AND subject = ?",
        (BEST_CLEARED, hyp_id),
    ).fetchone()
    return int(row[0] or 0)


def _reseed(store: StateStore, hyp_id: str, best: str | None, after: int) -> str | None:
    """Where the next run starts when the last `after` stalls all ended on `best`.

    Counted newest first over the hypothesis's `stalled` events, stopping at the first
    one on another best and at the last `reseed`, so a reseed is decided once per spell
    of stalls rather than at every stall after the first. The alternative is the
    highest-scoring keep under the stalled run's pins whose bytes are not the best's and
    that no drift check marked misaligned — a keep its own run was rewound away from is not
    ground to start from — else the stalled run's own base when it is not the best; with
    neither there is nowhere else to start, and the run starts from the best as before.

    The pins here are all four a run carries — `hypothesis.yaml`, `program.md`, the
    snapshot and the criteria — where the books, and the recent cards and coverage a
    proposer is shown, are selected by three, its earlier cards by the file with the
    snapshot or the criteria moved. A number does not depend on `program.md`, so what a
    strategy earned under another program is still what it earned; where a run starts does,
    since a keep is a file a proposer wrote to one program's rules. Measured in a live
    workspace on 2026-10-12: a hypothesis kept under a temporary program and then under its
    own restored one, and a reseed selecting by three started the next run from a keep the
    restored program forbids.
    """
    if best is None:
        return None
    rows = store.connection.execute(
        "SELECT kind, detail FROM events WHERE subject = ? AND kind IN (?, ?)"
        " ORDER BY event_id DESC LIMIT ?",
        (hyp_id, STALLED, RESEED, after),
    ).fetchall()
    spell = 0
    for row in rows:
        detail = json.loads(str(row["detail"]))
        if str(row["kind"]) != STALLED or detail.get("best_sha") != best:
            break
        spell += 1
    if spell < after:
        return None
    stalled = records.runs_of(store, hyp_id)[-1]  # a best is set by a run, so one exists
    keep = store.connection.execute(
        "SELECT cards.strategy_sha FROM cards JOIN runs ON runs.run_id = cards.run_id"
        " WHERE cards.hyp_id = ? AND runs.hypothesis_sha = ? AND runs.program_sha = ?"
        " AND runs.snapshot_id = ? AND runs.criteria_version = ? AND cards.status = 'keep'"
        " AND cards.aligned = 1 AND cards.strategy_sha != ?"
        " ORDER BY cards.metric DESC, cards.card_id DESC LIMIT 1",
        (
            hyp_id,
            stalled.hypothesis_sha,
            stalled.program_sha,
            stalled.snapshot_id,
            stalled.criteria_version,
            best,
        ),
    ).fetchone()
    if keep is not None:
        alternative, because = (
            str(keep["strategy_sha"]),
            "the best other aligned keep under the pins",
        )
    elif stalled.base_sha != best:
        alternative, because = stalled.base_sha, "the stalled run's own base"
    else:
        return None
    store.event(
        RESEED,
        hyp_id,
        {"from": best, "to": alternative, "stalls": spell, "because": because},
    )
    return alternative


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
    """Requeue a hypothesis whose run could not begin, behind the stalled ones.

    The run never began, so a reseed the queue passage carried was never consumed, and it
    rides on: the decision belongs to the next run that does begin.
    """
    return requeue(store, hyp_id, BASELINE_PRIORITY, reseed_from=reseed_of(store, hyp_id))


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


def _unheld(store: StateStore, hyp_id: str) -> None:
    """Refuse to queue a hypothesis a lane has claimed and not yet begun."""
    if claimed(store, hyp_id):
        raise PreconditionError(
            f"{hyp_id} is already held by a lane whose run is about to begin, so queueing it "
            "would start a second run",
            remedy=f"`kanso research status` shows the lane; wait for the run, or end it with "
            f"`kanso research end {hyp_id}` once it has begun",
        )


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
