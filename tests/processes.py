"""Whether a process is still running, as a test that orphans one has to ask it.

A process that has exited stays in the process table as a zombie until its parent reaps it,
and `os.kill(pid, 0)` succeeds on a zombie. A test that orphans a process cannot reap it —
the kernel hands it to another parent, which reaps it when it gets round to it — so this
asks `ps` for the process's state instead, and a zombie is not running.

`holding` stands in for a lane or the monitor whose daemon is gone: a process holding that
child's two locks, which are all `kanso.research.daemon.living` and `ending` read. `CARDING`
stands in for a lane busy with a card when it is killed.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Final

HOLDER: Final = r"""
import os, signal, sys, time
from pathlib import Path

from kanso.research import daemon
from kanso.workspace import find

if sys.argv[3] == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with daemon._supervised(find(Path(sys.argv[1])), sys.argv[2], os.getppid()):
    sys.stdout.write("held\n")
    sys.stdout.flush()
    time.sleep(60)
"""
"""A child of the daemon in all but its supervisor: it holds its lock and does nothing."""


def holding(root: Path, child: str, *, deaf: bool = False) -> subprocess.Popen[str]:
    """A process holding the lock of the daemon's child `child` in the workspace at `root`,
    once it holds it.

    One that is `deaf` ignores `SIGTERM`, as a lane waiting on a model goes on waiting; the
    other ends on it. Its parent is the test, which it never sees go, so only a signal ends it.
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(root), child, "deaf" if deaf else "plain"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline() == "held\n"
    return holder


CARDING: Final = r"""
import os, signal, subprocess, sys
from pathlib import Path

from kanso.nautilus import backtest
from kanso.research import daemon
from kanso.workspace import find

AFTER = (
    "import os, sys, time\n"
    "print(os.getpid(), flush=True)\n"
    "parent = os.getppid()\n"
    "while os.getppid() == parent:\n"
    "    time.sleep(0.02)\n"
    "time.sleep(float(sys.argv[1]))\n"
)


def same(fd, path):
    try:
        return os.path.samestat(os.fstat(fd), os.stat(path))
    except OSError:
        return False


ws = find(Path(sys.argv[1]))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with daemon._supervised(ws, "l1", os.getppid()):
    lock = daemon.lock_path(ws, "l1", os.getpid())
    subprocess.Popen(
        [sys.executable, "-c", AFTER, "0.3"],
        stdout=subprocess.DEVNULL,
        pass_fds=(next(fd for fd in range(3, 1024) if same(fd, lock)),),
        start_new_session=True,
    )
    sys.stdout.write("held\n")
    sys.stdout.flush()
    backtest.watched(
        [sys.executable, "-c", AFTER, "1.0"],
        cwd=ws.root,
        errors=Path(sys.argv[2]),
        env=None,
        budget_s=None,
        mem_cap_gb=None,
    )
"""
"""Lane l1 of the workspace at `argv[1]`, deaf to `SIGTERM` as a lane waiting on a model is, and
inside a card it started as a lane starts one (`backtest.watched`), which prints its pid to
`argv[2]` and ends itself a second after its lane is gone — as a card still importing what it
runs does. The lane's own lock is held besides by a process that lets go of it 0.3 s after the
lane is gone: a lane the kernel has yet to finish tearing down when it is read, which after
`SIGKILL` is a moment, stretched here so that a reader in that moment is not a matter of luck.
Its parent is the process it answers to."""


def carding(root: Path, said: Path) -> subprocess.Popen[str]:
    """`CARDING` started from this process, once it holds its locks."""
    lane = subprocess.Popen(
        [sys.executable, "-c", CARDING, str(root), str(said)], stdout=subprocess.PIPE, text=True
    )
    assert lane.stdout is not None and lane.stdout.readline() == "held\n"
    return lane


def card_of(said: Path, within_s: float = 30.0) -> int:
    """The pid the card `CARDING` started printed to `said`, once it has."""
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        text = said.read_text() if said.is_file() else ""
        if text.endswith("\n"):
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"no card printed its pid to {said} within {within_s:.0f}s")


def running(pid: int) -> bool:
    """Whether `pid` names a process that exists and is not a zombie."""
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def children(pid: int) -> list[tuple[int, str]]:
    """Every process whose parent is `pid`, with its whole command line, zombies included.

    `-ww` because the two `ps` differ when their output is a pipe: BSD's prints every column
    of the command, procps's cuts the line at 80 unless asked twice for width, and a lane's
    name is the last word of a command well over 80 characters long.
    """
    listing = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    found: list[tuple[int, str]] = []
    for line in listing.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1] == str(pid):
            found.append((int(fields[0]), fields[2]))
    return found


def ends(pid: int, within_s: float) -> bool:
    """Wait up to `within_s` for `pid` to stop running, and say whether it did."""
    deadline = time.monotonic() + within_s
    while running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not running(pid)
