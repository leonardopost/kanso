"""The queue: who is served next, who is passed over, and what a stall leaves behind.

The queue is the only thing standing between "many hypotheses" and "a few lanes", so its
whole behaviour is order and exclusion: priority then arrival, a dead hypothesis dropped,
a busy one skipped and left where it was.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from kanso.errors import PreconditionError
from kanso.hyp import set_status, show
from kanso.inbox import unread
from kanso.research import records, scheduler
from kanso.schemas import RunRecord
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.certify.test_run import a_card, with_n_fail, write_plan

from .conftest import DOCUMENT, FLAT, REVERTING, classify, document


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


def test_only_a_registered_and_living_hypothesis_may_be_queued(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)

    with pytest.raises(PreconditionError, match="not a registered hypothesis"):
        scheduler.enqueue(store, "nobody")

    set_status(store, hyp_id, "failed")
    with pytest.raises(PreconditionError, match="research does not resume"):
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
    hyp_id = classify(ws, store, DOCUMENT, FLAT)
    a_card(ws, store, FLAT)
    write_plan(ws)

    stall = scheduler.on_stall(ws, store, hyp_id)

    assert stall.verdict == "fail", "bytes that never trade earn nothing out of sample"
    assert stall.priority == scheduler.STALL_PRIORITY
    assert show(ws, store, hyp_id).status == "researching"  # type: ignore[union-attr]
    assert ids(store) == [hyp_id]
    assert not unread(store), "one failure is not yet an escalation"


def test_a_stall_whose_certificate_ends_the_hypothesis_leaves_the_queue(
    ws: Workspace, store: StateStore
) -> None:
    """Death is the one way out of the queue, and the failure run is the way to death."""
    hyp_id = classify(ws, store, DOCUMENT, FLAT)
    a_card(ws, store, FLAT)
    write_plan(ws)
    scheduler.enqueue(store, hyp_id)

    stall = scheduler.on_stall(with_n_fail(ws, 1), store, hyp_id)

    assert stall.verdict == "fail"
    assert stall.priority is None
    assert show(ws, store, hyp_id).status == "failed"  # type: ignore[union-attr]
    assert ids(store) == []
    assert [entry.kind for entry in unread(store)] == ["cert_failed"]


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
