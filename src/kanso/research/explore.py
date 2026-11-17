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
research. A dated spread's rows are withheld from the `hypothesis.yaml` it is shown, as they
are from a proposer (`kanso.research.driver.shown`): each is a year's median width, in force
from the year's first day, and the model writes a `strategy.py`. A candidate that keeps the line
standing for them is written with the parent's rows put back (`driver.restored`), those of the
names its own universe lists, in force from on or before its own research window's last day: one
that trades fewer names than its parent, or researches less, could not drop a row it was never
shown, and a row of a name outside its universe or from after its research is refused.

**The answer is judged on the ladder, like a proposal.** An id that is not one, or that is
registered or already has a directory; a `hypothesis.yaml` that does not parse as a
hypothesis, declares another id, or carries a classification or a `prior_trials`, since the
grid that chose the parent's seed is counted through `derived_from`; a strategy the static
alignment checks refuse against that hypothesis, or whose bytes this workspace already
stores; and windows that would research past the end of the parent's research window,
certify before the start of the parent's certification window, or certify inside the
candidate's own embargo counted from the last day the parent researched. That last
refusal is the embargo, stated as code. The idea was chosen by scores measured on the
parent's research, so the last day it saw is the parent's, not the one the candidate
declares, and `Windows.check_embargo`'s rule — no certification before `max(5 x horizon,
1d)` after the last researched day — is applied from there, at the candidate's horizon.
The parent's research end is the latest over every pin any of its runs held, because its
best and its stalls outlive a re-pin that moves the windows.
Every one of these is a complaint the model is asked again with, and a ladder that runs
out fails the step.

**A draft states its parent's mechanism.** The candidate's `hypothesis.yaml` states the
`mechanism` of the parent's newest pin, or one `[research] explore_mechanisms` lists: the
key adds mechanisms a draft may move to and never removes the parent's, and `[]`, its
default, is the parent's alone. An explored draft tests another idea inside the mechanism
its parent's research measured; another mechanism is the operator's to write with `kanso
hyp new`, and any other is a complaint on the ladder like the rest. The explorer is shown
the admissible set as `mechanisms` in the moving half, because `kanso.toml` can change
between two calls of one subject. The rule reads the field the draft declares and nothing
reads the code for a mechanism, so it binds what a draft states and never proves what it
tests: the thesis is still the operator's to read.

**A draft is measured once, as its parent's card.** An answer that passes every static
check has its `strategy.py` run once as one more card of the parent's newest run
(`loop.probe`): under that run's pinned `hypothesis.yaml`, snapshot and host version, its card
budget and memory cap, over the parent's research window and no other, which
`backtest.run_subprocess` enforces — the embargo is code here as on a card — and scored by the
installed kanso, as a card of a run resumed across an upgrade is. So the books it is compared
with are the parent's under those pins and the probe's reading, and where there are none — a
`kanso.toml` edit that moved the reading, or an upgrade whose superseded books `kanso state
prune` deleted — the seed says so (`books`) rather than reading no match as a pass. The
static checks come first, as a card's do: the seed passes the integrity
scan every card of the parent passes before it runs, and the candidate keeps or narrows the
parent's `universe` and `data_requirements` and keeps its `resolution` and `bar_resolution` —
each departure one complaint naming the field — because a seed measured under the parent's
file can trade only what that file loads; with those held, the candidate's own alignment check
implies the parent's, so a seed line is refused once. Only where the candidate's file does not
parse are the seed's lines read against the parent's. A seed that holds a book the parent
already stored on `[research] redundant_pct` of their shared sessions *and* scores within the
parent's noise floor of it is that card again, and a wrong answer that names it; one that
places no order holds the empty book every flat card holds, at a flat run's zero, so it is
refused wherever the parent stored one, by a complaint of its own. A book matched whose number
moved is a measurement, and is admitted with the match on record. A seed that raised, was
killed for its clock or its memory, or whose order the parent's sizing refused, is a wrong
answer too: a draft whose seed cannot run as its parent's card would be a baseline that
refuses its own run. A fault the workspace owns — a crash carrying kanso's remedy, a window
the catalog cannot give, pins that can no longer be set up, a host or hold that does not run —
fails the attempt with that remedy, since asking the model again cannot mend it, and the pins
are set up before any model is asked. Nothing is recorded as a card: the probe writes no card,
signature or trial and moves no best, stall or `n_trials`, and it runs in
`runs/<lane>/.explore-<parent>/`, which it removes — never the parent's lane directory, whose
card room the child empties. Its cost is one card budget per answer that passed the static
checks — two on the top tier's ladder, where `explore` is routed, three on a lower one — beside
the host-alone or hold run computed once per exploration where the parent's objective reads one.

**It writes a draft, and registers nothing.** The three files land in
`hypotheses/<id>/` through a temporary file and a rename each, into a directory that did
not exist — never over one that did, and never into the parent's, which a run has pinned.
Nothing registers a directory by its presence: `kanso hyp add` is still the only way a
hypothesis enters the registry, and the construct, the objective and the constraints are
still `kanso classify`'s to decide. The candidate's strategy is stored as a blob, so a
second exploration that writes the same bytes is refused as already stored, and so is what
its seed was measured from. What is left is an `explored` event under the parent, carrying
the seed's score (`Seed`), the parent's best measured under the same pins and reading, the
trials the draft inherits and the whole rationale, and an `explored` escalation under the new
id whose summary leads with that score, offering `hyp validate` and `hyp add`.

**The trigger is the operator's, or a count in `kanso.toml`.** `kanso hyp explore ID`
runs it by hand. `[research] explore_after_stalls` — five in the template, zero never — runs
it in a daemon lane after a run that stalled, once the parent's newest stalls, counted
since its last exploration, all ended on the same best and number at least that many.
Either trigger's attempt, failed or not, starts the count over — a candidate written is an
`explored` event under the parent, a failure an `explored_failed` one — so a provider that
is down costs one call per spell rather than one per stall. A hypothesis not registered or
never researched is refused before any attempt and leaves nothing. A lane's exploration
that fails is never the lane's failure: the parent was already requeued by its stall, and
nothing about a bad answer for a new idea is a reason to hold the old one. A stop or a drain
is neither a candidate nor a failure: it is the operator's word, as `loop.begin` treats a
drain. A lane passes its stop request, and an exploration that meets it — before a probe
starts, before the model is asked again, or as the error a stop or a drain raises out of a
probe's child — writes no event, so the spell is not spent and the parent's next stall after
the daemon starts again explores.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Final, cast

from kanso.classify.construct import PORTFOLIO
from kanso.criteria.integrity import scan
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
from kanso.nautilus import backtest
from kanso.research import align, driver, lanes, loop, records
from kanso.research.scheduler import STALLED
from kanso.schemas import Hypothesis, RunRecord, embargo_days, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from pathlib import Path

    from kanso.criteria import CardRun
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
    "Seed",
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

PROBE_PREFIX: Final = ".explore-"
"""What the directory a seed is measured in is named, under the lane's root, before the
parent's id: a dot-name, so no hypothesis's lane directory is ever it and `kanso doctor`'s lane
listing passes it over."""


@dataclass(frozen=True)
class Seed:
    """What a candidate's `strategy.py` scored as one more card of its parent, and what that
    number is read beside.

    `metric` and `se` are the parent's objective `objective` on the seed's run, over `window`,
    the parent's research window, on `snapshot`, the newest run's (`run_tag`), and `trades` the
    trades it closed; `measured_sha` names the blob it was scored from. `books` is how many of
    the parent's books under those pins and the probe's reading it was compared with, zero
    when none was stored there, and `same_book` a book it matched without repeating — its
    number moved past the floor. `parent_best` is the parent's best measured under the same
    pins and reading, from its own stored book, or `None` where it was not, so two numbers
    struck two ways are never set side by side. `inherited_trials` is the count the draft's
    search starts from through `derived_from`: every card of the parent's family.
    """

    metric: float
    se: float
    trades: int
    objective: str
    window: tuple[date, date]
    snapshot: str
    run_tag: str
    measured_sha: str
    books: int
    same_book: records.Match | None
    parent_best: float | None
    inherited_trials: int

    def payload(self) -> dict[str, object]:
        """The seed as one JSON object: the `explored` event's and `hyp explore --json`'s."""
        like = self.same_book
        return {
            "metric": self.metric,
            "se": self.se,
            "trades": self.trades,
            "objective": self.objective,
            "window": [self.window[0].isoformat(), self.window[1].isoformat()],
            "snapshot": self.snapshot,
            "run": self.run_tag,
            "measured_sha": self.measured_sha,
            "books": self.books,
            "same_book": None
            if like is None
            else {"like": like.like[:7], "pct": round(like.pct, 2), "metric": like.earned},
            "parent_best": self.parent_best,
            "inherited_trials": self.inherited_trials,
        }

    def said(self) -> str:
        """The head of the `explored` escalation's summary, which is what `kanso inbox` shows:
        the score as the parent's card, the parent's best beside it where it was measured the
        same way, the trials the draft inherits, and that nothing was compared where nothing
        was stored."""
        best = "" if self.parent_best is None else f" (best {self.parent_best:.4g})"
        none = "; no book of it stored to compare" if self.books == 0 else ""
        return (
            f"as its card the seed scored {self.metric:.4g} ± {self.se:.2g} on "
            f"{self.trades} trades{best}, {self.inherited_trials} trials inherited{none}"
        )


@dataclass(frozen=True)
class Explored:
    """One candidate written: where it went, what it is, what its seed scored and what was
    escalated."""

    parent: str
    hyp_id: str
    directory: Path
    strategy_sha: str
    tags: tuple[str, ...]
    rationale: str
    escalation_id: str
    seed: Seed

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
            "seed": self.seed.payload(),
        }


def _never() -> bool:
    """A stop request nothing makes: what an exploration by hand is given."""
    return False


def explore(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    lane: str = lanes.DEFAULT_LANE,
    stop: Callable[[], bool] = _never,
) -> Explored:
    """Write one new hypothesis from what the research of `hyp_id` learned.

    Refuses a hypothesis that is not registered, or that has never been researched: with
    no run there are no pins to read the research under and nothing learned to explore
    from. Past those two refusals it is an attempt, and an attempt that fails — pins that
    cannot be set up, no model to ask, a ladder that ran out, a seed the workspace could not
    measure, a directory that appeared — leaves an `explored_failed` event under the parent,
    carrying `lane`, before the error is raised, whether the operator ran it or a lane did.
    `lane` is the lane the call's spend is attributed to and whose root the seed is measured
    under.

    `stop` is a lane's stop request, asked before a seed is measured and before the model is
    asked again. A drain refusing a child (`backtest.DrainedError`), and any error raised
    while `stop` holds — the interrupt a `research stop` raises out of a probe's child among
    them — is the operator's word and not the attempt's failure: it is raised with no event,
    so the parent's spell of stalls is not spent.
    """
    show(ws, store, hyp_id)
    runs = records.runs_of(store, hyp_id)
    if not runs:
        raise PreconditionError(
            f"{hyp_id} has never been researched, so there is nothing its research learned "
            "to explore from",
            remedy=f"run `kanso research run {hyp_id}`",
        )
    try:
        return _attempt(ws, store, runs, lane, stop)
    except KansoError as exc:
        if isinstance(exc, backtest.DrainedError) or stop():
            raise
        store.event(
            EXPLORED_FAILED,
            hyp_id,
            {"lane": lane, "error": exc.message, "because": exc.remedy},
        )
        raise


def _attempt(
    ws: Workspace,
    store: StateStore,
    runs: list[RunRecord],
    lane: str,
    stop: Callable[[], bool],
) -> Explored:
    """Set the parent's newest pins up, ask for a candidate from its research, judge it on
    the ladder — statically, then once as the parent's card — and write it.

    The pins are set up before the model is asked, so a parent whose pins no longer can be
    pays for no answer. The host-alone or hold run a probe reads is computed once for every
    answer of the ladder, and the directory the seeds are measured in is removed however the
    attempt ends.
    """
    newest = runs[-1]
    hyp_id = newest.hyp_id
    parent = _pinned(store, newest.hypothesis_sha)
    seen = max(_pinned(store, sha).windows.research.end for sha in {r.hypothesis_sha for r in runs})
    pinned = _text(store, newest.hypothesis_sha)
    setup = loop.setup_of(ws, store, newest)
    cache: dict[str, CardRun] = {}
    accepted: list[tuple[dict[str, Any], loop.Probe]] = []
    directory = lanes.lane_dir(ws, lane, f"{PROBE_PREFIX}{hyp_id}")

    def check(data: Mapping[str, object]) -> Sequence[str]:
        written = {**data, "hypothesis_yaml": driver.restored(str(data["hypothesis_yaml"]), pinned)}
        complaints = _judge(ws, store, parent, seen, written)
        if not complaints:
            _halt(stop, hyp_id, "ran the seed")
            source = str(written["strategy_py"]).encode("utf-8")
            found = loop.probe(ws, store, newest, setup, source, directory=directory, cache=cache)
            complaints = _measured(store, parent, str(written["id"]), found)
            if not complaints:
                accepted.append((written, found))
                return complaints
        _halt(stop, hyp_id, "asked again")
        return complaints

    inputs = CallInputs(
        subject=hyp_id,
        stable={
            HYPOTHESIS_FILE: driver.shown(pinned),
            PROGRAM_FILE: _text(store, newest.program_sha),
        },
        dynamic=_dynamic(ws, store, newest, parent),
        check=check,
    )
    try:
        lanes.prepare(directory)
        route(ws, store, TASK, inputs, lane=lane)
    finally:
        lanes.remove(directory)
    written, found = accepted[-1]
    return _write(ws, store, newest, setup, written, found, lane)


def _halt(stop: Callable[[], bool], parent: str, before: str) -> None:
    """Refuse to go on once the lane was told to stop or drain: no seed is measured and no
    model asked again after it, because a drain begins nothing more and a frontier call made
    after one waits minutes for an answer the next spawn would refuse."""
    if stop():
        raise PreconditionError(
            f"the exploration of {parent} stopped before it {before}: the lane was told to "
            "stop or drain",
            remedy="start the daemon again; the parent's next stall explores",
        )


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


def after_stall(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    lane: str,
    *,
    stop: Callable[[], bool] = _never,
) -> Explored | None:
    """Explore from a parent whose run just stalled, when its stalls say so.

    What a daemon lane calls once a run it drove has stalled, with its own stop request. A
    failure returns `None` like a spell that did not call for one, and the lane carries on
    with its next claim; the `explored_failed` event is already written. A stop or a drain
    returns `None` too, having written nothing, and the lane's loop sees its stop. A parent
    that stalled is registered and has run, so neither refusal that precedes an attempt can
    reach here.
    """
    if not due(store, hyp_id, ws.config.research.explore_after_stalls):
        return None
    try:
        return explore(ws, store, hyp_id, lane=lane, stop=stop)
    except KansoError:
        return None


# --- what the explorer is shown ----------------------------------------------


def _dynamic(
    ws: Workspace, store: StateStore, newest: RunRecord, parent: Hypothesis
) -> dict[str, object]:
    """The parent's research as it stands: its best, coverage, keeps, stalls, verdicts, and
    the mechanisms a candidate may state."""
    best, _ = records.best_of(store, newest.hyp_id)
    return {
        STRATEGY_FILE: _text(store, best or newest.base_sha),
        "coverage": driver.coverage(store, newest),
        "keeps": _keeps(store, newest),
        "stalls": _stalls(store, newest.hyp_id),
        "certificates": _verdicts(store, newest.hyp_id),
        "taken_ids": _taken(ws, store),
        "mechanisms": sorted(_mechanisms(ws, parent)),
    }


def _mechanisms(ws: Workspace, parent: Hypothesis) -> set[str]:
    """What a candidate may state: the parent's own mechanism and every one the workspace
    lists in `[research] explore_mechanisms`."""
    return {parent.mechanism, *ws.config.research.explore_mechanisms}


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
    ws: Workspace, store: StateStore, parent: Hypothesis, seen: date, data: Mapping[str, object]
) -> list[str]:
    """Everything wrong with one candidate, or nothing when it may be written.

    `parent` is the newest pin of the hypothesis explored from, and `seen` the last day
    any of its pins researched.

    Every check that can still be made is made, so one retry can correct them all: an id
    that is wrong does not hide a strategy already stored, and only the checks that read
    the parsed hypothesis wait on a file that parses. The seed is run as the parent's card
    once these pass, so it is scanned here as every card of the parent is scanned before it
    runs, whether or not the file parses; and where the file does not, its lines are read
    against the parent's file, the one it will run under, since its own cannot be.
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
    complaints.extend(_scanned(parent, str(data["strategy_py"])))
    try:
        hyp = parse_yaml(Hypothesis, str(data["hypothesis_yaml"]), "hypothesis_yaml")
    except ValidationError as exc:
        return [
            *complaints,
            exc.message,
            *(
                f"strategy_py: as a card of {parent.id}, {said}"
                for said in align.problems(parent, strategy)
            ),
        ]
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
    if parent.id not in hyp.derived_from:
        complaints.append(
            f"hypothesis_yaml: derived_from does not name the parent {parent.id!r}; the "
            "candidate was chosen by the parent's research, so it states `derived_from: "
            f"[{parent.id}]` and its trials count the parent's"
        )
    if hyp.prior_trials is not None:
        complaints.append(
            "hypothesis_yaml: carries prior_trials; a candidate's search is its parent's, "
            "counted through `derived_from`, grids and cards alike, so leave it out"
        )
    complaints.extend(_mechanism(ws, parent, hyp))
    complaints.extend(_confined(parent, hyp))
    complaints.extend(_embargo(parent, seen, hyp))
    complaints.extend(f"strategy_py: {problem}" for problem in align.problems(hyp, strategy))
    return complaints


def _scanned(parent: Hypothesis, source: str) -> list[str]:
    """What the integrity scan refuses in the seed under the parent's construct, sizing and
    depth — the arguments every card of the parent is scanned with. A source that does not
    parse is the alignment check's to report, once."""
    try:
        ast.parse(source)
    except SyntaxError:
        return []
    construct = None if parent.construct is None else parent.construct.id
    found = scan(
        source,
        sized=parent.sizing is not None,
        construct=construct,
        depth=parent.depth is not None,
    )
    return [
        f"strategy_py: as a card of {parent.id}, {problem}; the seed runs as a card of "
        f"{parent.id} before it is written, so it passes the scan every card passes before it "
        "runs"
        for problem in found
    ]


_CONFINED: Final = (
    "the seed is measured once as a card of its parent before it is written, so a draft keeps "
    "or narrows its universe and data_requirements and keeps its resolution and "
    "bar_resolution; one that needs more is the operator's to write with `kanso hyp new`"
)


def _confined(parent: Hypothesis, hyp: Hypothesis) -> list[str]:
    """A candidate whose file asks for what the parent's file does not load: one complaint
    per field.

    The seed is measured under the parent's file, so it can trade only the names, read only
    the types and bars, that file loads. With these four held, the candidate's own alignment
    check implies the parent's — every name it holds the parent holds, its bar size is the
    parent's, every type it requires the parent requires — so a seed line is refused once.
    """
    complaints: list[str] = []
    names = [name for name in hyp.universe if name not in parent.universe]
    if names:
        complaints.append(
            f"hypothesis_yaml: universe lists {', '.join(map(repr, names))}, which "
            f"{parent.id}'s does not hold ({', '.join(parent.universe)}); {_CONFINED}"
        )
    kinds = [kind for kind in hyp.data_requirements if kind not in parent.data_requirements]
    if kinds:
        complaints.append(
            f"hypothesis_yaml: data_requirements lists {', '.join(map(repr, kinds))}, which "
            f"{parent.id}'s does not require ({', '.join(parent.data_requirements)}); "
            f"{_CONFINED}"
        )
    for field in ("resolution", "bar_resolution"):
        ours, theirs = getattr(hyp, field), getattr(parent, field)
        if ours != theirs:
            complaints.append(
                f"hypothesis_yaml: {field} is {ours!r}, and {parent.id}'s is {theirs!r}; "
                f"{_CONFINED}"
            )
    return complaints


def _measured(
    store: StateStore, parent: Hypothesis, candidate: str, found: loop.Probe
) -> list[str]:
    """What the seed's run as the parent's card says is wrong with it, or nothing.

    A fault the model can fix is a complaint: a sizing refusal, a kill for the clock or the
    memory — read off the `resources` verdict a card would carry — a seed that raised, a
    repeat of a card the parent already ran by book and score, and a seed that placed no
    order where the parent stored an empty book. A crash that carries kanso's remedy is the
    workspace's to fix, so it fails the attempt with that remedy rather than asking again.
    """
    result = found.result
    start, end = found.request.window
    card = f"strategy_py: run as a card of {parent.id}"
    refused = result.refused
    if refused is not None:
        return [
            f"{card} over {start}..{end}, the seed's order was refused by sizing rule "
            f"{refused.rule} at {refused.instrument_id}: {refused.why}; write a seed whose "
            "orders the parent's sizing admits"
        ]
    if found.killed:
        killed = found.killed[0].evidence
        if killed["killed"] == backtest.MEMORY:
            return [
                f"{card}, the seed was killed at {killed['peak_gb']:.2f} GB, over the "
                f"{killed['cap_gb']:.2f} GB a card of {parent.id} may hold; write a seed no "
                "heavier than the parent's cards"
            ]
        return [
            f"{card}, the seed was killed after {killed['wall_s']:.0f} s, over the "
            f"{killed['budget_s']:.0f} s a card of {parent.id} may take; write a seed no "
            "slower than the parent's cards"
        ]
    if result.crashed:
        tail = _tail(result.traceback_tail or result.reason or "no output")
        if result.remedy is not None:
            raise PreconditionError(
                f"the seed of {candidate} could not be measured as a card of {parent.id}: "
                f"{result.reason}: {tail}",
                remedy=result.remedy,
            )
        return [
            f"{card}, the seed raised:\n{tail}\nfix the fault; a seed that does not run has "
            "nothing to research"
        ]
    like = found.like
    if like is None or not like.agreed:
        return []
    named = _named(store, parent.id, like.like)
    if not result.run.fills:
        return [
            f"{card} over {start}..{end}, the seed placed no order, and the parent's card "
            f"{named} already measured an empty book; write a seed that trades the parent's "
            "universe at its resolution on data it requires"
        ]
    return [
        f"{card}, the seed held the same book as the parent's card {named} on {like.matched} "
        f"of {like.shared} sessions ({like.pct:.0f}%) and scored {found.metric:.4g} against "
        f"its {like.earned:.4g}, inside the {found.floor:.2g} that separates two results "
        f"there: it repeats the parent's card {like.like[:7]}; write a seed whose book or "
        "result the parent's cards have not measured"
    ]


def _tail(text: str) -> str:
    """The end of a crash, where the exception is: as much as a proposer is shown."""
    return "\n".join(text.splitlines()[-driver.CRASH_TAIL_LINES :])


def _named(store: StateStore, hyp_id: str, sha: str) -> str:
    """A card of the parent by its sha7 and, when one carries it, its newest description."""
    row = store.connection.execute(
        "SELECT description FROM cards WHERE hyp_id = ? AND strategy_sha = ?"
        " ORDER BY card_id DESC LIMIT 1",
        (hyp_id, sha),
    ).fetchone()
    return sha[:7] if row is None else f'{sha[:7]} ("{row["description"]}")'


def _mechanism(ws: Workspace, parent: Hypothesis, hyp: Hypothesis) -> list[str]:
    """A candidate states its parent's mechanism or one the workspace lists, and no other."""
    if hyp.mechanism in _mechanisms(ws, parent):
        return []
    listed = sorted(_mechanisms(ws, parent) - {parent.mechanism})
    admits = (
        f" nor one [research] explore_mechanisms admits ({', '.join(listed)})" if listed else ""
    )
    others = f" or one of {', '.join(listed)}" if listed else ""
    return [
        f"hypothesis_yaml: mechanism {hyp.mechanism!r} is not {parent.id}'s "
        f"({parent.mechanism!r}){admits}; an explored draft tests another idea inside the "
        f"mechanism its parent's research measured, so state mechanism: {parent.mechanism}{others}"
    ]


def _embargo(parent: Hypothesis, seen: date, hyp: Hypothesis) -> list[str]:
    """The parent's results may not choose data the candidate is certified on.

    The explorer is shown scores measured on the parent's research, so the candidate's
    embargo is counted from `seen`, the last day that research reached, and not from the
    end of the research window the candidate declares — which its own `check_embargo`
    has already measured from. A candidate that researched later than the parent's newest
    research window ends, or certified earlier than its certification window starts, is
    refused as well.
    """
    complaints: list[str] = []
    ours, theirs = hyp.windows, parent.windows
    earliest = seen + timedelta(days=embargo_days(hyp.horizon))
    if ours.certification.start < earliest:
        complaints.append(
            f"windows.certification.start: {ours.certification.start} is inside the embargo "
            f"counted from the end of the parent's research ({seen}) for a {hyp.horizon} "
            f"horizon; certification may not open before {earliest}"
        )
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
    ws: Workspace,
    store: StateStore,
    newest: RunRecord,
    setup: loop.Setup,
    answer: Mapping[str, Any],
    found: loop.Probe,
    lane: str,
) -> Explored:
    """The accepted candidate on disk, its strategy and its seed's measurement stored, and
    the operator told what the seed scored as the parent's card.

    The directory is created here and must not exist: a directory that appeared between the
    judgement and the write is someone else's, and is refused rather than written into.
    Nothing is stored for an answer the ladder refused, so the "already stored" refusal
    keeps meaning bytes a draft was written with.
    """
    parent = newest.hyp_id
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
    seed = _seed(store, newest, setup, found)
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
            "seed": seed.payload(),
            "rationale": rationale,
        },
    )
    entry = escalate(
        ws, store, EXPLORED, candidate_id, f"from {parent}: {seed.said()}: {rationale}"
    )
    return Explored(
        parent=parent,
        hyp_id=candidate_id,
        directory=directory,
        strategy_sha=sha,
        tags=tags,
        rationale=rationale,
        escalation_id=entry.escalation_id,
        seed=seed,
    )


def _seed(store: StateStore, newest: RunRecord, setup: loop.Setup, found: loop.Probe) -> Seed:
    """The accepted seed's score, its measurement stored, beside the parent's best under the
    same pins and reading and the trials the draft inherits."""
    measured = found.measured
    assert measured is not None  # only a seed that was measured is accepted
    best, _ = records.best_of(store, newest.hyp_id)
    return Seed(
        metric=found.metric,
        se=found.se,
        trades=len(found.result.run.trades),
        objective=measured.objective.id,
        window=found.request.window,
        snapshot=found.request.snapshot_id,
        run_tag=newest.tag,
        measured_sha=records.record_measurement(store, measured),
        books=found.books,
        same_book=found.like,
        parent_best=None
        if best is None
        else records.stored_metric(store, newest, best, setup.measured_under),
        inherited_trials=records.n_trials(store, newest.hyp_id),
    )


def _pinned(store: StateStore, sha: str) -> Hypothesis:
    return parse_yaml(Hypothesis, store.get_blob(sha).decode("utf-8"), HYPOTHESIS_FILE)


def _text(store: StateStore, sha: str) -> str:
    return store.get_blob(sha).decode("utf-8", errors="replace")
