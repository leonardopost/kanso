"""Replaying a target: what it runs, what it records and what it refuses to touch."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
from datetime import date

import pytest

from kanso import replay
from kanso.errors import PreconditionError, ValidationError
from kanso.replay import record
from kanso.replay.run import REPLAYED
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.replay.conftest import (
    FLAT,
    FORWARD,
    FORWARD_START,
    INSTRUMENT,
    RAISING,
    carded,
    composed,
)


def test_replays_the_forward_window_by_default(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The range runs from the forward window's start to the last day the catalog serves."""
    session = replay.run(ws, store, hyp=carded_hyp)

    assert session.range == FORWARD
    assert session.mode == "node"
    assert session.instruments == [INSTRUMENT]
    assert session.released == (FORWARD[1] - FORWARD[0]).days + 1


def test_a_replay_trades_and_records_its_intents(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A strategy that trades the saw-tooth leaves its orders in the session's stream."""
    session = replay.run(ws, store, hyp=carded_hyp)
    intents = record.intents_of(ws, session.session_id)

    assert session.intents == len(intents) > 0
    assert {intent.instrument for intent in intents} == {INSTRUMENT}
    assert {intent.side for intent in intents} == {"BUY", "SELL"}


def test_the_stream_is_the_points_that_were_released(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """Every released point is on the record, in availability order."""
    session = replay.run(ws, store, hyp=carded_hyp)
    stream = record.stream_of(ws, session.session_id)

    assert len(stream) == session.released
    assert [point.ts_init for point in stream] == sorted(point.ts_init for point in stream)
    assert {point.type for point in stream} == {"Bar"}
    assert session.clock_ns == stream[-1].ts_init


def test_the_engine_path_replays_the_same_range(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The research code path replays the same days and records the same shape of session."""
    session = replay.run(ws, store, hyp=carded_hyp, mode=replay.ENGINE)

    assert session.mode == "engine"
    assert session.range == FORWARD
    assert session.intents > 0


def test_both_paths_use_the_simulated_execution_client(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """Replay executes against the simulated client whatever else a workspace configures."""
    node = replay.run(ws, store, hyp=carded_hyp)
    engine = replay.run(ws, store, hyp=carded_hyp, mode=replay.ENGINE)

    assert node.exec_ == engine.exec_ == "sandbox"


def test_an_explicit_range_is_honoured(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A named range replaces the default, and only its days are released."""
    session = replay.run(ws, store, hyp=carded_hyp, start=date(2024, 3, 5), end=date(2024, 3, 10))

    assert session.range == (date(2024, 3, 5), date(2024, 3, 10))
    assert session.released == 6


def test_a_backwards_range_is_refused(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A range that ends before it begins is a validation failure, not an empty session."""
    with pytest.raises(ValidationError, match="ends before it begins"):
        replay.run(ws, store, hyp=carded_hyp, start=date(2024, 3, 10), end=date(2024, 3, 5))


def test_an_unknown_mode_is_refused(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """There are two code paths and no others."""
    with pytest.raises(ValidationError, match="is not a code path"):
        replay.run(ws, store, hyp=carded_hyp, mode="paper")


def test_a_negative_speed_is_refused(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """Speed is a rate; a negative one names no replay at all."""
    with pytest.raises(ValidationError, match="is not a rate"):
        replay.run(ws, store, hyp=carded_hyp, speed=-1.0)


def test_a_universe_with_no_data_is_refused(ws: Workspace, store: StateStore) -> None:
    """Without a manifest covering the universe there is no last day to replay to."""
    with pytest.raises(PreconditionError, match="catalog holds nothing"):
        replay.last_day(ws, ("NOTHING.XNAS",))


def test_a_strategy_version_is_replayed_from_its_pins(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A composed version names the sleeve it runs, and the session names the version."""
    composed(ws, store, carded_hyp)
    session = replay.run(ws, store, strategy=carded_hyp)

    assert session.target == f"{carded_hyp}@1"
    assert session.intents > 0


def test_a_session_is_recorded_in_the_state_store(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """A stage finds its own replay position through the store, not through the directory."""
    session = replay.run(ws, store, hyp=carded_hyp)
    row = store.connection.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session.session_id,)
    ).fetchone()

    assert row["mode"] == "node"
    assert row["exec_id"] == "sandbox"
    assert int(row["clock_ts"]) == session.clock_ns
    assert row["from_ts"] == FORWARD_START.isoformat()


def test_a_replay_appends_one_event(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """The event log records that a replay happened and what it found."""
    session = replay.run(ws, store, hyp=carded_hyp)
    events = store.events(kind=REPLAYED)

    assert [event.subject for event in events] == [session.session_id]
    assert events[0].detail["target"] == session.target
    assert events[0].detail["crashed"] is False


def test_a_replay_creates_no_card_and_moves_no_best(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """Replay is evaluation: it writes a session and leaves research exactly as it was."""
    before = store.connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    best_before = store.connection.execute(
        "SELECT best_sha, best_metric FROM hypotheses WHERE hyp_id = ?", (carded_hyp,)
    ).fetchone()

    replay.run(ws, store, hyp=carded_hyp)

    after = store.connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    best_after = store.connection.execute(
        "SELECT best_sha, best_metric FROM hypotheses WHERE hyp_id = ?", (carded_hyp,)
    ).fetchone()
    certificates = store.connection.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]

    assert after == before
    assert tuple(best_after) == tuple(best_before)
    assert certificates == 0


def test_a_strategy_that_raises_stops_the_node_and_says_so(
    ws: Workspace, store: StateStore
) -> None:
    """A live engine swallows a handler's exception; the session reports it instead."""
    hyp_id = carded(ws, store, strategy=RAISING)
    session = replay.run(ws, store, hyp=hyp_id)
    events = store.events(subject=session.session_id)

    assert session.intents == 0
    assert events[0].detail["crashed"] is True
    assert 0 < session.released < (FORWARD[1] - FORWARD[0]).days + 1
    assert len(record.stream_of(ws, session.session_id)) == session.released
    assert session.clock_ns == record.stream_of(ws, session.session_id)[-1].ts_init


def test_a_strategy_that_trades_nothing_replays_cleanly(ws: Workspace, store: StateStore) -> None:
    """A session with no orders is a session, not a failure."""
    hyp_id = carded(ws, store, strategy=FLAT)
    session = replay.run(ws, store, hyp=hyp_id)

    assert session.intents == 0
    assert session.released > 0


def test_sessions_are_listed_and_shown(ws: Workspace, store: StateStore, carded_hyp: str) -> None:
    """A session can be found again by id and appears in the listing, oldest first."""
    first = replay.run(ws, store, hyp=carded_hyp, mode=replay.ENGINE)
    second = replay.run(ws, store, hyp=carded_hyp, mode=replay.NODE)

    listed = [session.session_id for session in replay.sessions(ws)]

    assert listed == sorted(listed)
    assert set(listed) == {first.session_id, second.session_id}
    assert replay.show(ws, first.session_id).target == first.target


def test_a_speed_paces_a_replay_without_changing_it(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """Speed is wall-clock pacing on top of the flow control, not instead of it."""
    unpaced = replay.run(ws, store, hyp=carded_hyp)
    paced = replay.run(ws, store, hyp=carded_hyp, speed=1e9)

    assert paced.speed == 1e9
    assert record.intents_of(ws, paced.session_id) == record.intents_of(ws, unpaced.session_id)


def test_the_research_runner_cannot_reach_replay() -> None:
    """Replay is evaluation over the forward window; the card runner must not import it.

    Checked in the import graph rather than by convention: a module the runner cannot
    reach is a module a card cannot call, whatever anyone later writes in it. A replay
    measured over the forward window and fed back into a keep decision would be the
    embargo undone, and this is what makes that impossible rather than discouraged.
    """
    reachable = _reachable("kanso.research.loop")

    assert not any(name.startswith("kanso.replay") for name in reachable)
    assert "kanso.nautilus.backtest" in reachable, "the walk reaches what a card does run"


def test_no_module_of_the_research_package_imports_replay() -> None:
    """The two things that do replay are reached from the research package, and neither is it.

    Ending a run certifies, and a certification replays both code paths over the
    certification window because comparing them is what the parity gate *is*. The daemon
    runs the monitoring pass beside its lanes, and a pass that demotes redeploys a stage.
    Both are things that happen around research rather than in it, and neither is a module
    of this package: what is asserted here is that no research module reaches replay itself.
    """
    for name in _research_modules():
        assert not any(imported.startswith("kanso.replay") for imported in _imports(name)), name


def _research_modules() -> list[str]:
    """Every module of the research package, by import path."""
    package = importlib.import_module("kanso.research")
    found = [info.name for info in pkgutil.iter_modules(package.__path__)]
    return ["kanso.research", *(f"kanso.research.{name}" for name in found)]


def _reachable(root: str) -> set[str]:
    """Every kanso module reachable from `root` through the imports its sources name."""
    found: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        pending.extend(_imports(name))
    return found


def _imports(name: str) -> list[str]:
    """The kanso modules one module names in an import, wherever the import stands.

    `from kanso.research import lanes` names `kanso.research.lanes` and not the package's
    own `__init__`: what the importing module can call is the submodule, and reading it as
    the package would make every module of a package reachable from every other one, which
    is a graph that says nothing. A name that is not a module — a class, a constant — is
    read as the package, which is what was executed to reach it. Indented imports count: a
    deferred import is still a call this module can make.
    """
    source = inspect.getsource(importlib.import_module(name))
    named: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import kanso"):
            named.append(stripped.split()[1])
        elif stripped.startswith("from kanso"):
            named.extend(_named_by(stripped))
    return [one for one in named if one.startswith("kanso")]


def _named_by(line: str) -> list[str]:
    """What one `from kanso... import ...` line names, resolved to modules where it can be."""
    package, _, imported = line.partition(" import ")
    package = package.split()[1]
    if not imported or imported.startswith("("):
        return [package]
    found: list[str] = []
    for piece in imported.split(","):
        name = piece.strip().split()[0].strip()
        found.append(f"{package}.{name}" if _is_module(f"{package}.{name}") else package)
    return found


def _is_module(name: str) -> bool:
    """Whether this dotted name is an importable module rather than something inside one."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def test_a_session_can_be_replayed_from_its_own_record(
    ws: Workspace, store: StateStore, carded_hyp: str
) -> None:
    """The record holds enough to run the replay again, and running it again repeats it."""
    first = replay.run(ws, store, hyp=carded_hyp)

    again = replay.run(ws, store, hyp=carded_hyp, start=first.from_, end=first.to)

    assert again.session_id != first.session_id
    assert record.intents_of(ws, again.session_id) == record.intents_of(ws, first.session_id)
    assert record.stream_of(ws, again.session_id) == record.stream_of(ws, first.session_id)
    assert again.clock_ns == first.clock_ns
