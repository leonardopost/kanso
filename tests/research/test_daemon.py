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
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from kanso.errors import KansoError, PreconditionError
from kanso.research import daemon, lanes, records, scheduler
from kanso.research import driver as research_driver
from kanso.state import StateStore
from kanso.workspace import Workspace

from .conftest import DOCUMENT, classify, document
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
    """A child that can be asked to stop, and one that has to be made to."""

    def __init__(self, alive: bool = True, stubborn: bool = False) -> None:
        self.alive = alive
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.alive else 0

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

    monkeypatch.setattr(research_driver, "run", fake_run)

    assert daemon.worker(ws, "l1") == 0
    assert seen == [(hyp_id, "l1")]


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
        daemon.request_stop()
        raise PreconditionError("the baseline card did not run")

    monkeypatch.setattr(research_driver, "run", failing)

    assert daemon.worker(ws, "l1") == 0
    assert [item.priority for item in scheduler.queued(store)] == [scheduler.BASELINE_PRIORITY]
    assert daemon.LANE_FAILED in [event.kind for event in store.events(subject=hyp_id)]


def test_a_run_that_failed_mid_flight_stays_beside_the_stalled_ones(
    ws: Workspace, store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is still open, so the lane that resumes it finds it where it was."""
    hyp_id = classify(ws, store, DOCUMENT)
    open_run(store, hyp_id, lane="l1")

    def failing(*_: Any, **__: Any) -> Any:
        daemon.request_stop()
        raise PreconditionError("no model answered in a usable shape")

    monkeypatch.setattr(research_driver, "run", failing)

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
    ws: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: list[FakeChild] = []

    def fake_spawn(_ws: Workspace, _command: str, *_rest: str) -> Any:
        children.append(FakeChild(alive=len(children) < 2))
        return children[-1]

    monkeypatch.setattr(daemon, "_spawn", fake_spawn)
    threading.Timer(0.05, daemon.request_stop).start()

    assert daemon.serve(ws) == 0
    # Two lanes and the monitor.
    assert len(children) == 3
    assert [child.terminated for child in children] == [True, True, False]
    assert not daemon.pid_path(ws).exists()


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
    # Signalling a process that went away between the look and the send is not an error.
    daemon._signal(gone.pid, signal.SIGTERM)


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
