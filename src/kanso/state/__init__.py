"""Workspace state: one SQLite database behind `StateStore`, with versioned migrations.

`state.db` holds everything the workspace files do not: hypothesis status and pins, runs,
cards, certification plans and certificates, strategy versions, approvals, escalations,
the spend ledger, data snapshots, sessions, the event log, and the content-addressed
bytes of every file the research loop versions. `StateStore` is its only write path.
"""

from __future__ import annotations

from kanso.state.store import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    TABLES,
    Event,
    Migration,
    StateStore,
    migrations,
    usable,
)

__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "TABLES",
    "Event",
    "Migration",
    "StateStore",
    "migrations",
    "usable",
]
