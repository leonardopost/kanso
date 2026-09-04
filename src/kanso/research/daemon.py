"""The daemon: one supervisor, one worker per lane, and the monitor beside them.

`start` detaches a supervisor and returns. The supervisor takes an exclusive lock on a
file in the workspace, writes its pid beside it, and starts one worker process per lane
the envelope allows plus the monitor. The lock is what makes "one daemon per workspace" a
fact rather than a convention: a second `start` finds the lock held and refuses, and a
crashed daemon releases it when the kernel closes its file, so a stale pid file never
blocks a restart.

A worker is a process because a lane is: lanes never share files and a SQLite connection
does not survive a fork, so each opens its own store and works its own directory. What it
does is a loop of two questions — is there a run of mine to finish, and if not, is there
anything in the queue — and the first question comes first, which is the whole of "`start`
resumes active runs before taking new work". A hypothesis being worked in the interactive
lane is neither of those things, so an operator at a keyboard never costs a daemon lane
its turn.

**Stopping keeps everything.** `stop` sends one signal. The supervisor passes it on and
exits; a worker looks up between cards, leaves the run open and the lane directory where
it is, and exits too. A worker still inside a card when its grace runs out is killed, and
that costs the card and nothing else: the run, its blobs and its `best` are all in state.
Nothing is ended and nothing is cleaned up, so the next `start` picks the runs up where
they were left — which is why stopping the daemon is a cheap act an operator can perform
without thinking about what it costs.

On macOS the supervisor runs under `caffeinate -i`, so a research host does not idle-sleep
mid-run. The monitor loop is started here and has nothing to check until deployment
exists; it keeps its cadence so that the process it will need is already in place.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import platform
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import IO, Final

from kanso.env import read as read_envelope
from kanso.errors import KansoError, PreconditionError
from kanso.hyp import STRATEGY_FILE
from kanso.research import driver as research_driver
from kanso.research import lanes, records, scheduler
from kanso.schemas import RunRecord, parse_duration
from kanso.state import StateStore
from kanso.workspace import LANE_ROOT, Workspace, find

__all__ = [
    "BACKOFF_S",
    "CAFFEINATE",
    "CARDS_PER_TURN",
    "GRACE_S",
    "LANE_PREFIX",
    "LOG_NAME",
    "LaneRun",
    "MODULE",
    "PID_NAME",
    "POLL_S",
    "Status",
    "claim",
    "clear_stop",
    "lane_names",
    "log_path",
    "main",
    "monitor",
    "pid_of",
    "pid_path",
    "request_stop",
    "serve",
    "start",
    "status",
    "stop",
    "stopping",
    "worker",
]

MODULE: Final = "kanso.research"
"""How a child of the daemon is started: this package, run as one. The runnable module is
`kanso.research.__main__`, which nothing imports, so the child does not end up with a
second copy of this module's stop flag under the name `__main__`."""

PID_NAME: Final = "daemon.pid"
LOG_NAME: Final = "daemon.log"
"""Both live under `runs/`, which the workspace template gitignores. The pid file is
also the lock: the supervisor holds it open under `flock` for as long as it runs, so a
daemon that died releases it the moment the kernel closed the file and the leftover pid
inside it blocks nothing."""

LANE_PREFIX: Final = "l"
"""Daemon lanes are `l1`, `l2`, …; the interactive lane is `op` and is never one of them."""

CAFFEINATE: Final = ("caffeinate", "-i")
"""What keeps a macOS host awake for as long as the supervisor lives."""

POLL_S: Final = 1.0
"""How long a loop waits when there was nothing to do."""

BACKOFF_S: Final = 30.0
"""How long a lane waits after a hypothesis it took could not be researched at all."""

GRACE_S: Final = 5.0
"""How long a child is given to finish what it is on before the supervisor kills it."""

CARDS_PER_TURN: Final = 1
"""How many cards a lane asks the driver for before it looks up. One, so that stopping
the daemon costs at most one card and never waits out a whole run (D3 makes runs
indefinite, and a shutdown cannot wait for one to end)."""

START_TIMEOUT_S: Final = 20.0
STOP_TIMEOUT_S: Final = 20.0
"""How long `start` waits for a pid and `stop` for the process to go."""

_TICK: Final = 0.05
"""Waiting is sliced this small so a signal is noticed promptly."""

SERVE: Final = "serve"
LANE: Final = "lane"
MONITOR: Final = "monitor"
"""The three things this module can be run as."""

LANE_FAILED: Final = "lane_failed"
"""The event a worker appends when a hypothesis it took could not be researched."""

_STOPPING = False
"""Set by the signal handler; every loop in this process reads it between iterations."""


def request_stop(*_: object) -> None:
    """Ask this process's loops to finish what they are doing and return."""
    global _STOPPING
    _STOPPING = True


def clear_stop() -> None:
    """Forget a stop request. For a test that runs a loop more than once."""
    global _STOPPING
    _STOPPING = False


def stopping() -> bool:
    """Whether this process has been asked to stop."""
    return _STOPPING


@dataclass(frozen=True)
class LaneRun:
    """One open run and what its lane directory holds right now.

    `lane_sha` is the file an operator or a driver is editing, which is not the run's
    `best` between a proposal and its card and is nothing at all if the directory was
    removed under the run; a reader comparing the three shas can see which.
    """

    run: RunRecord
    lane_sha: str | None

    def payload(self) -> dict[str, object]:
        return {
            "id": self.run.hyp_id,
            "run_id": self.run.run_id,
            "tag": self.run.tag,
            "lane": self.run.lane,
            "dir": self.run.dir,
            "lane_sha": self.lane_sha,
            "base_sha": self.run.base_sha,
            "best_sha": self.run.best_sha,
            "best_metric": self.run.best_metric,
        }


@dataclass(frozen=True)
class Status:
    """What `research status` reports: the daemon, its lanes, its runs and its queue."""

    running: bool
    pid: int | None
    lanes: tuple[str, ...]
    runs: tuple[LaneRun, ...]
    queue: tuple[scheduler.QueueItem, ...]

    def payload(self) -> dict[str, object]:
        """The status as one JSON object."""
        return {
            "running": self.running,
            "pid": self.pid,
            "lanes": list(self.lanes),
            "runs": [run.payload() for run in self.runs],
            "queue": [item.payload() for item in self.queue],
        }


# --- paths and processes -----------------------------------------------------


def pid_path(ws: Workspace) -> Path:
    """Where the supervisor records its pid."""
    return ws.path(LANE_ROOT, PID_NAME)


def log_path(ws: Workspace) -> Path:
    """Where the daemon and its children send whatever they write to a stream."""
    return ws.path(LANE_ROOT, LOG_NAME)


def pid_of(ws: Workspace) -> int | None:
    """The pid of the daemon running in this workspace, or `None`.

    A pid file naming a process that is gone is a leftover, not a daemon, so it reads as
    "not running" and the next `start` overwrites it.
    """
    path = pid_path(ws)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        return None
    pid = int(text)
    return pid if _alive(pid) else None


def lane_names(ws: Workspace) -> tuple[str, ...]:
    """The daemon's lanes, one per lane the envelope's plan allows."""
    envelope = read_envelope(ws)
    if envelope is None:
        raise PreconditionError(
            "this workspace has no envelope, so the daemon does not know how many lanes fit",
            remedy="run `kanso env detect`",
        )
    return tuple(f"{LANE_PREFIX}{number}" for number in range(1, envelope.plan.lanes + 1))


def start(ws: Workspace) -> int:
    """Start the daemon and return its pid. Refuses when one is already running."""
    running = pid_of(ws)
    if running is not None:
        raise PreconditionError(
            f"a daemon is already running in this workspace (pid {running})",
            remedy="run `kanso research stop` first",
        )
    lane_names(ws)
    log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    with log_path(ws).open("ab") as log:
        child = subprocess.Popen(
            _argv(ws.root, SERVE, awake=True),
            cwd=str(ws.root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return _await_pid(ws, child)


def stop(ws: Workspace) -> int:
    """Signal the daemon and wait for it to go. Runs and lane directories stay."""
    pid = pid_of(ws)
    if pid is None:
        raise PreconditionError(
            "no daemon is running in this workspace",
            remedy="run `kanso research start`",
        )
    _signal(pid, signal.SIGTERM)
    if not _gone(pid, STOP_TIMEOUT_S):
        # The supervisor kills its own children after their grace, so one that is still
        # here has not answered at all; nothing it holds is worth waiting longer for.
        _signal(pid, signal.SIGKILL)
        _gone(pid, GRACE_S)
    pid_path(ws).unlink(missing_ok=True)
    return pid


def status(ws: Workspace, store: StateStore) -> Status:
    """The daemon, the lanes it would run, the active runs and the queue."""
    pid = pid_of(ws)
    return Status(
        running=pid is not None,
        pid=pid,
        lanes=lane_names(ws) if read_envelope(ws) is not None else (),
        runs=tuple(LaneRun(run, _lane_sha(ws, run)) for run in active_runs(store)),
        queue=tuple(scheduler.queued(store)),
    )


def active_runs(store: StateStore) -> list[RunRecord]:
    """Every run still open, in the order they were started."""
    rows = store.connection.execute(
        "SELECT hyp_id FROM runs WHERE ended_at IS NULL ORDER BY started_at, run_id"
    ).fetchall()
    found = [records.active(store, str(row["hyp_id"])) for row in rows]
    return [run for run in found if run is not None]


def _lane_sha(ws: Workspace, run: RunRecord) -> str | None:
    """The sha of the bytes the lane directory holds, or `None` when it holds none."""
    path = ws.root / run.dir / STRATEGY_FILE
    if not path.is_file():
        return None
    return sha256(path.read_bytes()).hexdigest()


# --- the three things this module runs as ------------------------------------


def serve(ws: Workspace) -> int:
    """The supervisor: hold the lock, start the children, wait, pass the signal on."""
    lock = _acquire(ws)
    _listen()
    children = [_spawn(ws, LANE, name) for name in lane_names(ws)]
    children.append(_spawn(ws, MONITOR))
    try:
        while not stopping():
            _wait(POLL_S)
    finally:
        for child in children:
            _terminate(child)
        pid_path(ws).unlink(missing_ok=True)
        lock.close()
    return 0


def worker(ws: Workspace, lane: str) -> int:
    """One lane: finish this lane's own run first, then take the next queued hypothesis.

    The driver is asked for `CARDS_PER_TURN` cards rather than for the run, so the lane
    reaches this loop between cards and a stop request is answered there. Everything the
    driver needs to carry on — how many non-keeps in a row, how long since the last
    alignment check, what the last diff was — it reads back out of the run, so a turn is
    a resumption and the run is unaware it was interrupted.

    A hypothesis it could not research goes back in the queue rather than out of it — a
    baseline that will not run returns behind the stalled ones, and anything that failed
    mid-run returns beside them — and the lane waits before taking anything else, so a
    provider that is down costs a call a minute rather than a call a second.
    """
    lane = lanes.check_lane(lane)
    _listen()
    with StateStore(ws.path("state.db")) as store:
        while not stopping():
            subject = claim(store, lane)
            if subject is None:
                _wait(POLL_S)
                continue
            try:
                research_driver.run(ws, store, subject, cards=CARDS_PER_TURN, lane=lane)
            except KansoError as exc:
                store.event(LANE_FAILED, subject, {"lane": lane, "error": exc.message})
                if records.active(store, subject) is None:
                    scheduler.on_baseline_failed(store, subject)
                else:
                    scheduler.requeue(store, subject, scheduler.STALL_PRIORITY)
                _wait(BACKOFF_S)
    return 0


def monitor(ws: Workspace) -> int:
    """The monitoring pass, on `[monitor] interval`.

    The loop keeps its cadence and does nothing: there is no deployed version to watch
    until a strategy is composed and deployed, and the pass that will watch one belongs
    with the stage gates rather than here.
    """
    interval = parse_duration(ws.config.monitor.interval, "monitor.interval").total_seconds()
    _listen()
    while not stopping():
        _wait(interval)
    return 0


def claim(store: StateStore, lane: str) -> str | None:
    """What this lane works next: its own unfinished run, else the head of the queue."""
    row = store.connection.execute(
        "SELECT hyp_id FROM runs WHERE ended_at IS NULL AND lane = ? ORDER BY started_at LIMIT 1",
        (lane,),
    ).fetchone()
    if row is not None:
        return str(row["hyp_id"])
    return scheduler.dequeue(store)


def main(argv: Sequence[str]) -> int:
    """`python -m kanso.research <serve|lane|monitor> <workspace> [lane]`."""
    if len(argv) < 2:
        raise PreconditionError(
            f"usage: python -m {MODULE} <{SERVE}|{LANE}|{MONITOR}> <workspace> [lane]"
        )
    command, root, *rest = argv
    ws = find(Path(root))
    if command == SERVE:
        return serve(ws)
    if command == LANE:
        return worker(ws, rest[0])
    if command == MONITOR:
        return monitor(ws)
    raise PreconditionError(f"{command!r} is not one of {SERVE}, {LANE}, {MONITOR}")


# --- the small mechanics -----------------------------------------------------


def _argv(root: Path, command: str, *rest: str, awake: bool = False) -> list[str]:
    """The command line for one child, under `caffeinate` where the host sleeps."""
    argv = [sys.executable, "-m", MODULE, command, str(root), *rest]
    if awake and platform.system() == "Darwin":
        return [*CAFFEINATE, *argv]
    return argv


def _spawn(ws: Workspace, command: str, *rest: str) -> subprocess.Popen[bytes]:
    """Start one child in this process's session, so a signal reaches the whole daemon."""
    with log_path(ws).open("ab") as log:
        return subprocess.Popen(
            _argv(ws.root, command, *rest),
            cwd=str(ws.root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )


def _terminate(child: subprocess.Popen[bytes]) -> None:
    """Ask a child to stop, and insist when its grace runs out.

    A lane inside a card cannot answer until the card ends, and a card may be allowed
    minutes; a shutdown that waited for one would not be a shutdown. So the grace is
    short and the card is what a stop costs.
    """
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=GRACE_S)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _acquire(ws: Workspace) -> IO[bytes]:
    """Lock the pid file and stamp this process on it, or refuse because it is held.

    The lock and the pid are one file so that "who is the daemon" and "is it still alive"
    cannot disagree: the answer is whoever holds the lock, and the number inside is theirs.
    """
    path = pid_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise PreconditionError(
            f"another daemon holds {path}",
            remedy="run `kanso research stop`, or wait for the running daemon to exit",
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode())
    handle.flush()
    return handle


def _await_pid(ws: Workspace, child: subprocess.Popen[bytes]) -> int:
    """Wait for the detached supervisor to report its pid, or say why it never did."""
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        pid = pid_of(ws)
        if pid is not None:
            return pid
        if child.poll() is not None:
            raise PreconditionError(
                f"the daemon exited immediately (status {child.returncode})",
                remedy=f"read {log_path(ws)}",
            )
        time.sleep(_TICK)
    raise PreconditionError(  # pragma: no cover - only a host that cannot start a process
        f"the daemon did not report a pid within {START_TIMEOUT_S:.0f}s",
        remedy=f"read {log_path(ws)}",
    )


def _listen() -> None:
    """Answer a stop signal by asking this process's loops to finish."""
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def _wait(seconds: float) -> None:
    """Sleep, in slices, so a stop request is noticed rather than slept through."""
    deadline = time.monotonic() + seconds
    while not stopping() and time.monotonic() < deadline:
        time.sleep(_TICK)


def _signal(pid: int, number: int) -> None:
    """Send one signal, tolerating a process that went away between the check and the send."""
    with contextlib.suppress(OSError):
        os.kill(pid, number)


def _gone(pid: int, seconds: float) -> bool:
    """Wait up to `seconds` for a process to exit, and say whether it did."""
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(_TICK)
    return not _alive(pid)


def _alive(pid: int) -> bool:
    """Whether a process with this id exists."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
