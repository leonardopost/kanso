"""The session record: what is written, how it reads back, and what it refuses to overwrite."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from nautilus_trader.model.data import Bar

from kanso.errors import PreconditionError, ValidationError
from kanso.replay import record
from kanso.replay.record import Intent, Point, Session
from kanso.schemas import load_yaml
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import FORWARD, INSTRUMENT, bars

STARTED = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


def session(**changes: object) -> Session:
    """One session record, with these fields replaced."""
    base: dict[str, object] = {
        "session_id": record.session_id("node", "demo_mr@1", FORWARD, STARTED),
        "mode": "node",
        "target": "demo_mr@1",
        "instruments": [INSTRUMENT],
        "from": FORWARD[0],
        "to": FORWARD[1],
        "speed": 0.0,
        "exec": "sandbox",
        "released": 2,
        "intents": 1,
        "clock_ns": 1_234,
        "started_at": STARTED,
        "ended_at": STARTED,
    }
    return Session.model_validate({**base, **changes})


def intent() -> Intent:
    """One recorded order."""
    return Intent(1_000, INSTRUMENT, "BUY", 10.0, "MARKET", None)


# --- the record ---------------------------------------------------------------


def test_a_session_round_trips_through_its_file(ws: Workspace) -> None:
    """What is written is what reads back, aliases and all."""
    written = record.write(ws, session(), [], [intent()])

    read_back = record.read(ws, written.session_id)

    assert read_back == written
    assert read_back.range == FORWARD
    assert read_back.exec_ == "sandbox"


def test_the_file_spells_the_reserved_names_as_the_record_does(ws: Workspace) -> None:
    """`from` and `exec` are the field names, whatever Python calls them."""
    written = record.write(ws, session(), [], [])
    text = (record.session_dir(ws, written.session_id) / record.SESSION_FILE).read_text()

    assert "from: '2024-03-01'" in text
    assert "exec: sandbox" in text


def test_a_second_session_of_the_same_thing_gets_its_own_id(ws: Workspace) -> None:
    """A session is never overwritten, however fast two of them run."""
    first = record.write(ws, session(), [], [])
    second = record.write(ws, session(), [], [])
    third = record.write(ws, session(), [], [])

    assert first.session_id != second.session_id != third.session_id
    assert second.session_id.endswith("-2")
    assert third.session_id.endswith("-3")


def test_a_session_id_names_the_path_and_the_target() -> None:
    """Two paths over one target never collide, and two targets never do either."""
    node = record.session_id("node", "demo_mr@1", FORWARD, STARTED)
    engine = record.session_id("engine", "demo_mr@1", FORWARD, STARTED)
    other = record.session_id("node", "other@1", FORWARD, STARTED)

    assert node != engine
    assert node != other
    assert node.startswith("20240301T120000Z-node-")


def test_an_unknown_session_is_refused(ws: Workspace) -> None:
    """A session that was never written cannot be shown."""
    with pytest.raises(PreconditionError, match="no session"):
        record.read(ws, "nothing")


def test_listing_a_workspace_with_no_sessions_is_empty(ws: Workspace) -> None:
    """A workspace that has replayed nothing lists nothing."""
    assert record.list_sessions(ws) == []


def test_a_directory_without_a_record_is_not_a_session(ws: Workspace) -> None:
    """Only a directory holding a record counts, so scratch beside them is ignored."""
    record.write(ws, session(), [], [])
    (record.sessions_path(ws) / "scratch").mkdir()

    assert len(record.list_sessions(ws)) == 1


def test_a_speed_below_zero_is_not_a_session() -> None:
    """The record refuses what the runner refuses."""
    with pytest.raises(ValidationError, match="speed"):
        session(speed=-1.0)


# --- the stream ---------------------------------------------------------------


def test_the_stream_round_trips(ws: Workspace) -> None:
    """A released point reads back with both of its timestamps and its instrument."""
    points = [Point.of(bar) for bar in bars(FORWARD)[:3]]

    written = record.write(ws, session(), points, [])

    assert record.stream_of(ws, written.session_id) == tuple(points)


def test_the_intents_round_trip(ws: Workspace) -> None:
    """An order reads back exactly, including a price it did not carry."""
    limit = Intent(2_000, INSTRUMENT, "SELL", 5.0, "LIMIT", 10.25)

    written = record.write(ws, session(), [], [intent(), limit])

    assert record.intents_of(ws, written.session_id) == (intent(), limit)


def test_an_intent_is_read_off_the_tuple_the_runner_records() -> None:
    """The runner records a tuple; the session records the same six fields."""
    made = Intent.of((1_000, INSTRUMENT, "BUY", 10.0, "MARKET", None))

    assert made == intent()


def test_a_bar_is_filed_under_its_instrument() -> None:
    """A bar names its instrument through its bar type, not directly."""
    bar = bars(FORWARD)[0]

    assert isinstance(bar, Bar)
    assert Point.of(bar).instrument == INSTRUMENT


def test_a_market_wide_point_names_no_instrument() -> None:
    """A point belonging to no instrument records none rather than inventing one."""

    class Anonymous:
        ts_init = 5
        ts_event = 4

    assert Point.of(Anonymous()).instrument is None


def test_a_custom_point_is_read_through_its_wrapper() -> None:
    """The catalog returns a custom type wrapped, and the record looks inside."""

    class Inner:
        instrument_id = INSTRUMENT

    class Wrapped:
        ts_init = 7
        ts_event = 6
        data = Inner()

    made = Point.of(Wrapped())

    assert made.type == "Inner"
    assert made.instrument == INSTRUMENT


def test_a_missing_stream_file_is_refused(ws: Workspace) -> None:
    """A session whose stream was deleted says so rather than reading as empty."""
    written = record.write(ws, session(), [], [])
    (record.session_dir(ws, written.session_id) / record.STREAM_FILE).unlink()

    with pytest.raises(PreconditionError, match="is missing"):
        record.stream_of(ws, written.session_id)


# --- the row ------------------------------------------------------------------


def test_the_row_carries_the_session_clock(ws: Workspace, store: StateStore) -> None:
    """The replay position is exact, so it is stored as digits rather than as a timestamp."""
    written = record.write(ws, session(clock_ns=1_711_000_000_123_456_789), [], [])
    record.insert(store, written)

    row = store.connection.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (written.session_id,)
    ).fetchone()

    assert int(row["clock_ts"]) == 1_711_000_000_123_456_789
    assert row["to_ts"] == FORWARD[1].isoformat()


def test_a_session_without_a_clock_records_none(ws: Workspace, store: StateStore) -> None:
    """A replay that released nothing has no position to resume from."""
    written = record.write(ws, session(clock_ns=None, ended_at=None), [], [])
    record.insert(store, written)

    row = store.connection.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (written.session_id,)
    ).fetchone()

    assert row["clock_ts"] is None
    assert row["ended_at"] is None


def test_the_written_file_validates_as_the_model(ws: Workspace) -> None:
    """The file on disk is the schema, not a rendering of it."""
    written = record.write(ws, session(), [], [])
    path = record.session_dir(ws, written.session_id) / record.SESSION_FILE

    assert load_yaml(Session, path).from_ == date(2024, 3, 1)
