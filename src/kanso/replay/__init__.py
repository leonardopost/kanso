"""Replay: running a certified thing over historical data on both code paths.

`run` replays a target — a composed strategy version, or a hypothesis's card — from its
forward window through the end of the catalog, on the live code path (`node`) or the
research one (`engine`), and writes a session. `parity` runs both and compares the order
intents they produced, which is the check that the deployed path is the researched one.

Nothing here creates a card, moves a hypothesis's `best` or certifies anything: a replay is
evaluation over the window that judges a deployment, and evidence taken from it and fed back
into research would be the embargo undone. What it does record beside its session, when its
range reaches its target's forward window, is that it read it: a look of the target's lineage
at that window (`kanso.certify.looks`), one for a parity's two paths, and none for a parity of
a version over days a stage already showed it.
"""

from __future__ import annotations

from kanso.replay.parity import Divergence, Parity, compare, of_sessions, parity
from kanso.replay.record import Intent, Point, Session
from kanso.replay.run import ENGINE, MODES, NODE, last_day, run, sessions, show
from kanso.replay.target import Target, resolve

__all__ = [
    "ENGINE",
    "MODES",
    "NODE",
    "Divergence",
    "Intent",
    "Parity",
    "Point",
    "Session",
    "Target",
    "compare",
    "last_day",
    "of_sessions",
    "parity",
    "resolve",
    "run",
    "sessions",
    "show",
]
