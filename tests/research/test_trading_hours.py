"""A card that trades after the close is discarded by the session the operator required.

The workspace is the loop suite's, with each day's bar stamped at 16:30 in New York instead of
16:00 UTC: 21:30 UTC through January and February, on Eastern Standard Time. Every order the
reverting strategy sends is filled on a bar, so every fill it makes is outside a
09:30-16:00 session, and every position it opens is still held at the next morning's open.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from nautilus_trader.model.data import Bar

from kanso.data import catalog, snapshot
from kanso.data.instruments import resolve_universe
from kanso.env import write as write_envelope
from kanso.research import loop
from kanso.state import StateStore
from kanso.workspace import Workspace, find, init

from .conftest import (
    CLOSE_NS,
    ENVELOPE,
    INSTRUMENT,
    RESEARCH,
    REVERTING,
    SECOND_NS,
    SPAN,
    bars,
    classify,
    dataset,
    document,
    write_instruments,
)

AFTER_THE_CLOSE_NS = (21 * 3_600 + 30 * 60) * SECOND_NS
"""16:30 in New York on Eastern Standard Time, UTC-5."""

REGULAR = {"id": "trading_hours", "params": {"session": "09:30-16:00", "tz": "America/New_York"}}


def after_the_close(made: list[Bar]) -> list[Bar]:
    """The same bars, each moved from 16:00 UTC to 16:30 in New York."""
    shift = AFTER_THE_CLOSE_NS - CLOSE_NS
    return [
        Bar(
            bar.bar_type,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            ts_event=bar.ts_event + shift,
            ts_init=bar.ts_init + shift,
        )
        for bar in made
    ]


@pytest.fixture(scope="module")
def evening(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The loop suite's workspace, its bars printed after the New York close."""
    root = tmp_path_factory.mktemp("evening") / "ws"
    ws = init(root)
    with StateStore(ws.path("state.db")) as opened:
        opened.migrate()
    write_instruments(ws)
    resolve_universe(ws, [INSTRUMENT], RESEARCH[0])
    catalog.write(ws, after_the_close(bars(SPAN)), ref=dataset(), source="synthetic")
    snapshot.freeze(ws)
    write_envelope(ws, ENVELOPE)
    return root


@pytest.fixture
def ws(evening: Path, tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    shutil.copytree(evening, root)
    return find(root)


def test_a_card_that_trades_at_16_30_in_new_york_is_discarded(ws: Workspace) -> None:
    with StateStore(ws.path("state.db")) as store:
        hyp_id = classify(ws, store, document(required_constraints=[REGULAR]))
        run = loop.begin(ws, store, hyp_id)
        (ws.root / run.dir / "strategy.py").write_bytes(REVERTING)

        made = loop.card(ws, store, hyp_id, "buys the trough after the close")

    assert made.status == "discard"
    assert made.n_trades > 0
    (gate,) = [found for found in made.gate_results if found.id == "trading_hours"]
    assert not gate.passed
    evidence = gate.evidence
    assert (evidence["n_fills"], evidence["n_fills_outside"]) == (14, 14)
    assert evidence["earliest_fill_outside"] == {
        "instrument": INSTRUMENT,
        "side": "BUY",
        "ts_ns": 1_704_490_201_000_000_000,
        "local": "2024-01-05T16:30:01-05:00",
    }
    assert evidence["latest_fill_outside"]["local"] == "2024-01-31T16:30:01-05:00"
    assert evidence["latest_fill_outside"]["side"] == "SELL"
    assert (evidence["n_positions"], evidence["n_held_outside"]) == (7, 7)
    assert evidence["earliest_held_outside"]["session_closed"] == "2024-01-05T16:00:00-05:00"
