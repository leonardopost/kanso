"""Runs and cards as rows: what the store enforces, and what it hands back."""

from __future__ import annotations

import pytest

from kanso.errors import PreconditionError
from kanso.research import loop, records
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import RAISING, REVERTING


def test_a_hypothesis_that_never_ran_has_no_run_and_no_best(store: StateStore) -> None:
    assert records.active(store, "nobody_here") is None
    assert records.runs_of(store, "nobody_here") == []
    assert records.best_of(store, "nobody_here") == (None, None)
    assert records.n_trials(store, "nobody_here") == 0


def test_a_second_active_run_of_one_hypothesis_cannot_be_written(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    duplicate = run.model_copy(update={"run_id": "another", "tag": "20240102-1"})
    with pytest.raises(PreconditionError, match="already has an active run"):
        records.insert(store, duplicate)


def test_runs_come_back_in_the_order_they_were_started(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    first = loop.begin(ws, store, registered)
    loop.end(ws, store, registered)
    second = loop.begin(ws, store, registered)

    held = records.runs_of(store, registered)
    assert [run.run_id for run in held] == [first.run_id, second.run_id]
    assert held[0].ended_at is not None
    assert held[1].ended_at is None


def test_a_card_comes_back_as_it_was_written(
    ws: Workspace, store: StateStore, registered: str
) -> None:
    run = loop.begin(ws, store, registered)
    (ws.root / run.dir / "strategy.py").write_bytes(REVERTING)
    written = loop.card(ws, store, registered, "the trough rule")

    read = records.cards_of(store, registered)[-1]
    assert read.model_dump(exclude={"created_at"}) == written.model_dump(exclude={"created_at"})
    assert read.venue_model.venue == "XNAS"
    assert read.aligned is True
    assert read.crash_tail is None


def test_a_crashed_card_keeps_its_tail(ws: Workspace, store: StateStore, registered: str) -> None:
    run = loop.begin(ws, store, registered)
    (ws.root / run.dir / "strategy.py").write_bytes(RAISING)
    loop.card(ws, store, registered, "boom")

    read = records.cards_of(store, registered)[-1]
    assert read.status == "crash"
    assert read.crash_tail is not None


def test_the_tag_counts_the_runs_of_that_day(store: StateStore) -> None:
    from datetime import date

    assert records.next_tag(store, "demo_mr", date(2024, 5, 6)) == "20240506-1"
