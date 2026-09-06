"""Where a command finds the workspace, the state store and the global options.

Every command needs the same three things and finds them the same way, so the lookup
lives here rather than once per command module. `--workspace` and `--json` are options of
the application, and a command may repeat `--json` for itself; `global_json` is what makes
`kanso --json data show` and `kanso data show --json` the same invocation.

A command that reads or writes state opens the store through `store`, which refuses a
database with migrations pending rather than applying them behind the operator's back:
`init` migrates a fresh workspace and `migrate` is the one command that moves the schema.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import typer

from kanso import workspace
from kanso.state import StateStore, usable
from kanso.workspace import Workspace

STATE_DB = "state.db"
"""The workspace file the state store lives in."""


def global_json(ctx: typer.Context) -> bool:
    """Whether `--json` was given to the application rather than to the command."""
    return bool(ctx.find_root().params.get("as_json"))


def workspace_option(ctx: typer.Context) -> Path | None:
    """The `--workspace` the application was given, wherever the command sits."""
    value = ctx.find_root().obj
    return value if isinstance(value, Path) else None


def open_workspace(ctx: typer.Context) -> Workspace:
    """The workspace the command acts on, discovered from `--workspace` or the cwd."""
    return workspace.find(workspace_option(ctx))


@contextmanager
def store(ws: Workspace) -> Iterator[StateStore]:
    """The workspace's state store, open for the command and closed after it."""
    with StateStore(ws.path(STATE_DB)) as opened:
        usable(opened, ws.path(STATE_DB))
        yield opened
