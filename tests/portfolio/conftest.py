"""A workspace with something composed in it, and the pieces a deployment needs beside it.

Deployment starts where composition stops, so this builds on the replay suite's workspace —
the same synthetic saw-tooth, extended into March so the forward window has data — and adds
what a stage needs: composed strategies indexed in the state store, and a portfolio file
whose stages can be reconfigured a test at a time.

Everything is synthetic and every instrument is `manual`, so no credential and no network is
reachable from any of it.
"""

from __future__ import annotations

from typing import Any

import pytest

from kanso.portfolio import files
from kanso.schemas import ExecutionClientSpec, Portfolio, StrategyFile
from kanso.state import StateStore
from kanso.strategy import files as strategy_files
from kanso.workspace import Workspace
from tests.replay.conftest import (
    CAPITAL,
    DOCUMENT,
    FLAT,
    FORWARD_START,
    HYP_ID,
    INSTRUMENT,
    REVERTING,
    carded,
    composed,
    document,
    prepared,
    store,
    ws,
)

__all__ = [
    "CAPITAL",
    "DOCUMENT",
    "FLAT",
    "FORWARD_START",
    "HYP_ID",
    "INSTRUMENT",
    "REVERTING",
    "prepared",
    "store",
    "ws",
]

WALL = ExecutionClientSpec(id="house", capital="real", clock="wall")
"""A broker's live client, as an adapter would declare one: real money on a wall clock."""

BROKER_PAPER = ExecutionClientSpec(id="house_paper", capital="broker_paper", clock="wall")
"""A broker's paper client: no real money, but still matching against current prices."""


def deployable(
    ws: Workspace,
    store: StateStore,
    hyp_id: str = HYP_ID,
    *,
    sleeve: bytes = REVERTING,
    doc: dict[str, Any] | None = None,
) -> StrategyFile:
    """A composed strategy, its implementation and its state rows: what `deploy` admits.

    Composition itself has its own tests; what deployment needs of it is a version in the
    `composed` state with an implementation on disk and a row in the store, which is exactly
    what `strat compose` leaves behind.
    """
    carded(ws, store, doc=doc or document(id=hyp_id), strategy=sleeve)
    file = composed(ws, store, hyp_id, sleeve=sleeve)
    strategy_files.record(store, hyp_id, file.latest())
    return file


def reconfigure(ws: Workspace, stage: str, **changes: Any) -> Portfolio:
    """Rewrite one stage of the portfolio file, as an operator editing it would."""
    portfolio = files.read(ws)
    updated = files.stage_of(portfolio, stage).model_copy(update=changes)
    written = files.with_stage(portfolio, stage, updated)
    files.write(ws, written)
    return written


def repin(ws: Workspace, strategy_id: str, **pins: Any) -> StrategyFile:
    """Rewrite one version's pins, so a deployment can be shown refusing them."""
    file = strategy_files.require(ws, strategy_id)
    latest = file.latest()
    repinned = latest.model_copy(update={"pins": latest.pins.model_copy(update=pins)})
    written = file.model_copy(update={"versions": [*file.versions[:-1], repinned]})
    strategy_files.write(ws, written)
    return written


def second_version(ws: Workspace, store: StateStore, strategy_id: str) -> int:
    """A second composed version of a strategy, as a later certification produces one."""
    from datetime import UTC, datetime

    from kanso.strategy.impl import generate as generate_impl
    from tests.replay.conftest import hypothesis

    held = strategy_files.require(ws, strategy_id)
    latest = held.latest()
    made = latest.model_copy(
        update={
            "version": latest.version + 1,
            "state": "composed",
            "created_at": datetime.now(tz=UTC),
        }
    )
    strategy_files.write(ws, strategy_files.appended(held, strategy_id, made))
    strategy_files.record(store, strategy_id, made)
    generate_impl(ws, store, strategy_id, made, hypothesis(id=strategy_id), CAPITAL)
    return made.version


@pytest.fixture
def composed_strategy(ws: Workspace, store: StateStore) -> StrategyFile:
    """The demo sleeve, composed and ready for the paper stage."""
    return deployable(ws, store)
