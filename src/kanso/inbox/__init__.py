"""Escalations: the few things kanso interrupts an operator for, and where they land.

An autonomous loop that asked for guidance would not be autonomous, so kanso asks for
almost nothing: five kinds of event, each one a decision the operator alone can make or a
fact they alone can act on. Everything else is decided by a rule and recorded.

Each escalation is three writes that say the same thing three ways. A row in the state
store, so `status` can count what is unread without reading a file. A line appended to
`escalations/inbox.md`, so the operator's own editor and their agent see it without kanso
running. An event, so the log a run is reconstructed from carries the moment it happened.

Acknowledging one marks the line read and is never an approval: nothing in this package
moves capital, and the one act that does is a named CLI approval elsewhere.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from kanso.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "ESCALATED",
    "INBOX",
    "KINDS",
    "SUMMARY_LIMIT",
    "Escalation",
    "ack",
    "escalate",
    "inbox_file",
    "unread",
]

KINDS: Final = ("misaligned", "cert_failed", "promotable", "demoted", "deploy_blocked")
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
        tail = f" · actions: {self.actions}" if self.actions else ""
        return (
            f"- [{mark}] {self.escalation_id} {self.created_at} {self.kind} "
            f"{self.subject} — {self.summary}{tail}"
        )

    def payload(self) -> dict[str, object]:
        """The entry as one JSON object."""
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

    The summary is truncated rather than refused: an escalation is a message to a person
    and losing it because a caller was verbose would be the worse failure.
    """
    if kind not in KINDS:
        raise ValidationError(
            f"escalation: {kind!r} is not an escalation kind",
            remedy=f"one of {', '.join(KINDS)}",
        )
    entry = Escalation(
        escalation_id=uuid.uuid4().hex[:_ID_LENGTH],
        kind=kind,
        subject=subject,
        summary=_short(summary),
        actions=actions.replace("\n", " ").strip(),
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
    store.event(ESCALATED, subject, {"id": entry.escalation_id, "kind": kind})
    # The configured webhook posts the same entry; it arrives with the monitor.
    return entry


def unread(store: StateStore) -> list[Escalation]:
    """Every entry nobody has acknowledged, oldest first."""
    rows = store.connection.execute(
        "SELECT * FROM escalations WHERE acked_at IS NULL ORDER BY created_at, escalation_id"
    ).fetchall()
    return [_entry(row) for row in rows]


def ack(store: StateStore, escalation_id: str) -> Escalation:
    """Mark one entry read. Never an approval, and idempotent."""
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


def _short(summary: str) -> str:
    one_line = " ".join(summary.split())
    if len(one_line) <= SUMMARY_LIMIT:
        return one_line
    return one_line[: SUMMARY_LIMIT - 1].rstrip() + "…"


def _append(ws: Workspace, entry: Escalation) -> None:
    """Append the line, creating the file when a workspace predates it."""
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
