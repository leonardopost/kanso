"""`kanso inbox`: what is waiting for a person, and marking that it has been read.

With no argument it lists the entries nobody has acknowledged, oldest first, each with the
commands its kind offers over its subject — so the answer to "what now" is on the line
that raised the question. `ack` marks one entry read.

`ack` is never an approval. It writes one timestamp on one row: it moves no capital,
changes no status and stands for no decision, so acknowledging that a hypothesis failed
still leaves it failed and acknowledging that a version is promotable still leaves the
promotion to `promote --live --as NAME`. Acknowledging twice is the same as once.

The file `escalations/inbox.md` is append-only and is never rewritten, so a line
acknowledged an hour ago still reads as unchecked there; the rows are what this command
answers from, and the file is the operator's own permanent log.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from kanso import inbox
from kanso.cli.context import global_json, open_workspace, store
from kanso.cli.render import Report, emit, field, indent
from kanso.workspace import Workspace

app = typer.Typer(help="Unread escalations, and acknowledging one.", invoke_without_command=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON object.")]

KIND, SUBJECT = 14, 20
"""Widths of the kind and subject columns of the human list."""


@app.callback(invoke_without_command=True)
def inbox_command(ctx: typer.Context, as_json: JsonOption = False) -> None:
    """List the escalations nobody has acknowledged."""
    if ctx.invoked_subcommand is not None:
        return
    emit(as_json or global_json(ctx), lambda: _unread(open_workspace(ctx)))


@app.command("ack")
def ack_command(
    ctx: typer.Context,
    entry_id: Annotated[str, typer.Argument(metavar="ID", help="An escalation id.")],
    as_json: JsonOption = False,
) -> None:
    """Mark one entry read. Never an approval, and idempotent."""
    emit(as_json or global_json(ctx), lambda: _ack(open_workspace(ctx), entry_id))


# -- command bodies ---------------------------------------------------------------


def _unread(ws: Workspace) -> Report:
    with store(ws) as opened:
        entries = inbox.unread(opened)
    data: dict[str, Any] = {
        "unread": len(entries),
        "entries": [entry.payload() for entry in entries],
        "path": str(inbox.inbox_file(ws)),
    }
    lines = [field("unread", f"{len(entries)} escalation(s)")]
    for entry in entries:
        lines.append(
            f"{entry.escalation_id} {entry.kind:<{KIND}}{entry.subject:<{SUBJECT}}{entry.summary}"
        )
        lines.append(indent(entry.actions, len(entry.escalation_id) + 1))
    lines.append(field("file", inbox.inbox_file(ws)))
    return Report(data=data, lines=tuple(lines))


def _ack(ws: Workspace, entry_id: str) -> Report:
    with store(ws) as opened:
        entry = inbox.ack(opened, entry_id)
        left = len(inbox.unread(opened))
    data: dict[str, Any] = {**entry.payload(), "unread": left}
    lines = (
        field("acked", f"{entry.escalation_id} · {entry.kind} · {entry.subject}"),
        field("summary", entry.summary),
        # Acknowledging is reading, not deciding: the actions the entry offers are still
        # the operator's to take.
        field("actions", entry.actions),
        field("unread", f"{left} left"),
    )
    return Report(data=data, lines=lines)
