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

**Stopping keeps everything.** `stop` sends one signal. The supervisor passes it on to
every child at once and exits; a worker kills the card it is watching, begins no further
proposal or card, leaves the run open and the lane directory where it is, and exits too. A
worker still busy when the one grace the supervisor gives them all runs out — waiting on a
model, say — is killed, and that costs the call and nothing else: the run, its blobs and
its `best` are all in state. Nothing is ended and nothing is cleaned up, so the next
`start` picks the runs up where they were left — which is why stopping the daemon is a
cheap act an operator can perform without thinking about what it costs.

**Nothing the daemon starts outlives it.** The lanes and the monitor run in the supervisor's
process group, and a supervisor that does not answer `stop` is killed with that whole
group. A card leads a session of its own, so no kill aimed at its lane reaches it; it
watches its parent instead and ends itself once the lane that started it is gone. A lane
and the monitor watch theirs the same way, and stop as though told to once the supervisor
is gone however it went.

On macOS the supervisor runs under `caffeinate -i`, so a research host does not idle-sleep
mid-run. The monitor is a fourth kind of child beside the lanes: one pass of the paper and
live gates per `[monitor] interval`, in a process of its own for the same reason a lane is
in one.
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
from kanso.nautilus import backtest
from kanso.research import driver as research_driver
from kanso.research import explore, lanes, records, scheduler
from kanso.schemas import RunRecord, parse_duration
from kanso.state import StateStore, usable
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
"""How long the children, all together, are given to finish what they are on before the
supervisor kills whichever are left."""

CARDS_PER_TURN: Final = 1
"""How many cards a lane asks the driver for before it looks up. One, so that stopping
the daemon costs at most one card and never waits out a whole run — a run is
indefinite, and a shutdown cannot wait for one to end."""

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

MONITOR_FAILED: Final = "monitor_failed"
"""The event a monitoring pass that could not run at all leaves; the loop keeps its cadence."""

_STOPPING = False
"""Set by the signal handler; every loop in this process reads it between iterations."""

_SUPERVISOR: int | None = None
"""The supervisor a lane or the monitor answers to, once it has taken it on (`_answer_to`).
`None` in the supervisor itself, whose own parent is whatever started it — the `start`
command, which exits at once — and is nothing to answer to."""


def request_stop(*_: object) -> None:
    """Ask this process's loops to finish what they are doing and return.

    A card in flight is killed with them: the child leads its own session and would
    otherwise outlive the lane, unbudgeted. The card is not recorded and the run stays
    open, so the next start resumes it from its last card.
    """
    global _STOPPING
    _STOPPING = True
    backtest.interrupt()


def clear_stop() -> None:
    """Forget a stop request, and the supervisor. For a test that runs a loop more than once."""
    global _STOPPING, _SUPERVISOR
    _STOPPING = False
    _SUPERVISOR = None
    backtest.resume()


def stopping() -> bool:
    """Whether this process has been asked to stop.

    In a lane or the monitor, a supervisor that is no longer this process's parent is the
    same request, taken the moment it is noticed, card interrupt and all: the supervisor
    was killed, crashed or went without it, and a child nobody supervises would go on
    working runs the next `start` hands to a lane of the same name.
    """
    if not _STOPPING and _SUPERVISOR is not None and os.getppid() != _SUPERVISOR:
        request_stop()
    return _STOPPING


def _answer_to(supervisor: int) -> None:
    """Take `supervisor` on as the process this one stops without (`stopping`)."""
    global _SUPERVISOR
    _SUPERVISOR = supervisor


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
    """Signal the daemon and wait for it to go. Runs and lane directories stay.

    The supervisor signals its children together and kills what is left after one shared
    grace, so a supervisor still here after `STOP_TIMEOUT_S` has not answered at all, and
    nothing it holds is worth waiting longer for. It is then killed with its process group,
    which holds every lane and the monitor, so a stop that has to insist still leaves no
    child of the daemon running (`_kill_group`).
    """
    pid = pid_of(ws)
    if pid is None:
        raise PreconditionError(
            "no daemon is running in this workspace",
            remedy="run `kanso research start`",
        )
    _signal(pid, signal.SIGTERM)
    if not _gone(pid, STOP_TIMEOUT_S):
        _kill_group(pid)
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
    """The supervisor: hold the lock, recover what a dead lane dropped, start the children,
    wait, pass the signal on."""
    lock = _acquire(ws)
    _listen()
    recover(ws)
    children = [_spawn(ws, LANE, name) for name in lane_names(ws)]
    children.append(_spawn(ws, MONITOR))
    try:
        while not stopping():
            _wait(POLL_S)
    finally:
        _terminate(children)
        pid_path(ws).unlink(missing_ok=True)
        lock.close()
    return 0


def recover(ws: Workspace) -> list[str]:
    """Put back in the queue every hypothesis a dead lane dropped between claim and run.

    A lane takes a hypothesis out of the queue when it claims it and records the run only
    once the baseline has finished, which can be minutes later; a lane killed in between
    leaves the hypothesis with neither a run nor a place in the queue, where nothing
    reports it, and only the queue's record of the claim says a lane was holding it. The
    supervisor reads that record every time it starts. A hypothesis whose run the operator
    ended, or that the operator took out of the queue, is left where they put it.
    """
    with StateStore(ws.path("state.db")) as store:
        usable(store, ws.path("state.db"))
        return scheduler.recover(store)


def worker(ws: Workspace, lane: str) -> int:
    """One lane: finish this lane's own run first, then take the next queued hypothesis.

    The driver is asked for `CARDS_PER_TURN` cards rather than for the run, so the lane
    reaches this loop between cards and a stop request is answered there. Everything the
    driver needs to carry on — how many non-keeps in a row, how long since the last
    alignment check, what the last diff was — it reads back out of the run, so a turn is
    a resumption and the run is unaware it was interrupted.

    A failure is recorded with the remedy its error carried, because the message says which
    step gave up and only the remedy says why: a proposer's ladder names the models it
    tried, and what the last of them answered is the whole of the diagnosis.

    A hypothesis it could not research goes back in the queue rather than out of it — a
    baseline that will not run returns behind the stalled ones, and anything that failed
    mid-run returns beside them — and the lane waits before taking anything else, so a
    provider that is down costs a call a minute rather than a call a second. The two
    exceptions are the operator's: a hypothesis retired, or taken out of the queue, while
    the lane held it stays out.

    A turn that ended in a stall is where `[research] explore_after_stalls` is read: after
    the driver returns, so the stall's certification and its requeue are already done and
    the parent is not held while a model writes a new hypothesis. What that exploration
    does, failure included, is its own events (`research/explore.py`) and never a
    `lane_failed`.

    A lane told to stop starts nothing more: the driver asks before every proposal and
    every card, and an exploration is not begun either. The same holds once the supervisor
    that started the lane is gone, however it went (`stopping`).
    """
    lane = lanes.check_lane(lane)
    _listen()
    _answer_to(os.getppid())
    with StateStore(ws.path("state.db")) as store:
        usable(store, ws.path("state.db"))
        while not stopping():
            subject = claim(store, lane)
            if subject is None:
                _wait(POLL_S)
                continue
            try:
                outcome = research_driver.run(
                    ws, store, subject, cards=CARDS_PER_TURN, lane=lane, stop=stopping
                )
            except KansoError as exc:
                if stopping():
                    break  # the card was interrupted, not failed; the run resumes next start
                store.event(
                    LANE_FAILED,
                    subject,
                    {"lane": lane, "error": exc.message, "because": exc.remedy},
                )
                scheduler.put_back(store, subject)
                _wait(BACKOFF_S)
            else:
                if outcome.ended and not stopping():
                    explore.after_stall(ws, store, subject, lane)
    return 0


def monitor(ws: Workspace) -> int:
    """The monitoring pass, on `[monitor] interval`.

    One pass per interval, over its own store, for the same reason a lane has one: a
    SQLite connection belongs to the process that opened it. A pass that raises is
    recorded and the cadence is kept, because the loop that watches the money is the last
    thing that should stop when one version of it cannot be judged.

    A workspace with nothing deployed is the ordinary case here: the pass finds no version
    and the loop simply keeps its cadence until deployment gives it one. Like a lane, the
    monitor stops once the supervisor that started it is gone (`stopping`).
    """
    from kanso.monitor import run_once

    interval = parse_duration(ws.config.monitor.interval, "monitor.interval").total_seconds()
    _listen()
    _answer_to(os.getppid())
    with StateStore(ws.path("state.db")) as store:
        usable(store, ws.path("state.db"))
        while not stopping():
            try:
                run_once(ws, store)
            except KansoError as exc:
                store.event(MONITOR_FAILED, MONITOR, {"error": exc.message})
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
    return scheduler.dequeue(store, lane)


def main(argv: Sequence[str]) -> int:
    """`python -m kanso.research <serve|lane|monitor> <workspace> [lane]`.

    The schema is checked here, before the supervisor takes the lock or spawns anything,
    because this is the entry point a service unit starts and the supervisor itself never
    opens the store. Checking only where a store is opened for work would let a unit come
    up, spawn lanes that each refuse, and restart them for as long as the unit is enabled;
    the operator would see a crash loop rather than the one sentence that explains it.
    """
    if len(argv) < 2:
        raise PreconditionError(
            f"usage: python -m {MODULE} <{SERVE}|{LANE}|{MONITOR}> <workspace> [lane]"
        )
    command, root, *rest = argv
    if command not in (SERVE, LANE, MONITOR):
        raise PreconditionError(f"{command!r} is not one of {SERVE}, {LANE}, {MONITOR}")
    ws = find(Path(root))
    with StateStore(ws.path("state.db")) as opened:
        usable(opened, ws.path("state.db"))
    if command == SERVE:
        return serve(ws)
    if command == LANE:
        return worker(ws, rest[0])
    return monitor(ws)


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


def _terminate(children: Sequence[subprocess.Popen[bytes]]) -> None:
    """Ask every child to stop at once, and kill whichever is left after one shared grace.

    A lane inside a card answers at its watcher's next poll, but a lane waiting on a model
    answers only when the call returns, which may be minutes; a shutdown that waited for one
    would not be a shutdown. So every child is signalled before any is waited on, and they
    share one `GRACE_S`: the supervisor is gone within about that however many lanes it
    runs, well inside what `stop` gives it. Signalled one at a time, each with a grace of its
    own, seven busy children took seven graces — longer than `stop` waits — and the
    supervisor was killed with the last of them never signalled at all, measured on a live
    workspace on 2026-09-25: three children orphaned, still starting cards minutes later.
    A lane killed here cannot kill the card it was watching, which leads its own session;
    the card ends itself once its lane is gone (`kanso.nautilus.backtest`).
    """
    running = [child for child in children if child.poll() is None]
    for child in running:
        child.terminate()
    deadline = time.monotonic() + GRACE_S
    for child in running:
        try:
            child.wait(timeout=max(0.0, deadline - time.monotonic()))
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


def _kill_group(pid: int) -> None:
    """Kill a supervisor and every process in the group it leads.

    `start` runs the supervisor in a session of its own, and a service manager starts it
    as a group leader too, so its group is the daemon: the supervisor, its lanes and its
    monitor, which it spawns without a group of their own. A supervisor that leads no group
    shares one with whatever started it, and that group is not the daemon's to kill, so it
    is killed alone and its lanes stop on their own once it is gone (`worker`).
    """
    try:
        leads = os.getpgid(pid) == pid
    except OSError:
        return
    if leads:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGKILL)
    else:
        _signal(pid, signal.SIGKILL)


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
