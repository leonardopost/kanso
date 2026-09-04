"""Escalations: the few things kanso interrupts an operator for, and where they land.

An autonomous loop that asked for guidance would not be autonomous, so kanso asks for
almost nothing: five kinds of event, each one a decision the operator alone can make or a
fact they alone can act on. Everything else is decided by a rule and recorded.

Each escalation is three writes that say the same thing three ways, in this order. A row
in the state store, so `status` can count what is unread without reading a file. A line
appended to `escalations/inbox.md`, so the operator's own editor and their agent see it
without kanso running. An event, so the log a run is reconstructed from carries the moment
it happened. The row is written first because it is the source of truth for what is
unread: a row whose line never landed is a cosmetic loss, a line nobody can acknowledge
is not.

The file is strictly append-only and is never rewritten. Nothing here opens it for
anything but appending, so acknowledging an entry moves the row and leaves every byte
already in the file exactly where it was — which is what lets an operator keep their own
notes in it, and what makes it safe to tail.

Every kind carries the commands it offers, because the first reader of these lines is an
agent that has to decide what to do next, and one shape of line is cheaper to read than
five. A caller with something more specific to offer passes its own actions instead.

Acknowledging an entry marks it read and is never an approval: nothing in this module
moves capital, and the one act that does is a named CLI approval elsewhere.

`escalate` is the single point every kind passes through, which is the seam the rest of
this feature needs: the optional escalation webhook is a JSON POST of `payload()` to the
configured URL at the end of it, and a new kind is an entry in `ACTIONS`.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from kanso.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from collections.abc import Mapping
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "ACTIONS",
    "ESCALATED",
    "INBOX",
    "KINDS",
    "SEPARATOR",
    "SUMMARY_LIMIT",
    "Escalation",
    "ack",
    "escalate",
    "inbox_file",
    "unread",
]

SEPARATOR: Final = " · "
"""What separates the summary from the actions, and one action from the next."""

ACTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "misaligned": (
        "kanso research show {subject}",
        "kanso align check {subject}",
    ),
    "cert_failed": (
        "kanso cert show {subject}",
        "kanso research show {subject}",
        "kanso hyp retire {subject}",
    ),
    "promotable": (
        "kanso strat show {subject}",
        "kanso promote {subject} --live --as <your name>",
    ),
    "demoted": (
        "kanso strat show {subject}",
        "kanso portfolio show",
    ),
    "deploy_blocked": (
        "kanso portfolio show",
        "kanso doctor",
    ),
}
"""What each kind offers, as commands over its subject — a hypothesis id for the two
research kinds, a strategy version for the three portfolio ones.

Replanning is deliberately absent from `cert_failed`: a plan rewritten because its gates
failed is no longer a blind one, and the ways out of a failed hypothesis are more
research or retirement. `promote` keeps its `--as`, since a promotion without a name is
refused and an agent reading this line should see that before it runs one.
"""

KINDS: Final = tuple(ACTIONS)
"""The whole of what kanso escalates: drift, repeated certification failure, a version
ready for real capital, a demotion that already happened, a deployment that cannot start."""

INBOX: Final = ("escalations", "inbox.md")
"""The append-only file, relative to the workspace root."""

SUMMARY_LIMIT: Final = 200
"""A summary is one line an operator reads at a glance, so it is capped rather than wrapped."""

ESCALATED: Final = "escalated"
"""The event kind every escalation appends, under its subject."""

_ID_LENGTH: Final = 8


@dataclass(frozen=True)
class Escalation:
    """One entry: what happened, to what, and what the operator can do about it."""

    escalation_id: str
    kind: str
    subject: str
    summary: str
    actions: str
    created_at: str
    acked_at: str | None = None

    def line(self) -> str:
        """The inbox line, in the one shape every reader of the file parses."""
        mark = "x" if self.acked_at else " "
        tail = f"{SEPARATOR}actions: {self.actions}" if self.actions else ""
        return (
            f"- [{mark}] {self.escalation_id} {self.created_at} {self.kind} "
            f"{self.subject} — {self.summary}{tail}"
        )

    def payload(self) -> dict[str, object]:
        """The entry as one JSON object: what `status` prints and what a webhook posts."""
        return {
            "id": self.escalation_id,
            "kind": self.kind,
            "subject": self.subject,
            "summary": self.summary,
            "actions": self.actions,
            "created_at": self.created_at,
            "acked_at": self.acked_at,
        }


def inbox_file(ws: Workspace) -> Path:
    """Where the inbox lives in this workspace."""
    return ws.path(*INBOX)


def escalate(
    ws: Workspace,
    store: StateStore,
    kind: str,
    subject: str,
    summary: str,
    actions: str = "",
) -> Escalation:
    """Record one escalation, append its line and log the event.

    The summary is folded to one line and truncated rather than refused: an escalation is
    a message to a person, and losing it because a caller was verbose would be the worse
    failure. A kind outside the five, or an entry naming nothing or saying nothing, is a
    caller's bug and is refused loudly instead of landing as an unreadable line.

    Actions default to what the kind offers over this subject; a caller passing its own
    replaces them.
    """
    if kind not in ACTIONS:
        raise ValidationError(
            f"escalation: {kind!r} is not an escalation kind",
            remedy=f"one of {', '.join(KINDS)}",
        )
    named = subject.strip()
    said = _short(summary)
    if not named or not said:
        raise ValidationError(
            f"escalation: a {kind} entry needs a subject and a summary",
            remedy="pass the hypothesis or strategy it is about, and one line about it",
        )
    entry = Escalation(
        escalation_id=uuid.uuid4().hex[:_ID_LENGTH],
        kind=kind,
        subject=named,
        summary=said,
        actions=_actions(kind, named, actions),
        created_at=datetime.now(tz=UTC).isoformat(),
    )
    store.connection.execute(
        "INSERT INTO escalations (escalation_id, kind, subject, summary, actions, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            entry.escalation_id,
            entry.kind,
            entry.subject,
            entry.summary,
            entry.actions,
            entry.created_at,
        ),
    )
    _append(ws, entry)
    store.event(ESCALATED, entry.subject, {"id": entry.escalation_id, "kind": kind})
    return entry


def unread(store: StateStore) -> list[Escalation]:
    """Every entry nobody has acknowledged, oldest first.

    The rows answer this, not the file: the file is append-only, so a line acknowledged an
    hour ago still reads as unchecked there.
    """
    rows = store.connection.execute(
        "SELECT * FROM escalations WHERE acked_at IS NULL ORDER BY created_at, escalation_id"
    ).fetchall()
    return [_entry(row) for row in rows]


def ack(store: StateStore, escalation_id: str) -> Escalation:
    """Mark one entry read. Never an approval, and idempotent.

    This writes one timestamp on one row. It moves no capital, changes no status and
    stands for no decision: an operator who has read that a version is promotable still
    promotes it by name, and one who has read that a hypothesis failed still retires it.
    """
    row = store.connection.execute(
        "SELECT * FROM escalations WHERE escalation_id = ?", (escalation_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(
            f"inbox: no escalation {escalation_id!r}",
            remedy="run `kanso inbox` to list the unread entries",
        )
    entry = _entry(row)
    if entry.acked_at is not None:
        return entry
    now = datetime.now(tz=UTC).isoformat()
    store.connection.execute(
        "UPDATE escalations SET acked_at = ? WHERE escalation_id = ?", (now, escalation_id)
    )
    return replace(entry, acked_at=now)


def _actions(kind: str, subject: str, given: str) -> str:
    """The caller's actions, folded to one line, else the ones the kind offers."""
    mine = " ".join(given.split())
    if mine:
        return mine
    return SEPARATOR.join(action.format(subject=subject) for action in ACTIONS[kind])


def _short(summary: str) -> str:
    one_line = " ".join(summary.split())
    if len(one_line) <= SUMMARY_LIMIT:
        return one_line
    return one_line[: SUMMARY_LIMIT - 1].rstrip() + "…"


def _append(ws: Workspace, entry: Escalation) -> None:
    """Append the line, creating the file when a workspace predates it.

    Append mode is the whole of the write path: no reader, no rewrite, no truncation.
    """
    path = ws.path(*INBOX)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.line() + "\n")


def _entry(row: sqlite3.Row) -> Escalation:
    return Escalation(
        escalation_id=str(row["escalation_id"]),
        kind=str(row["kind"]),
        subject=str(row["subject"]),
        summary=str(row["summary"]),
        actions=str(row["actions"]),
        created_at=str(row["created_at"]),
        acked_at=None if row["acked_at"] is None else str(row["acked_at"]),
    )
