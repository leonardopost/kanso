"""`results.tsv`: the run log, rendered from state rather than appended to on disk.

The file an operator or a coding agent reads is a projection. Every row is a card in the
state store, so the history survives what happens to the workspace: a discard restores
the lane's `strategy.py` and the row that produced it stays, a lane directory is removed
when a run ends and every row of that run stays, and a file deleted by hand comes back on
the next card. Nothing about a result lives only in a file.

Rows are ordered as the cards were recorded, across every run of the hypothesis, so the
file reads as one continuous experiment log rather than one per run. The row itself is
defined by the card model, so this module decides where the file goes and in what order
its rows are, and never how a number is spelled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from kanso.hyp import hypothesis_dir
from kanso.research.lanes import write_atomic
from kanso.research.records import cards_of
from kanso.schemas import RESULTS_HEADER

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["RESULTS_FILE", "results_file", "results_tsv", "write_results"]

RESULTS_FILE: Final = "results.tsv"
"""The rendered log beside the hypothesis, gitignored by the workspace template."""


def results_tsv(store: StateStore, hyp_id: str) -> str:
    """The whole log of a hypothesis: a header and one row per card, oldest first."""
    rows = [card.row() for card in cards_of(store, hyp_id)]
    return "\n".join([RESULTS_HEADER, *rows]) + "\n"


def results_file(ws: Workspace, hyp_id: str) -> Path:
    """Where the rendered log is written: `hypotheses/<id>/results.tsv`."""
    return hypothesis_dir(ws, hyp_id) / RESULTS_FILE


def write_results(ws: Workspace, store: StateStore, hyp_id: str) -> Path:
    """Render the log and replace the workspace copy atomically."""
    path = results_file(ws, hyp_id)
    return write_atomic(path, results_tsv(store, hyp_id).encode("utf-8"))
