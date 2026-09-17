"""Exploration: a new hypothesis, written for a hypothesis whose research stopped learning.

A run climbs from one strategy by one change at a time, and a hypothesis that stalls again
and again on the same best has found the top of the hill its thesis stands on. Another
change to `strategy.py` will not leave it; another thesis might. `explore` asks the best
model on the register for one — a new hypothesis directory, whole — from what the parent's
research learned, and leaves it where the operator decides what happens to it.

**What it is shown is the research, never the certification.** The parent's pinned
`hypothesis.yaml` and `program.md` are the stable half, because they do not move for the
parent while its pins hold. The moving half is its best `strategy.py`, the coverage of
every card under its newest pins by tag (`research/driver.py`'s own table), its keeps with
their research-window scores, its stalls, and its certificates as a verdict and the ids
of the gates that failed — the ids alone, for the reason `research/driver.py` gives for
the failing gates a proposal is shown: evidence measured on a certification window, handed
to something whose output is researched next, is a route from the embargoed window into
research.

**The answer is judged on the ladder, like a proposal.** An id that is not one, or that is
registered or already has a directory; a `hypothesis.yaml` that does not parse as a
hypothesis, declares another id, or carries a classification; a strategy the static
alignment checks refuse against that hypothesis, or whose bytes this workspace already
stores; and windows that would research past the end of the parent's research window or
certify before the start of the parent's certification window. That last refusal is the
embargo, stated as code: the parent's scores were measured on its research window, and an
idea written from them certified there would be certified on the data that chose it.
Every one of these is a complaint the model is asked again with, and a ladder that runs
out fails the step.

**It writes a draft, and registers nothing.** The three files land in
`hypotheses/<id>/` through a temporary file and a rename each, into a directory that did
not exist — never over one that did, and never into the parent's, which a run has pinned.
Nothing registers a directory by its presence: `kanso hyp add` is still the only way a
hypothesis enters the registry, and the construct, the objective and the constraints are
still `kanso classify`'s to decide. The candidate's strategy is stored as a blob, so a
second exploration that writes the same bytes is refused as already stored. What is left
is an `explored` event under the parent and an `explored` escalation under the new id,
offering `hyp validate` and `hyp add`.

**The trigger is the operator's, or a count the operator set.** `kanso hyp explore ID`
runs it by hand. `[research] explore_after_stalls` — zero, never, in the template — runs
it in a daemon lane after a run that stalled, once the parent's newest stalls, counted
since its last exploration, all ended on the same best and number at least that many. An
attempt, failed or not, starts the count over, so a provider that is down costs one call
per spell rather than one per stall. A lane's exploration that fails is an
`explored_failed` event and never the lane's failure: the parent was already requeued by
its stall, and nothing about a bad answer for a new idea is a reason to hold the old one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, cast

from kanso.classify.construct import PORTFOLIO
from kanso.errors import KansoError, PreconditionError, ValidationError
from kanso.hyp import (
    HYPOTHESES,
    HYPOTHESIS_FILE,
    PROGRAM_FILE,
    STRATEGY_FILE,
    check_id,
    hypothesis_dir,
    show,
)
from kanso.inbox import escalate
from kanso.models import CallInputs, route
from kanso.research import align, driver, lanes, records
from kanso.research.scheduler import STALLED
from kanso.schemas import Hypothesis, RunRecord, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "CERTIFICATE_LINES",
    "EXPLORED",
    "EXPLORED_FAILED",
    "KEEP_LINES",
    "STALL_LINES",
    "TASK",
    "Explored",
    "after_stall",
    "due",
    "explore",
]

TASK: Final = "explore"
"""The task class this step calls; the router owns its tier, effort and output cap."""

EXPLORED: Final = "explored"
"""The event under the parent and the escalation kind under the new id."""

EXPLORED_FAILED: Final = "explored_failed"
"""The event a lane's exploration that could not finish leaves under the parent."""

KEEP_LINES: Final = 20
"""How many of the parent's keeps the explorer is shown, highest score first."""

STALL_LINES: Final = 5
"""How many of the parent's stalls the explorer is shown, newest first."""

CERTIFICATE_LINES: Final = 5
"""How many of the parent's certificates the explorer is shown, newest first."""

_FILES: Final = ("hypothesis_yaml", "program_md", "strategy_py")
_NAMES: Final = (HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE)
"""The three answer fields and the files they are written to, in the same order."""

_CLASSIFICATION: Final = ("construct", "objective", "constraints")


@dataclass(frozen=True)
class Explored:
    """One candidate written: where it went, what it is and what was escalated."""

    parent: str
    hyp_id: str
    directory: Path
    strategy_sha: str
    tags: tuple[str, ...]
    rationale: str
    escalation_id: str

    def payload(self) -> dict[str, object]:
        """The candidate as one JSON object."""
        return {
            "parent": self.parent,
            "id": self.hyp_id,
            "dir": str(self.directory),
            "files": sorted(_NAMES),
            "strategy_sha": self.strategy_sha,
            "tags": list(self.tags),
            "rationale": self.rationale,
            "escalation": self.escalation_id,
        }


def explore(
    ws: Workspace, store: StateStore, hyp_id: str, *, lane: str = lanes.DEFAULT_LANE
) -> Explored:
    """Write one new hypothesis from what the research of `hyp_id` learned.

    Refuses a hypothesis that is not registered, or that has never been researched: with
    no run there are no pins to read the research under and nothing learned to explore
    from. `lane` is the lane the call's spend is attributed to.
    """
    show(ws, store, hyp_id)
    runs = records.runs_of(store, hyp_id)
    if not runs:
        raise PreconditionError(
            f"{hyp_id} has never been researched, so there is nothing its research learned "
            "to explore from",
            remedy=f"run `kanso research run {hyp_id}`",
        )
    newest = runs[-1]
    parent = parse_yaml(
        Hypothesis, store.get_blob(newest.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    accepted: list[dict[str, Any]] = []

    def check(data: Mapping[str, object]) -> Sequence[str]:
        complaints = _judge(ws, store, parent, data)
        if not complaints:
            accepted.append(dict(data))
        return complaints

    inputs = CallInputs(
        subject=hyp_id,
        stable={
            HYPOTHESIS_FILE: _text(store, newest.hypothesis_sha),
            PROGRAM_FILE: _text(store, newest.program_sha),
        },
        dynamic=_dynamic(ws, store, newest),
        check=check,
    )
    route(ws, store, TASK, inputs, lane=lane)
    return _write(ws, store, hyp_id, accepted[-1], lane)


def due(store: StateStore, hyp_id: str, after: int) -> bool:
    """Whether the parent's newest stalls call for an exploration.

    Counted newest first over its `stalled` events, stopping at the first one on another
    best and at the last exploration, whether it wrote a candidate or failed. `after` of
    zero is never.
    """
    if after <= 0:
        return False
    rows = store.connection.execute(
        "SELECT kind, detail FROM events WHERE subject = ? AND kind IN (?, ?, ?)"
        " ORDER BY event_id DESC LIMIT ?",
        (hyp_id, STALLED, EXPLORED, EXPLORED_FAILED, after),
    ).fetchall()
    spell: list[object] = []
    for row in rows:
        if str(row["kind"]) != STALLED:
            break
        best = json.loads(str(row["detail"])).get("best_sha")
        if spell and best != spell[0]:
            break
        spell.append(best)
    return len(spell) >= after


def after_stall(ws: Workspace, store: StateStore, hyp_id: str, lane: str) -> Explored | None:
    """Explore from a parent whose run just stalled, when its stalls say so.

    What a daemon lane calls once a run it drove has stalled. A failure is recorded as an
    `explored_failed` event carrying the message and the remedy, and returns `None` like a
    spell that did not call for one: the lane carries on with its next claim.
    """
    if not due(store, hyp_id, ws.config.research.explore_after_stalls):
        return None
    try:
        return explore(ws, store, hyp_id, lane=lane)
    except KansoError as exc:
        store.event(
            EXPLORED_FAILED,
            hyp_id,
            {"lane": lane, "error": exc.message, "because": exc.remedy},
        )
        return None


# --- what the explorer is shown ----------------------------------------------


def _dynamic(ws: Workspace, store: StateStore, newest: RunRecord) -> dict[str, object]:
    """The parent's research as it stands: its best, coverage, keeps, stalls, verdicts."""
    best, _ = records.best_of(store, newest.hyp_id)
    return {
        STRATEGY_FILE: _text(store, best or newest.base_sha),
        "coverage": driver.coverage(store, newest),
        "keeps": _keeps(store, newest),
        "stalls": _stalls(store, newest.hyp_id),
        "certificates": _verdicts(store, newest.hyp_id),
        "taken_ids": _taken(ws, store),
    }


def _keeps(store: StateStore, newest: RunRecord) -> list[dict[str, object]]:
    """The parent's keeps under its newest pins, highest research-window score first."""
    rows = store.connection.execute(
        "SELECT cards.strategy_sha, cards.metric, cards.metric_se, cards.description,"
        " cards.tags FROM cards JOIN runs ON runs.run_id = cards.run_id"
        " WHERE cards.hyp_id = ? AND runs.hypothesis_sha = ? AND runs.snapshot_id = ?"
        " AND runs.criteria_version = ? AND cards.status = 'keep'"
        " ORDER BY cards.metric DESC, cards.card_id DESC LIMIT ?",
        (
            newest.hyp_id,
            newest.hypothesis_sha,
            newest.snapshot_id,
            newest.criteria_version,
            KEEP_LINES,
        ),
    ).fetchall()
    return [
        {
            "sha7": str(row["strategy_sha"])[:7],
            "metric": float(row["metric"]),
            "metric_se": float(row["metric_se"] or 0.0),
            "desc": str(row["description"]),
            "tags": json.loads(str(row["tags"])),
        }
        for row in rows
    ]


def _stalls(store: StateStore, hyp_id: str) -> dict[str, object]:
    """How often the parent stalled, and the newest few: on which best, and the verdict."""
    total = store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE kind = ? AND subject = ?", (STALLED, hyp_id)
    ).fetchone()
    rows = store.connection.execute(
        "SELECT detail FROM events WHERE kind = ? AND subject = ? ORDER BY event_id DESC LIMIT ?",
        (STALLED, hyp_id, STALL_LINES),
    ).fetchall()
    newest: list[dict[str, object]] = []
    for row in rows:
        detail = json.loads(str(row["detail"]))
        best = detail.get("best_sha")
        newest.append(
            {
                "best_sha7": None if best is None else str(best)[:7],
                "verdict": detail.get("verdict"),
            }
        )
    return {"count": int(total[0]), "newest": newest}


def _verdicts(store: StateStore, hyp_id: str) -> list[dict[str, object]]:
    """The parent's newest certificates: the subject, the verdict and the failing gate ids.

    Never a gate's evidence, and never a gate that passed: an id names what did not hold
    and measures nothing.
    """
    rows = store.connection.execute(
        "SELECT strategy_sha, verdict, gates FROM certificates WHERE hyp_id = ?"
        " ORDER BY created_at DESC LIMIT ?",
        (hyp_id, CERTIFICATE_LINES),
    ).fetchall()
    return [
        {
            "sha7": str(row["strategy_sha"])[:7],
            "verdict": str(row["verdict"]),
            "failing_gates": [
                str(gate["id"])
                for gate in cast(list[dict[str, Any]], json.loads(str(row["gates"])))
                if not gate.get("pass", True)
            ],
        }
        for row in rows
    ]


def _taken(ws: Workspace, store: StateStore) -> list[str]:
    """Every id a candidate may not take: registered, or with a directory already."""
    registered = {str(row[0]) for row in store.connection.execute("SELECT hyp_id FROM hypotheses")}
    root = ws.path(HYPOTHESES)
    present = {path.name for path in root.iterdir() if path.is_dir()} if root.is_dir() else set()
    return sorted(registered | present | {PORTFOLIO})


# --- what an answer must be --------------------------------------------------


def _judge(
    ws: Workspace, store: StateStore, parent: Hypothesis, data: Mapping[str, object]
) -> list[str]:
    """Everything wrong with one candidate, or nothing when it may be written.

    Every check that can still be made is made, so one retry can correct them all: an id
    that is wrong does not hide a strategy already stored, and only the checks that read
    the parsed hypothesis wait on a file that parses.
    """
    candidate_id = str(data["id"])
    strategy = str(data["strategy_py"]).encode("utf-8")
    complaints: list[str] = []
    try:
        check_id(candidate_id)
    except ValidationError as exc:
        complaints.append(f"{exc.message}; {exc.remedy}")
    else:
        if candidate_id in _taken(ws, store):
            complaints.append(
                f"id: {candidate_id!r} is taken — registered, reserved, or already a directory "
                f"under {HYPOTHESES}/ — so choose one that is not in `taken_ids`"
            )
    sha = sha256(strategy).hexdigest()
    if store.has_blob(sha):
        complaints.append(
            f"strategy_py: these bytes are already stored in this workspace ({sha[:7]}), so "
            "they are not a new idea; write the strategy the new hypothesis tests"
        )
    try:
        hyp = parse_yaml(Hypothesis, str(data["hypothesis_yaml"]), "hypothesis_yaml")
    except ValidationError as exc:
        return [*complaints, exc.message]
    if hyp.id != candidate_id:
        complaints.append(
            f"hypothesis_yaml: declares id {hyp.id!r}, and the answer's id is {candidate_id!r}"
        )
    stated = (hyp.construct, hyp.objective, hyp.constraints)
    written = [key for key, value in zip(_CLASSIFICATION, stated, strict=True) if value]
    if written:
        complaints.append(
            f"hypothesis_yaml: carries {', '.join(written)}; a candidate is written as a draft "
            "and classified by `kanso classify`, so leave all three out"
        )
    complaints.extend(_embargo(parent, hyp))
    complaints.extend(f"strategy_py: {problem}" for problem in align.problems(hyp, strategy))
    return complaints


def _embargo(parent: Hypothesis, hyp: Hypothesis) -> list[str]:
    """The parent's results may not choose data the candidate is certified on.

    The explorer is shown scores measured on the parent's research window, so a candidate
    that researched later than that window ends, or certified earlier than the parent's
    certification window starts, would put what the parent's research saw on the far side
    of the candidate's own embargo.
    """
    complaints: list[str] = []
    ours, theirs = hyp.windows, parent.windows
    if ours.research.end > theirs.research.end:
        complaints.append(
            f"windows.research.end: {ours.research.end} is after the parent's research window "
            f"ends ({theirs.research.end}); the candidate researches no later than its parent"
        )
    if ours.certification.start < theirs.certification.start:
        complaints.append(
            f"windows.certification.start: {ours.certification.start} is before the parent's "
            f"certification window starts ({theirs.certification.start}); the parent's "
            "research scores were measured there, and a candidate is not certified on them"
        )
    return complaints


# --- what is written ---------------------------------------------------------


def _write(
    ws: Workspace, store: StateStore, parent: str, answer: Mapping[str, Any], lane: str
) -> Explored:
    """The accepted candidate on disk, its strategy stored, and the operator told.

    The directory is created here and must not exist: a directory that appeared between the
    judgement and the write is someone else's, and is refused rather than written into.
    """
    candidate_id = str(answer["id"])
    directory = hypothesis_dir(ws, candidate_id)
    try:
        directory.mkdir(parents=True)
    except FileExistsError:
        raise PreconditionError(
            f"{directory} appeared while the candidate was being judged, and a candidate is "
            "never written into a directory that exists",
            remedy=f"run `kanso hyp explore {parent}` again",
        ) from None
    for field, name in zip(_FILES, _NAMES, strict=True):
        lanes.write_atomic(directory / name, str(answer[field]).encode("utf-8"))
    sha = store.put_blob(str(answer["strategy_py"]).encode("utf-8"))
    tags = tuple(str(tag) for tag in answer["tags"])
    rationale = str(answer["rationale"])
    store.event(
        EXPLORED,
        parent,
        {
            "parent": parent,
            "id": candidate_id,
            "strategy_sha": sha,
            "tags": list(tags),
            "lane": lane,
        },
    )
    entry = escalate(ws, store, EXPLORED, candidate_id, f"from {parent}: {rationale}")
    return Explored(
        parent=parent,
        hyp_id=candidate_id,
        directory=directory,
        strategy_sha=sha,
        tags=tags,
        rationale=rationale,
        escalation_id=entry.escalation_id,
    )


def _text(store: StateStore, sha: str) -> str:
    return store.get_blob(sha).decode("utf-8", errors="replace")
