"""The daemon: the lock, the lanes, what a worker takes next, and what stopping keeps.

The process machinery is exercised for real once — a detached supervisor that writes a pid,
holds a lock and stops on a signal — and everything else is exercised in this process, so
the loops are tested rather than merely started.
"""

from __future__ import annotations

import contextlib
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kanso.errors import KansoError, PreconditionError
from kanso.hyp import set_status
from kanso.nautilus import backtest
from kanso.research import daemon, explore, lanes, records, scheduler
from kanso.research import driver as research_driver
from kanso.research import loop as research_loop
from kanso.state import StateStore
from kanso.workspace import Workspace
from tests.processes import children, ends, running

from .conftest import DOCUMENT, classify, document
from .mocked import ALIGNED, SEED, proposal, scripted, write_script
from .test_scheduler import open_run


@pytest.fixture(autouse=True)
def quiet_signals() -> Iterator[None]:
    """The loops install handlers in whatever process runs them, including this one."""
    saved = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    daemon.clear_stop()
    yield
    for number, handler in saved.items():
        signal.signal(number, handler)
    daemon.clear_stop()


@pytest.fixture
def stopped(ws: Workspace) -> Iterator[Workspace]:
    """Whatever a test starts, leave nothing running behind it."""
    yield ws
    if daemon.pid_of(ws) is not None:  # pragma: no cover - only when a test failed early
        with contextlib.suppress(KansoError, OSError):
            daemon.stop(ws)


class FakeChild:
    """A child that can be asked to stop, and one that has to be made to.

    One that is not alive has ended with `code`, as `Popen.returncode` reads: the status it
    exited with, or the negated number of the signal that killed it.
    """

    def __init__(
        self, alive: bool = True, stubborn: bool = False, code: int = 0, pid: int = 4242
    ) -> None:
        self.alive = alive
        self.stubborn = stubborn
        self.code = code
        self.pid = pid
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.alive else self.code

    def terminate(self) -> None:
        self.terminated = True
        self.alive = self.stubborn

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        if self.stubborn and timeout is not None:
            raise subprocess.TimeoutExpired("child", timeout)
        return 0


# --- the command line a child is started with --------------------------------


def test_the_supervisor_is_kept_awake_on_macos_and_plain_elsewhere(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert daemon._argv(ws.root, "serve", awake=True)[:2] == list(daemon.CAFFEINATE)
    assert daemon._argv(ws.root, "lane", "l1")[0] == sys.executable

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert daemon._argv(ws.root, "serve", awake=True)[0] == sys.executable


def test_the_lanes_are_the_envelope_s_and_never_the_interactive_one(ws: Workspace) -> None:
    assert daemon.lane_names(ws) == ("l1", "l2")
    assert lanes.DEFAULT_LANE not in daemon.lane_names(ws)

    ws.path("envelope.yaml").unlink()
    with pytest.raises(PreconditionError, match="no envelope"):
        daemon.lane_names(ws)


# --- what a lane takes next --------------------------------------------------


def test_a_lane_finishes_its_own_run_before_it_takes_anything_new(
    ws: Workspace, store: StateStore
) -> None:
    mine = classify(ws, store, DOCUMENT)
    queued = classify(ws, store, document(id="demo_two"))
    open_run(store, mine, lane="l1")
    scheduler.enqueue(store, queued, priority=9)

    assert daemon.claim(store, "l1") == mine
    assert daemon.claim(store, "l2") == queued
    assert daemon.claim(store, "l2") is None


def test_the_interactive_lane_s_run_is_not_a_daemon_lane_s_work(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id, lane="op")
    scheduler.enqueue(store, hyp_id)

    assert daemon.claim(store, "l1") is None
    assert [item.hyp_id for item in scheduler.queued(store)] == [hyp_id]


# --- the three loops ---------------------------------------------------------


def test_a_worker_researches_what_it_claims_and_stops_when_asked(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    seen: list[tuple[str, str]] = []

    def fake_run(_ws: Workspace, _store: StateStore, subject: str, **kwargs: Any) -> Any:
        seen.append((subject, str(kwargs["lane"])))
        daemon.request_stop()
        return SimpleNamespace(ended=False)

    monkeypatch.setattr(research_driver, "run", fake_run)
    monkeypatch.setattr(explore, "after_stall", lambda *_: pytest.fail("no stall, no explore"))

    assert daemon.worker(ws, "l1") == 0
    assert seen == [(hyp_id, "l1")]


def test_a_turn_that_stalled_asks_whether_to_explore_after_the_driver_returned(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside the driver, so the parent is requeued before any model writes anything."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    asked: list[tuple[str, str, bool]] = []

    def stalled(_ws: Workspace, opened: StateStore, subject: str, **_: Any) -> Any:
        scheduler.requeue(opened, subject, scheduler.STALL_PRIORITY)
        return SimpleNamespace(ended=True)

    def after_stall(_ws: Workspace, opened: StateStore, subject: str, lane: str) -> None:
        asked.append((subject, lane, bool(scheduler.queued(opened))))
        daemon.request_stop()

    monkeypatch.setattr(research_driver, "run", stalled)
    monkeypatch.setattr(explore, "after_stall", after_stall)

    assert daemon.worker(ws, "l1") == 0
    assert asked == [(hyp_id, "l1", True)]
    assert daemon.LANE_FAILED not in [event.kind for event in store.events(subject=hyp_id)]


def test_a_worker_with_nothing_to_do_waits_rather_than_spinning(
    ws: Workspace, store: StateStore
) -> None:
    assert daemon.claim(store, "l1") is None
    threading.Timer(0.05, daemon.request_stop).start()

    assert daemon.worker(ws, "l1") == 0


def test_a_hypothesis_whose_baseline_will_not_run_goes_back_behind_the_others(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)

    def failing(*_: Any, **__: Any) -> Any:
        raise PreconditionError(
            "the baseline card did not run", remedy="try again with a smaller window"
        )

    monkeypatch.setattr(research_driver, "run", failing)
    monkeypatch.setattr(daemon, "_wait", lambda _seconds: daemon.request_stop())

    assert daemon.worker(ws, "l1") == 0
    assert [item.priority for item in scheduler.queued(store)] == [scheduler.BASELINE_PRIORITY]
    failed = [event for event in store.events(subject=hyp_id) if event.kind == daemon.LANE_FAILED]
    assert [event.detail["because"] for event in failed] == ["try again with a smaller window"]


def test_a_hypothesis_taken_out_while_a_lane_held_it_does_not_come_back_when_it_fails(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator ran `queue remove` in the minutes between the claim and the failure."""
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)

    def failing(_ws: Workspace, opened: StateStore, subject: str, **_: Any) -> Any:
        with StateStore(ws.path("state.db")) as operator:
            assert scheduler.remove(operator, subject) == "lane"
        raise PreconditionError("the baseline card did not run")

    monkeypatch.setattr(research_driver, "run", failing)
    monkeypatch.setattr(daemon, "_wait", lambda _seconds: daemon.request_stop())

    assert daemon.worker(ws, "l1") == 0
    assert scheduler.queued(store) == []
    assert daemon.LANE_FAILED in [event.kind for event in store.events(subject=hyp_id)]


def test_a_hypothesis_retired_while_a_lane_held_it_stays_out_and_the_lane_lives(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)

    def failing(_ws: Workspace, opened: StateStore, subject: str, **_: Any) -> Any:
        set_status(opened, subject, "retired")
        raise PreconditionError("the baseline card did not run")

    monkeypatch.setattr(research_driver, "run", failing)
    monkeypatch.setattr(daemon, "_wait", lambda _seconds: daemon.request_stop())

    assert daemon.worker(ws, "l1") == 0
    assert scheduler.queued(store) == []


def test_a_run_that_failed_mid_flight_stays_beside_the_stalled_ones(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is still open, so the lane that resumes it finds it where it was."""
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id, lane="l1")

    def failing(*_: Any, **__: Any) -> Any:
        raise PreconditionError("no model answered in a usable shape")

    monkeypatch.setattr(research_driver, "run", failing)
    monkeypatch.setattr(daemon, "_wait", lambda _seconds: daemon.request_stop())

    assert daemon.worker(ws, "l1") == 0
    assert [item.priority for item in scheduler.queued(store)] == [scheduler.STALL_PRIORITY]
    assert records.active(store, hyp_id) is not None


def test_the_monitor_runs_a_pass_every_interval(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    passes: list[int] = []

    def counted(*_: Any, **__: Any) -> list[Any]:
        passes.append(1)
        daemon.request_stop()
        return []

    monkeypatch.setattr("kanso.monitor.run_once", counted)

    assert daemon.monitor(ws) == 0
    assert passes == [1], "the loop is the pass, not a placeholder around one"


def test_a_pass_that_cannot_run_is_recorded_and_the_cadence_is_kept(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop that watches the money is the last thing that should stop."""

    def failing(*_: Any, **__: Any) -> Any:
        daemon.request_stop()
        raise PreconditionError("the plan names a stage this build cannot judge")

    monkeypatch.setattr("kanso.monitor.run_once", failing)

    assert daemon.monitor(ws) == 0
    assert [event.kind for event in store.events(subject="monitor")] == ["monitor_failed"]


def test_a_workspace_with_nothing_deployed_is_an_ordinary_pass(
    ws: Workspace, store: StateStore
) -> None:
    """The pass finds no version and the loop simply keeps its cadence."""
    threading.Timer(0.05, daemon.request_stop).start()

    assert daemon.monitor(ws) == 0


def test_the_supervisor_writes_a_pid_starts_its_children_and_stops_them(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child found ended once the daemon is stopping is neither recorded nor started again."""
    children: list[FakeChild] = []

    def fake_spawn(_ws: Workspace, _command: str, *_rest: str) -> Any:
        children.append(FakeChild(alive=len(children) < 2))
        return children[-1]

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    monkeypatch.setattr(daemon, "_wait", lambda _seconds: daemon.request_stop())

    assert daemon.serve(ws) == 0
    # Two lanes and the monitor.
    assert len(children) == 3
    assert [child.terminated for child in children] == [True, True, False]
    assert not daemon.pid_path(ws).exists()
    assert store.events(kind=daemon.MONITOR_DIED) == []


def test_the_supervisor_starts_again_a_child_that_ended_while_it_ran(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under its own name, and the child started in its place is the one a stop signals."""
    spawned: list[tuple[tuple[str, ...], FakeChild]] = []

    def fake_spawn(_ws: Workspace, command: str, *rest: str) -> Any:
        # The first lane has ended by the supervisor's first look; everything after it runs.
        child = FakeChild(alive=bool(spawned), code=1, pid=100 + len(spawned))
        spawned.append(((command, *rest), child))
        return child

    looks: list[float] = []

    def waiting(seconds: float) -> None:
        looks.append(seconds)
        if len(looks) == 2:
            daemon.request_stop()

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    monkeypatch.setattr(daemon, "_wait", waiting)
    monkeypatch.setattr(daemon, "RESTART_S", 0.0)

    assert daemon.serve(ws) == 0

    assert [argv for argv, _ in spawned] == [
        ("lane", "l1"),
        ("lane", "l2"),
        ("monitor",),
        ("lane", "l1"),
    ]
    ended, *running = (child for _, child in spawned)
    assert not ended.terminated, "it had ended; there was nothing left to signal"
    assert all(child.terminated for child in running)
    (died,) = store.events(kind=daemon.LANE_DIED)
    assert died.subject == "l1"
    assert (died.detail["pid"], died.detail["exit"], died.detail["signal"]) == (100, 1, None)
    assert died.detail["daemon"] == os.getpid()


def test_a_lane_that_died_after_a_long_run_is_recorded_and_started_again_at_once(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel's OOM killer on a large card, measured as a lane taken by `SIGKILL`."""
    spawned: list[tuple[str, ...]] = []
    replacement = FakeChild(pid=5151)

    def fake_spawn(_ws: Workspace, *argv: str) -> Any:
        spawned.append(argv)
        return replacement

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    killed = FakeChild(alive=False, code=-signal.SIGKILL, pid=4242)
    child = daemon._Child(daemon.LANE, "l1", process=killed, started=1_000.0)  # type: ignore[arg-type]
    later = 1_000.0 + daemon.SETTLED_S + 12.5

    daemon._tend(ws, [child], later)

    assert spawned == [("lane", "l1")]
    assert (child.process, child.started, child.wait) == (replacement, later, 0.0)
    (died,) = store.events(kind=daemon.LANE_DIED)
    assert died.subject == "l1"
    assert died.detail == {
        "lane": "l1",
        "pid": 4242,
        "exit": None,
        "signal": "SIGKILL",
        "lived_s": daemon.SETTLED_S + 12.5,
        "restart_in_s": 0.0,
        "daemon": os.getpid(),
        "run": None,
        "put_back": [],
    }


def test_a_child_that_keeps_dying_young_waits_longer_each_time_up_to_a_cap(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that cannot stay up costs a start every few minutes, never one a second; one
    that ran long enough before it died is started again at once, and the count starts over."""
    spawned: list[FakeChild] = []

    def dying(*_: object) -> Any:
        spawned.append(FakeChild(alive=False, code=1))
        return spawned[-1]

    monkeypatch.setattr(daemon, "_spawn", dying)
    child = daemon._Child(daemon.MONITOR, daemon.MONITOR, process=dying(), started=0.0)
    now = 1.0
    waits: list[float] = []
    for _ in range(12):
        daemon._tend(ws, [child], now)
        waits.append(child.wait)
        assert child.process is None and child.due == now + child.wait
        daemon._tend(ws, [child], now + child.wait - 0.5)
        assert child.process is None, "started again before its wait was over"
        now += child.wait
        daemon._tend(ws, [child], now)
        assert child.process is spawned[-1]
        now += 1.0

    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 300.0, 300.0, 300.0, 300.0]
    assert daemon.RESTART_S == 2.0 and daemon.RESTART_CAP_S == 300.0
    died = store.events(kind=daemon.MONITOR_DIED)
    assert [event.detail["restart_in_s"] for event in died] == waits
    assert {event.subject for event in died} == {daemon.MONITOR}
    assert set(died[0].detail) == {"pid", "exit", "signal", "lived_s", "restart_in_s", "daemon"}

    started = len(spawned)
    daemon._tend(ws, [child], now - 1.0 + daemon.SETTLED_S)

    assert child.wait == 0.0 and len(spawned) == started + 1, "a settled child comes back at once"


def test_a_dead_lane_s_open_run_is_left_to_the_lane_started_in_its_place(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id, lane="l1")
    monkeypatch.setattr(daemon, "_spawn", lambda *_a: FakeChild())
    killed = FakeChild(alive=False, code=-signal.SIGKILL)
    child = daemon._Child(daemon.LANE, "l1", process=killed, started=0.0)  # type: ignore[arg-type]

    daemon._tend(ws, [child], daemon.SETTLED_S)

    (died,) = store.events(kind=daemon.LANE_DIED)
    assert (died.detail["run"], died.detail["put_back"]) == (hyp_id, [])
    assert records.active(store, hyp_id) is not None
    assert scheduler.queued(store) == []
    assert daemon.claim(store, "l1") == hyp_id, "the lane started in its place resumes it"


def test_what_a_dead_lane_held_with_no_run_goes_back_and_its_directory_with_it(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane killed in its baseline holds a claim, no run, and a lane directory with the
    payload of a card nobody will read. The lane beside it is alive and keeps its claim."""
    beginning = classify(ws, store, DOCUMENT)
    elsewhere = classify(ws, store, document(id="demo_two"))
    scheduler.enqueue(store, beginning)
    scheduler.enqueue(store, elsewhere)
    assert daemon.claim(store, "l1") == beginning
    assert daemon.claim(store, "l2") == elsewhere
    left = lanes.prepare(lanes.lane_dir(ws, "l1", beginning))
    (left / ".card").mkdir()
    (left / ".card" / "request.pkl").write_bytes(b"a window of points nobody will read")
    theirs = lanes.prepare(lanes.lane_dir(ws, "l2", elsewhere))
    monkeypatch.setattr(daemon, "_spawn", lambda *_a: FakeChild())
    killed = FakeChild(alive=False, code=-signal.SIGKILL)
    child = daemon._Child(daemon.LANE, "l1", process=killed, started=0.0)  # type: ignore[arg-type]

    daemon._tend(ws, [child], 1.0)

    (died,) = store.events(kind=daemon.LANE_DIED)
    assert (died.detail["run"], died.detail["put_back"]) == (None, [beginning])
    assert [item.hyp_id for item in scheduler.queued(store)] == [beginning]
    assert not left.exists()
    assert theirs.is_dir() and scheduler.claimed(store, elsewhere)


def test_a_child_s_end_is_recorded_as_its_exit_status_or_the_signal_that_took_it() -> None:
    assert daemon._ending(0) == {"exit": 0, "signal": None}
    assert daemon._ending(1) == {"exit": 1, "signal": None}
    assert daemon._ending(-signal.SIGKILL) == {"exit": None, "signal": "SIGKILL"}
    # A signal the platform names no member for — a real-time one on Linux — by its number.
    assert daemon._ending(-200) == {"exit": None, "signal": "200"}


def test_a_child_still_working_when_its_grace_runs_out_is_killed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane inside a card cannot answer, and a shutdown does not wait out a card."""
    child = FakeChild(stubborn=True)
    monkeypatch.setattr(daemon, "_spawn", lambda *_a: child)
    monkeypatch.setattr(daemon, "GRACE_S", 0.01)
    daemon.request_stop()

    assert daemon.serve(ws) == 0
    assert (child.terminated, child.killed) == (True, True)


class Deaf(FakeChild):
    """A child that answers no signal and takes the whole timeout it is given to say so."""

    def __init__(self, log: list[tuple[object, ...]], name: str) -> None:
        super().__init__(stubborn=True)
        self.log = log
        self.name = name

    def terminate(self) -> None:
        self.log.append(("terminate", self.name))
        super().terminate()

    def wait(self, timeout: float | None = None) -> int:
        self.log.append(("wait", self.name, timeout))
        if timeout is not None:
            time.sleep(timeout)
        return super().wait(timeout)

    def kill(self) -> None:
        self.log.append(("kill", self.name))
        super().kill()


def test_every_child_is_signalled_before_any_is_waited_on_and_they_share_one_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seven lanes busy in cards cost one grace, not seven: signalled one at a time, each
    waited on in turn, they outlasted `stop`'s patience and the last were never signalled."""
    monkeypatch.setattr(daemon, "GRACE_S", 0.2)
    log: list[tuple[object, ...]] = []
    children = [Deaf(log, f"l{number}") for number in range(1, 8)]

    daemon._terminate(children)  # type: ignore[arg-type]

    assert log[:7] == [("terminate", child.name) for child in children]
    waits = [entry for entry in log if entry[0] == "wait" and entry[2] is not None]
    assert [entry[1] for entry in waits] == [child.name for child in children]
    first, *rest = (float(str(entry[2])) for entry in waits)
    assert 0.0 < first <= 0.2
    assert rest == [0.0] * 6, "the grace is spent once, by the first child waited on"
    assert all(child.killed for child in children)


def test_a_supervisor_goes_within_one_grace_however_many_children_will_not_answer(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured on real processes that ignore the signal, as a lane stuck in a call does."""
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    spawned: list[subprocess.Popen[bytes]] = []
    ready: list[float] = []

    def deaf_child(*_: object) -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
        assert child.stdout is not None and child.stdout.readline() == b"ready\n"
        spawned.append(child)
        ready.append(time.monotonic())
        return child

    monkeypatch.setattr(daemon, "_spawn", deaf_child)
    monkeypatch.setattr(daemon, "GRACE_S", 1.0)
    daemon.request_stop()

    assert daemon.serve(ws) == 0
    took = time.monotonic() - ready[-1]

    assert len(spawned) == 3, "two lanes and the monitor"
    assert all(child.poll() is not None for child in spawned)
    assert took < 2.0, f"shutting three children down took {took:.2f}s: a grace each"


def test_a_daemon_that_will_not_answer_is_killed_with_every_process_in_its_group(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lanes and the monitor run in the supervisor's group: none survives its kill."""
    code = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "lane = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
        "sys.stdout.write(f'{lane.pid}\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    supervisor = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
    )
    assert supervisor.stdout is not None
    lane = int(supervisor.stdout.readline())
    path = daemon.pid_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{supervisor.pid}\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "STOP_TIMEOUT_S", 0.2)
    try:
        assert daemon.stop(ws) == supervisor.pid

        assert supervisor.wait(timeout=10) != 0
        assert ends(lane, within_s=10.0), "the lane outlived the supervisor it belonged to"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(supervisor.pid, signal.SIGKILL)


def test_a_lane_whose_supervisor_is_gone_stops_as_though_told_to(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever took the supervisor, a lane nobody supervises takes nothing more."""
    parents = iter([4242])
    monkeypatch.setattr(daemon.os, "getppid", lambda: next(parents, 1))
    monkeypatch.setattr(daemon, "claim", lambda *_: pytest.fail("the lane claimed work"))

    assert daemon.worker(ws, "l1") == 0
    assert daemon.stopping()
    from kanso.nautilus import backtest as runner

    assert runner._INTERRUPT.is_set(), "a card in flight is killed as on a stop"


def test_a_monitor_whose_supervisor_is_gone_stops_after_the_pass_it_is_on(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    parents = iter([4242, 4242])
    passes: list[int] = []
    monkeypatch.setattr(daemon.os, "getppid", lambda: next(parents, 1))
    monkeypatch.setattr("kanso.monitor.run_once", lambda *_: passes.append(1) or [])

    assert daemon.monitor(ws) == 0
    assert passes == [1]
    assert daemon.stopping()


def test_a_supervisor_answers_to_nothing_above_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its own parent is the `start` command, which exits at once: that is no stop."""
    monkeypatch.setattr(daemon.os, "getppid", lambda: 1)

    assert daemon.stopping() is False


def test_a_lane_hands_its_stop_request_to_the_driver_and_explores_nothing_once_stopped(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    asked: list[bool] = []

    def stalled_then_stopped(
        _ws: Workspace, _store: StateStore, subject: str, **kwargs: Any
    ) -> Any:
        stop = kwargs["stop"]
        asked.append(stop())
        daemon.request_stop()
        asked.append(stop())
        return SimpleNamespace(ended=True)

    monkeypatch.setattr(research_driver, "run", stalled_then_stopped)
    monkeypatch.setattr(explore, "after_stall", lambda *_: pytest.fail("explored after a stop"))

    assert daemon.worker(ws, "l1") == 0
    assert asked == [False, True]


def test_two_supervisors_cannot_hold_one_workspace(ws: Workspace) -> None:
    """The pid file is the lock, so the holder and the pid inside it cannot disagree."""
    held = daemon._acquire(ws)
    try:
        assert daemon.pid_of(ws) == os.getpid()
        with pytest.raises(PreconditionError, match="another daemon holds"):
            daemon._acquire(ws)
    finally:
        held.close()
        daemon.pid_path(ws).unlink()


# --- start, stop and status --------------------------------------------------


def test_start_detaches_and_stop_leaves_the_run_and_the_lane_directory(
    stopped: Workspace, store: StateStore
) -> None:
    ws = stopped
    hyp_id = classify(ws, store, DOCUMENT)
    run = open_run(store, hyp_id, lane="op")
    directory = lanes.prepare(ws.root / run.dir)
    (directory / "strategy.py").write_bytes(b"# the operator is mid-edit\n")

    pid = daemon.start(ws)

    assert pid != os.getpid()
    # A session of its own is what makes it survive the process that started it.
    assert os.getsid(pid) != os.getsid(os.getpid())
    assert daemon.pid_of(ws) == pid
    with pytest.raises(PreconditionError, match="already running"):
        daemon.start(ws)

    assert daemon.stop(ws) == pid

    assert daemon.pid_of(ws) is None
    assert (
        store.connection.execute("SELECT COUNT(*) FROM runs WHERE ended_at IS NULL").fetchone()[0]
        == 1
    )
    assert (directory / "strategy.py").read_bytes() == b"# the operator is mid-edit\n"


SLOW_SEED = SEED.replace(
    b"    def on_start(self) -> None:\n",
    b'    def on_start(self) -> None:\n        while self.mode == "slow":\n            pass\n',
)
"""The research slice's steerable seed with one more mode: `slow` never leaves `on_start`,
so a card of it is running for as long as anything lets it."""


def until(found: Callable[[], Any], what: str, within_s: float = 60.0) -> Any:
    """Poll `found` until it returns something other than `None`, and return that."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        value = found()
        if value is not None:
            return value
        time.sleep(0.1)
    pytest.fail(f"waited {within_s:.0f}s for {what}")


def lane_process(supervisor: int, name: str) -> int | None:
    """The running lane `name` the supervisor started, found by its command line."""
    for pid, command in children(supervisor):
        words = command.split()
        if words[-3:-2] == [daemon.LANE] and words[-1] == name and running(pid):
            return pid
    return None


def test_a_lane_killed_mid_card_is_started_again_and_resumes_its_run(
    stopped: Workspace, store: StateStore
) -> None:
    """Measured with real processes, as a research host loses one: a lane is killed outright
    in the middle of a card while the daemon runs, and the daemon keeps every lane it planned.

    The death is recorded as it happened, the lane is started again under its own name, the
    lane in its place resumes the run the dead one left — its first card is a card of that
    run — and that card reclaims the payload the killed card left in the lane's `.card/`.
    """
    ws = stopped
    hyp_id = classify(ws, store, DOCUMENT, SLOW_SEED)
    run = research_loop.begin(ws, store, hyp_id, lane="l1")
    scripted(ws, propose=[proposal("slow")], align_check=[ALIGNED])
    room = ws.root / run.dir / backtest.CARD_ROOM

    supervisor = daemon.start(ws)
    lane = until(lambda: lane_process(supervisor, "l1"), "lane l1 to start")
    until(lambda: (room / backtest.REQUEST_FILE).is_file() or None, "l1's card payload")
    card = until(
        lambda: next((pid for pid, _ in children(lane) if running(pid)), None), "l1's card"
    )
    time.sleep(1.0)
    assert running(card), "the card was running while its lane was"
    # The lane started in the dead one's place walks the script from its start.
    write_script(ws, "mid", {"propose": [proposal("revert")]})
    os.kill(lane, signal.SIGKILL)
    left = room / "left-by-the-dead-lane"
    left.write_bytes(b"")

    assert ends(card, within_s=10.0), "the card outlived its lane"
    died = until(lambda: next(iter(store.events(kind=daemon.LANE_DIED)), None), "the record")
    assert died.subject == "l1"
    assert (died.detail["pid"], died.detail["signal"], died.detail["exit"]) == (
        lane,
        "SIGKILL",
        None,
    )
    assert (died.detail["run"], died.detail["put_back"]) == (hyp_id, [])
    # Seconds old, on any host this suite runs on — but the rule is asserted, not the clock.
    young = float(died.detail["lived_s"]) < daemon.SETTLED_S
    assert died.detail["restart_in_s"] == (daemon.RESTART_S if young else 0.0)
    again = until(lambda: lane_process(supervisor, "l1"), "l1 to be started again")
    assert again != lane
    resumed = until(
        lambda: next(
            (
                card
                for card in records.cards_of(store, hyp_id)
                if card.desc == proposal("revert")["desc"]
            ),
            None,
        ),
        "the first card of the lane started again",
        within_s=90.0,
    )

    assert (resumed.run_id, resumed.lane) == (run.run_id, "l1")
    assert not left.exists(), "its first card emptied the room the killed card left"
    assert [
        (item.child, item.deaths, item.signal) for item in daemon.status(ws, store).restarts
    ] == [("l1", 1, "SIGKILL")]
    daemon.stop(ws)
    assert ends(again, within_s=10.0)
    assert len(store.events(kind=daemon.LANE_DIED)) == 1, "a stop is not a death"
    assert store.events(kind=daemon.MONITOR_DIED) == []


def test_a_daemon_that_will_not_answer_a_signal_is_killed(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    deaf = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert deaf.stdout is not None and deaf.stdout.readline() == b"ready\n"
    path = daemon.pid_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{deaf.pid}\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "STOP_TIMEOUT_S", 0.2)

    assert daemon.stop(ws) == deaf.pid

    assert deaf.wait(timeout=10) != 0
    assert daemon.pid_of(ws) is None


def test_stopping_nothing_is_a_precondition_failure(ws: Workspace) -> None:
    with pytest.raises(PreconditionError, match="no daemon is running"):
        daemon.stop(ws)


def test_a_pid_file_left_by_a_dead_process_is_not_a_daemon(ws: Workspace) -> None:
    path = daemon.pid_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("not a number\n", encoding="utf-8")
    assert daemon.pid_of(ws) is None

    gone = subprocess.Popen([sys.executable, "-c", ""])
    gone.wait()
    path.write_text(f"{gone.pid}\n", encoding="utf-8")
    assert daemon.pid_of(ws) is None
    # Signalling a process that went away between the look and the send is not an error,
    # and nor is killing the group of one that went.
    daemon._signal(gone.pid, signal.SIGTERM)
    daemon._kill_group(gone.pid)


def test_a_daemon_that_exits_at_once_is_reported_rather_than_waited_for(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon, "_argv", lambda *_a, **_k: [sys.executable, "-c", "raise SystemExit(3)"]
    )

    with pytest.raises(PreconditionError, match="exited immediately"):
        daemon.start(ws)


def test_status_reports_the_lanes_the_runs_and_the_queue(ws: Workspace, store: StateStore) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id, lane="l1")
    queued = classify(ws, store, document(id="demo_two"))
    scheduler.enqueue(store, queued)

    reported = daemon.status(ws, store)

    assert reported.running is False and reported.pid is None
    assert reported.lanes == ("l1", "l2")
    assert [item.run.hyp_id for item in reported.runs] == [hyp_id]
    assert [item.hyp_id for item in reported.queue] == [queued]
    payload: Any = reported.payload()
    assert payload["runs"][0]["lane"] == "l1"
    # The lane directory of this bare run was never written, so it holds no bytes.
    assert payload["runs"][0]["lane_sha"] is None
    assert payload["queue"][0]["id"] == queued

    lanes.write_atomic(lanes.lane_dir(ws, "l1", hyp_id) / "strategy.py", b"# edited\n")
    edited: Any = daemon.status(ws, store).payload()
    assert edited["runs"][0]["lane_sha"] == sha256(b"# edited\n").hexdigest()

    ws.path("envelope.yaml").unlink()
    assert daemon.status(ws, store).lanes == ()
    assert daemon.status(ws, store).restarts == ()


def test_status_reports_every_child_the_running_daemon_started_again(
    ws: Workspace, store: StateStore
) -> None:
    """Each once, by how often it died and how the newest death went, in the order they
    first died; a death recorded under another daemon is that daemon's and not reported."""
    running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    path = daemon.pid_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{running.pid}\n", encoding="utf-8")
    killed = {"pid": 7, "exit": None, "signal": "SIGKILL", "lived_s": 412.3, "restart_in_s": 0.0}
    try:
        store.event(daemon.LANE_DIED, "l2", {"lane": "l2", **killed, "daemon": running.pid + 1})
        store.event(daemon.LANE_DIED, "l1", {"lane": "l1", **killed, "daemon": running.pid})
        exited = {**killed, "exit": 1, "signal": None, "lived_s": 3.0, "restart_in_s": 2.0}
        store.event(daemon.MONITOR_DIED, daemon.MONITOR, {**exited, "daemon": running.pid})
        young = {**killed, "lived_s": 5.5, "restart_in_s": 4.0}
        store.event(daemon.LANE_DIED, "l1", {"lane": "l1", **young, "daemon": running.pid})

        reported = daemon.status(ws, store)
    finally:
        running.kill()
        running.wait()
        path.unlink()

    newest = store.events(kind=daemon.LANE_DIED)[-1]
    assert reported.running and reported.pid == running.pid
    assert reported.restarts == (
        daemon.Restart("l1", 2, newest.ts, None, "SIGKILL", 5.5, 4.0),
        daemon.Restart(
            daemon.MONITOR,
            1,
            store.events(kind=daemon.MONITOR_DIED)[0].ts,
            1,
            None,
            3.0,
            2.0,
        ),
    )
    payload: Any = reported.payload()
    assert payload["restarts"][0] == {
        "child": "l1",
        "deaths": 2,
        "at": newest.ts,
        "exit": None,
        "signal": "SIGKILL",
        "lived_s": 5.5,
        "restart_in_s": 4.0,
    }
    assert daemon.status(ws, store).restarts == (), "no daemon runs, so none restarted anything"


# --- the child's entry point -------------------------------------------------


def test_the_module_runs_as_a_supervisor_a_lane_or_the_monitor(
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "_spawn", lambda *_a: FakeChild(alive=False))
    root = str(ws.root)

    daemon.request_stop()
    assert daemon.main(["serve", root]) == 0
    assert daemon.main(["lane", root, "l1"]) == 0
    assert daemon.main(["monitor", root]) == 0


def test_the_module_refuses_a_command_it_does_not_have(ws: Workspace) -> None:
    with pytest.raises(PreconditionError, match="usage:"):
        daemon.main(["serve"])
    with pytest.raises(PreconditionError, match="is not one of"):
        daemon.main(["dance", str(ws.root)])


def test_a_stop_request_can_be_taken_back(ws: Workspace) -> None:
    assert daemon.stopping() is False
    daemon.request_stop()
    assert daemon.stopping() is True
    daemon.clear_stop()
    assert daemon.stopping() is False
    assert Path(daemon.log_path(ws)).name == daemon.LOG_NAME


def test_no_module_of_the_research_package_reaches_for_git(ws: Workspace) -> None:
    """The daemon starts processes; none of them is ever git."""
    import kanso.research

    modules = sorted(Path(kanso.research.__file__ or "").parent.glob("*.py"))
    assert len(modules) >= 9
    for module in modules:
        source = module.read_text(encoding="utf-8")
        assert '"git"' not in source and "'git'" not in source, module.name
    # The only commands the package builds are this interpreter and the macOS keep-awake.
    built = [
        daemon._argv(ws.root, "serve", awake=True)[0],
        daemon._argv(ws.root, "lane", "l1")[0],
        daemon._argv(ws.root, "monitor")[0],
    ]
    assert set(built) <= {sys.executable, daemon.CAFFEINATE[0]}


def test_a_stop_request_interrupts_the_card_and_is_taken_back_with_it() -> None:
    from kanso.nautilus import backtest as runner

    daemon.clear_stop()
    daemon.request_stop()
    assert daemon.stopping() and runner._INTERRUPT.is_set()
    daemon.clear_stop()
    assert not daemon.stopping() and not runner._INTERRUPT.is_set()


def test_a_card_interrupted_by_a_stop_is_neither_failed_nor_requeued(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run stays open and untouched, so the next start resumes it from its last card."""
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id, lane="l1")

    def interrupted(*_: Any, **__: Any) -> Any:
        daemon.request_stop()
        raise PreconditionError("the card was interrupted: the lane running it was told to stop")

    monkeypatch.setattr(research_driver, "run", interrupted)

    assert daemon.worker(ws, "l1") == 0
    assert scheduler.queued(store) == []
    assert records.active(store, hyp_id) is not None
    assert all(event.kind != "lane_failed" for event in store.events(subject=hyp_id))


def test_the_supervisor_puts_back_what_a_dead_lane_dropped(
    ws: Workspace, store: StateStore
) -> None:
    hyp_id = classify(ws, store, DOCUMENT)
    scheduler.enqueue(store, hyp_id)
    assert daemon.claim(store, "l1") == hyp_id

    assert daemon.recover(ws) == [hyp_id]
    assert [item.hyp_id for item in scheduler.queued(store)] == [hyp_id]
