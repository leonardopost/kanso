"""Acknowledgement: read, and nothing more.

`inbox ack` is the one verb the inbox offers, and the whole design is that it cannot be
mistaken for consent. It writes one timestamp on one row: no approval, no status change,
no event, nothing an operator agent could reach for instead of the named approval that
moves live capital.
"""

from __future__ import annotations

import pytest

from kanso.errors import Exit, ValidationError
from kanso.inbox import ack, escalate, unread
from kanso.state import StateStore
from kanso.workspace import Workspace


def test_acknowledging_marks_one_entry_read_and_is_idempotent(
    ws: Workspace, store: StateStore
) -> None:
    entry = escalate(ws, store, "misaligned", "demo_mr", "drifted from its thesis")

    first = ack(store, entry.escalation_id)
    second = ack(store, entry.escalation_id)

    assert first.acked_at is not None
    assert second == first
    assert unread(store) == []
    assert first.line().startswith(f"- [x] {entry.escalation_id} ")


def test_acknowledging_approves_nothing(ws: Workspace, store: StateStore) -> None:
    entry = escalate(ws, store, "promotable", "mr_demo@1", "all paper gates pass")
    events = len(store.events())

    acked = ack(store, entry.escalation_id)

    approvals = store.connection.execute("SELECT count(*) FROM approvals").fetchone()[0]
    assert approvals == 0
    assert len(store.events()) == events
    assert acked.payload() | {"acked_at": None} == entry.payload()


def test_acknowledging_one_entry_leaves_the_others_unread(ws: Workspace, store: StateStore) -> None:
    first = escalate(ws, store, "cert_failed", "demo_mr", "3 of 5 cert gates failed")
    second = escalate(ws, store, "demoted", "mr_demo@1", "a live gate failed")

    ack(store, first.escalation_id)

    assert [entry.escalation_id for entry in unread(store)] == [second.escalation_id]


def test_acknowledging_something_that_is_not_an_entry_is_refused(store: StateStore) -> None:
    with pytest.raises(ValidationError, match="no escalation") as caught:
        ack(store, "nope")

    assert caught.value.code is Exit.VALIDATION


def test_the_unread_are_oldest_first(ws: Workspace, store: StateStore) -> None:
    entries = [
        escalate(ws, store, "cert_failed", f"h{n}", "3 of 5 cert gates failed") for n in range(5)
    ]

    assert [entry.escalation_id for entry in unread(store)] == [
        entry.escalation_id for entry in entries
    ]


def test_two_entries_of_the_same_instant_still_have_an_order(store: StateStore) -> None:
    """`status` prints the first few, so the order cannot depend on the row layout."""
    for escalation_id in ("bbbbbbbb", "aaaaaaaa"):
        store.connection.execute(
            "INSERT INTO escalations (escalation_id, kind, subject, summary, actions, created_at)"
            " VALUES (?, 'cert_failed', 'demo_mr', 'failed', '', '2026-01-01T00:00:00+00:00')",
            (escalation_id,),
        )

    assert [entry.escalation_id for entry in unread(store)] == ["aaaaaaaa", "bbbbbbbb"]
