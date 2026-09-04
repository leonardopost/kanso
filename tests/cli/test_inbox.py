"""`kanso inbox`: reading what is waiting for a person, and saying it has been read.

The escalation these tests read is a real one written by a real drift, because what the
command is for is the moment an operator comes back to a workspace a daemon has been
working in and asks what happened while they were away.

Acknowledging is reading and nothing else. The one assertion that matters here is that the
entry's actions survive the acknowledgement: the operator still has to take them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from kanso.errors import Exit

from . import mocked
from .conftest import HYP_ID, at, payload


@pytest.fixture
def escalated(runner: CliRunner, mocked_ws: Path) -> Path:
    """A workspace with one unread escalation: a run that stopped testing its thesis."""
    mocked.scripted(mocked_ws, align_check=[mocked.DRIFTED])
    assert at(runner, mocked_ws, "research", "begin", HYP_ID).exit_code == Exit.OK
    assert at(runner, mocked_ws, "align", "check", HYP_ID).exit_code == Exit.OK
    return mocked_ws


def entries(runner: CliRunner, root: Path) -> list[dict[str, Any]]:
    result = at(runner, root, "inbox", "--json")
    assert result.exit_code == Exit.OK, result.stdout
    listed: list[dict[str, Any]] = payload(result)["entries"]
    return listed


def test_an_empty_inbox_says_so_and_names_the_file(runner: CliRunner, mocked_ws: Path) -> None:
    result = at(runner, mocked_ws, "inbox", "--json")

    document = payload(result)
    assert result.exit_code == Exit.OK
    assert document["unread"] == 0
    assert document["entries"] == []
    assert document["path"] == str(mocked_ws / "escalations" / "inbox.md")


def test_the_unread_are_listed_with_what_to_do_about_them(
    runner: CliRunner, escalated: Path
) -> None:
    (entry,) = entries(runner, escalated)

    assert entry["kind"] == "misaligned"
    assert entry["subject"] == HYP_ID
    assert entry["acked_at"] is None
    assert f"kanso align check {HYP_ID}" in entry["actions"]


def test_the_human_list_is_the_entry_then_its_actions(runner: CliRunner, escalated: Path) -> None:
    result = at(runner, escalated, "inbox")

    assert result.exit_code == Exit.OK
    assert "misaligned" in result.stdout
    assert HYP_ID in result.stdout
    assert f"kanso align check {HYP_ID}" in result.stdout
    assert str(escalated / "escalations" / "inbox.md") in result.stdout


def test_acknowledging_takes_it_off_the_list(runner: CliRunner, escalated: Path) -> None:
    (entry,) = entries(runner, escalated)

    result = at(runner, escalated, "inbox", "ack", entry["id"], "--json")

    document = payload(result)
    assert result.exit_code == Exit.OK
    assert document["id"] == entry["id"]
    assert document["acked_at"] is not None
    assert document["unread"] == 0
    assert entries(runner, escalated) == []


def test_acknowledging_decides_nothing(runner: CliRunner, escalated: Path) -> None:
    (entry,) = entries(runner, escalated)

    result = at(runner, escalated, "inbox", "ack", entry["id"])

    assert result.exit_code == Exit.OK
    # The entry is read; the actions it offers are still the operator's to take.
    assert f"kanso align check {HYP_ID}" in result.stdout
    assert payload(at(runner, escalated, "hyp", "show", HYP_ID, "--json"))["status"] != "retired"


def test_acknowledging_twice_is_acknowledging_once(runner: CliRunner, escalated: Path) -> None:
    (entry,) = entries(runner, escalated)
    first = payload(at(runner, escalated, "inbox", "ack", entry["id"], "--json"))

    again = payload(at(runner, escalated, "inbox", "ack", entry["id"], "--json"))

    assert again["acked_at"] == first["acked_at"]


def test_acknowledging_something_that_is_not_an_entry_is_refused(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "inbox", "ack", "deadbeef", "--json")

    assert result.exit_code == Exit.VALIDATION
    assert "no escalation" in payload(result)["error"]


def test_the_file_keeps_every_line_the_command_ever_listed(
    runner: CliRunner, escalated: Path
) -> None:
    (entry,) = entries(runner, escalated)
    before = (escalated / "escalations" / "inbox.md").read_text(encoding="utf-8")

    at(runner, escalated, "inbox", "ack", entry["id"])

    assert (escalated / "escalations" / "inbox.md").read_text(encoding="utf-8") == before
    assert entry["id"] in before


def test_the_json_option_is_the_same_before_or_after_the_command(
    runner: CliRunner, mocked_ws: Path
) -> None:
    result = at(runner, mocked_ws, "--json", "inbox")

    assert payload(result)["unread"] == 0
