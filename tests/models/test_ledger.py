"""The spend ledger: what a row holds, and what a slice of it adds up to."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from importlib import import_module

import pytest

from kanso.models import LedgerEntry, cost_of, ledger, spend
from kanso.models.ledger import NO_LANE
from kanso.state import StateStore

ledger_module = import_module("kanso.models.ledger")
"""The module, not the function of the same name the package re-exports."""

TODAY = date(2026, 3, 1)
"""The day every row in this module is stamped with."""


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger stamps its own rows, so a test that names a day has to fix the clock.

    Without this, a test computing "today" for itself disagrees with the row the ledger
    wrote whenever the two straddle UTC midnight — a rare, real failure that says nothing
    about the ledger.
    """

    class _Frozen:
        @staticmethod
        def now(tz: object = None) -> datetime:
            return datetime(TODAY.year, TODAY.month, TODAY.day, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(ledger_module, "datetime", _Frozen)


def entry(**changes: object) -> LedgerEntry:
    fields: dict[str, object] = {
        "task_class": "propose",
        "model": "mid_mock",
        "tokens_in": 1000,
        "tokens_out": 200,
        "cost": 0.01,
        "lane": "op",
    }
    fields.update(changes)
    return LedgerEntry(**fields)  # type: ignore[arg-type]


def test_an_empty_ledger_totals_nothing(store: StateStore) -> None:
    assert spend(store) == spend(store)
    assert spend(store).calls == 0
    assert spend(store).cost == 0.0
    assert spend(store).by_lane == {}


def test_a_call_is_recorded_with_its_tokens_and_cost(store: StateStore) -> None:
    ledger(store, entry())
    total = spend(store)
    assert (total.calls, total.tokens_in, total.tokens_out) == (1, 1000, 200)
    assert total.cost == 0.01
    assert total.by_lane == {"op": 0.01}


def test_totals_split_by_lane_and_by_day(store: StateStore) -> None:
    ledger(store, entry(lane="op", cost=0.01))
    ledger(store, entry(lane="a", cost=0.02))
    ledger(store, entry(lane="a", cost=0.03))
    total = spend(store)
    assert total.by_lane == {"a": 0.05, "op": 0.01}
    assert list(total.by_day) == [TODAY.isoformat()]


def test_a_call_that_belongs_to_no_lane_is_still_counted(store: StateStore) -> None:
    ledger(store, entry(task_class="check", lane=None, cost=0.5))
    total = spend(store)
    assert total.cost == 0.5
    assert total.by_lane == {NO_LANE: 0.5}


def test_a_lane_filter_selects_only_that_lane(store: StateStore) -> None:
    ledger(store, entry(lane="op", cost=0.01))
    ledger(store, entry(lane="a", cost=0.02))
    assert spend(store, lane="a").cost == 0.02
    assert spend(store, lane="a").calls == 1


def test_a_day_filter_selects_only_that_day(store: StateStore) -> None:
    ledger(store, entry(cost=0.01))
    assert spend(store, day=TODAY).calls == 1
    assert spend(store, day=TODAY - timedelta(days=1)).calls == 0


def test_the_two_filters_combine(store: StateStore) -> None:
    ledger(store, entry(lane="a", cost=0.02))
    assert spend(store, day=TODAY, lane="a").cost == 0.02
    assert spend(store, day=TODAY, lane="op").calls == 0


def test_the_cache_flag_round_trips_including_its_absence(store: StateStore) -> None:
    ledger(store, entry(cache_hit=True))
    ledger(store, entry(cache_hit=False))
    ledger(store, entry(cache_hit=None))
    rows = store.connection.execute("SELECT cache_hit FROM spend ORDER BY spend_id").fetchall()
    assert [row[0] for row in rows] == [1, 0, None]


def test_a_failed_attempt_is_a_row_like_any_other(store: StateStore) -> None:
    """Spend is spend: an answer that was rejected was still generated and billed."""
    ledger(store, entry(tokens_out=0, cost=0.004))
    assert spend(store).calls == 1
    assert spend(store).cost == 0.004


def test_cost_is_arithmetic_on_the_register_prices() -> None:
    assert cost_of(1_000_000, 0, 3.0, 15.0) == 3.0
    assert cost_of(0, 1_000_000, 3.0, 15.0) == 15.0
    assert cost_of(0, 0, 3.0, 15.0) == 0.0
