"""Research: lane directories, the card sequence, the keep rule and the rendered log.

`begin` starts a run in a lane, `card` evaluates whatever the lane's `strategy.py` now
says, and `end` closes the run and removes the directory. Between them sit the four rules
this package exists to enforce: a run is pinned to the bytes it began with, a card is
judged before it is executed, an improvement must clear its own noise floor, and every
result is a record in the state store rather than a line in a file.

The interactive path and the autonomous driver reach exactly these functions, so a card
proposed by a coding agent and a card proposed by the driver are evaluated identically.
The driver adds only what a person supplies by hand: what to change next, whether the file
still tests the idea, and when to stop. The scheduler decides which hypothesis a free lane
takes, and the daemon is the process that gives it one.
"""

from __future__ import annotations

from kanso.research.align import align_static
from kanso.research.align import check as align_check
from kanso.research.daemon import Status, claim, lane_names, serve, start, status, stop, worker
from kanso.research.diff import apply as apply_diff
from kanso.research.diff import unified as unified_diff
from kanso.research.driver import Outcome
from kanso.research.driver import run as drive
from kanso.research.keep import keep
from kanso.research.lanes import DEFAULT_LANE, lane_dir
from kanso.research.loop import BASELINE, Setup, begin, card, end
from kanso.research.records import active, cards_of, n_trials, runs_of
from kanso.research.results import results_file, results_tsv, write_results
from kanso.research.scheduler import QueueItem, Stall, dequeue, enqueue, on_stall, queued, requeue

__all__ = [
    "BASELINE",
    "DEFAULT_LANE",
    "Outcome",
    "QueueItem",
    "Setup",
    "Stall",
    "Status",
    "active",
    "align_check",
    "align_static",
    "apply_diff",
    "begin",
    "card",
    "cards_of",
    "claim",
    "dequeue",
    "drive",
    "end",
    "enqueue",
    "keep",
    "lane_dir",
    "lane_names",
    "n_trials",
    "on_stall",
    "queued",
    "requeue",
    "results_file",
    "results_tsv",
    "runs_of",
    "serve",
    "start",
    "status",
    "stop",
    "unified_diff",
    "worker",
    "write_results",
]
