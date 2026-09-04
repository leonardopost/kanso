"""The file only ever grows.

An operator keeps notes in this file and their agent tails it, so a rewrite would lose
work no one asked kanso to touch. Everything mutable about an entry lives in the row
instead, which is why acknowledging one leaves the file byte-identical.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from kanso.inbox import ack, escalate, inbox_file, unread
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import entry_lines


def test_a_second_entry_leaves_the_first_where_it_was(ws: Workspace, store: StateStore) -> None:
    escalate(ws, store, "misaligned", "demo_mr", "drifted from its thesis")
    before = inbox_file(ws).read_bytes()

    second = escalate(ws, store, "cert_failed", "demo_mr", "3 of 5 cert gates failed")

    after = inbox_file(ws).read_bytes()
    assert after.startswith(before)
    assert after[len(before) :].decode("utf-8") == second.line() + "\n"


def test_the_operators_own_notes_survive_an_escalation(ws: Workspace, store: StateStore) -> None:
    with inbox_file(ws).open("a", encoding="utf-8") as handle:
        handle.write("\nwatch demo_mr — I think the exit is wrong\n")
    before = inbox_file(ws).read_bytes()

    escalate(ws, store, "cert_failed", "demo_mr", "certification failed")

    assert inbox_file(ws).read_bytes().startswith(before)


def test_acknowledging_does_not_change_a_byte_of_the_file(ws: Workspace, store: StateStore) -> None:
    entry = escalate(ws, store, "promotable", "mr_demo@1", "all paper gates pass")
    before = inbox_file(ws).read_bytes()

    ack(store, entry.escalation_id)

    assert inbox_file(ws).read_bytes() == before
    assert entry_lines(ws) == [entry.line()]
    assert entry_lines(ws)[0].startswith("- [ ] ")
    assert unread(store) == []


def test_nothing_opens_the_file_for_anything_but_appending(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The append-only promise, enforced rather than described."""
    target = inbox_file(ws)
    opened: list[str] = []
    real = Path.open

    def spy(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if self == target:
            opened.append(mode)
        return real(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)

    entry = escalate(ws, store, "deploy_blocked", "mr_demo@2", "no unallocated capital")
    ack(store, entry.escalation_id)
    unread(store)

    assert opened == ["a"]


def test_the_file_comes_back_when_a_workspace_predates_it(ws: Workspace, store: StateStore) -> None:
    """A workspace scaffolded before the inbox existed still escalates."""
    shutil.rmtree(inbox_file(ws).parent)

    entry = escalate(ws, store, "demoted", "mr_demo@1", "a live gate failed")

    assert entry_lines(ws) == [entry.line()]


def test_the_rows_and_not_the_file_say_what_is_unread(ws: Workspace, store: StateStore) -> None:
    """Anyone may write in this file; only kanso writes the rows."""
    entry = escalate(ws, store, "misaligned", "demo_mr", "drifted from its thesis")
    with inbox_file(ws).open("a", encoding="utf-8") as handle:
        handle.write("- [ ] deadbeef 2026-01-01T00:00:00+00:00 cert_failed other — invented\n")

    assert [found.escalation_id for found in unread(store)] == [entry.escalation_id]
    assert len(entry_lines(ws)) == 2
