"""The append-only event log and its typed accessor."""

from __future__ import annotations

import pytest

from kanso.errors import ValidationError
from kanso.state import StateStore


def test_events_are_appended_in_order_with_increasing_ids(store: StateStore) -> None:
    first = store.event("initialised", "workspace", {"path": "/tmp/ws"})
    second = store.event("migrated", "state.db", {"applied": ["0001_init.sql"]})
    assert second > first

    events = store.events()
    assert [e.event_id for e in events] == [first, second]
    assert [e.kind for e in events] == ["initialised", "migrated"]
    assert events[0].subject == "workspace"
    assert events[0].detail == {"path": "/tmp/ws"}
    assert events[1].detail == {"applied": ["0001_init.sql"]}
    assert events[0].ts <= events[1].ts


def test_detail_defaults_to_an_empty_object(store: StateStore) -> None:
    store.event("noticed", "demo_mr")
    assert store.events()[0].detail == {}


def test_events_filter_by_kind_and_subject_and_limit(store: StateStore) -> None:
    store.event("carded", "demo_mr", {"n": 1})
    store.event("carded", "demo_mr", {"n": 2})
    store.event("carded", "other", {"n": 3})
    store.event("drifted", "demo_mr", {})

    assert len(store.events(kind="carded")) == 3
    assert len(store.events(subject="demo_mr")) == 3
    assert len(store.events(kind="carded", subject="demo_mr")) == 2
    assert [e.detail["n"] for e in store.events(kind="carded", limit=2)] == [1, 2]
    assert store.events(kind="never-happened") == []


def test_a_detail_that_is_not_json_is_refused(store: StateStore) -> None:
    with pytest.raises(ValidationError, match="JSON-serialisable") as caught:
        store.event("bad", "demo_mr", {"conn": object()})
    assert caught.value.code == 3
    assert store.events() == []


def test_events_survive_the_store_being_reopened(store: StateStore) -> None:
    store.event("initialised", "workspace")
    path = store.path
    store.close()
    with StateStore(path) as reopened:
        assert [e.kind for e in reopened.events()] == ["initialised"]
