"""Screens: measuring a relationship in real data before any lane is spent on it.

A screen runs no strategy, places no order and calls no model. It reads declared series
through the runner's own reader, measures declared relationships between them over a
declared lattice, sets each against the hurdle a trade would be charged, and records the
result under the pins that produced it. What a result is for — whether a hypothesis is
worth autoresearch's tokens — is decided by whoever reads it; a screen never refuses a lane.
"""

from __future__ import annotations

from kanso.screen.data import Fetched, LegPlan, fetch, plan
from kanso.screen.files import (
    SCREEN_FILE,
    SCREENS,
    Validated,
    check_id,
    scaffold,
    screen_dir,
    validate,
)
from kanso.screen.library import catalogue, check, screen_version

__all__ = [
    "SCREENS",
    "SCREEN_FILE",
    "Fetched",
    "LegPlan",
    "Validated",
    "catalogue",
    "check",
    "check_id",
    "fetch",
    "plan",
    "scaffold",
    "screen_dir",
    "screen_version",
    "validate",
]
