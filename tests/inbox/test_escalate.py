"""What an escalation is: one line an operator agent can act on, and three writes of it.

The line's shape is a contract with a reader kanso does not control — the operator's own
agent, reading the file with kanso not running — so it is asserted literally here rather
than described.
"""

from __future__ import annotations

import re
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kanso.errors import Exit, ValidationError
from kanso.inbox import (
    ACTIONS,
    ESCALATED,
    KINDS,
    SEPARATOR,
    SUMMARY_LIMIT,
    Escalation,
    escalate,
    inbox_file,
    unread,
)
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import entry_lines

ENTRY: Final = re.compile(
    r"^- \[ \] (?P<id>[0-9a-f]{8}) (?P<ts>\S+) (?P<kind>\S+) (?P<subject>\S+) — "
    r"(?P<summary>.+) · actions: (?P<actions>.+)$"
)
"""The promised shape: id, timestamp, kind, subject, summary, actions."""


def test_an_entry_has_the_shape_its_readers_parse(ws: Workspace, store: StateStore) -> None:
    entry = escalate(ws, store, "cert_failed", "demo_mr", "3 of 5 cert gates failed")

    found = ENTRY.match(entry.line())

    assert found is not None
    assert found["id"] == entry.escalation_id
    assert found["ts"] == entry.created_at
    assert found["kind"] == "cert_failed"
    assert found["subject"] == "demo_mr"
    assert found["summary"] == "3 of 5 cert gates failed"
    assert found["actions"] == entry.actions
    assert entry_lines(ws) == [entry.line()]


def test_the_three_writes_say_the_same_thing(ws: Workspace, store: StateStore) -> None:
    """A row to count, a line to read, an event to reconstruct the run from."""
    entry = escalate(ws, store, "cert_failed", "demo_mr", "3 of 5 cert gates failed")

    assert unread(store) == [entry]
    assert entry_lines(ws) == [entry.line()]
    logged = [event for event in store.events(subject="demo_mr") if event.kind == ESCALATED]
    assert [event.detail for event in logged] == [
        {"id": entry.escalation_id, "kind": "cert_failed"}
    ]


def test_the_template_header_is_still_above_the_first_entry(
    ws: Workspace, store: StateStore
) -> None:
    escalate(ws, store, "cert_failed", "demo_mr", "certification failed")

    assert inbox_file(ws).read_text(encoding="utf-8").startswith("# Escalations\n")


def test_the_kinds_are_the_five_and_every_one_offers_something() -> None:
    assert KINDS == ("misaligned", "cert_failed", "promotable", "demoted", "deploy_blocked")
    assert all(ACTIONS[kind] for kind in KINDS)
    assert all(action.startswith("kanso ") for actions in ACTIONS.values() for action in actions)


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_carries_the_actions_it_offers(
    ws: Workspace, store: StateStore, kind: str
) -> None:
    entry = escalate(ws, store, kind, "demo_mr@3", "something the operator decides")

    assert entry.actions.split(SEPARATOR) == [
        action.format(subject="demo_mr@3") for action in ACTIONS[kind]
    ]
    assert entry.line().endswith(f"{SEPARATOR}actions: {entry.actions}")


def test_no_offered_action_skips_the_approval_or_replans_a_failure() -> None:
    """The line is read by an agent, so what it offers is part of the safety story."""
    promotions = [
        action for actions in ACTIONS.values() for action in actions if " promote " in action
    ]
    assert promotions
    assert all("--as" in action for action in promotions)
    assert not [action for action in ACTIONS["cert_failed"] if "--replan" in action]


def test_a_caller_with_something_more_specific_replaces_them(
    ws: Workspace, store: StateStore
) -> None:
    entry = escalate(
        ws,
        store,
        "misaligned",
        "demo_mr",
        "drifted from its thesis",
        actions="kanso research show demo_mr --sha abc1234\nkanso align check demo_mr",
    )

    assert entry.actions == "kanso research show demo_mr --sha abc1234 kanso align check demo_mr"
    assert "\n" not in entry.line()


def test_a_kind_outside_the_five_is_refused(ws: Workspace, store: StateStore) -> None:
    with pytest.raises(ValidationError, match="not an escalation kind") as caught:
        escalate(ws, store, "interesting", "demo_mr", "look at this")

    assert caught.value.code is Exit.VALIDATION
    assert entry_lines(ws) == []
    assert unread(store) == []


@pytest.mark.parametrize(("subject", "summary"), [("", "it failed"), ("demo_mr", "   ")])
def test_an_entry_naming_nothing_or_saying_nothing_is_refused(
    ws: Workspace, store: StateStore, subject: str, summary: str
) -> None:
    with pytest.raises(ValidationError, match="needs a subject and a summary"):
        escalate(ws, store, "cert_failed", subject, summary)

    assert entry_lines(ws) == []


def test_a_long_summary_is_shortened_rather_than_lost(ws: Workspace, store: StateStore) -> None:
    entry = escalate(ws, store, "cert_failed", " demo_mr ", "gate " * 200)

    assert len(entry.summary) == SUMMARY_LIMIT
    assert entry.summary.endswith("…")
    assert entry.subject == "demo_mr"
    assert [event.subject for event in store.events(kind=ESCALATED)] == ["demo_mr"]
    assert "kanso cert show demo_mr" in entry.actions


def test_a_summary_over_several_lines_becomes_one(ws: Workspace, store: StateStore) -> None:
    entry = escalate(ws, store, "cert_failed", "demo_mr", "deflated_sharpe\nfailed:\t0.1 < 0.3")

    assert entry.summary == "deflated_sharpe failed: 0.1 < 0.3"
    assert entry_lines(ws) == [entry.line()]


def test_every_entry_gets_its_own_id(ws: Workspace, store: StateStore) -> None:
    entries = [escalate(ws, store, "demoted", f"s{n}@1", "a live gate failed") for n in range(20)]

    assert len({entry.escalation_id for entry in entries}) == 20
    assert entry_lines(ws) == [entry.line() for entry in entries]


def test_an_entry_is_one_json_object(ws: Workspace, store: StateStore) -> None:
    """The shape `status` prints and the optional webhook posts."""
    entry = escalate(ws, store, "promotable", "mr_demo@1", "all paper gates pass")

    assert entry.payload() == {
        "id": entry.escalation_id,
        "kind": "promotable",
        "subject": "mr_demo@1",
        "summary": "all paper gates pass",
        "actions": entry.actions,
        "created_at": entry.created_at,
        "acked_at": None,
    }


def test_an_entry_with_nothing_to_offer_still_renders() -> None:
    """Entries are built by `escalate`, but the row is public and older ones carried none."""
    bare = Escalation(
        escalation_id="0" * 8,
        kind="misaligned",
        subject="demo_mr",
        summary="drifted",
        actions="",
        created_at="2026-01-01T00:00:00+00:00",
    )

    assert bare.line() == "- [ ] 00000000 2026-01-01T00:00:00+00:00 misaligned demo_mr — drifted"


@settings(
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(summary=st.text(max_size=400), actions=st.text(max_size=200))
def test_whatever_a_caller_says_the_file_stays_one_line_per_entry(
    ws: Workspace, store: StateStore, summary: str, actions: str
) -> None:
    before = len(entry_lines(ws))

    entry = escalate(ws, store, "cert_failed", "demo_mr", f"cert: {summary}", actions=actions)

    lines = entry_lines(ws)
    assert len(lines) == before + 1
    assert lines[-1] == entry.line()
    assert len(entry.summary) <= SUMMARY_LIMIT
    assert entry.summary.startswith("cert:")
    assert entry.actions
    assert "\n" not in entry.line()
