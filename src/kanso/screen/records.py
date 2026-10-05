"""A screen's results: recorded under their pins, rendered beside the screen, read back.

A result is keyed by the screen's bytes, the snapshot and the measure library's version, and
is immutable: the same three again are the same measurement, and the stored row is returned
rather than a second measured. `state.db` is the record; the YAML file beside the screen is
a rendering of it, as a certificate's is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

from kanso.schemas import write_yaml
from kanso.schemas.screen import ScreenResult

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

SCREENED: Final = "screened"
"""The event a recorded result appends, under the screen's id."""


def stored(store: StateStore, sha: str, snapshot_id: str, version: str) -> ScreenResult | None:
    """The result already measured under these three pins, or `None`."""
    row = store.connection.execute(
        "SELECT result FROM screen_results"
        " WHERE screen_sha = ? AND snapshot_id = ? AND screen_version = ?",
        (sha, snapshot_id, version),
    ).fetchone()
    return None if row is None else ScreenResult.model_validate(json.loads(row[0]))


def record(store: StateStore, result: ScreenResult, instruments: list[str]) -> None:
    """Record a result and append its event, in one transaction."""
    document = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO screen_results (screen_sha, snapshot_id, screen_version, screen_id,"
            " hyp_id, window_start, window_end, instruments, result, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.sha,
                result.snapshot,
                result.version,
                result.screen,
                result.hyp,
                str(result.window.start),
                str(result.window.end),
                json.dumps(sorted(instruments)),
                json.dumps(document, sort_keys=True),
                result.created_at.isoformat(),
            ),
        )
    store.event(
        SCREENED,
        result.screen,
        {
            "sha": result.sha,
            "snapshot": result.snapshot,
            "version": result.version,
            "cells": len(result.cells),
            "worth_a_lane": result.summary.worth_a_lane,
        },
    )


def results(store: StateStore, screen_id: str | None = None) -> list[ScreenResult]:
    """Every result of one screen, or of every screen, newest first."""
    query = "SELECT result FROM screen_results"
    arguments: tuple[str, ...] = ()
    if screen_id is not None:
        query += " WHERE screen_id = ?"
        arguments = (screen_id,)
    rows = store.connection.execute(f"{query} ORDER BY created_at DESC", arguments).fetchall()
    return [ScreenResult.model_validate(json.loads(row[0])) for row in rows]


def render(ws: Workspace, result: ScreenResult) -> Path:
    """Write the result beside its screen, named by its three pins, and return the path."""
    directory = ws.path("screens", result.screen)
    directory.mkdir(parents=True, exist_ok=True)
    digest = result.version.rpartition("+")[2][:7]
    path = directory / f"{result.sha[:7]}-s{result.snapshot[:7]}-v{digest}.yaml"
    return write_yaml(result, path)
