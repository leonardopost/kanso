"""Whether a process is still running, as a test that orphans one has to ask it.

A process that has exited stays in the process table as a zombie until its parent reaps it,
and `os.kill(pid, 0)` succeeds on a zombie. A test that orphans a process cannot reap it —
the kernel hands it to another parent, which reaps it when it gets round to it — so this
asks `ps` for the process's state instead, and a zombie is not running.

`holding` stands in for a lane or the monitor whose daemon is gone: a process holding that
child's lock, which is all `kanso.research.daemon.living` reads.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Final

HOLDER: Final = r"""
import signal, sys, time
from pathlib import Path

from kanso.research import daemon
from kanso.workspace import find

if sys.argv[3] == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with daemon._supervised(find(Path(sys.argv[1])), sys.argv[2]):
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
