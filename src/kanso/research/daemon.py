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
every child at once and exits; a worker kills the card or the certification it is watching,
begins no further proposal or card, leaves the run open and the lane directory where it is,
and exits too — whatever it ran before, since every trading node hands the stop signals
back to the process that built it (`kanso.nautilus.session.signals_kept`). The monitor
exits once a demotion it is making has finished (`kanso.portfolio.child`). A worker still
busy when the one grace the supervisor gives them all runs out — waiting on a
model, say — is killed, and that costs the call and nothing else: the run, its blobs and
its `best` are all in state. Nothing is ended and nothing is cleaned up, so the next
`start` picks the runs up where they were left — which is why stopping the daemon is a
cheap act an operator can perform without thinking about what it costs.

**Nothing the daemon starts outlives it.** The lanes and the monitor run in the supervisor's
process group, and a supervisor that does not answer `stop` is killed with that whole
group. A card leads a session of its own, so no kill aimed at its lane reaches it; it
watches its parent instead and ends itself once the lane that started it is gone. A lane
and the monitor watch their supervisor the same way, on a thread of their own, from the
supervisor's pid on their command line rather than from whatever parent they find once
started, and stop as though told to once it is gone however it went: a card in flight is
killed at that thread's next look, not run to its end. Each holds a lock named for it and
its pid under `runs/` for as long as it lives, so one still running after its supervisor is
gone — waiting on a model, say, when the supervisor was killed — is never invisible:
`status` names it beside the stopped daemon, `start` and `serve` refuse while it runs, and
`stop` ends it. And each holds a second lock that every card, certification or demotion it
starts inherits, held until the last of them has exited, which is what `stop` waits on
before it returns: it sees each end rather than guessing how long that takes, and names one
still running when its patience runs out (`ORPHAN_S`).

**Every child the plan names stays running.** A lane or the monitor can end while nobody
stopped the daemon — the kernel's OOM killer takes a lane on a large card, a lane crashes —
so the supervisor looks at its children every `POLL_S`. One found ended is recorded
(`lane_died`, `monitor_died`: how it ended, how long it had run, how long it waits) and
started again under the same name. A lane started in a dead one's place resumes the open
run the dead one left, because a lane's own run is the first thing it claims, and that
run's next card empties the `.card/` the killed card left. What the dead lane held with no
run — a baseline in flight, a stall not yet decided — goes back in the queue as the next
`start` would put it back, only sooner, and the directory that baseline was running in goes
with it. A child that ran less than `SETTLED_S` waits before it comes back: `RESTART_S`
after the first such death, twice as long after each one in a row, never more than
`RESTART_CAP_S`, so a lane that cannot stay up costs a start every five minutes rather than
a start a second. A child that ends because the daemon is stopping is not started again.

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
import threading
import time
from collections.abc import Iterator, Sequence
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
from kanso.research.loop import TakenError
from kanso.schemas import RunRecord, parse_duration
from kanso.state import Event, StateStore, usable
from kanso.workspace import LANE_ROOT, Workspace, find

__all__ = [
    "BACKOFF_S",
    "CAFFEINATE",
    "CARDS_PER_TURN",
    "ChildPid",
    "GRACE_S",
    "LANE_DIED",
    "LANE_PREFIX",
    "LOCK_SUFFIX",
    "LOG_NAME",
    "LaneRun",
    "MODULE",
    "MONITOR_DIED",
    "ORPHAN_S",
    "PID_NAME",
    "POLL_S",
    "RESTART_CAP_S",
    "RESTART_S",
    "Restart",
    "SETTLED_S",
    "Status",
    "Stopped",
    "WORK_SUFFIX",
    "claim",
    "clear_stop",
    "ending",
    "held_off",
    "lane_names",
    "living",
    "lock_path",
    "log_path",
    "main",
    "monitor",
    "pid_of",
    "pid_path",
    "request_stop",
    "restarts",
    "serve",
    "start",
    "status",
    "stop",
    "stopping",
    "work_path",
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

LOCK_SUFFIX: Final = ".lock"
"""A lane or the monitor holds `runs/<name>.<pid>.lock` under `flock` for as long as it
lives, so whether it is alive is the kernel's answer rather than a pid's, and its pid is in
the file's name rather than in what a reader might catch half written. One whose lock nobody
holds is what a child killed outright left; the next supervisor removes it."""

WORK_SUFFIX: Final = ".work"
"""A lane or the monitor also holds `runs/<name>.<pid>.work` under `flock`, and hands it down
to every card, certification or demotion it starts (`backtest.handed_down`), so the lock is
held until the child and everything it started have exited. Its own `.lock` says whether the
lane is alive; this one says whether anything it started still is."""

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

ORPHAN_S: Final = 10.0
"""The longest `stop` waits, once no lane or monitor is left, for a card, a certification or
a demotion whose lane or monitor was killed outright to notice and end itself. It waits on
the lock each inherited (`WORK_SUFFIX`), so it returns the moment the last has exited: within
`backtest.PARENT_POLL_S` of its lane going for one that is looking, and once it has imported
what it runs for one still starting, which looks only then — 0.9 to 2.9 s for a card,
measured on 2026-10-08 on the development Mac under a load average of 3.7. Waiting a fixed
second instead, `stop` returned on a loaded CI runner while such a card still ran."""

RESTART_S: Final = 2.0
"""How long a child that died young waits to be started again the first time; each young
death in a row after that doubles the wait."""

RESTART_CAP_S: Final = 300.0
"""The longest a child waits to be started again, however many times in a row it died young."""

SETTLED_S: Final = 60.0
"""How long a child must have run for its death to be a one-off rather than a loop: one that
ran this long is started again at once, and the doubling starts over."""

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

LANE_DIED: Final = "lane_died"
"""The event the supervisor appends, under the lane's name, for a lane that ended while the
daemon was not stopping: its pid, the status it exited with or the signal that took it, how
long it had run, how long it waits to be started again, the supervisor's pid, the hypothesis
whose open run it left to the lane started in its place, and what it held with no run and
put back in the queue."""

MONITOR_DIED: Final = "monitor_died"
"""The same for the monitor, under `monitor`, without the two fields only a lane has."""

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
    working runs the next `start` hands to a lane of the same name. The loops ask between
    iterations, and a thread asks every `backtest.PARENT_POLL_S` besides (`_supervised`),
    because a lane watching a card asks nothing until the card is over.
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
class Restart:
    """A child the running daemon started again: how many times it died, and how the newest
    death went — when, the status it exited with or the signal that took it, how long it had
    run, and how long it waited to be started again."""

    child: str
    deaths: int
    at: str
    exit: int | None
    signal: str | None
    lived_s: float
    restart_in_s: float

    def payload(self) -> dict[str, object]:
        return {
            "child": self.child,
            "deaths": self.deaths,
            "at": self.at,
            "exit": self.exit,
            "signal": self.signal,
            "lived_s": self.lived_s,
            "restart_in_s": self.restart_in_s,
        }


@dataclass(frozen=True)
class ChildPid:
    """A lane or the monitor that is alive, by name and pid, as the lock it holds says."""

    child: str
    pid: int

    @property
    def label(self) -> str:
        """How a report names it: `lane l1 (pid 4242)`, or `monitor (pid 4243)`."""
        what = self.child if self.child == MONITOR else f"lane {self.child}"
        return f"{what} (pid {self.pid})"

    def payload(self) -> dict[str, object]:
        return {"child": self.child, "pid": self.pid}


@dataclass(frozen=True)
class Status:
    """What `research status` reports: the daemon, the children alive, what children that are
    gone started and is still running, its lanes, what it had to start again, its runs and
    its queue.

    `children` is read off the children's own locks, not off the supervisor, so a child that
    outlived its supervisor is listed beside a daemon that reads as stopped. `ending` names a
    lane or the monitor that is gone by the card, certification or demotion of it still
    running, read off the lock that one inherited: it ends itself once it sees its lane gone.
    """

    running: bool
    pid: int | None
    children: tuple[ChildPid, ...]
    ending: tuple[ChildPid, ...]
    lanes: tuple[str, ...]
    runs: tuple[LaneRun, ...]
    queue: tuple[scheduler.QueueItem, ...]
    restarts: tuple[Restart, ...]

    def payload(self) -> dict[str, object]:
        """The status as one JSON object."""
        return {
            "running": self.running,
            "pid": self.pid,
            "children": [child.payload() for child in self.children],
            "ending": [child.payload() for child in self.ending],
            "lanes": list(self.lanes),
            "restarts": [item.payload() for item in self.restarts],
            "runs": [run.payload() for run in self.runs],
            "queue": [item.payload() for item in self.queue],
        }


@dataclass(frozen=True)
class Stopped:
    """What `stop` did: the supervisor it stopped, when one was running; every lane or monitor
    it found still running once no supervisor was left, and ended itself; and every lane or
    monitor whose card, certification or demotion was still running when it stopped waiting
    (`ORPHAN_S`), which ends itself."""

    pid: int | None
    orphans: tuple[ChildPid, ...]
    ending: tuple[ChildPid, ...]

    def payload(self) -> dict[str, object]:
        return {
            "running": False,
            "pid": self.pid,
            "orphans": [child.payload() for child in self.orphans],
            "ending": [child.payload() for child in self.ending],
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


def lock_path(ws: Workspace, child: str, pid: int) -> Path:
    """The lock the child `child` running as `pid` holds for as long as it lives."""
    return ws.path(LANE_ROOT, f"{child}.{pid}{LOCK_SUFFIX}")


def work_path(ws: Workspace, child: str, pid: int) -> Path:
    """The lock the child `child` running as `pid` holds, and hands down to what it starts."""
    return ws.path(LANE_ROOT, f"{child}.{pid}{WORK_SUFFIX}")


def living(ws: Workspace) -> tuple[ChildPid, ...]:
    """Every lane and monitor alive in this workspace, whichever daemon started it.

    Read off the locks the children hold rather than off the supervisor, so a child still
    running after its supervisor is gone is found as surely as one the running daemon
    supervises. A lock nobody holds is one a child killed outright left, and names nothing.
    """
    return _holders(ws, LOCK_SUFFIX)


def ending(ws: Workspace) -> tuple[ChildPid, ...]:
    """Every lane or monitor that is gone while a card, certification or demotion it started
    still runs, read off the lock that one inherited.

    The living are read first: a lane exiting cleanly removes its `.work` before its `.lock`,
    so one read alive here is never read as gone with its work still held.
    """
    alive = living(ws)
    return tuple(child for child in _holders(ws, WORK_SUFFIX) if child not in alive)


def _holders(ws: Workspace, suffix: str) -> tuple[ChildPid, ...]:
    """Every child whose `runs/<child>.<pid><suffix>` somebody holds, by name and pid."""
    found: list[ChildPid] = []
    for path in sorted(ws.path(LANE_ROOT).glob(f"*{suffix}")):
        child, _, pid = path.name.removesuffix(suffix).partition(".")
        if pid.isdigit() and _held(path):
            found.append(ChildPid(child, int(pid)))
    return tuple(found)


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
    """Start the daemon and return its pid.

    Refuses when one is already running, and when a lane or the monitor of one that is gone
    still runs: a lane started now would claim the very run that child is still working,
    in the same lane directory.
    """
    running = pid_of(ws)
    if running is not None:
        raise PreconditionError(
            f"a daemon is already running in this workspace (pid {running})",
            remedy="run `kanso research stop` first",
        )
    _refuse_outlived(ws, "start again")
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


def stop(ws: Workspace) -> Stopped:
    """Signal the daemon, wait for it to go, end any child of it still running, and wait for
    what those children started to end. Runs and lane directories stay.

    The supervisor signals its children together and kills what is left after one shared
    grace, so a supervisor still here after `STOP_TIMEOUT_S` has not answered at all, and
    nothing it holds is worth waiting longer for. It is then killed with its process group,
    which holds every lane and the monitor, so a stop that has to insist still leaves no
    child of the daemon running (`_kill_group`); the stop waits for the kernel to release the
    locks of the lanes and the monitor it killed that way, so none of them is then taken for
    a child that outlived its supervisor. A child still running once the supervisor is gone
    — a supervisor that led no group, or one that was gone before the stop — is ended here
    (`_end`), and a stop with no supervisor but such a child is a stop of that child, not a
    refusal.

    A card leads a session of its own, so a lane killed outright leaves it to see its lane
    gone and end itself. The stop waits for that on the lock each card inherited
    (`_settle`), which it reads only once every lane it killed has let go of its own: a lane
    read as alive has its card's lock read as its own. So it returns once every card,
    certification and demotion of the daemon has exited — and, should one not have exited
    `ORPHAN_S` after the last lane went, names it rather than returning as though it had.
    """
    pid = pid_of(ws)
    if pid is None and not living(ws) and not ending(ws):
        raise PreconditionError(
            "no daemon is running in this workspace",
            remedy="run `kanso research start`",
        )
    if pid is not None:
        _signal(pid, signal.SIGTERM)
        if not _gone(pid, STOP_TIMEOUT_S):
            killed = _kill_group(ws, pid)
            _gone(pid, GRACE_S)
            _released(ws, killed)
        pid_path(ws).unlink(missing_ok=True)
    orphans = _end(ws)
    return Stopped(pid, orphans, _settle(ws))


def status(ws: Workspace, store: StateStore) -> Status:
    """The daemon, the children alive, what children that are gone started and is still
    running, the lanes it would run, what it started again, the active runs and the queue."""
    pid = pid_of(ws)
    return Status(
        running=pid is not None,
        pid=pid,
        children=living(ws),
        ending=ending(ws),
        lanes=lane_names(ws) if read_envelope(ws) is not None else (),
        runs=tuple(LaneRun(run, _lane_sha(ws, run)) for run in active_runs(store)),
        queue=tuple(scheduler.queued(store)),
        restarts=restarts(store, pid),
    )


def restarts(store: StateStore, supervisor: int | None) -> tuple[Restart, ...]:
    """Every child the daemon running as `supervisor` started again, in the order they first
    died, each with the newest of its deaths; nothing when no daemon runs.

    A death names the supervisor that recorded it, so one recorded under a daemon that has
    since stopped is that daemon's, and not reported against the one running now.
    """
    if supervisor is None:
        return ()
    ended = sorted(
        (*store.events(kind=LANE_DIED), *store.events(kind=MONITOR_DIED)),
        key=lambda event: event.event_id,
    )
    deaths: dict[str, int] = {}
    newest: dict[str, Event] = {}
    for event in ended:
        if event.detail.get("daemon") != supervisor:
            continue
        deaths[event.subject] = deaths.get(event.subject, 0) + 1
        newest[event.subject] = event
    return tuple(_restart(event, deaths[child]) for child, event in newest.items())


def _restart(event: Event, deaths: int) -> Restart:
    """One child's restarts, as its newest death recorded them."""
    code = event.detail.get("exit")
    name = event.detail.get("signal")
    return Restart(
        child=event.subject,
        deaths=deaths,
        at=event.ts,
        exit=code if isinstance(code, int) else None,
        signal=name if isinstance(name, str) else None,
        lived_s=float(str(event.detail["lived_s"])),
        restart_in_s=float(str(event.detail["restart_in_s"])),
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
    look at them every `POLL_S` and start again any that ended, and pass a stop on.

    The look is skipped once a stop has been asked for, so a child that ends because the
    daemon is stopping is never recorded as a death and never started again. It refuses, as
    `start` does, while a lane or the monitor of a daemon that is gone still runs — this is
    what a service unit runs, and what a hand starts, without `start`'s look first — and
    before any child starts, the locks children killed outright left are removed (`_sweep`).
    """
    lock = _acquire(ws)
    _listen()
    _sweep(ws)
    recover(ws)
    children = [_Child(LANE, name) for name in lane_names(ws)]
    children.append(_Child(MONITOR, MONITOR))
    for child in children:
        _start(ws, child, time.monotonic())
    try:
        while not stopping():
            _wait(POLL_S)
            if not stopping():
                _tend(ws, children, time.monotonic())
    finally:
        _terminate([child.process for child in children if child.process is not None])
        pid_path(ws).unlink(missing_ok=True)
        lock.close()
    return 0


def recover(ws: Workspace) -> list[str]:
    """Put back in the queue every hypothesis a dead lane dropped between claim and run.

    A lane takes a hypothesis out of the queue when it claims it and records the run only
    once the baseline has finished, which can be minutes later; a lane killed in between
    leaves the hypothesis with neither a run nor a place in the queue, where nothing
    reports it, and only the queue's record of the claim says a lane was holding it. The
    supervisor reads that record every time it starts, and for one lane whenever it finds
    that lane dead (`_bury`). A hypothesis whose run the operator ended, or that the
    operator took out of the queue, is left where they put it.
    """
    with StateStore(ws.path("state.db")) as store:
        usable(store, ws.path("state.db"))
        return scheduler.recover(store)


def worker(ws: Workspace, lane: str, supervisor: int) -> int:
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
    the lane held it stays out. One taken out before its run began is not a failure at
    all: the lane lets go of it at the next catalog read of its baseline, or within a
    second of the card (`loop.begin`), records nothing and claims the next hypothesis
    without waiting.

    A turn that ended in a stall is where `[research] explore_after_stalls` is read: after
    the driver returns, so the stall's certification and its requeue are already done and
    the parent is not held while a model writes a new hypothesis. That certification is made
    in a child the lane watches as it watches a card (`kanso.certify.child`), so the lane is
    left holding none of what its windows cost. What that exploration
    does, failure included, is its own events (`research/explore.py`) and never a
    `lane_failed`. The lane itself is held while it explores, and claims nothing: the
    `explore` class is routed to the top tier, so its ladder is two attempts, each waiting up
    to `REQUEST_TIMEOUT_S` (420 s) for an answer — about fourteen minutes at worst, once per
    spell — and a supervisor that died meanwhile is noticed only when the call returns. A
    `research stop` is not delayed by it: the lane is killed after the shared grace and the
    call with it.

    A lane told to stop starts nothing more: the driver asks before every proposal and
    every card, and an exploration is not begun either. The same holds once `supervisor`,
    the supervisor that started the lane, is gone, however it went — before the lane had
    started, too — and a card it is watching then is killed and not recorded, as on a stop
    (`_supervised`).
    """
    lane = lanes.check_lane(lane)
    _listen()
    with _supervised(ws, lane, supervisor), StateStore(ws.path("state.db")) as store:
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
            except TakenError:
                continue  # the operator took it back before its run began; take the next
            except KansoError as exc:
                if stopping():
                    break  # the card was interrupted, not failed; the run resumes next start
                store.event(
                    LANE_FAILED,
                    subject,
                    {"lane": lane, "error": exc.message, "because": exc.remedy},
                )
                scheduler.put_back(store, subject, lane)
                _wait(BACKOFF_S)
            else:
                if outcome.ended and not stopping():
                    explore.after_stall(ws, store, subject, lane)
    return 0


def monitor(ws: Workspace, supervisor: int) -> int:
    """The monitoring pass, on `[monitor] interval`.

    One pass per interval, over its own store, for the same reason a lane has one: a
    SQLite connection belongs to the process that opened it. A pass that raises is
    recorded and the cadence is kept, because the loop that watches the money is the last
    thing that should stop when one version of it cannot be judged.

    A workspace with nothing deployed is the ordinary case here: the pass finds no version
    and the loop simply keeps its cadence until deployment gives it one. Like a lane, the
    monitor stops once `supervisor`, the supervisor that started it, is gone (`stopping`).
    """
    from kanso.monitor import run_once

    interval = parse_duration(ws.config.monitor.interval, "monitor.interval").total_seconds()
    _listen()
    with _supervised(ws, MONITOR, supervisor), StateStore(ws.path("state.db")) as store:
        usable(store, ws.path("state.db"))
        while not stopping():
            try:
                run_once(ws, store)
            except KansoError as exc:
                store.event(MONITOR_FAILED, MONITOR, {"error": exc.message})
            _wait(interval)
    return 0


def claim(store: StateStore, lane: str) -> str | None:
    """What this lane works next: its own unfinished run, else the head of the queue.

    Its own run comes first, which is what makes a lane started in a dead one's place
    resume the run the dead one left.
    """
    mine = _run_in(store, lane)
    return mine if mine is not None else scheduler.dequeue(store, lane)


def _run_in(store: StateStore, lane: str) -> str | None:
    """The hypothesis whose open run this lane works, if it has one."""
    row = store.connection.execute(
        "SELECT hyp_id FROM runs WHERE ended_at IS NULL AND lane = ? ORDER BY started_at LIMIT 1",
        (lane,),
    ).fetchone()
    return None if row is None else str(row["hyp_id"])


def main(argv: Sequence[str]) -> int:
    """`python -m kanso.research serve <workspace>`, and the supervisor's own
    `lane <workspace> <lane> <supervisor>` and `monitor <workspace> <supervisor>`.

    A lane and the monitor are told the supervisor's pid rather than reading their parent once
    started. The interpreter takes about a second to import this module — 1.11 to 1.19 s,
    measured on 2026-10-08 — and a lane whose supervisor died in that second took whatever
    adopted it for its supervisor: started that way at `cfa334e`, one was still running
    under PPID 1 twenty seconds later, when the measurement killed it. Told the pid, the same
    lane ended 1.35 s after its supervisor was started.

    The schema is checked here, before the supervisor takes the lock or spawns anything,
    because this is the entry point a service unit starts. Checked only where a lane opens
    the store for work, an un-migrated workspace would bring the unit up and have every lane
    refuse, each started again and refusing again for as long as the unit is enabled; the
    operator would read a stream of deaths rather than the one sentence that explains them.
    """
    usage = (
        f"usage: python -m {MODULE} {SERVE} <workspace>"
        f" | {LANE} <workspace> <lane> <supervisor> | {MONITOR} <workspace> <supervisor>"
    )
    if len(argv) < 2:
        raise PreconditionError(usage)
    command, root, *rest = argv
    if command not in (SERVE, LANE, MONITOR):
        raise PreconditionError(f"{command!r} is not one of {SERVE}, {LANE}, {MONITOR}")
    if len(rest) != {SERVE: 0, LANE: 2, MONITOR: 1}[command] or (rest and not rest[-1].isdigit()):
        raise PreconditionError(usage)
    ws = find(Path(root))
    with StateStore(ws.path("state.db")) as opened:
        usable(opened, ws.path("state.db"))
    if command == SERVE:
        return serve(ws)
    if command == LANE:
        return worker(ws, rest[0], int(rest[1]))
    return monitor(ws, int(rest[0]))


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


@dataclass
class _Child:
    """One child the supervisor keeps running: what it is, its process, and its restarts.

    `process` is `None` while a child that ended waits to be started again at `due`; `wait`
    is how long it waited before its latest start, which the next young death doubles.
    """

    command: str
    name: str
    process: subprocess.Popen[bytes] | None = None
    started: float = 0.0
    wait: float = 0.0
    due: float = 0.0


def _start(ws: Workspace, child: _Child, now: float) -> None:
    """Start a child's process: a lane under its own name, or the monitor, each told the
    supervisor it answers to."""
    rest = (child.name,) if child.command == LANE else ()
    child.process = _spawn(ws, child.command, *rest, str(os.getpid()))
    child.started = now


def _tend(ws: Workspace, children: Sequence[_Child], now: float) -> None:
    """Record every child found ended since the last look, and start again each whose wait
    is over. `serve` calls this only while the daemon is not stopping."""
    for child in children:
        process = child.process
        code = None if process is None else process.poll()
        if process is not None and code is not None:
            _bury(ws, child, process.pid, code, now)
        if child.process is None and now >= child.due:
            _start(ws, child, now)


def _bury(ws: Workspace, child: _Child, pid: int, code: int, now: float) -> None:
    """Record a child that ended without being asked to, and set when it starts again.

    A lane's open run is only named: the lane started in its place claims it before
    anything else. What the lane held with no run goes back in the queue now, as the next
    `start` would put it back, and the lane directory a baseline in flight was running in
    goes with it — the hypothesis may well begin next in another lane, and nothing would
    ever empty this one, payload and all.
    """
    lived = now - child.started
    child.wait = _next_wait(child.wait, lived)
    child.due = now + child.wait
    child.process = None
    lock_path(ws, child.name, pid).unlink(missing_ok=True)
    _drop(work_path(ws, child.name, pid))  # unless a card it started has yet to see it gone
    detail: dict[str, object] = {
        "pid": pid,
        **_ended_by(code),
        "lived_s": round(lived, 3),
        "restart_in_s": child.wait,
        "daemon": os.getpid(),
    }
    with StateStore(ws.path("state.db")) as store:
        if child.command == MONITOR:
            store.event(MONITOR_DIED, MONITOR, detail)
            return
        run = _run_in(store, child.name)
        put_back = scheduler.recover(store, child.name)
        for hyp_id in put_back:
            lanes.remove(lanes.lane_dir(ws, child.name, hyp_id))
        store.event(
            LANE_DIED,
            child.name,
            {"lane": child.name, **detail, "run": run, "put_back": put_back},
        )


def _next_wait(wait: float, lived: float) -> float:
    """How long a child that ran `lived` seconds waits to be started again, when it waited
    `wait` before its latest start: not at all once it had settled, else `RESTART_S`
    doubling with each young death in a row, up to `RESTART_CAP_S`."""
    if lived >= SETTLED_S:
        return 0.0
    return min(max(wait * 2.0, RESTART_S), RESTART_CAP_S)


def _ended_by(code: int) -> dict[str, object]:
    """How a child ended, from its `Popen.returncode`: the status it exited with, or the
    signal that killed it — by name, or by number where the platform names none."""
    if code >= 0:
        return {"exit": code, "signal": None}
    try:
        name = signal.Signals(-code).name
    except ValueError:
        name = str(-code)
    return {"exit": None, "signal": name}


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
    the card ends itself once its lane is gone (`kanso.nautilus.backtest`), and `stop` waits
    for that on the lock the card inherited (`_settle`).
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
    handle = _lock(ws)
    if handle is None:
        raise PreconditionError(
            f"another daemon holds {pid_path(ws)}",
            remedy="run `kanso research stop`, or wait for the running daemon to exit",
        )
    with _released_on_refusal(handle):
        _refuse_outlived(ws, "start again")
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n".encode())
    handle.flush()
    return handle


@contextlib.contextmanager
def held_off(ws: Workspace) -> Iterator[None]:
    """Hold the daemon's lock for as long as the body runs, without being the daemon.

    For work that must have the state store to itself (`kanso state prune`). The lock is
    the one the supervisor takes, so a daemon already running refuses the work here, and a
    `start` made while the work runs finds the lock held and its supervisor exits at once.
    A lane or the monitor of a daemon that is gone writes the store as surely as one of a
    daemon running, so one still running refuses the work too; and so does a certification
    or a demotion one of them started, which writes the store itself until it sees its
    parent gone (`kanso.certify.child`, `kanso.portfolio.child`), read off the lock it
    inherited as `ending` reads it. Nothing is written on the file, so no `status` reads this
    process as a daemon.
    """
    handle = _lock(ws)
    if handle is None:
        raise PreconditionError(
            f"a daemon is running in this workspace: it holds {pid_path(ws)}",
            remedy="run `kanso research stop`, then run this again",
        )
    with _released_on_refusal(handle):
        _refuse_outlived(ws, "run this again")
        started = ending(ws)
        if started:
            named = ", ".join(f"what {child.label} started" for child in started)
            raise PreconditionError(
                f"still running from a daemon that is gone: {named}",
                remedy="run `kanso research stop`, which waits for it to end, then run this again",
            )
    try:
        yield
    finally:
        handle.close()


def _lock(ws: Workspace) -> IO[bytes] | None:
    """The pid file, open and locked by this process, or `None` when another holds it."""
    path = pid_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


@contextlib.contextmanager
def _supervised(ws: Workspace, name: str, supervisor: int) -> Iterator[None]:
    """Run the body as the daemon's child `name`: holding its two locks, answering to
    `supervisor`, and watching it go on a thread of its own.

    The first lock is `runs/<name>.<pid>.lock`, held under `flock` until the process ends
    however it ends, so `living` finds a child exactly as long as it lives; a card it starts
    does not inherit it, since a child is spawned with its descriptors closed. The second is
    `runs/<name>.<pid>.work`, which every card, certification or demotion it starts does
    inherit (`backtest.handed_down`), so it is held until the last of them has exited too:
    what `stop` waits on (`_settle`). On the way out the `.work` file goes before the
    `.lock`, so a reader that finds the lane alive never reads its work as orphaned
    (`ending`).

    The thread asks `stopping` every `backtest.PARENT_POLL_S`, as a card asks after its lane
    (`backtest.end_with`). Without it the question went unasked for as long as a lane
    watched a card, a baseline, a hold or a certification, whose watch reads only the
    interrupt. Measured in the suite's workspace on 2026-10-08, a lane whose supervisor alone
    was killed outright mid-card ran the card on to its budget and recorded it as a `crash`
    59.9 seconds after the kill; with the thread the card ended 0.34 seconds after it and
    nothing was recorded. A model call in flight is still answered before the lane looks up,
    and the driver then starts no card.
    """
    pid = os.getpid()
    lock, work = lock_path(ws, name, pid), work_path(ws, name, pid)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("ab") as alive, work.open("ab") as started:
        fcntl.flock(alive.fileno(), fcntl.LOCK_EX)
        fcntl.flock(started.fileno(), fcntl.LOCK_EX)
        _answer_to(supervisor)
        done = threading.Event()
        watcher = threading.Thread(
            target=_watch_supervisor, args=(done,), name="kanso-supervisor", daemon=True
        )
        watcher.start()
        try:
            with backtest.handed_down(started.fileno()):
                yield
        finally:
            done.set()
            watcher.join()
            work.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)


def _watch_supervisor(done: threading.Event) -> None:
    """Ask `stopping` every `backtest.PARENT_POLL_S` until it says so, or until `done`."""
    while not done.wait(backtest.PARENT_POLL_S):
        if stopping():
            return


def _held(path: Path) -> bool:
    """Whether a child, or something it handed its lock down to, holds the lock on `path`.

    Asked with a shared lock, which only a child's exclusive one refuses, so two readers
    asking at once never read each other as a child.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return False  # the child ended, and removed it, between the listing and the look
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def _drop(path: Path) -> None:
    """Remove a child's lock file, unless somebody still holds it."""
    if not _held(path):
        path.unlink(missing_ok=True)


def _sweep(ws: Workspace) -> None:
    """Remove every child's lock nobody holds: what a child killed outright left behind."""
    for suffix in (LOCK_SUFFIX, WORK_SUFFIX):
        for path in ws.path(LANE_ROOT).glob(f"*{suffix}"):
            _drop(path)


def _refuse_outlived(ws: Workspace, then: str) -> None:
    """Refuse while a lane or the monitor of a daemon that is gone still runs: a lane started
    beside it would claim the very run it is still working, in the same lane directory, and
    it writes the store as any lane does."""
    left = living(ws)
    if left:
        raise PreconditionError(
            f"still running from a daemon that is gone: {_named(left)}",
            remedy=f"run `kanso research stop`, which ends it, then {then}",
        )


@contextlib.contextmanager
def _released_on_refusal(handle: IO[bytes]) -> Iterator[None]:
    """Close the daemon's lock if the body refuses, so a refusal holds nothing."""
    try:
        yield
    except BaseException:
        handle.close()
        raise


def _end(ws: Workspace) -> tuple[ChildPid, ...]:
    """End every lane and monitor still running once the supervisor is gone, and name them.

    They are signalled together and share one `GRACE_S`, as the supervisor's own children do
    (`_terminate`): a lane answers at its next safe point, killing a card it is watching. One
    still holding its lock after that — waiting on a model, say, or a monitor waiting on a
    demotion — is killed, and a card it was running is left to end itself, which `stop` then
    waits for (`_settle`). It returns only once each killed one has let go of its lock, since
    until then `ending` reads it as alive and its card's lock as its own. Measured on
    2026-10-08 on the development Mac, with a lane that ignored `SIGTERM` inside a card that
    ended itself half a second after the lane was gone and a grace of 0.2 s: returning
    straight after the kill, `stop` read nothing ending and returned in 0.22 s with the card
    still running, ten times in ten; waiting, it returned in 0.76 to 0.79 s with the card gone.
    """
    left = living(ws)
    if not left:
        return left
    for child in left:
        _signal(child.pid, signal.SIGTERM)
    _released(ws, left)
    for child in _still(ws, left):
        _signal(child.pid, signal.SIGKILL)
    _released(ws, left)
    return left


def _still(ws: Workspace, these: Sequence[ChildPid]) -> list[ChildPid]:
    """Which of `these` are still alive."""
    return [child for child in living(ws) if child in these]


def _released(ws: Workspace, these: Sequence[ChildPid]) -> None:
    """Wait up to `GRACE_S` for every one of `these` to let go of its lock.

    The kernel releases a process's locks only as it closes its descriptors on the way out,
    so the lock of one just killed is still held for a moment, and a reader in that moment
    reads the child as alive.
    """
    deadline = time.monotonic() + GRACE_S
    while _still(ws, these) and time.monotonic() < deadline:
        time.sleep(_TICK)


def _settle(ws: Workspace) -> tuple[ChildPid, ...]:
    """Wait up to `ORPHAN_S` for every card, certification and demotion of a lane or monitor
    that is gone to end itself, and name the lanes and monitors whose work still runs.

    Read off the lock each inherited, which the kernel releases as the last of them exits,
    so the wait is as long as they take and no longer.
    """
    deadline = time.monotonic() + ORPHAN_S
    while ending(ws) and time.monotonic() < deadline:
        time.sleep(_TICK)
    return ending(ws)


def _named(found: Sequence[ChildPid]) -> str:
    """`lane l1 (pid 4242), monitor (pid 4243)`."""
    return ", ".join(child.label for child in found)


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


def _kill_group(ws: Workspace, pid: int) -> tuple[ChildPid, ...]:
    """Kill a supervisor and every process in the group it leads, and return the lanes and
    the monitor killed with it.

    `start` runs the supervisor in a session of its own, and a service manager starts it
    as a group leader too, so its group is the daemon: the supervisor, its lanes and its
    monitor, which it spawns without a group of their own. A supervisor that leads no group
    shares one with whatever started it, and that group is not the daemon's to kill, so it
    is killed alone and its lanes stop on their own once it is gone (`worker`). A lane or the
    monitor of a daemon gone before this one is in that daemon's group, and is not returned.
    """
    group = _group_of(pid)
    if group is None:
        return ()
    if group != pid:
        _signal(pid, signal.SIGKILL)
        return ()
    members = tuple(child for child in living(ws) if _group_of(child.pid) == pid)
    with contextlib.suppress(OSError):
        os.killpg(pid, signal.SIGKILL)
    return members


def _group_of(pid: int) -> int | None:
    """The process group `pid` is in, or `None` once it is gone."""
    try:
        return os.getpgid(pid)
    except OSError:
        return None


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
