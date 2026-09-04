"""Research: lane directories, the card sequence, the keep rule and the rendered log.

`begin` starts a run in a lane, `card` evaluates whatever the lane's `strategy.py` now
says, and `end` closes the run and removes the directory. Between them sit the four rules
this package exists to enforce: a run is pinned to the bytes it began with, a card is
judged before it is executed, an improvement must clear its own noise floor, and every
result is a record in the state store rather than a line in a file.

The interactive path and the autonomous driver reach exactly these functions, so a card
proposed by a coding agent and a card proposed by the driver are evaluated identically.
"""

from __future__ import annotations

from kanso.research.align import align_static
from kanso.research.keep import keep
from kanso.research.lanes import DEFAULT_LANE, lane_dir, log_file
from kanso.research.loop import BASELINE, Setup, begin, card, end
from kanso.research.records import active, cards_of, n_trials, runs_of
from kanso.research.results import results_file, results_tsv, write_results

__all__ = [
    "BASELINE",
    "DEFAULT_LANE",
    "Setup",
    "active",
    "align_static",
    "begin",
    "card",
    "cards_of",
    "end",
    "keep",
    "lane_dir",
    "log_file",
    "n_trials",
    "results_file",
    "results_tsv",
    "runs_of",
    "write_results",
]
