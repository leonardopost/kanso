"""The queue: who is served next, who is passed over, and what a stall leaves behind.

The queue is the only thing standing between "many hypotheses" and "a few lanes", so its
whole behaviour is order and exclusion: priority then arrival, a dead hypothesis dropped,
a busy one skipped and left where it was.
"""

from __future__ import annotations

import errno
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kanso import hyp
from kanso.certify import certificate as certificates
from kanso.certify import child
from kanso.certify import plan as plans
from kanso.certify.plan import read_plan
from kanso.certify.run import precheck, precheck_at_stall
from kanso.data import snapshot as snapshots
from kanso.errors import Exit, KansoError, PreconditionError
from kanso.hyp import set_status, show
from kanso.hyp.registry import BEST_CLEARED, REPINNED, RESCOPED
from kanso.inbox import unread
from kanso.nautilus import backtest
from kanso.research import explore, loop, passages, records, scheduler
from kanso.research.loop import BEGUN
from kanso.schemas import RunRecord
from kanso.state import StateStore, usable
from kanso.workspace import Workspace
from tests.certify.test_precheck import TIGHT, WIDE, measurement, searched
from tests.certify.test_run import (
    CERT_GATES,
    SEARCHED,
    a_card,
    a_manifest,
    snapshot_id,
    write_plan,
)

from .conftest import (
    ATTACHED_HOST,
    DOCUMENT,
    FLAT,
    OVERLAY,
    REVERTING,
    WEAK,
    classify,
    document,
)
from .mocked import Recorder, fresh_cursors, scripted, tuned  # noqa: F401
from .test_attached import compose_host

JANUARY = REVERTING.replace(
    b"    def on_bar(self, bar) -> None:\n        self.closes.append",
    b"    def on_bar(self, bar) -> None:\n"
    b"        if bar.ts_event >= 1_706_745_600_000_000_000:  # 2024-02-01, UTC\n"
    b"            return\n"
    b"        self.closes.append",
)
"""The reverter, trading January alone: it earns in every research fold and stands still over
the certification window, so its research gates pass, a stall certifies it, and its
certificate fails. Bytes that never trade fail their folds on research data, and a stall
spares them before any certification window is read."""


def register(ws: Workspace, store: StateStore, hyp_id: str) -> str:
    """A second, third, … classified hypothesis beside the demo one."""
    return classify(ws, store, document(id=hyp_id))


def open_run(store: StateStore, hyp_id: str, lane: str = "op") -> RunRecord:
    """An active run record, without the cost of a baseline card."""
    sha = store.put_blob(f"{hyp_id}-{lane}".encode())
    return records.insert(
        store,
        RunRecord(
            run_id=f"run-{hyp_id}-{lane}",
            hyp_id=hyp_id,
            tag="20240101-1",
            lane=lane,
            dir=f"runs/{lane}/{hyp_id}",
            base_sha=sha,
            hypothesis_sha=sha,
            program_sha=sha,
            snapshot_id="snap",
            criteria_version="0.1.0",
            card_budget_s=60.0,
            baseline_wall_s=1.0,
            baseline_peak_mem_gb=1.0,
            started_at=records.now(),
        ),
    )


def certificate(store: StateStore, hyp_id: str, sha: str, verdict: str = "pass") -> None:
    """One certificate row, as `cert run` will write it."""
    store.connection.execute(
        "INSERT INTO certificates (hyp_id, strategy_sha, plan_version, nautilus_version,"
        " snapshot_id, criteria_version, n_trials, verdict, path, created_at)"
        " VALUES (?, ?, 1, '1.231.0', 'snap', '0.1.0', 1, ?, 'certificates/x.yaml', ?)",
        (hyp_id, sha, verdict, datetime.now(tz=UTC).isoformat()),
    )


def ids(store: StateStore) -> list[str]:
    return [item.hyp_id for item in scheduler.queued(store)]


def test_the_queue_serves_priority_first_then_arrival(ws: Workspace, store: StateStore) -> None:
    first = classify(ws, store, DOCUMENT)
    second = register(ws, store, "demo_two")
    third = register(ws, store, "demo_three")

    scheduler.enqueue(store, first)
    scheduler.enqueue(store, second)
    scheduler.enqueue(store, third, priority=5)

    assert ids(store) == [third, first, second]
    assert scheduler.dequeue(store) == third
    assert scheduler.dequeue(store) == first
    assert scheduler.dequeue(store) == second
    assert scheduler.dequeue(store) is None


def test_enqueueing_twice_keeps_the_place_and_may_only_raise_the_priority(
    ws: Workspace, store: StateStore
) -> None:
    first = classify(ws, store, DOCUMENT)
    second = register(ws, store, "demo_two")
    scheduler.enqueue(store, first)
    scheduler.enqueue(store, second)

    assert scheduler.enqueue(store, first).priority == 0
    assert ids(store) == [first, second]

    raised = scheduler.enqueue(store, first, priority=9)

    assert raised.priority == 9
    assert ids(store) == [first, second]


def test_requeueing_lands_behind_everything_already_in_the_band(
    ws: Workspace, store: StateStore
) -> None:
    first = classify(ws, store, DOCUMENT)
    second = register(ws, store, "demo_two")
    scheduler.enqueue(store, first, priority=-1)
    scheduler.enqueue(store, second, priority=-1)

    scheduler.requeue(store, first, -1)

    assert ids(store) == [second, first]


def test_a_dead_hypothesis_is_dropped_on_sight(ws: Workspace, store: StateStore) -> None:
    first = classify(ws, store, DOCUMENT)
    second = register(ws, store, "demo_two")
    scheduler.enqueue(store, first)
    scheduler.enqueue(store, second)
    set_status(store, first, "retired")

    assert scheduler.dequeue(store) == second
    assert ids(store) == []


def test_a_hypothesis_being_researched_is_passed_over_and_left_in_the_queue(
    ws: Workspace, store: StateStore
) -> None:
    """The interactive lane never blocks a daemon lane: its subject is simply not free."""
    busy = classify(ws, store, DOCUMENT)
    free = register(ws, store, "demo_two")
    scheduler.enqueue(store, busy)
    scheduler.enqueue(store, free)
    open_run(store, busy, lane="op")

    assert scheduler.dequeue(store) == free
    assert ids(store) == [busy]


def test_two_lanes_reaching_for_the_same_head_at_once_do_not_both_get_it(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lanes poll in step. Between this lane's look and its take, another lane takes it."""
    contested = classify(ws, store, DOCUMENT)
    free = register(ws, store, "demo_two")
    scheduler.enqueue(store, contested)
    scheduler.enqueue(store, free)
    looked = scheduler.active_run

    def other_lane_takes_it_first(s: StateStore, hyp_id: str) -> RunRecord | None:
        monkeypatch.setattr(scheduler, "active_run", looked)
        with StateStore(ws.path("state.db")) as other:
            usable(other, ws.path("state.db"))
            assert scheduler.dequeue(other) == contested
        return looked(s, hyp_id)

    monkeypatch.setattr(scheduler, "active_run", other_lane_takes_it_first)

    assert scheduler.dequeue(store) == free
    assert ids(store) == []


def test_a_lane_whose_take_removes_nothing_moves_on_without_claiming(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other lane takes the head after this lane found it unclaimed and before its
    delete: the delete removes nothing, so this lane records no claim and takes the next."""
    contested = classify(ws, store, DOCUMENT)
    free = register(ws, store, "demo_two")
    scheduler.enqueue(store, contested)
    scheduler.enqueue(store, free)
    looked = scheduler.claimed

    def other_lane_takes_it_after_the_look(s: StateStore, hyp_id: str) -> bool:
        held = looked(s, hyp_id)
        monkeypatch.setattr(scheduler, "claimed", looked)
        with StateStore(ws.path("state.db")) as other:
            usable(other, ws.path("state.db"))
            assert scheduler.dequeue(other, "l2") == contested
        return held

    monkeypatch.setattr(scheduler, "claimed", other_lane_takes_it_after_the_look)

    assert scheduler.dequeue(store, "l1") == free
    assert ids(store) == []
    claims = store.events(kind=scheduler.CLAIMED)
    assert [(event.subject, event.detail["lane"]) for event in claims] == [
        (contested, "l2"),
        (free, "l1"),
    ]


def test_a_host_that_composes_wakes_its_idle_attached_hypotheses(
    ws: Workspace, store: StateStore
) -> None:
    """Idle means neither running, nor queued, nor held; everything else keeps its place."""
    compose_host(ws, store, sized=True)
    idle = classify(ws, store, OVERLAY)
    running = classify(ws, store, {**OVERLAY, "id": "demo_running"})
    open_run(store, running, lane="l1")
    waiting = classify(ws, store, {**OVERLAY, "id": "demo_waiting"})
    scheduler.enqueue(store, waiting, priority=scheduler.STALL_PRIORITY)
    held = classify(ws, store, {**OVERLAY, "id": "demo_held"})
    scheduler.enqueue(store, held)
    assert scheduler.dequeue(store, "l2") == held
    retired = classify(ws, store, {**OVERLAY, "id": "demo_retired"})
    set_status(store, retired, "retired")
    classify(ws, store, DOCUMENT)

    assert scheduler.on_host_composed(ws, store, ATTACHED_HOST, 2) == [idle]

    assert [(item.hyp_id, item.priority) for item in scheduler.queued(store)] == [
        (idle, 0),
        (waiting, scheduler.STALL_PRIORITY),
    ]
    woke = store.events(kind=scheduler.HOST_COMPOSED)
    assert [(event.subject, event.detail) for event in woke] == [
        (idle, {"host": ATTACHED_HOST, "version": 2})
    ]
    assert scheduler.on_host_composed(ws, store, ATTACHED_HOST, 3) == [], "nothing idle is left"
    assert scheduler.on_host_composed(ws, store, "another_host", 1) == []


def test_only_a_registered_and_living_hypothesis_may_be_queued(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)

    with pytest.raises(PreconditionError, match="not a registered hypothesis"):
        scheduler.enqueue(store, "nobody")

    set_status(store, hyp_id, "failed")
    assert scheduler.enqueue(store, hyp_id).hyp_id == hyp_id, (
        "a hypothesis an older kanso ended comes back; only an operator ends one now"
    )

    scheduler.drop(store, hyp_id)
    set_status(store, hyp_id, "retired")
    with pytest.raises(PreconditionError, match="resumes it only when you say so"):
        scheduler.enqueue(store, hyp_id)


def test_dropping_a_hypothesis_that_is_not_queued_does_nothing(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)

    scheduler.drop(store, hyp_id)

    assert ids(store) == []


def test_a_stall_with_an_uncertified_best_certifies_it_and_requeues_it(
    ws: Workspace, store: StateStore
) -> None:
    """The whole of the stall path: candidate, certificate, then back in the queue."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.certifiable is True
    assert stall.verdict == "pass", "the saw-tooth is as tradable past the embargo as before it"
    assert stall.priority == scheduler.STALL_PRIORITY
    assert show(ws, store, hyp_id).status == "certified"  # type: ignore[union-attr]
    assert ids(store) == [hyp_id], "a certificate is a milestone, not the end of the queue"
    kinds = [event.kind for event in store.events(subject=hyp_id)]
    assert scheduler.CERTIFIABLE in kinds and scheduler.STALLED in kinds


def test_a_stall_whose_certificate_fails_returns_the_hypothesis_to_research(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, JANUARY)
    a_card(ws, store, JANUARY)
    write_plan(ws)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.verdict == "fail", "bytes that stop trading earn nothing out of sample"
    assert stall.priority == scheduler.STALL_PRIORITY
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert ids(store) == [hyp_id]
    assert not unread(store), "one failure is not yet an escalation"


def test_a_stall_whose_certificate_fails_comes_back_to_the_queue(
    ws: Workspace, store: StateStore
) -> None:
    """A failing verdict is a result, not a sentence. Only an operator ends a hypothesis."""
    hyp_id = classify(ws, store, DOCUMENT, JANUARY)
    a_card(ws, store, JANUARY)
    write_plan(ws)
    scheduler.enqueue(store, hyp_id)

    stall = scheduler.on_stall(tuned(ws, n_fail=1), store, hyp_id)

    assert stall.verdict == "fail"
    assert stall.priority == scheduler.STALL_PRIORITY
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert ids(store) == [hyp_id], "back in the queue, so a free lane takes it again"
    assert [entry.kind for entry in unread(store)] == ["cert_failed"]


def test_a_retire_that_lands_during_the_certification_is_still_honoured(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run ended before the stall, so the operator is free to retire while it certifies.

    Requeueing one anyway would raise out of `_alive` and take the lane down with it, so
    the retire is checked on the far side of the certification as well as the near side.
    """
    hyp_id = classify(ws, store, DOCUMENT, JANUARY)
    a_card(ws, store, JANUARY)
    write_plan(ws)
    scheduler.enqueue(store, hyp_id)
    real = child.certify_in_child

    def retiring(*args: Any, **kwargs: Any) -> Any:
        made = real(*args, **kwargs)
        hyp.retire(ws, store, hyp_id)
        return made

    monkeypatch.setattr(child, "certify_in_child", retiring)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.verdict == "fail", "the certification ran and its verdict stands"
    assert stall.priority is None
    assert ids(store) == []
    assert show(ws, store, hyp_id).status == "retired"  # type: ignore[union-attr]


def test_a_hypothesis_that_keeps_failing_keeps_getting_lanes(
    ws: Workspace, store: StateStore
) -> None:
    """The lane never runs out. Research is indefinite, and this is where that is true.

    Five stalls in a row, each certifying and each failing. Before, the third ended the
    hypothesis and the queue dropped it; a lane that came free afterwards had nothing of
    it to take, and no command anywhere could give it back.
    """
    hyp_id = classify(ws, store, DOCUMENT, JANUARY)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])  # five trials behind it by the last
    policy = tuned(ws, n_fail=3)  # read by the child that certifies, so it is on disk

    for attempt in range(1, 6):
        # Each attempt's keep beats the last, as a keep beats its run's base: an equal
        # metric would leave the hypothesis's best — and the certificate — where it was.
        a_card(
            ws,
            store,
            JANUARY + f"# attempt {attempt}\n".encode(),
            seq=attempt,
            metric=float(attempt),
        )
        scheduler.enqueue(store, hyp_id)
        stall = scheduler.on_stall(policy, store, hyp_id)
        assert stall.verdict == "fail"
        assert ids(store) == [hyp_id], f"still queued after {attempt} failing certificates"

    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert [entry.kind for entry in unread(store)] == ["cert_failed"], "escalated at 3, not 5"


def test_a_retired_hypothesis_is_the_one_thing_a_stall_leaves_out(
    ws: Workspace, store: StateStore
) -> None:
    """The operator retired it mid-certification, and the scheduler honours that."""
    hyp_id = classify(ws, store, DOCUMENT, FLAT)
    a_card(ws, store, FLAT)
    write_plan(ws)
    scheduler.enqueue(store, hyp_id)
    set_status(store, hyp_id, "retired")

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.priority is None
    assert ids(store) == []


@pytest.mark.parametrize("metric", [0.0, -1.54])
def test_a_stall_on_a_best_that_proved_no_edge_is_not_certified(
    ws: Workspace, store: StateStore, metric: float
) -> None:
    """A best at or below zero on its own objective showed no edge in research.

    Every objective in the library measures an edge whose zero is none, so a certificate
    cannot find what research did not: the plan's model call and the certification window's
    backtests would be spent on a verdict already known. Measured on a live workspace, a
    stall on a five-second ladder whose best was its seed at -1.54 held a lane for over
    ninety minutes certifying it, while another hypothesis waited in the queue.
    """
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING, metric=metric)
    write_plan(ws)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.certifiable is False
    assert stall.verdict is None
    assert stall.best_sha is not None
    assert stall.priority == scheduler.STALL_PRIORITY
    assert show(ws, store, hyp_id).status not in ("candidate", "certified")  # type: ignore[union-attr]
    assert store.events(kind=scheduler.CERTIFIABLE) == []
    assert store.connection.execute("SELECT COUNT(*) FROM certificates").fetchone()[0] == 0
    assert ids(store) == [hyp_id], "back to research, as any stall"
    (stalled,) = store.events(kind=scheduler.STALLED)
    assert stalled.detail["best_metric"] == metric, "the event says what the best scored"


def refused(*_: object, **__: object) -> Any:
    raise AssertionError("a spared stall started a certification or read its window")


def spared(ws: Workspace, store: StateStore, hyp_id: str, stall: scheduler.Stall) -> None:
    """What a stall spared by its search correction leaves: no certificate, no `certifiable`,
    the hypothesis researching as it was and back in the queue at −1, and the precheck that
    refused it on its `stalled` event."""
    assert (stall.certifiable, stall.verdict, stall.priority) == (
        False,
        None,
        scheduler.STALL_PRIORITY,
    )
    assert stall.precheck is not None
    assert (stall.precheck["judged"], stall.precheck["pass"]) == (True, False)
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert store.events(kind=scheduler.CERTIFIABLE) == []
    assert store.connection.execute("SELECT COUNT(*) FROM certificates").fetchone()[0] == 0
    assert ids(store) == [hyp_id], "back to research, as any stall"
    (stalled,) = store.events(kind=scheduler.STALLED)
    assert stalled.detail["precheck"] == stall.precheck
    assert stalled.detail["certifiable"] is False


def test_a_stall_whose_best_fails_its_search_correction_on_research_data_is_spared(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The correction reads research alone and a certificate is a conjunction, so a best it
    refuses would fail certification whatever the window showed: the lane judges it from the
    measurement the card kept, starts no child and reads no window."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    searched(ws, store, *WIDE)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    monkeypatch.setattr(child, "certify_in_child", refused)
    monkeypatch.setattr(backtest, "window_chunks", refused)

    stall = scheduler.on_stall(ws, store, hyp_id)

    spared(ws, store, hyp_id, stall)
    assert stall.precheck is not None
    assert stall.precheck["gate"] == "deflated_sharpe" and stall.precheck["measured"] is False


def test_a_stall_whose_best_s_folds_do_not_recur_is_spared_in_the_lane(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`walk_forward_consistency` reads the research folds alone, so a best positive in two of
    four folds under a plan asking for four is refused from the measurement the card kept: no
    child, no window, back in the queue at −1, and every reading on the `stalled` event."""
    from tests.certify.test_precheck import EVERY_FOLD, FICKLE

    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store, run=FICKLE))
    write_plan(ws, gates=EVERY_FOLD)
    monkeypatch.setattr(child, "certify_in_child", refused)
    monkeypatch.setattr(backtest, "window_chunks", refused)

    stall = scheduler.on_stall(ws, store, hyp_id)

    spared(ws, store, hyp_id, stall)
    assert stall.precheck is not None
    assert stall.precheck["gate"] == "walk_forward_consistency"
    assert stall.precheck["measured"] is False
    readings = cast("list[dict[str, Any]]", stall.precheck["gates"])
    assert [(reading["gate"], reading["judged"], reading["pass"]) for reading in readings] == [
        ("deflated_sharpe", False, None),
        ("walk_forward_consistency", True, False),
    ], "the plan's word that it carries no correction, then the refusal"
    assert readings[1]["evidence"]["positive_folds"] == 2


def test_a_stall_whose_best_passes_its_search_correction_is_certified_as_before(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    searched(ws, store, *TIGHT)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert (stall.certifiable, stall.verdict) == (True, "pass")
    assert show(ws, store, hyp_id).status == "certified"  # type: ignore[union-attr]
    assert stall.precheck is not None
    assert (stall.precheck["judged"], stall.precheck["pass"]) == (True, True)
    assert stall.precheck["measured"] is True, "the child's reading, on the run it made"
    kinds = [event.kind for event in store.events(subject=hyp_id)]
    assert kinds.index(scheduler.CERTIFIABLE) < kinds.index(scheduler.STALLED)


def test_a_stall_the_lane_cannot_judge_is_judged_by_the_child_before_the_window(
    ws: Workspace, store: StateStore
) -> None:
    """A card measured under another kanso is no reading of the arithmetic certification would
    use, so the lane judges nothing and the child runs the research window first; a
    correction that holds there certifies as before."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store, criteria_version="0.0.1"))
    searched(ws, store, *TIGHT)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert (stall.certifiable, stall.verdict) == (True, "pass")
    assert stall.precheck is not None
    assert (stall.precheck["judged"], stall.precheck["pass"]) == (True, True)
    assert stall.precheck["measured"] is True


def test_a_stall_the_child_refuses_on_research_data_is_spared_by_the_child(
    ws: Workspace, store: StateStore
) -> None:
    """Where the lane could not judge, the child's refusal spares the stall just the same: it
    stops before the certification window, writes nothing and moves nothing."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    a_card(ws, store, REVERTING)
    searched(ws, store, *WIDE)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    stall = scheduler.on_stall(ws, store, hyp_id)

    spared(ws, store, hyp_id, stall)
    assert stall.precheck is not None and stall.precheck["measured"] is True
    (stalled,) = store.events(kind=scheduler.STALLED)
    assert float(str(stalled.detail["cert_wall_s"])) > 0, "the child ran the research window"


@pytest.mark.parametrize(
    ("kept", "trials"),
    [(True, TIGHT), (True, WIDE), (False, TIGHT)],
    ids=["kept-tight", "kept-wide", "none-kept"],
)
def test_a_stall_on_a_snapshot_that_cannot_support_a_claim_records_the_child_s_precheck(
    ws: Workspace, store: StateStore, kept: bool, trials: tuple[float, ...]
) -> None:
    """Certification judges no correction on such a snapshot, so neither does the lane, however
    the card's own measurement would read — a reading of it that refused the best would spare
    a stall whose failing certificate is how the snapshot reaches the operator. The child runs
    no window and writes that certificate, counted toward `n_fail`, and its precheck judged
    nothing, ran nothing and says why — and that is what the stall records, never the lane's
    reading of the card, which would set a correction's verdict or a reason about the card
    beside a certificate whose correction failed on the snapshot."""
    a_manifest(ws, publication="unknown")
    snapshots.freeze(ws)
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store) if kept else None)
    searched(ws, store, *trials)
    lane = precheck(ws, store, hyp_id, sha, write_plan(ws, gates=[*CERT_GATES, SEARCHED]))
    assert (lane.judged, lane.passed, lane.measured) == (False, None, False)
    assert lane.reason is not None
    assert ("cannot support a claim" in lane.reason) is kept, "the snapshot, where a card kept"

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert (stall.certifiable, stall.verdict) == (True, "fail")
    assert show(ws, store, hyp_id).cert_failures == 1  # type: ignore[union-attr]
    assert stall.precheck is not None
    assert (stall.precheck["judged"], stall.precheck["pass"]) == (False, None)
    assert stall.precheck["measured"] is False, "the child ran no window for it"
    assert "cannot support a claim" in str(stall.precheck["reason"])
    made = certificates.latest(store, hyp_id)
    assert made is not None and made.verdict == "fail"
    (deflation,) = [gate for gate in made.gates if gate.id == SEARCHED["id"]]
    assert stall.precheck["evidence"] == deflation.evidence, "the certificate's own reasons"
    (stalled,) = store.events(kind=scheduler.STALLED)
    assert stalled.detail["precheck"] == stall.precheck
    assert store.events(kind=scheduler.CERTIFIABLE) == []
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]


def test_a_stall_with_no_plan_plans_in_the_lane_before_its_precheck(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, recorded: Recorder
) -> None:
    """The precheck reads the threshold the planner chose, so the plan is made in the lane: one
    planner call, pinned, and the stall spared on it without a child."""
    scripted(ws)
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    searched(ws, store, *WIDE)
    monkeypatch.setattr(child, "certify_in_child", refused)

    stall = scheduler.on_stall(ws, store, hyp_id, "l1")

    assert len(recorded.of("certify_plan")) == 1
    pinned = read_plan(ws, hyp_id)
    assert pinned is not None and pinned.plan_version == 1
    spared(ws, store, hyp_id, stall)
    assert stall.precheck is not None
    assert stall.precheck["evidence"]["min_dsr"] == next(
        gate.params["min_dsr"] for gate in pinned.gates if gate.id == "deflated_sharpe"
    )


def test_a_stall_reads_kanso_toml_as_the_certification_child_it_would_start_reads_it(
    ws: Workspace, store: StateStore
) -> None:
    """A lane reads `kanso.toml` when it starts and the child it starts reads it again, so the
    lane's plan and precheck read what the child reads. Once an operator edits `[research]
    folds` under a running daemon, a card measured under the folds the lane started with is no
    reading of the arithmetic certification makes: the lane judges nothing on it — where,
    judged on its own start's folds, it spared the stall on a reading the child never makes
    and withheld whatever the child would have certified — and the child judges the
    correction on the research run it makes under the file's folds."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    sha = a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    searched(ws, store, *WIDE)
    pinned = write_plan(ws, gates=[*CERT_GATES, SEARCHED])
    started = ws.config.research.folds
    config = ws.path("kanso.toml")
    text = config.read_text(encoding="utf-8")
    edited = re.sub(r"(?m)^folds = \d+", f"folds = {started - 1}", text)
    assert edited != text, "the workspace's kanso.toml declares its folds"
    config.write_text(edited, encoding="utf-8")
    assert precheck(ws, store, hyp_id, sha, pinned).spared, "judged on the lane's start"

    lane = precheck_at_stall(ws.root, store, hyp_id, sha)
    stall = scheduler.on_stall(ws, store, hyp_id)

    assert ws.config.research.folds == started, "the lane's own reading is its start's"
    assert (lane.judged, lane.passed, lane.measured) == (False, None, False)
    assert lane.reason is not None
    assert f"under folds {started}, and certification would measure it under {started - 1}" in (
        lane.reason
    )
    spared(ws, store, hyp_id, stall)
    assert stall.precheck is not None
    assert stall.precheck["measured"] is True, "the child's reading, on the run it made"
    (stalled,) = store.events(kind=scheduler.STALLED)
    assert float(str(stalled.detail["cert_wall_s"])) > 0, "the child ran the research window"


def test_a_card_the_lane_cannot_read_leaves_the_stall_to_the_child(
    ws: Workspace, store: StateStore
) -> None:
    """The child turns a fault into a refusal its lane records; the lane reading a card in the
    child's place has nothing to do so, and died of a measurement it could not decode where
    the child never reads one. So the reading judges nothing and says what raised, and the
    child certifies the stall on the research run it makes, as it did before the lane judged
    anything."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    sha = a_card(ws, store, REVERTING, measured_sha=store.put_blob(b"no measurement"))
    searched(ws, store, *TIGHT)
    write_plan(ws, gates=[*CERT_GATES, SEARCHED])

    lane = precheck_at_stall(ws.root, store, hyp_id, sha)
    stall = scheduler.on_stall(ws, store, hyp_id)

    assert (lane.judged, lane.passed, lane.measured) == (False, None, False)
    assert lane.reason is not None
    assert lane.reason.startswith(f"reading the newest card of {sha[:7]} raised error")
    assert (stall.certifiable, stall.verdict) == (True, "pass")
    assert stall.precheck is not None
    assert (stall.precheck["judged"], stall.precheck["pass"]) == (True, True)
    assert stall.precheck["measured"] is True


def test_a_fault_planning_a_stall_is_the_refusal_the_child_reported(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, recorded: Recorder
) -> None:
    """Planned in the child, a plan pinned on a full disk came back to the lane as a refusal,
    which a daemon lane records and backs off from; planned in the lane, the `OSError` ended
    the lane. It is raised as that refusal: exit 1, naming what raised, with `kanso cert run`
    as the way to see it, and no child started."""
    scripted(ws)
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    searched(ws, store, *WIDE)

    def full(*_: object) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(plans, "_pin", full)
    monkeypatch.setattr(child, "certify_in_child", refused)

    with pytest.raises(KansoError) as raised:
        scheduler.on_stall(ws, store, hyp_id, "l1")

    assert raised.value.code is Exit.ERROR
    assert f"of {hyp_id} raised OSError: [Errno {errno.ENOSPC}]" in raised.value.message
    assert raised.value.remedy is not None
    assert f"`kanso cert run {hyp_id}`" in raised.value.remedy
    assert isinstance(raised.value.__cause__, OSError)
    assert len(recorded.of("certify_plan")) == 1
    assert read_plan(ws, hyp_id) is None
    assert store.events(kind=scheduler.STALLED) == []


@pytest.mark.parametrize("met", ["unplanned", "unsnapped"])
def test_a_refusal_the_lane_meets_in_the_child_s_place_is_raised_as_itself(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, met: str
) -> None:
    """A refusal is no fault. Planning with no model to plan with, or reading a card whose
    snapshot the workspace no longer holds, refuses in the lane as it refused in the child:
    as itself, with its own remedy — never wrapped as a fault, nor read as a precheck that
    judged nothing and let through to a child that would refuse it the same way."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    a_card(ws, store, REVERTING, measured_sha=measurement(ws, store))
    searched(ws, store, *WIDE)
    if met == "unsnapped":
        write_plan(ws, gates=[*CERT_GATES, SEARCHED])
        snapshots.snapshot_file(ws, snapshot_id(ws)).unlink()
    monkeypatch.setattr(child, "certify_in_child", refused)

    with pytest.raises(KansoError) as raised:
        scheduler.on_stall(ws, store, hyp_id)

    assert raised.value.code is not Exit.ERROR, "a refusal, not a fault"
    assert raised.value.remedy is not None
    assert "raised" not in raised.value.message
    assert store.events(kind=scheduler.STALLED) == []


def test_a_stall_on_a_best_whose_search_cannot_be_priced_is_not_certified(
    ws: Workspace, store: StateStore
) -> None:
    """Certification would refuse it, and the lane with it: the run goes on drawing cards."""
    from tests.certify.test_run import searched_then_switched

    hyp_id = DOCUMENT["id"]
    searched_then_switched(ws, store, after=1)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.certifiable is False
    assert stall.best_sha is not None
    assert store.connection.execute("SELECT COUNT(*) FROM certificates").fetchone()[0] == 0
    assert ids(store) == [hyp_id]


def test_a_stall_with_no_keep_only_requeues(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.certifiable is False
    assert stall.best_sha is None
    assert show(ws, store, hyp_id).status == "classified"  # type: ignore[union-attr]
    assert ids(store) == [hyp_id]


def test_a_stall_on_bytes_already_certified_is_not_worth_certifying_again(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    run = open_run(store, hyp_id)
    sha = store.put_blob(b"kept")
    records.set_best(store, run, sha, 1.25)
    certificate(store, hyp_id, sha)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.certifiable is False
    assert stall.best_sha == sha
    assert ids(store) == [hyp_id]


def test_a_stall_on_bytes_certified_before_other_bytes_is_not_certified_again(
    ws: Workspace, store: StateStore
) -> None:
    """The best that stalled was certified long ago, and other bytes were certified since.

    A certificate is immutable and certification refuses the repeat, so asking only whether
    the best was the newest certificate's subject sent it back to be refused at every stall,
    and the lane failed each time — measured on a live workspace, five times in two hours on
    one hypothesis. The stall asks whether these bytes were judged under the plan it would
    run and the engine installed.
    """
    hyp_id = classify(ws, store, DOCUMENT)
    write_plan(ws)
    run = open_run(store, hyp_id)
    kept = store.put_blob(b"kept")
    certificate(store, hyp_id, kept, verdict="fail")
    certificate(store, hyp_id, store.put_blob(b"certified since"), verdict="fail")
    records.set_best(store, run, kept, 1.25)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.certifiable is False
    assert stall.best_sha == kept
    assert ids(store) == [hyp_id], "back to research, not a failed lane"


def stall_twice(ws: Workspace, store: StateStore, hyp_id: str) -> list[scheduler.Stall]:
    """Two stalls on a best already certified, so nothing is certified in between."""
    return [scheduler.on_stall(ws, store, hyp_id) for _ in range(2)]


def test_a_spell_of_stalls_on_one_best_reseeds_the_next_run_from_the_best_other_keep(
    ws: Workspace, store: StateStore
) -> None:
    """The decision is a `reseed` event and rides on the queue passage, where `put_back`
    keeps it; the best itself is not moved."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    best = a_card(ws, store, REVERTING, seq=1, metric=2.0)
    other = a_card(ws, store, WEAK, seq=2, metric=1.0, best=False)
    lesser = a_card(ws, store, FLAT, seq=3, metric=0.5, best=False)
    assert lesser != other
    certificate(store, hyp_id, best)

    first, second = stall_twice(ws, store, hyp_id)

    assert first.reseed_from is None, "one stall is a stall"
    assert second.reseed_from == other, "the highest-scoring keep that is not the best"
    assert passages.reseed_of(store, hyp_id) == other
    assert records.best_of(store, hyp_id) == (best, 2.0)
    (event,) = store.events(kind=scheduler.RESEED, subject=hyp_id)
    assert event.detail == {
        "from": best,
        "to": other,
        "stalls": 2,
        "because": "the best other aligned keep under the pins",
    }
    assert second.payload()["reseed_from"] == other
    # A third stall on the same best is a new spell: one stall since the reseed.
    assert scheduler.on_stall(ws, store, hyp_id).reseed_from is None
    assert passages.reseed_of(store, hyp_id) is None, "the newest passage carries none"


def test_the_spell_of_stalls_before_a_reseed_is_read_from_kanso_toml(
    ws: Workspace, store: StateStore
) -> None:
    workspace = tuned(ws, reseed_after_stalls=1)
    hyp_id = classify(workspace, store, DOCUMENT, REVERTING)
    best = a_card(workspace, store, REVERTING, seq=1, metric=2.0)
    other = a_card(workspace, store, WEAK, seq=2, metric=1.0, best=False)
    certificate(store, hyp_id, best)

    stall = scheduler.on_stall(workspace, store, hyp_id)

    assert stall.reseed_from == other, "one stall is a spell of one"
    (event,) = store.events(kind=scheduler.RESEED, subject=hyp_id)
    assert event.detail["stalls"] == 1


def test_a_keep_a_drift_check_marked_is_never_where_a_reseed_starts(
    ws: Workspace, store: StateStore
) -> None:
    """A misaligned keep is one its own run was rewound away from, so it is passed over
    however well it scored, and with no aligned keep left the stalled run's base is next."""
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    best = a_card(ws, store, REVERTING, seq=1, metric=3.0)
    drifted = a_card(ws, store, WEAK, seq=2, metric=2.0, best=False)
    aligned = a_card(ws, store, FLAT, seq=3, metric=1.0, best=False)
    store.connection.execute(
        "UPDATE cards SET aligned = 0 WHERE hyp_id = ? AND strategy_sha = ?", (hyp_id, drifted)
    )
    certificate(store, hyp_id, best)

    _, second = stall_twice(ws, store, hyp_id)

    assert second.reseed_from == aligned
    assert passages.reseed_of(store, hyp_id) == aligned

    store.connection.execute(
        "UPDATE cards SET aligned = 0 WHERE hyp_id = ? AND strategy_sha = ?", (hyp_id, aligned)
    )
    _, fourth = stall_twice(ws, store, hyp_id)

    stalled = records.runs_of(store, hyp_id)[-1]
    assert fourth.reseed_from == stalled.base_sha != drifted
    assert store.events(kind=scheduler.RESEED, subject=hyp_id)[-1].detail["because"] == (
        "the stalled run's own base"
    )


def test_a_reseed_starts_only_from_a_keep_written_under_the_stalled_run_s_program(
    ws: Workspace, store: StateStore
) -> None:
    """`program.md` is pinned by a run like `hypothesis.yaml`, and a keep written to
    another program's rules is not ground for this one's next run, however well it scored.

    Measured in a live workspace on 2026-10-12: keeps under a temporary program and then
    under the restored original, same hypothesis file, snapshot and criteria, and a reseed
    that matched those three started the next run from a keep the restored program
    forbids. The mirror case shows it is the stalled run's program that decides.
    """
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    best = a_card(ws, store, REVERTING, seq=1, metric=3.0)
    foreign = a_card(ws, store, WEAK, seq=2, metric=2.0, best=False)
    own = a_card(ws, store, FLAT, seq=3, metric=1.0, best=False)
    temporary = store.put_blob(b"# program\nflatten at the target\n")
    store.connection.execute(
        "UPDATE runs SET program_sha = ? WHERE hyp_id = ? AND run_id IN (SELECT run_id FROM"
        " cards WHERE strategy_sha = ?)",
        (temporary, hyp_id, foreign),
    )
    pins = {
        (r.hypothesis_sha, r.snapshot_id, r.criteria_version)
        for r in records.runs_of(store, hyp_id)
    }
    assert len(pins) == 1, "the three other pins are one"
    stalled = records.runs_of(store, hyp_id)[-1]
    assert stalled.program_sha != temporary
    certificate(store, hyp_id, best)

    _, second = stall_twice(ws, store, hyp_id)

    assert second.reseed_from == own != foreign
    assert passages.reseed_of(store, hyp_id) == own

    store.connection.execute(
        "UPDATE runs SET program_sha = ? WHERE run_id = ?", (temporary, stalled.run_id)
    )
    _, fourth = stall_twice(ws, store, hyp_id)

    assert fourth.reseed_from == foreign, "under the temporary program, its keep is the best"


def test_with_no_other_keep_the_reseed_is_the_stalled_run_s_base_or_nothing(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    best = a_card(ws, store, REVERTING, seq=1, metric=2.0)
    certificate(store, hyp_id, best)

    _, second = stall_twice(ws, store, hyp_id)

    assert second.reseed_from is None, "the run began from the best: nowhere else to start"
    assert store.events(kind=scheduler.RESEED, subject=hyp_id) == []

    # A later run that began from other bytes and kept nothing: its base is the way out,
    # and the spell is already two stalls long when the third lands on the same best.
    run = open_run(store, hyp_id)
    records.close(store, run)
    third, fourth = stall_twice(ws, store, hyp_id)
    assert third.reseed_from == run.base_sha
    (event,) = store.events(kind=scheduler.RESEED, subject=hyp_id)
    assert event.detail["because"] == "the stalled run's own base"
    assert fourth.reseed_from is None, "one stall since the reseed"


def test_stalls_on_different_bests_are_not_one_spell(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    first = a_card(ws, store, REVERTING, seq=1, metric=1.0)
    certificate(store, hyp_id, first)
    scheduler.on_stall(ws, store, hyp_id)
    second = a_card(ws, store, WEAK, seq=2, metric=2.0)
    certificate(store, hyp_id, second)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.best_sha == second
    assert stall.reseed_from is None
    assert store.events(kind=scheduler.RESEED, subject=hyp_id) == []


def test_the_reseed_passage_survives_a_put_back_and_a_recover(
    ws: Workspace, store: StateStore
) -> None:
    """Backlog row 62 measured that a decision written only at the stall does not survive
    this workspace; one on the passage is re-written by every return to the queue that
    precedes a run, and only a run beginning consumes it."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.requeue(store, hyp_id, scheduler.STALL_PRIORITY, reseed_from="b" * 64)
    assert scheduler.dequeue(store, "l1") == hyp_id
    assert passages.reseed_of(store, hyp_id) == "b" * 64, "a claim does not consume it"

    assert scheduler.put_back(store, hyp_id) is not None, "the lane failed before a run"
    assert passages.reseed_of(store, hyp_id) == "b" * 64, "put back with the decision"
    assert scheduler.dequeue(store, "l2") == hyp_id
    assert scheduler.recover(store) == [hyp_id]
    assert passages.reseed_of(store, hyp_id) == "b" * 64, "recovered with the decision"

    store.event(BEGUN, hyp_id, {"run_id": "r", "tag": "t", "lane": "l3"})
    assert passages.reseed_of(store, hyp_id) is None, "a run beginning consumed it"
    scheduler.requeue(store, hyp_id, scheduler.STALL_PRIORITY)
    assert passages.reseed_of(store, hyp_id) is None, "a plain requeue carries none"


def test_a_baseline_that_will_not_run_returns_behind_the_stalled_ones(
    ws: Workspace, store: StateStore
) -> None:
    stalled = classify(ws, store, DOCUMENT)
    broken = register(ws, store, "demo_two")
    open_run(store, stalled)
    scheduler.on_stall(ws, store, stalled)

    item = scheduler.on_baseline_failed(store, broken)

    assert item.priority == scheduler.BASELINE_PRIORITY
    assert ids(store) == [stalled, broken]


# --- parking ---------------------------------------------------------------


def saturated(ws: Workspace, store: StateStore, after: int) -> tuple[Workspace, str, str]:
    """A hypothesis whose best is already certified, so a stall certifies nothing and runs
    nothing, under `[research] park_after_stalls = after`."""
    workspace = tuned(ws, park_after_stalls=after)
    hyp_id = classify(workspace, store, DOCUMENT, REVERTING)
    best = a_card(workspace, store, REVERTING, seq=1, metric=2.0)
    certificate(store, hyp_id, best)
    return workspace, hyp_id, best


def stalls(ws: Workspace, store: StateStore, hyp_id: str, n: int) -> list[scheduler.Stall]:
    return [scheduler.on_stall(ws, store, hyp_id) for _ in range(n)]


def priorities(store: StateStore) -> list[tuple[str, int]]:
    return [(item.hyp_id, item.priority) for item in scheduler.queued(store)]


def test_at_zero_no_spell_of_stalls_parks_a_hypothesis(ws: Workspace, store: StateStore) -> None:
    """The template's 0 is never: every stall requeues at −1, however long the spell."""
    workspace, hyp_id, _ = saturated(ws, store, 0)

    made = stalls(workspace, store, hyp_id, 4)

    assert [(m.priority, m.parked, m.stalls_on_best) for m in made] == [
        (scheduler.STALL_PRIORITY, False, n) for n in range(1, 5)
    ]
    recorded = store.events(kind=scheduler.STALLED, subject=hyp_id)
    assert [(e.detail["parked"], e.detail["stalls_on_best"]) for e in recorded] == [
        (False, n) for n in range(1, 5)
    ]
    assert priorities(store) == [(hyp_id, scheduler.STALL_PRIORITY)]


def test_the_nth_stall_on_one_best_parks_the_hypothesis_at_minus_three(
    ws: Workspace, store: StateStore
) -> None:
    workspace, hyp_id, best = saturated(ws, store, 3)
    status = show(workspace, store, hyp_id).status  # type: ignore[union-attr]

    first, second, third = stalls(workspace, store, hyp_id, 3)

    assert [(m.priority, m.parked) for m in (first, second)] == [
        (scheduler.STALL_PRIORITY, False)
    ] * 2, "two stalls of three park nothing"
    assert (third.priority, third.parked, third.stalls_on_best) == (
        scheduler.PARK_PRIORITY,
        True,
        3,
    )
    assert (third.payload()["parked"], third.payload()["stalls_on_best"]) == (True, 3)
    event = store.events(kind=scheduler.STALLED, subject=hyp_id)[-1]
    assert (event.detail["best_sha"], event.detail["parked"], event.detail["stalls_on_best"]) == (
        best,
        True,
        3,
    )
    assert priorities(store) == [(hyp_id, scheduler.PARK_PRIORITY)]
    assert passages.last_passage(store, hyp_id) == (
        scheduler.QUEUED,
        {"priority": scheduler.PARK_PRIORITY},
    ), "the queued passage records where it went"
    latest = scheduler.last_stall(store, hyp_id)
    assert latest is not None and (latest["parked"], latest["stalls_on_best"]) == (True, 3)
    after = show(workspace, store, hyp_id).status  # type: ignore[union-attr]
    assert after == status, "a park ends nothing"
    assert records.best_of(store, hyp_id) == (best, 2.0)

    fourth = scheduler.on_stall(workspace, store, hyp_id)
    assert (fourth.priority, fourth.parked, fourth.stalls_on_best) == (
        scheduler.PARK_PRIORITY,
        True,
        4,
    )


def test_a_stall_on_another_best_starts_the_spell_over(ws: Workspace, store: StateStore) -> None:
    workspace, hyp_id, _ = saturated(ws, store, 2)
    scheduler.on_stall(workspace, store, hyp_id)
    better = a_card(workspace, store, WEAK, seq=2, metric=3.0)
    certificate(store, hyp_id, better)

    moved, again = stalls(workspace, store, hyp_id, 2)

    assert (moved.best_sha, moved.stalls_on_best, moved.parked, moved.priority) == (
        better,
        1,
        False,
        scheduler.STALL_PRIORITY,
    )
    assert (again.stalls_on_best, again.parked, again.priority) == (
        2,
        True,
        scheduler.PARK_PRIORITY,
    )


def test_neither_a_reseed_nor_an_exploration_starts_the_spell_over(
    ws: Workspace, store: StateStore
) -> None:
    """Both leave the best where it was, so the hypothesis is still stalling on it; a reseed
    still rides on the passage that parks it."""
    workspace, hyp_id, _ = saturated(ws, store, 3)
    workspace = tuned(workspace, reseed_after_stalls=1)
    other = a_card(workspace, store, WEAK, seq=2, metric=1.0, best=False)

    first = scheduler.on_stall(workspace, store, hyp_id)
    store.event(explore.EXPLORED, hyp_id, {"id": "demo_drafted"})
    second = scheduler.on_stall(workspace, store, hyp_id)
    store.event(explore.EXPLORED_FAILED, hyp_id, {"error": "no model"})
    third = scheduler.on_stall(workspace, store, hyp_id)

    assert [m.reseed_from for m in (first, second, third)] == [other] * 3
    assert len(store.events(kind=scheduler.RESEED, subject=hyp_id)) == 3
    assert [m.stalls_on_best for m in (first, second, third)] == [1, 2, 3]
    assert (third.parked, third.priority) == (True, scheduler.PARK_PRIORITY)
    assert passages.reseed_of(store, hyp_id) == other, "the reseed rides on the parked passage"


def test_a_hypothesis_with_no_best_stalls_on_one_best(ws: Workspace, store: StateStore) -> None:
    workspace = tuned(ws, park_after_stalls=2)
    hyp_id = classify(workspace, store, DOCUMENT)

    first, second = stalls(workspace, store, hyp_id, 2)

    assert (first.best_sha, first.parked, first.priority) == (None, False, scheduler.STALL_PRIORITY)
    assert (second.best_sha, second.stalls_on_best, second.parked, second.priority) == (
        None,
        2,
        True,
        scheduler.PARK_PRIORITY,
    )


def test_a_stall_spared_on_research_data_counts_toward_the_spell(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spare certifies nothing and leaves the best unjudged, so the next stall is spared
    again on the same bytes: one best, two stalls."""
    from tests.certify.test_precheck import EVERY_FOLD, FICKLE

    workspace = tuned(ws, park_after_stalls=2)
    hyp_id = classify(workspace, store, DOCUMENT, REVERTING)
    set_status(store, hyp_id, "researching")
    a_card(workspace, store, REVERTING, measured_sha=measurement(workspace, store, run=FICKLE))
    write_plan(workspace, gates=EVERY_FOLD)
    monkeypatch.setattr(child, "certify_in_child", refused)
    monkeypatch.setattr(backtest, "window_chunks", refused)

    first, second = stalls(workspace, store, hyp_id, 2)

    for made in (first, second):
        assert made.precheck is not None and made.precheck["pass"] is False, "spared"
    assert (first.parked, second.parked, second.stalls_on_best) == (False, True, 2)
    assert second.priority == scheduler.PARK_PRIORITY


def test_a_stall_that_certified_counts_toward_the_spell(ws: Workspace, store: StateStore) -> None:
    workspace = tuned(ws, park_after_stalls=2)
    hyp_id = classify(workspace, store, DOCUMENT, JANUARY)
    a_card(workspace, store, JANUARY)
    write_plan(workspace)

    first, second = stalls(workspace, store, hyp_id, 2)

    assert (first.verdict, first.parked) == ("fail", False)
    assert second.verdict is None, "its best is certified now"
    assert (second.stalls_on_best, second.parked, second.priority) == (
        2,
        True,
        scheduler.PARK_PRIORITY,
    )


def test_a_parked_hypothesis_whose_lane_fails_mid_run_goes_back_at_minus_three(
    ws: Workspace, store: StateStore
) -> None:
    """The case a band written only at the stall loses: `docs/backlog.md` row 62 measured 373
    `lane_failed` events against 8 `stalled`, and the daemon puts a failed run back at −1."""
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    assert scheduler.dequeue(store, "l1") == hyp_id
    open_run(store, hyp_id, lane="l1")

    item = scheduler.put_back(store, hyp_id, "l1")

    assert item is not None and item.priority == scheduler.PARK_PRIORITY
    assert passages.last_passage(store, hyp_id) == (
        scheduler.QUEUED,
        {"priority": scheduler.PARK_PRIORITY},
    )


def test_a_parked_hypothesis_returns_at_minus_three_from_a_baseline_and_a_recovered_hold(
    ws: Workspace, store: StateStore
) -> None:
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    assert scheduler.dequeue(store, "l1") == hyp_id

    put = scheduler.put_back(store, hyp_id, "l1")  # its baseline would not run

    assert put is not None and put.priority == scheduler.PARK_PRIORITY
    assert scheduler.on_baseline_failed(store, hyp_id).priority == scheduler.PARK_PRIORITY
    assert scheduler.dequeue(store, "l2") == hyp_id
    store.event(BEGUN, hyp_id, {"run_id": "r", "tag": "t", "lane": "l2"})
    scheduler.hold(store, hyp_id, "l2")  # a stall ended the run, and the lane died
    assert scheduler.recover(store) == [hyp_id]
    assert priorities(store) == [(hyp_id, scheduler.PARK_PRIORITY)]


def test_a_keep_that_beats_the_best_lifts_the_park_at_the_next_return(
    ws: Workspace, store: StateStore
) -> None:
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    assert scheduler.dequeue(store, "l1") == hyp_id
    run = open_run(store, hyp_id, lane="l1")
    records.set_best(store, run, store.put_blob(b"better"), 5.0)

    item = scheduler.put_back(store, hyp_id, "l1")

    assert item is not None and item.priority == scheduler.STALL_PRIORITY


@pytest.mark.parametrize("key", [0, 10])
def test_setting_the_key_to_zero_or_above_the_spell_lifts_the_park_at_the_next_stall(
    ws: Workspace, store: StateStore, key: int
) -> None:
    """The park is the newest stall's word, so the key read at a stall decides it."""
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    assert priorities(store) == [(hyp_id, scheduler.PARK_PRIORITY)]

    third = scheduler.on_stall(tuned(workspace, park_after_stalls=key), store, hyp_id)

    assert (third.parked, third.stalls_on_best, third.priority) == (
        False,
        3,
        scheduler.STALL_PRIORITY,
    )
    assert store.events(kind=scheduler.STALLED, subject=hyp_id)[-1].detail["parked"] is False
    assert scheduler.dequeue(store, "l1") == hyp_id
    open_run(store, hyp_id, lane="l1")
    item = scheduler.put_back(store, hyp_id, "l1")
    assert item is not None and item.priority == scheduler.STALL_PRIORITY


@pytest.mark.parametrize("clearer", ["re-pin", "from-workspace"])
def test_a_cleared_best_lifts_the_park_though_the_baseline_keeps_the_same_bytes_again(
    ws: Workspace, store: StateStore, clearer: str
) -> None:
    """A re-pin whose scope moved, and `research begin --from-workspace`, clear the best and
    record `best_cleared`; the next baseline keeps the workspace `strategy.py`, which a keep
    wrote there, so the best is the parked bytes again — under another scope, so the stalls
    measured under the old one neither hold the park nor count toward the next spell."""
    workspace, hyp_id, best = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    assert scheduler.parked(store, hyp_id)
    if clearer == "re-pin":
        moved = {**cast("dict[str, Any]", DOCUMENT["costs"]), "commission_bps": 1.0}
        classify(workspace, store, document(costs=moved), REVERTING)
    else:
        loop._base_source(workspace, store, hyp_id, from_workspace=True)
    assert records.best_of(store, hyp_id) == (None, None)
    assert store.events(kind=BEST_CLEARED, subject=hyp_id)
    assert scheduler.dequeue(store, "l1") == hyp_id
    run = open_run(store, hyp_id, lane="l1")
    records.set_best(store, run, best, 2.0)  # the baseline, on the same bytes
    assert records.best_of(store, hyp_id)[0] == best

    assert scheduler.parked(store, hyp_id) is False
    item = scheduler.put_back(store, hyp_id, "l1")

    assert item is not None and item.priority == scheduler.STALL_PRIORITY
    records.close(store, run)
    after = scheduler.on_stall(workspace, store, hyp_id)
    assert (after.best_sha, after.stalls_on_best, after.parked, after.priority) == (
        best,
        1,
        False,
        scheduler.STALL_PRIORITY,
    ), "a new spell"


MIN_TRADES = {"id": "min_trades", "params": {"min": 30}}
"""A card gate the demo hypothesis does not name."""


@pytest.mark.parametrize("moved", ["scope", "gate"])
def test_a_re_pin_that_moves_the_scope_or_adds_a_gate_lifts_a_park_on_no_best(
    ws: Workspace, store: StateStore, moved: str
) -> None:
    """No best is one best, and a re-pin with no best to clear writes no `best_cleared`: its
    `repinned` event records `rescoped`, and the stalls behind it — measured under the scope it
    replaced — neither hold the park nor count toward the next spell."""
    workspace = tuned(ws, park_after_stalls=2)
    hyp_id = classify(workspace, store, DOCUMENT)
    stalls(workspace, store, hyp_id, 2)
    assert scheduler.parked(store, hyp_id)
    if moved == "scope":
        costs = {**cast("dict[str, Any]", DOCUMENT["costs"]), "commission_bps": 1.0}
        classify(workspace, store, document(costs=costs))
    else:
        classify(workspace, store, document(required_constraints=[MIN_TRADES]))

    assert store.events(kind=BEST_CLEARED, subject=hyp_id) == [], "no best to clear"
    assert store.events(kind=REPINNED, subject=hyp_id)[-1].detail[RESCOPED] is True
    assert scheduler.parked(store, hyp_id) is False
    assert scheduler.dequeue(store, "l1") == hyp_id
    run = open_run(store, hyp_id, lane="l1")
    item = scheduler.put_back(store, hyp_id, "l1")
    assert item is not None and item.priority == scheduler.STALL_PRIORITY
    records.close(store, run)

    after = scheduler.on_stall(workspace, store, hyp_id)

    assert (after.best_sha, after.stalls_on_best, after.parked, after.priority) == (
        None,
        1,
        False,
        scheduler.STALL_PRIORITY,
    ), "a new spell"


def test_a_re_pin_within_the_scope_leaves_a_park_on_no_best_where_it_is(
    ws: Workspace, store: StateStore
) -> None:
    """A retitled file is the same experiment: `rescoped` is false, and the spell runs on."""
    workspace = tuned(ws, park_after_stalls=2)
    hyp_id = classify(workspace, store, DOCUMENT)
    stalls(workspace, store, hyp_id, 2)

    classify(workspace, store, document(title="Demo, retitled"))

    assert store.events(kind=REPINNED, subject=hyp_id)[-1].detail[RESCOPED] is False
    assert scheduler.parked(store, hyp_id)
    third = scheduler.on_stall(workspace, store, hyp_id)
    assert (third.stalls_on_best, third.parked, third.priority) == (
        3,
        True,
        scheduler.PARK_PRIORITY,
    )


def test_a_re_pin_that_lifts_a_park_leaves_a_row_waiting_at_minus_three_where_it_stands(
    ws: Workspace, store: StateStore
) -> None:
    """The park is read at a return, so a re-pin that clears the best moves no row already
    waiting: it stays behind −2 until a lane serves it, or `queue add --priority` raises it."""
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    other = register(workspace, store, "demo_two")
    scheduler.requeue(store, other, scheduler.BASELINE_PRIORITY)
    waiting = [(other, scheduler.BASELINE_PRIORITY), (hyp_id, scheduler.PARK_PRIORITY)]
    assert priorities(store) == waiting

    moved = {**cast("dict[str, Any]", DOCUMENT["costs"]), "commission_bps": 1.0}
    classify(workspace, store, document(costs=moved), REVERTING)

    assert scheduler.parked(store, hyp_id) is False, "its next return goes to its band"
    assert priorities(store) == waiting, "and the row waiting now stays where it is"
    scheduler.enqueue(store, hyp_id, priority=scheduler.STALL_PRIORITY)
    assert priorities(store) == [
        (hyp_id, scheduler.STALL_PRIORITY),
        (other, scheduler.BASELINE_PRIORITY),
    ]


@pytest.mark.parametrize("after", [1, 2])
def test_a_stall_decided_across_a_re_pin_that_cleared_its_best_stands_in_no_spell(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, after: int
) -> None:
    """A re-pin is not refused while a stall is decided — the lane's run has ended — so one
    whose scope moved can clear the best between the stall reading it and recording itself.
    That stall was measured under the scope the re-pin replaced: it parks nothing, and once
    the next baseline keeps the same bytes again the first stall under the new scope is the
    first of its spell, at either key."""
    workspace, hyp_id, best = saturated(ws, store, after)
    stalls(workspace, store, hyp_id, after - 1)
    real = certificates.judged
    re_pinned: list[str] = []

    def re_pinning(*args: Any, **kwargs: Any) -> bool:
        if not re_pinned:
            moved = {**cast("dict[str, Any]", DOCUMENT["costs"]), "commission_bps": 1.0}
            re_pinned.append(classify(workspace, store, document(costs=moved), REVERTING))
        return real(*args, **kwargs)

    monkeypatch.setattr(certificates, "judged", re_pinning)

    across = scheduler.on_stall(workspace, store, hyp_id)

    assert re_pinned == [hyp_id], "the re-pin landed while the stall was decided"
    assert records.best_of(store, hyp_id) == (None, None)
    assert (across.best_sha, across.parked, across.stalls_on_best, across.priority) == (
        best,
        False,
        0,
        scheduler.STALL_PRIORITY,
    )
    detail = store.events(kind=scheduler.STALLED, subject=hyp_id)[-1].detail
    assert (detail["best_sha"], detail["parked"], detail["stalls_on_best"]) == (best, False, 0)
    assert scheduler.parked(store, hyp_id) is False
    assert priorities(store) == [(hyp_id, scheduler.STALL_PRIORITY)]

    assert scheduler.dequeue(store, "l1") == hyp_id
    run = open_run(store, hyp_id, lane="l1")
    records.set_best(store, run, best, 2.0)  # the baseline, on the same bytes
    assert scheduler.parked(store, hyp_id) is False
    item = scheduler.put_back(store, hyp_id, "l1")
    assert item is not None and item.priority == scheduler.STALL_PRIORITY
    records.close(store, run)

    first = scheduler.on_stall(workspace, store, hyp_id)

    assert first.stalls_on_best == 1, "a new spell, which the stall across the re-pin is not in"
    assert (first.parked, first.priority) == (
        (True, scheduler.PARK_PRIORITY) if after == 1 else (False, scheduler.STALL_PRIORITY)
    )


@pytest.mark.parametrize("after", [1, 2])
def test_a_stall_whose_best_was_cleared_and_re_kept_while_it_was_decided_stands_in_no_spell(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, after: int
) -> None:
    """The best the stall reads again is the one it read, but a `best_cleared` came between —
    `research begin --from-workspace`, and a baseline that kept the same bytes — so the stall
    was measured before a clearing and counts in no spell: the best alone cannot say so, and the
    newest clearing's id does."""
    workspace, hyp_id, best = saturated(ws, store, after)
    stalls(workspace, store, hyp_id, after - 1)
    real = certificates.judged
    cleared: list[str] = []

    def clearing(*args: Any, **kwargs: Any) -> bool:
        if not cleared:
            loop._base_source(workspace, store, hyp_id, from_workspace=True)
            records.set_best(store, records.runs_of(store, hyp_id)[-1], best, 2.0)
            cleared.append(hyp_id)
        return real(*args, **kwargs)

    monkeypatch.setattr(certificates, "judged", clearing)

    across = scheduler.on_stall(workspace, store, hyp_id)

    assert cleared == [hyp_id], "the best was cleared and re-kept while the stall was decided"
    assert len(store.events(kind=BEST_CLEARED, subject=hyp_id)) == 1
    assert records.best_of(store, hyp_id)[0] == best, "the same bytes again"
    assert (across.best_sha, across.parked, across.stalls_on_best, across.priority) == (
        best,
        False,
        0,
        scheduler.STALL_PRIORITY,
    )
    detail = store.events(kind=scheduler.STALLED, subject=hyp_id)[-1].detail
    assert (detail["best_sha"], detail["parked"], detail["stalls_on_best"]) == (best, False, 0)
    assert scheduler.parked(store, hyp_id) is False

    first = scheduler.on_stall(workspace, store, hyp_id)

    assert first.stalls_on_best == 1, "a new spell, which the stall across the clearing is not in"
    assert (first.parked, first.priority) == (
        (True, scheduler.PARK_PRIORITY) if after == 1 else (False, scheduler.STALL_PRIORITY)
    )


def test_a_stall_on_no_best_decided_across_a_re_pin_that_moved_its_scope_stands_in_no_spell(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No best is read again as no best, so only the re-pin's `rescoped` says the stall was
    measured under the scope it replaced: it parks nothing, and the next stall is the first of
    its spell."""
    workspace = tuned(ws, park_after_stalls=2)
    hyp_id = classify(workspace, store, DOCUMENT)
    stalls(workspace, store, hyp_id, 1)
    real = records.best_of
    reads: list[str] = []

    def re_pinning(*args: Any, **kwargs: Any) -> Any:
        reads.append("read")
        if len(reads) == 2:  # the stall reads its best again before it records itself
            costs = {**cast("dict[str, Any]", DOCUMENT["costs"]), "commission_bps": 1.0}
            classify(workspace, store, document(costs=costs))
        return real(*args, **kwargs)

    monkeypatch.setattr(records, "best_of", re_pinning)

    across = scheduler.on_stall(workspace, store, hyp_id)

    assert store.events(kind=REPINNED, subject=hyp_id)[-1].detail[RESCOPED] is True
    assert (across.best_sha, across.parked, across.stalls_on_best, across.priority) == (
        None,
        False,
        0,
        scheduler.STALL_PRIORITY,
    ), "two stalls on no best, one of them under the replaced scope"
    monkeypatch.setattr(records, "best_of", real)
    first = scheduler.on_stall(workspace, store, hyp_id)
    assert (first.stalls_on_best, first.parked) == (1, False)


def a_run_that_stalled(store: StateStore, hyp_id: str, seq: int) -> RunRecord:
    """A run as a lane hands it to its stall: begun, with the `run_begun` `loop.begin` records,
    and closed, as `loop.end` closes it before the stall is decided."""
    run = open_run(store, hyp_id, lane=f"l{seq}")
    store.event(BEGUN, hyp_id, {"run_id": run.run_id, "tag": run.tag, "lane": run.lane})
    return records.close(store, run)


@pytest.mark.parametrize("moved", ["scope", "gate"])
def test_a_stall_whose_run_closed_before_a_re_pin_that_moved_its_scope_stands_in_no_spell(
    ws: Workspace, store: StateStore, moved: str
) -> None:
    """A re-pin is accepted the moment the stalled run closes, and the lane removes the run's
    directory and records its end before the stall reads anything, so a re-pin landing then is
    already in the stall's first read. The run's `run_begun` is what says the run was measured
    under the scope the re-pin replaced: that stall parks nothing, and the stalls of the runs
    begun after the re-pin are a new spell, parked at the key and not before it."""
    workspace = tuned(ws, park_after_stalls=3)
    hyp_id = classify(workspace, store, DOCUMENT)
    before = []
    for seq in (1, 2):
        a_run_that_stalled(store, hyp_id, seq)
        before.append(scheduler.on_stall(workspace, store, hyp_id))
    assert [(m.stalls_on_best, m.parked) for m in before] == [(1, False), (2, False)]
    a_run_that_stalled(store, hyp_id, 3)
    if moved == "scope":
        costs = {**cast("dict[str, Any]", DOCUMENT["costs"]), "commission_bps": 1.0}
        classify(workspace, store, document(costs=costs))
    else:
        classify(workspace, store, document(required_constraints=[MIN_TRADES]))
    assert store.events(kind=REPINNED, subject=hyp_id)[-1].detail[RESCOPED] is True

    across = scheduler.on_stall(workspace, store, hyp_id)

    assert (across.best_sha, across.stalls_on_best, across.parked, across.priority) == (
        None,
        0,
        False,
        scheduler.STALL_PRIORITY,
    ), "measured under the scope the re-pin replaced"
    after = []
    for seq in (4, 5, 6):
        a_run_that_stalled(store, hyp_id, seq)
        after.append(scheduler.on_stall(workspace, store, hyp_id))
    assert [(m.stalls_on_best, m.parked) for m in after] == [(1, False), (2, False), (3, True)]


def test_no_clearing_lands_between_a_stall_s_check_and_its_record(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stall reads its best again, counts its spell and records itself in one write
    transaction, so `research begin --from-workspace` from another process while the spell is
    counted cannot clear the best until the stall is recorded. The stall is decided on what it
    read, and the clearing after it lifts the park it recorded and starts the next spell — never
    a park recorded after a clearing, which would hold across it."""
    workspace, hyp_id, best = saturated(ws, store, 3)
    stalls(workspace, store, hyp_id, 2)
    real = scheduler._spell
    waited: list[str] = []

    def clearing(*args: Any, **kwargs: Any) -> int:
        with StateStore(store.path) as other:
            other.connection.execute("PRAGMA busy_timeout = 0")  # refused, rather than waiting
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                loop._base_source(workspace, other, hyp_id, from_workspace=True)
        waited.append(hyp_id)
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_spell", clearing)

    third = scheduler.on_stall(workspace, store, hyp_id)

    assert waited == [hyp_id], "the clearing was tried while the spell was counted"
    assert store.events(kind=BEST_CLEARED, subject=hyp_id) == []
    assert (third.best_sha, third.stalls_on_best, third.parked, third.priority) == (
        best,
        3,
        True,
        scheduler.PARK_PRIORITY,
    )
    monkeypatch.setattr(scheduler, "_spell", real)
    loop._base_source(workspace, store, hyp_id, from_workspace=True)  # once the stall is recorded
    records.set_best(store, records.runs_of(store, hyp_id)[-1], best, 2.0)  # the same bytes
    assert scheduler.parked(store, hyp_id) is False, "the clearing came after the park"

    fourth = scheduler.on_stall(workspace, store, hyp_id)

    assert (fourth.stalls_on_best, fourth.parked, fourth.priority) == (
        1,
        False,
        scheduler.STALL_PRIORITY,
    ), "a new spell"


def test_a_claim_on_a_row_queued_below_the_park_recovers_where_it_stood(
    ws: Workspace, store: StateStore
) -> None:
    """The park lowers a return to −3 and never raises one: a row an operator queued at −5
    and a lane claimed comes back at −5 when the lane dies."""
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    assert scheduler.remove(store, hyp_id) == "queue"
    scheduler.enqueue(store, hyp_id, priority=-5)
    assert scheduler.dequeue(store, "l1") == hyp_id

    assert scheduler.recover(store) == [hyp_id]

    assert scheduler.parked(store, hyp_id)
    assert priorities(store) == [(hyp_id, -5)]


def test_queue_add_raises_a_parked_row_until_its_next_return(
    ws: Workspace, store: StateStore
) -> None:
    """`enqueue` is not held to the park: the operator's word stands, for one return."""
    workspace, hyp_id, _ = saturated(ws, store, 2)
    stalls(workspace, store, hyp_id, 2)
    other = register(workspace, store, "demo_two")
    scheduler.requeue(store, other, scheduler.STALL_PRIORITY)
    assert priorities(store) == [
        (other, scheduler.STALL_PRIORITY),
        (hyp_id, scheduler.PARK_PRIORITY),
    ]

    raised = scheduler.enqueue(store, hyp_id, priority=0)

    assert raised.priority == 0
    assert priorities(store) == [(hyp_id, 0), (other, scheduler.STALL_PRIORITY)]
    assert scheduler.dequeue(store, "l1") == hyp_id
    assert scheduler.recover(store) == [hyp_id], "the lane died before its run began"
    assert priorities(store) == [
        (other, scheduler.STALL_PRIORITY),
        (hyp_id, scheduler.PARK_PRIORITY),
    ], "back on the same best, so back behind"


@pytest.mark.parametrize("act", ["retire", "remove"])
def test_a_retire_or_removal_during_a_parking_stall_s_certification_requeues_nothing(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch, act: str
) -> None:
    workspace = tuned(ws, park_after_stalls=1)
    hyp_id = classify(workspace, store, DOCUMENT, JANUARY)
    a_card(workspace, store, JANUARY)
    write_plan(workspace)
    scheduler.hold(store, hyp_id, "l1")

    def leaving(*_: object, **__: object) -> Any:
        if act == "retire":
            hyp.retire(workspace, store, hyp_id)
        else:
            assert scheduler.remove(store, hyp_id) == "lane"
        return SimpleNamespace(certificate=None, precheck=None, peak_mem_gb=0.0, wall_s=0.0)

    monkeypatch.setattr(child, "certify_in_child", leaving)

    stall = scheduler.on_stall(workspace, store, hyp_id, "l1")

    assert (stall.priority, stall.parked, stall.stalls_on_best) == (None, True, 1)
    assert (stall.payload()["parked"], stall.payload()["stalls_on_best"]) == (True, 1)
    detail = store.events(kind=scheduler.STALLED, subject=hyp_id)[-1].detail
    assert (detail["parked"], detail["stalls_on_best"]) == (True, 1), "what the Stall says"
    assert ids(store) == []
    assert scheduler.recover(store) == []
    if act == "retire":
        hyp.resume(workspace, store, hyp_id)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l2") == hyp_id
    open_run(store, hyp_id, lane="l2")
    item = scheduler.put_back(store, hyp_id, "l2")
    assert item is not None and item.priority == scheduler.PARK_PRIORITY, "the park it recorded"


def test_the_queue_serves_zero_then_minus_one_two_and_three_and_a_park_waiting_alone(
    ws: Workspace, store: StateStore
) -> None:
    parked = classify(ws, store, DOCUMENT)
    baseline = register(ws, store, "demo_two")
    stalled = register(ws, store, "demo_three")
    fresh = register(ws, store, "demo_four")
    scheduler.enqueue(store, parked, priority=scheduler.PARK_PRIORITY)
    scheduler.enqueue(store, baseline, priority=scheduler.BASELINE_PRIORITY)
    scheduler.enqueue(store, stalled, priority=scheduler.STALL_PRIORITY)
    scheduler.enqueue(store, fresh)

    served = [scheduler.dequeue(store, lane) for lane in ("l1", "l2", "l3")]

    assert served == [fresh, stalled, baseline]
    assert scheduler.dequeue(store, "l4") == parked, "nothing else waits, so the park is served"
    assert scheduler.dequeue(store, "l5") is None


def test_the_queue_payload_is_json(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(ws, store, DOCUMENT, REVERTING)
    scheduler.enqueue(store, hyp_id, priority=3)
    a_card(ws, store, REVERTING)
    write_plan(ws)

    payload: dict[str, Any] = scheduler.queued(store)[0].payload()
    stall = scheduler.on_stall(ws, store, hyp_id).payload()

    assert json.loads(json.dumps(payload))["priority"] == 3
    assert json.loads(json.dumps(stall))["certifiable"] is True
    assert json.loads(json.dumps(stall))["verdict"] == "pass"


def test_a_claim_is_recorded_under_the_lane_that_made_it(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)

    assert scheduler.dequeue(store, "l2") == hyp_id

    claims = store.events(kind=scheduler.CLAIMED, subject=hyp_id)
    assert [event.detail["lane"] for event in claims] == ["l2"]
    assert scheduler.claimed(store, hyp_id)


def test_a_claimed_hypothesis_is_not_claimed_again_before_its_run_begins(
    ws: Workspace, store: StateStore
) -> None:
    """Between the claim and the run there is no run to see, and the claim has to be enough."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l1") == hyp_id
    # A row beside an open claim: what an older kanso's `queue add` left, and what a lane
    # killed between its claim and its run leaves for `recover` to put right.
    store.connection.execute(
        "INSERT INTO queue (hyp_id, priority, enqueued_at) VALUES (?, ?, ?)",
        (hyp_id, 0, "2024-03-01T00:00:00+00:00"),
    )

    assert scheduler.dequeue(store, "l2") is None
    assert [item.hyp_id for item in scheduler.queued(store)] == [hyp_id]
    assert [e.detail["lane"] for e in store.events(kind=scheduler.CLAIMED, subject=hyp_id)] == [
        "l1"
    ]


def test_queueing_a_hypothesis_a_lane_has_claimed_and_not_begun_is_refused(
    ws: Workspace, store: StateStore
) -> None:
    """The claim is the window a second row would be claimed in; a run open is not, since
    the queue is how an interactive run's hypothesis is handed to the daemon."""
    claimed = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, claimed)
    assert scheduler.dequeue(store, "l1") == claimed
    running = register(ws, store, "demo_two")
    scheduler.enqueue(store, running)
    assert scheduler.dequeue(store, "l2") == running
    open_run(store, running, lane="l2")

    with pytest.raises(PreconditionError, match="already held"):
        scheduler.enqueue(store, claimed)
    scheduler.enqueue(store, running)

    assert [item.hyp_id for item in scheduler.queued(store)] == [running]
    assert scheduler.dequeue(store, "l3") is None


def test_a_hypothesis_dropped_between_claim_and_run_is_put_back(
    ws: Workspace, store: StateStore
) -> None:
    """A dead lane leaves its subject with neither a run nor a place, and a claim on record.

    Everything else with neither a run nor a place is left alone: a run the operator ended,
    a hypothesis nobody queued, one the operator took out, one whose claim became a run.
    """
    dropped = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, dropped)
    assert scheduler.dequeue(store, "l1") == dropped
    busy = register(ws, store, "demo_two")
    scheduler.enqueue(store, busy)
    assert scheduler.dequeue(store, "l2") == busy
    open_run(store, busy, lane="l2")
    ended = register(ws, store, "demo_three")
    scheduler.enqueue(store, ended)
    assert scheduler.dequeue(store, "l3") == ended
    store.event(BEGUN, ended, {"lane": "l3"})
    taken_out = register(ws, store, "demo_four")
    scheduler.enqueue(store, taken_out)
    assert scheduler.dequeue(store, "l3") == taken_out
    assert scheduler.remove(store, taken_out) == "lane"
    waiting = register(ws, store, "demo_five")
    scheduler.enqueue(store, waiting)
    register(ws, store, "demo_six")

    assert scheduler.recover(store) == [dropped]

    assert ids(store) == [waiting, dropped]
    assert scheduler.recover(store) == []


def test_remove_takes_a_hypothesis_out_of_the_queue(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)

    assert scheduler.remove(store, hyp_id) == "queue"

    assert ids(store) == []
    assert [event.kind for event in store.events(subject=hyp_id)][-2:] == [
        scheduler.QUEUED,
        scheduler.REMOVED,
    ]
    with pytest.raises(PreconditionError, match="not in the queue"):
        scheduler.remove(store, hyp_id)


def test_remove_reaches_a_hypothesis_a_lane_holds_and_its_failure_leaves_it_out(
    ws: Workspace, store: StateStore
) -> None:
    """The operator's removal outranks the lane's put-back; the lane's next failure or the
    next start does not bring the hypothesis back."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l1") == hyp_id

    assert scheduler.remove(store, hyp_id) == "lane"

    assert not scheduler.claimed(store, hyp_id)
    assert scheduler.put_back(store, hyp_id) is None
    assert scheduler.recover(store) == []
    assert ids(store) == []


def test_a_failed_lane_does_not_put_back_a_hypothesis_another_lane_claimed_since(
    ws: Workspace, store: StateStore
) -> None:
    """Measured on a live workspace on 2026-09-29: a lane's baseline card ran past its budget
    after the operator had taken the hypothesis out from under it, re-pinned it and queued it
    again, and a second lane had claimed it; the first lane's failure put it back a second
    time and a third lane began a run beside the second's. The claim on record is the second
    lane's, so the first lane's put-back is not its to make."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l1") == hyp_id
    assert scheduler.remove(store, hyp_id) == "lane"
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l2") == hyp_id

    assert scheduler.put_back(store, hyp_id, "l1") is None

    assert ids(store) == []
    assert scheduler.claimed(store, hyp_id)
    assert scheduler.put_back(store, hyp_id, "l2") is not None, "the lane that holds it may"
    assert ids(store) == [hyp_id]


def test_recover_for_one_lane_puts_back_only_what_that_lane_held(
    ws: Workspace, store: StateStore
) -> None:
    """What the supervisor does when one lane dies under a running daemon: every other lane
    is alive and still holds what it claimed, and a claim put back would be taken twice."""
    dead = classify(ws, store, DOCUMENT)
    alive = register(ws, store, "demo_two")
    held = register(ws, store, "demo_three")
    for hyp_id in (dead, alive, held):
        scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l1") == dead
    assert scheduler.dequeue(store, "l2") == alive
    assert scheduler.dequeue(store, "l1") == held
    scheduler.hold(store, held, "l1")

    assert scheduler.recover(store, "l1") == [dead, held]

    assert ids(store) == [dead, held]
    assert scheduler.claimed(store, alive), "the living lane's claim is its own"
    assert scheduler.recover(store, "l1") == []


def test_recover_returns_each_at_the_priority_it_held_in_claim_order(
    ws: Workspace, store: StateStore
) -> None:
    demoted = classify(ws, store, DOCUMENT)
    fresh = register(ws, store, "demo_two")
    scheduler.enqueue(store, demoted, priority=scheduler.BASELINE_PRIORITY)
    scheduler.enqueue(store, fresh)
    assert scheduler.dequeue(store, "l1") == fresh
    assert scheduler.dequeue(store, "l2") == demoted

    assert scheduler.recover(store) == [fresh, demoted]

    assert [(item.hyp_id, item.priority) for item in scheduler.queued(store)] == [
        (fresh, 0),
        (demoted, scheduler.BASELINE_PRIORITY),
    ]


def test_a_stall_holds_the_hypothesis_in_the_lane_s_name_until_the_scheduler_decides(
    ws: Workspace, store: StateStore
) -> None:
    """The run has ended and the queue has not taken it: a lane that fails or dies now owes it."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l1") == hyp_id
    store.event(BEGUN, hyp_id, {"lane": "l1"})
    assert scheduler.put_back(store, hyp_id) is None, "an ended run is the operator's doing"

    scheduler.hold(store, hyp_id, "l1")

    assert scheduler.claimed(store, hyp_id)
    put = scheduler.put_back(store, hyp_id)
    assert put is not None and put.priority == scheduler.BASELINE_PRIORITY
    other = register(ws, store, "demo_two")
    scheduler.enqueue(store, other)
    assert scheduler.dequeue(store, "l2") == other
    store.event(BEGUN, other, {"lane": "l2"})
    scheduler.hold(store, other, "l2")
    assert scheduler.recover(store) == [other], "the lane died before the scheduler decided"
    assert [(item.hyp_id, item.priority) for item in scheduler.queued(store)] == [
        (other, scheduler.STALL_PRIORITY),
        (hyp_id, scheduler.BASELINE_PRIORITY),
    ]


def test_a_retire_while_a_lane_holds_it_closes_the_claim_so_resume_does_not_revive_it(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l1") == hyp_id
    set_status(store, hyp_id, "retired")

    assert scheduler.put_back(store, hyp_id) is None
    set_status(store, hyp_id, "researching")

    assert not scheduler.claimed(store, hyp_id)
    assert scheduler.recover(store) == []
    closing = store.events(kind=scheduler.REMOVED, subject=hyp_id)[-1]
    assert closing.detail == {"from": "lane", "lane": "l1", "because": "retired"}


def test_remove_from_a_lane_names_the_lane_that_may_not_begin(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert scheduler.dequeue(store, "l2") == hyp_id

    assert scheduler.remove(store, hyp_id) == "lane"

    assert scheduler.taken(store, hyp_id, "l2")
    assert not scheduler.taken(store, hyp_id, "op"), "another lane may still begin it by hand"
    scheduler.enqueue(store, hyp_id)
    assert not scheduler.taken(store, hyp_id, "l2"), "queued again, the removal is spent"


def test_a_removal_that_lands_during_the_certification_is_honoured(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, DOCUMENT, JANUARY)
    a_card(ws, store, JANUARY)
    write_plan(ws)
    scheduler.hold(store, hyp_id, "l1")
    real = child.certify_in_child

    def removing(*args: Any, **kwargs: Any) -> Any:
        assert scheduler.remove(store, hyp_id) == "lane"
        return real(*args, **kwargs)

    monkeypatch.setattr(child, "certify_in_child", removing)

    stall = scheduler.on_stall(ws, store, hyp_id, "l1")

    assert stall.verdict == "fail"
    assert stall.priority is None
    assert ids(store) == []
    assert scheduler.recover(store) == []


def test_put_back_returns_a_failed_lane_s_hypothesis_by_where_it_got_to(
    ws: Workspace, store: StateStore
) -> None:
    never_began = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, never_began)
    assert scheduler.dequeue(store, "l1") == never_began
    mid_run = register(ws, store, "demo_two")
    open_run(store, mid_run, lane="l2")
    retired = register(ws, store, "demo_three")
    scheduler.enqueue(store, retired)
    assert scheduler.dequeue(store, "l3") == retired
    set_status(store, retired, "retired")

    assert scheduler.put_back(store, never_began) is not None
    assert scheduler.put_back(store, mid_run) is not None
    assert scheduler.put_back(store, retired) is None

    assert [(item.hyp_id, item.priority) for item in scheduler.queued(store)] == [
        (mid_run, scheduler.STALL_PRIORITY),
        (never_began, scheduler.BASELINE_PRIORITY),
    ]
