"""Whether a process is still running, as a test that orphans one has to ask it.

A process that has exited stays in the process table as a zombie until its parent reaps it,
and `os.kill(pid, 0)` succeeds on a zombie. A test that orphans a process cannot reap it —
the kernel hands it to another parent, which reaps it when it gets round to it — so this
asks `ps` for the process's state instead, and a zombie is not running.
"""

from __future__ import annotations

import subprocess
import time


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
