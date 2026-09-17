"""The queue's record of where a hypothesis is between the queue and a run.

A lane that takes a hypothesis out of the queue holds it, with no row to show for it,
until its baseline has run and the run row exists — and again between a stall, which ends
the run, and the scheduler's decision on where the hypothesis goes. Four events mark the
passages across those gaps: `queued` and `claimed`, which the scheduler appends, `removed`,
which the operator's `queue remove` appends, and `run_begun`, which `loop.begin` appends
once the run row exists. The last of them is the answer to "who has it": a claim with no
run behind it is a lane's, a removal naming a lane is the operator's word against that
lane, and a run beginning consumes the claim.

This module holds the constants and the read, and nothing else, because two modules need
them that must not meet: the scheduler, which reaches certification and through it
replay, and `loop.begin`, which is the card runner and may reach neither
(`tests/replay/test_run.py` walks the import graph to make sure).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore

__all__ = [
    "BEGUN",
    "CLAIMED",
    "QUEUED",
    "REMOVED",
    "RESEED_FROM",
    "Passage",
    "last_passage",
    "reseed_of",
    "taken",
]

QUEUED: Final = "queued"
CLAIMED: Final = "claimed"
REMOVED: Final = "removed"
BEGUN: Final = "run_begun"
"""The four events that move a hypothesis between the queue and a run."""

PASSAGES: Final = (QUEUED, CLAIMED, REMOVED, BEGUN)

RESEED_FROM: Final = "reseed_from"
"""The key a `queued` passage carries when the scheduler decided the next run starts from
another keep than the best: the sha of the blob to start from. It rides on the passage
rather than on the stall's own event because the passage is what survives `put_back`
and `recover` — a decision written only where the stall was decided does not."""

Passage = tuple[str, dict[str, object]]
"""One passage as read back: its kind and its detail."""


def last_passage(store: StateStore, hyp_id: str) -> Passage | None:
    """The most recent passage recorded for `hyp_id`, or `None` if it never had one."""
    marks = ", ".join("?" for _ in PASSAGES)
    row = store.connection.execute(
        f"SELECT kind, detail FROM events WHERE subject = ? AND kind IN ({marks}) "
        "ORDER BY event_id DESC LIMIT 1",
        (hyp_id, *PASSAGES),
    ).fetchone()
    if row is None:
        return None
    detail = json.loads(str(row["detail"]))
    return str(row["kind"]), dict(detail) if isinstance(detail, dict) else {}


def reseed_of(store: StateStore, hyp_id: str) -> str | None:
    """The sha the next run of `hyp_id` was told to start from, or `None`.

    Read from the newest `queued` passage, and only while no run has begun since: a run
    beginning consumes the decision, so a reseed decided before the last run cannot leak
    into the one after it.
    """
    row = store.connection.execute(
        "SELECT kind, detail FROM events WHERE subject = ? AND kind IN (?, ?)"
        " ORDER BY event_id DESC LIMIT 1",
        (hyp_id, QUEUED, BEGUN),
    ).fetchone()
    if row is None or str(row["kind"]) != QUEUED:
        return None
    detail = json.loads(str(row["detail"]))
    found = detail.get(RESEED_FROM) if isinstance(detail, dict) else None
    return found if isinstance(found, str) and found else None


def taken(store: StateStore, hyp_id: str, lane: str) -> bool:
    """Whether the operator took `hyp_id` out of `lane`'s hands.

    True when the last passage is a removal naming that lane: the lane may not begin the
    run it was about to, and its failure does not bring the hypothesis back.
    """
    passage = last_passage(store, hyp_id)
    return passage is not None and passage[0] == REMOVED and passage[1].get("lane") == lane
