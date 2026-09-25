"""The driver: propose, apply, evaluate, repeat, for as long as the operator lets it.

This is autoresearch. It begins a run if the hypothesis has none, then asks a model for
the next single change to `strategy.py`, applies it, and evaluates the result as one card
— the same card an operator gets by editing the file by hand, through the same function.
There is no second evaluation path, so nothing the driver produces is judged more kindly
than something a person produced.

Five rules make the loop finite in effort while remaining infinite in time.

**A proposal is a diff, and a diff that does not fit is a wrong answer.** The model is
given the file's exact bytes and returns a unified diff over them. Applying it happens
here, in-package, on lines: a diff that does not apply, that names another file, or that
leaves the file unchanged is invalid output and takes the router's retry ladder, exactly
as a malformed JSON object would. So is one that produces bytes the hypothesis has already
carded under the run's pins — the same file, the same hypothesis, the same snapshot, the
same criteria — because a card is a deterministic function of those and a second one
would only repeat the first while counting as a second trial. The refusal names the card
it repeats. None of these becomes a card, so a wasted answer costs a call rather than a
trial. A ladder that runs out having judged a repeat anywhere in it is a *miss*: the model
was asked three times and reached for a change already tried at least once, which is what a
discard says too, so it counts toward the stall exactly as a discard does and is recorded
as an event rather than a card. A ladder that runs out without ever proposing a repeat —
answers that do not apply, or do not parse — is still a failure of the step. A candidate
that ran and held the same book as a strategy already judged under the pins is refused by
the loop after the backtest rather than before it, and is not the same kind of thing: it
cost the engine time and produced a real number, so it is a card with a status of its own,
it counts as a trial and it enters the coverage. It is a card in every count a card is in:
it advances the drift clock, and the diff that produced it is what the next turn is shown
as `last_diff`. What it costs is the turn — it counts toward the stall like a discard —
and the `redundant` event carries the one fact the card cannot, which is the card it
repeated. A candidate that held such a book and moved the number past the floor is not
refused at all; it appends a `same_book` event instead, and the next proposal is shown the
two kinds in one list with `repeat` saying which.

**A crash buys a repair, and only so many.** A card that raised spent a proposal and
returned nothing: the idea in it was never judged, because it never ran. Measured in a
live workspace, five consecutive crashes on one hypothesis were five state-handling slips
— `'dict' object has no attribute 'append'`, an `InstrumentId` given as a `str`, an
attribute read before it was set — on ideas straight out of that hypothesis's own declared
families, and not one of the five was ever judged. So the turn after a crash is a repair:
the proposer is given the traceback and the diff that produced it, over the file it now
holds, and asked to make the same change with the fault fixed. `REPAIRS` of them, then the
idea is dropped and the next turn asks for a new one — a proposer that cannot fix its own
`AttributeError` in two goes is not going to, and the run has other things to try.

**Context is bounded, not summarised.** The stable half of the prompt — the program, the
hypothesis, the objective's definition — is byte-identical on every call of a run, so a
provider cache hits; the moving half is the current file, the last `context_cards` cards
of the hypothesis under the run's pins — across runs, so a run that begins after a stall
is not shown a blank slate and made to re-walk the last run's discards — the previous
diff, the tail of a crash if the last card crashed with the repair it owes, and the
coverage table: every card
under the pins, read back by the tags its proposer gave it, as a count, the best score
and its status, and the newest card per tag. The recent cards say what was tried last;
the coverage says what has been tried at all, in a size that does not grow with the
hypothesis. Nothing else, however much of it exists.

**The search has a phase, and the phase is a rule, not a mood.** Misses since the last
keep drive it: for the first `[research] local_cards` the proposer is asked for local
changes — a parameter, a threshold, a window — and for the next `structural_cards` a
change that moves no structure is refused on the ladder like a repeat, where structure is
the syntax tree of `strategy.py` with every constant blanked. Then local again, round
until a keep or a stall. The rule is stated in the instruction and the phase is a fact of
every call, because a refusal the proposer was never told about is a wasted ladder.

**Drift is checked on a clock, not on suspicion.** Every `align_every` cards the run is
asked whether it still tests the idea, and a drift rewinds it and carries on — carrying the
reasons with it. A rewound run told nothing walks back into the drift it was rewound for:
measured in a live workspace, one hypothesis was rewound four times in six hours for the
same complaint, because the file it resumed from said nothing about why the last one went.
So every drift this run was rewound for reaches the next proposal, newest first.

**A run ends on a stall, and a stall is not an ending.** `stall_k` consecutive non-keeps
close the run and hand the hypothesis to the scheduler, which certifies what is worth
certifying and puts the hypothesis back in the queue either way. Between the two the lane
holds the hypothesis in its own name, so a certification that cannot run, or a stop that
lands during one, leaves the hypothesis owed to the queue rather than lost.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from kanso.config import ResearchConfig
from kanso.criteria import catalogue
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE
from kanso.models import CallInputs, route
from kanso.research import align, lanes, records, scheduler
from kanso.research import diff as diffs
from kanso.research import loop as research_loop
from kanso.schemas import Card, Hypothesis, RunRecord, Tag, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "CRASH_TAIL_LINES",
    "DRIFT_LINES",
    "GATE_LINES",
    "LOCAL",
    "REDUNDANT_LINES",
    "REPAIRS",
    "REPEATED",
    "STRUCTURAL",
    "TASK",
    "NothingNewError",
    "Outcome",
    "coverage",
    "phase",
    "run",
]

TASK: Final = "propose"
"""The task class every card of a run is proposed by."""

CRASH_TAIL_LINES: Final = 50
"""How much of a crash a proposer is shown: the end, where the exception is."""

REPAIRS: Final = 2
"""How many repair turns one crashed idea buys before it is a miss like any other.

A bound rather than a setting, like `CRASH_TAIL_LINES` beside it and unlike `stall_k`: it
shapes one prompt rather than the search, and the number that matters is small. One repair
catches the slip a traceback names outright, a second catches the one the first uncovers,
and a third is a proposer that has stopped reading the traceback."""

GATE_LINES: Final = 10
"""How many failing certification gates are fed back into the next proposal."""

DRIFT_LINES: Final = 3
"""How many of a run's rewinds are fed back into the next proposal, newest first."""

REDUNDANT_LINES: Final = 10
"""How many books already measured are fed back into the next proposal, newest first.

Both kinds count against it: the repeats, and the cards that held a measured book and
moved the number past the floor. One window rather than one each, because they are one
fact to the proposer — this book has been held — and the entry's `repeat` says which of
the two it was. Ten books is ten books: a second window would make the prompt longer
rather than better, and the proposer is steered by which book has been trodden, which both
kinds name.

What one kind takes from the other was measured rather than assumed, on the live workspace
of 2026-09-18, by classifying each of its 3,092 refusals as 0.9.0 would and walking the
stream a window at a time. 106 of the 3,092 are the second kind — 3.4% — but they arrive
in runs, so 506 of the 3,092 turns would have been shown a repeat they were not: 1,049
lines in all, a third of a line per turn, and on the intraday hypothesis where the second
kind is 23% of the stream, 264 of 325 turns and 2.3 lines of the ten. That is the price of
the newest ten being the newest ten, and it is paid in older repeats for newer books.

Read across the pins rather than within the run, like `_recent` and unlike `_rewound_for`:
a rewind is a fact about one run's file, but a book already held is a fact about the
hypothesis, and every reseed used to wipe the record of it. Measured on one live
hypothesis, 560 of 991 refusals repeated an idea from an *earlier* run against 18 within
their own — thirty-one to one — so a window of three that only ever looked inside the run
showed the proposer almost none of what it had already spent a backtest learning."""

CARDS: Final = "cards"
STALLED: Final = "stalled"
STOPPED: Final = "stopped"
"""Why a driver stopped: it reached the count it was given, the run stalled, or the process
driving it was told to stop."""

REPEATED: Final = "repeated"
"""The event a miss appends, under the hypothesis id: the ladder ran out having judged an
answer that reproduced bytes already carded, or — in the structural phase — one that
moved no structure."""

LOCAL: Final = "local"
STRUCTURAL: Final = "structural"
"""The two phases of a search, by misses since the last keep: `local_cards` of the first,
then `structural_cards` of the second, then round again."""


class NothingNewError(PreconditionError):
    """The proposer had nothing new: the ladder ran out on bytes already carded."""


_UNCHANGED: Final = (
    "diff: applies but leaves strategy.py exactly as it was, so it is not an experiment"
)
_ALREADY_CARDED: Final = (
    "diff: applies, but produces bytes this hypothesis already carded under the run's pins"
    " ({sha7}: {status} at {metric:.4g}), so its result is known and it is not an experiment"
)
_ONE_LINE: Final = "desc: one line describing the change, with no tab and no newline"
_NO_STRUCTURE: Final = (
    "diff: applies, but moves no structure — the syntax tree of strategy.py is unchanged once "
    "every constant is blanked — and the search is in its structural phase after {misses} "
    "misses since the last keep, so a parameter, a threshold or a respelling is not the "
    "experiment being asked for; change what the strategy reads, holds, filters on or exits on"
)


@dataclass(frozen=True)
class Outcome:
    """What one invocation of the driver did."""

    hyp_id: str
    run_id: str
    lane: str
    proposed: int
    keeps: int
    discards: int
    crashes: int
    missed: int
    redundant: int
    checks: int
    drifts: int
    reason: str
    ended: bool
    best_sha: str | None
    best_metric: float | None

    def payload(self) -> dict[str, object]:
        """The outcome as one JSON object."""
        return {
            "id": self.hyp_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "proposed": self.proposed,
            "keeps": self.keeps,
            "discards": self.discards,
            "crashes": self.crashes,
            "missed": self.missed,
            "redundant": self.redundant,
            "checks": self.checks,
            "drifts": self.drifts,
            "reason": self.reason,
            "ended": self.ended,
            "best_sha": self.best_sha,
            "best_metric": self.best_metric,
        }


def _never() -> bool:
    """A stop request nothing makes: what a driver run from the command line is given."""
    return False


def run(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    cards: int | None = None,
    lane: str = lanes.DEFAULT_LANE,
    stop: Callable[[], bool] = _never,
) -> Outcome:
    """Research a hypothesis: begin a run if it has none, then propose cards.

    `cards` counts the proposals this call asks for — cards and misses alike — so an
    exhausted proposer cannot keep a bounded call running; the baseline and everything a
    previous call left behind do not count. `None` researches until the run stalls, which
    is what a daemon lane asks for.

    `stop` is asked before every proposal, again before the card a proposal produced, and
    before an alignment check falls due. A daemon lane passes its own stop request, so a
    lane told to stop while a model was answering starts neither the card nor another
    call: the answer in hand is dropped, the lane directory is left as it was, and the run
    resumes at its next turn. The baseline is a card too, and the runner itself refuses to
    start one in a process told to stop (`kanso.nautilus.backtest.run_subprocess`).
    """
    lane = lanes.check_lane(lane)
    if records.active(store, hyp_id) is None:
        research_loop.begin(ws, store, hyp_id, lane=lane)
    active = records.require_active(store, hyp_id, lane)
    settings = ws.config.research
    directory = ws.root / active.dir
    tally = {"keep": 0, "discard": 0, "crash": 0}
    proposed = missed = redundant = checks = drifts = 0
    misses = _trailing_non_keeps(store, active)
    waiting = align.since(store, active)
    previous = _last_diff(store, active)
    reason = CARDS

    while cards is None or proposed < cards:
        if stop():
            reason = STOPPED
            break
        source = align.lane_strategy(store, active, directory)
        try:
            desc, patch, candidate, tags = _propose(
                ws, store, active, source, previous, lane, misses
            )
        except NothingNewError as exc:
            store.event(
                REPEATED,
                hyp_id,
                {"run_id": active.run_id, "lane": lane, "because": exc.remedy},
            )
            proposed += 1
            missed += 1
            misses += 1
            if misses >= settings.stall_k:
                reason = STALLED
                break
            continue
        if stop():
            reason = STOPPED
            break
        lanes.write_atomic(directory / STRATEGY_FILE, candidate)
        made: Card | None
        try:
            made = research_loop.card(ws, store, hyp_id, desc, lane=lane, tags=tags)
        except research_loop.RedundantError:
            made = None
        proposed += 1
        waiting += 1
        previous = patch
        if made is None:
            redundant += 1
            misses += 1
        else:
            tally[made.status] += 1
            misses = 0 if made.status == "keep" else misses + 1
        if waiting >= settings.align_every and not stop():
            aligned, _ = align.check(ws, store, hyp_id, lane)
            checks += 1
            drifts += 0 if aligned else 1
            waiting = 0
        if misses >= settings.stall_k:
            reason = STALLED
            break
        active = records.require_active(store, hyp_id, lane)

    if reason == STALLED:
        research_loop.end(ws, store, hyp_id)
        scheduler.hold(store, hyp_id, lane)
        scheduler.on_stall(ws, store, hyp_id, lane)
    best_sha, best_metric = records.best_of(store, hyp_id)
    return Outcome(
        hyp_id=hyp_id,
        run_id=active.run_id,
        lane=lane,
        proposed=proposed,
        keeps=tally["keep"],
        discards=tally["discard"],
        crashes=tally["crash"],
        missed=missed,
        redundant=redundant,
        checks=checks,
        drifts=drifts,
        reason=reason,
        ended=reason == STALLED,
        best_sha=best_sha,
        best_metric=best_metric,
    )


# --- one proposal ------------------------------------------------------------


def _propose(
    ws: Workspace,
    store: StateStore,
    active: RunRecord,
    source: bytes,
    previous: str,
    lane: str,
    misses: int,
) -> tuple[str, str, bytes, list[Tag]]:
    """Ask for the next change: its description, its diff, the new bytes and its tags.

    Applying the diff is the caller's check, so a diff that will not fit is corrected on
    the router's ladder rather than turned into a card that could not have run. `misses`
    is how many non-keeps in a row the run has recorded, which sets the phase: in the
    structural one a candidate whose syntax tree equals the file's once constants are
    blanked is refused on the ladder, and a ladder that runs out on such answers is a
    miss like a repeat.
    """
    applied: dict[str, bytes] = {}
    repeat: dict[str, str] = {}
    current = phase(ws.config.research, misses)
    skeleton = _skeleton(source) if current == STRUCTURAL else None

    def judge(data: Mapping[str, object]) -> list[str]:
        complaints: list[str] = []
        if any(character in str(data["desc"]) for character in "\t\r\n"):
            complaints.append(_ONE_LINE)
        try:
            candidate = diffs.apply(source, str(data["diff"]))
        except ValidationError as exc:
            complaints.append(exc.message)
            return complaints
        if candidate == source:
            complaints.append(_UNCHANGED)
            return complaints
        if skeleton is not None and _skeleton(candidate) == skeleton:
            said = _NO_STRUCTURE.format(misses=misses)
            repeat.setdefault("complaint", said)
            complaints.append(said)
            return complaints
        seen = _carded(store, active, candidate)
        if seen is not None:
            said = _ALREADY_CARDED.format(
                sha7=str(seen["strategy_sha"])[:7],
                status=str(seen["status"]),
                metric=float(seen["metric"]),
            )
            repeat.setdefault("complaint", said)
            complaints.append(said)
            return complaints
        applied["source"] = candidate
        return complaints

    inputs = CallInputs(
        subject=active.hyp_id,
        stable=_stable(store, active),
        dynamic=_dynamic(ws, store, active, source, previous, misses),
        check=judge,
    )
    try:
        answer = route(ws, store, TASK, inputs, lane=lane)
    except PreconditionError as exc:
        said = repeat.get("complaint")
        if said is not None:
            raise NothingNewError(exc.message, remedy=said) from exc
        raise
    tags = cast(list[Tag], [str(tag) for tag in cast(list[object], answer.data["tags"])])
    return str(answer.data["desc"]), str(answer.data["diff"]), applied["source"], tags


def _stable(store: StateStore, active: RunRecord) -> dict[str, object]:
    """The facts that do not move for the life of a run, from the blobs it pinned."""
    hyp = _pinned(store, active)
    return {
        PROGRAM_FILE: store.get_blob(active.program_sha).decode("utf-8", errors="replace"),
        HYPOTHESIS_FILE: store.get_blob(active.hypothesis_sha).decode("utf-8", errors="replace"),
        "objective": _objective(hyp),
    }


def _dynamic(
    ws: Workspace,
    store: StateStore,
    active: RunRecord,
    source: bytes,
    previous: str,
    misses: int,
) -> dict[str, object]:
    """What has changed since the last call: the file, the recent cards, the coverage of
    every card under the pins by tag, the phase, and the last diff.

    `crash_tail` is read from the newest of the recent cards and `repair` from the newest
    card of the run, and those are the same row: `_recent` reaches across runs but orders
    by `card_id`, a hypothesis has at most one active run, and a run opens with a baseline
    card that ran — a baseline that did not is refused before the run exists. So a run
    that begins after another stalled on a crash is shown that crash among its recent
    cards, as the record of what was tried, and never as a traceback over a file it does
    not hold.
    """
    recent = _recent(store, active, ws.config.research.context_cards)
    facts: dict[str, object] = {
        STRATEGY_FILE: source.decode("utf-8", errors="replace"),
        "recent_cards": [_summary(row) for row in recent],
        "coverage": coverage(store, active),
        "phase": {
            "name": phase(ws.config.research, misses),
            "misses_since_keep": misses,
            "local_cards": ws.config.research.local_cards,
            "structural_cards": ws.config.research.structural_cards,
        },
        "last_diff": previous,
    }
    if recent and recent[-1]["crash_tail"]:
        facts["crash_tail"] = _tail(str(recent[-1]["crash_tail"]), CRASH_TAIL_LINES)
    owed = _repair(store, active, source)
    if owed is not None:
        facts["repair"] = owed
    failing = _failing_gates(store, active.hyp_id)
    if failing:
        facts["failing_certification_gates"] = failing
    rewound = _rewound_for(store, active)
    if rewound:
        facts["rewound_for"] = rewound
    redundant = _redundant_in(store, active)
    if redundant:
        facts["redundant"] = redundant
    return facts


def _repair(store: StateStore, active: RunRecord, source: bytes) -> dict[str, object] | None:
    """The crashed idea this turn is asked to repair, or `None` when none is owed.

    It lives in the driver's turn, and the two places it could have lived say why. The
    router's ladder judges an answer against the file in hand and retries inside one call,
    but a crash is not an answer — it is a card, and its fault is only known after a
    backtest the ladder returned from long before. A sixth task class would reach the
    routing table of every register an operator maintains by hand, and would buy nothing: a
    repair asks the same question `propose` asks, over the same file, with one more fact,
    which is also what keeps the stable half of the prompt byte-identical and the provider
    cache warm.

    The bound is read from the run's own tail rather than carried in memory, so a call
    that resumes a run continues the count instead of starting it over, and it is spent
    per idea rather than per spell of bad luck. The tail is counted in groups of
    `REPAIRS + 1`: the first crash of a streak is a fresh idea, the `REPAIRS` crashes after
    it are its repairs, and the crash after those is the fresh idea that the last one
    bought — which owes a first repair of its own. Counting the streak instead would give
    the run's first crashed idea two repairs and every idea after it none until something
    landed, which is the case the repair exists for: five consecutive crashes were five
    ideas, not one idea five times.

    The diff runs from the file in hand to the bytes that crashed, so applying it exactly
    reproduces the crash — which the `_carded` check then refuses, making a repair that
    changes nothing a wrong answer on the ladder rather than a second identical card.
    """
    newest: sqlite3.Row | None = store.connection.execute(
        "SELECT strategy_sha, status, description FROM cards WHERE run_id = ?"
        " ORDER BY seq DESC LIMIT 1",
        (active.run_id,),
    ).fetchone()
    if newest is None or str(newest["status"]) != "crash":
        return None
    streak = store.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE run_id = ? AND seq > COALESCE("
        " (SELECT MAX(seq) FROM cards WHERE run_id = ? AND status != 'crash'), 0)",
        (active.run_id, active.run_id),
    ).fetchone()
    attempt = int(streak[0]) % (REPAIRS + 1)
    if attempt == 0:
        return None
    return {
        "attempt": attempt,
        "of": REPAIRS,
        "desc": str(newest["description"]),
        "diff": diffs.unified(source, store.get_blob(str(newest["strategy_sha"]))),
    }


def phase(settings: ResearchConfig, misses: int) -> str:
    """Which phase `misses` non-keeps in a row put the search in.

    Local for the first `local_cards`, structural for the next `structural_cards`, and
    round again: a run that has missed twenty times under the template is local once
    more, because the structural phase is a spell of refusing the cheap change, not a
    permanent narrowing of what may be proposed.
    """
    span = settings.local_cards + settings.structural_cards
    return LOCAL if misses % span < settings.local_cards else STRUCTURAL


def _skeleton(source: bytes) -> str | None:
    """The syntax tree of `source` with every constant blanked, or `None` if it does not
    parse — and a file that does not parse is judged by the card it crashes, not here."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            node.value = None
    return ast.dump(tree)


def _summary(row: sqlite3.Row) -> dict[str, object]:
    """One card as the proposer sees it: what was tried, what it scored, and what refused it.

    The gates that did not pass carry their evidence, because a card discarded before any
    backtest ran — the whole of `strategy_integrity` — otherwise reaches the proposer as a
    metric of zero with no reason, and a proposer told nothing repeats itself.
    """
    made: dict[str, object] = {
        "sha7": str(row["strategy_sha"])[:7],
        "status": str(row["status"]),
        "metric": float(row["metric"]),
        "metric_se": float(row["metric_se"] or 0.0),
        "desc": str(row["description"]),
    }
    refused = _refused(row)
    if refused:
        made["failed_gates"] = refused
    return made


def _refused(row: sqlite3.Row) -> list[dict[str, object]]:
    """The gates this card failed, each with the evidence the gate recorded."""
    return [
        {"id": gate["id"], "evidence": gate.get("evidence", {})}
        for gate in json.loads(str(row["gate_results"]))
        if not gate.get("pass", True)
    ]


def _objective(hyp: Hypothesis) -> dict[str, object]:
    """The objective a run optimises, with the keep rule and the definition behind it."""
    ref = hyp.objective
    if ref is None:  # pragma: no cover - a classified hypothesis always carries one
        return {}
    item = catalogue().get(ref.id)
    return {
        "id": ref.id,
        "params": ref.params.model_dump(),
        "meaningful_when": None if item is None else item.meaningful_when,
    }


def _pinned(store: StateStore, active: RunRecord) -> Hypothesis:
    text = store.get_blob(active.hypothesis_sha).decode("utf-8")
    return parse_yaml(Hypothesis, text, HYPOTHESIS_FILE)


def _tail(text: str, lines: int) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _failing_gates(store: StateStore, hyp_id: str) -> list[str]:
    """Which gates the newest failed certificate reported as failing — the ids alone.

    Never the evidence. A certification gate measures the certification window, and its
    evidence says so in numbers: `embargoed_window` records `certification`, and
    `walk_forward_consistency` records `certification` and the value of every fold. This
    list is read by the proposer, which writes the next `strategy.py`, so handing it that
    dict is a route from the embargoed window into research — the one thing the embargo
    exists to refuse, arriving through the door marked feedback rather than through a
    backtest request. An id names what to work on and measures nothing.
    """
    row = store.connection.execute(
        "SELECT gates FROM certificates WHERE hyp_id = ? AND verdict = 'fail'"
        " ORDER BY created_at DESC LIMIT 1",
        (hyp_id,),
    ).fetchone()
    if row is None:
        return []
    gates: Any = json.loads(str(row["gates"]))
    failed = [gate for gate in gates if not gate.get("pass", True)]
    return [str(gate["id"]) for gate in failed[:GATE_LINES]]


_PINNED: Final = (
    " JOIN runs ON runs.run_id = cards.run_id"
    " WHERE cards.hyp_id = ? AND runs.hypothesis_sha = ? AND runs.snapshot_id = ?"
    " AND runs.criteria_version = ?"
)
"""The cards of a hypothesis that answer the same question this run asks: made under the
same hypothesis file, the same snapshot and the same criteria, in whichever run."""


def _pins(active: RunRecord) -> tuple[str, str, str, str]:
    return (active.hyp_id, active.hypothesis_sha, active.snapshot_id, active.criteria_version)


def _rewound_for(store: StateStore, active: RunRecord) -> list[str]:
    """Why this run has been rewound, newest first: the reason each drift check gave.

    A reason is a sentence about `strategy.py` and the hypothesis it is meant to test, both
    of which the proposer is already shown in full, so unlike a failing certification gate's
    evidence it carries nothing measured on the embargoed window and nothing measured at
    all. What it carries is the one thing the rewound file cannot say: which direction the
    check refused, and why.
    """
    rows = store.connection.execute(
        "SELECT detail FROM events WHERE kind = ? AND subject = ?"
        " AND json_extract(detail, '$.run_id') = ? ORDER BY event_id DESC LIMIT ?",
        (align.DRIFTED, active.hyp_id, active.run_id, DRIFT_LINES),
    ).fetchall()
    said = [str(json.loads(str(row["detail"])).get("reason") or "") for row in rows]
    return [reason for reason in said if reason]


def _redundant_in(store: StateStore, active: RunRecord) -> list[dict[str, object]]:
    """The newest books already measured under this run's pins: what was tried, and what
    it matched.

    Both kinds, and each says which it is. The card says a candidate was redundant; only
    the event says which stored signature it matched and on how much of it, and that is
    the half the proposer can act on — and a card that matched a book and moved the number
    further than the floor has no status of its own at all, so without its event the
    proposer is shown nothing and re-treads the book it was never told about. Its `repeat`
    is `False`: the turn was not refused, and the rule the proposer is given — hold a book
    already measured and move the number, and it is an experiment — is the rule that
    admitted it.

    Joined to `runs` so the window is the pins rather than the run, for the reason
    `REDUNDANT_LINES` gives: the same bet spelled a third way is refused against the first
    run's spelling as much as against this run's, and a proposer shown neither writes it a
    fourth time.

    The pins and not the reading. An entry is not an anchor — `records.matched_book` picks
    those, under the reading the asking card was measured with — it is the record that an
    idea has been tried, and an idea tried under three folds was tried. The reading could
    not narrow this window in any case: `loop.Setup.measured_under` is a fact about a card
    rather than about a run, since the warmup prefix it digests is resolved from the
    catalog for every card and can move within one run, so there is no one reading a run
    could be joined by.
    """
    rows = store.connection.execute(
        "SELECT events.kind, events.detail FROM events JOIN runs"
        " ON runs.run_id = json_extract(events.detail, '$.run_id')"
        " WHERE events.kind IN (?, ?) AND events.subject = ? AND runs.hypothesis_sha = ?"
        " AND runs.snapshot_id = ? AND runs.criteria_version = ?"
        " ORDER BY events.event_id DESC LIMIT ?",
        (
            research_loop.REDUNDANT,
            research_loop.SAME_BOOK,
            active.hyp_id,
            active.hypothesis_sha,
            active.snapshot_id,
            active.criteria_version,
            REDUNDANT_LINES,
        ),
    ).fetchall()
    found: list[dict[str, object]] = []
    for row in rows:
        detail = json.loads(str(row["detail"]))
        entry: dict[str, object] = {key: detail.get(key) for key in ("desc", "like", "pct")}
        entry["repeat"] = str(row["kind"]) == research_loop.REDUNDANT
        found.append(entry)
    return found


def _recent(store: StateStore, active: RunRecord, limit: int) -> list[sqlite3.Row]:
    """The last `limit` cards of the hypothesis under this run's pins, oldest first.

    Across runs: a run that begins after a stall starts from the same bytes the last one
    stalled on, and a proposer shown only its own run's cards re-walks the last run's
    discards. Read as rows rather than as `Card`s, and bounded in SQL rather than in
    Python, because a hypothesis is unbounded and a proposer is shown a fixed window of it
    either way.
    """
    rows = store.connection.execute(
        "SELECT cards.strategy_sha, cards.status, cards.metric, cards.metric_se,"
        " cards.description, cards.crash_tail, cards.gate_results FROM cards"
        f"{_PINNED} ORDER BY cards.card_id DESC LIMIT ?",
        (*_pins(active), limit),
    ).fetchall()
    return list(reversed(rows))


def coverage(store: StateStore, active: RunRecord) -> dict[str, dict[str, object]]:
    """Every card under this run's pins, read back by tag: how many, the best and the newest.

    One row per tag in `kanso.schemas.TAGS` that at least one card carries: the count, the
    highest metric and the status of the card that scored it, and the sha7 of the newest.
    Built from `cards` alone — research-window metrics — so nothing measured on the
    certification window reaches the proposer through it. Bounded by the vocabulary rather
    than by the hypothesis, which is what lets it stand in for the cards `context_cards`
    leaves out.
    """
    rows = store.connection.execute(
        "SELECT cards.strategy_sha, cards.status, cards.metric, cards.tags FROM cards"
        f"{_PINNED} ORDER BY cards.card_id",
        _pins(active),
    ).fetchall()
    counts: dict[str, int] = {}
    best: dict[str, tuple[float, str]] = {}
    newest: dict[str, str] = {}
    for row in rows:
        metric, status, sha7 = (
            float(row["metric"]),
            str(row["status"]),
            str(row["strategy_sha"])[:7],
        )
        for tag in json.loads(str(row["tags"])):
            counts[tag] = counts.get(tag, 0) + 1
            if tag not in best or metric > best[tag][0]:
                best[tag] = (metric, status)
            newest[tag] = sha7
    return {
        tag: {
            "count": counts[tag],
            "best_metric": best[tag][0],
            "best_status": best[tag][1],
            "newest": newest[tag],
        }
        for tag in sorted(counts)
    }


def _carded(store: StateStore, active: RunRecord, candidate: bytes) -> sqlite3.Row | None:
    """The card these bytes already have under this run's pins, or `None`."""
    row: sqlite3.Row | None = store.connection.execute(
        f"SELECT cards.strategy_sha, cards.status, cards.metric FROM cards{_PINNED}"
        " AND cards.strategy_sha = ? ORDER BY cards.card_id DESC LIMIT 1",
        (*_pins(active), hashlib.sha256(candidate).hexdigest()),
    ).fetchone()
    return row


def _trailing_non_keeps(store: StateStore, active: RunRecord) -> int:
    """How many cards and misses this run has recorded since its last keep, so a resume
    continues the count rather than starting it over.

    A redundant result is counted once, by its card. Only a `repeated` miss is counted
    from the event log, because that one leaves no card at all; counting the `redundant`
    event as well would advance the stall twice for one turn.
    """
    cards = store.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE run_id = ? AND seq > COALESCE("
        " (SELECT MAX(seq) FROM cards WHERE run_id = ? AND status = 'keep'), 0)",
        (active.run_id, active.run_id),
    ).fetchone()
    misses = store.connection.execute(
        "SELECT COUNT(*) FROM events WHERE kind = ? AND subject = ?"
        " AND json_extract(detail, '$.run_id') = ? AND ts > COALESCE("
        " (SELECT MAX(created_at) FROM cards WHERE run_id = ? AND status = 'keep'), '')",
        (REPEATED, active.hyp_id, active.run_id, active.run_id),
    ).fetchone()
    return int(cards[0]) + int(misses[0])


def _last_diff(store: StateStore, active: RunRecord) -> str:
    """The change the previous card of this run tried, or nothing when it has had one."""
    rows = store.connection.execute(
        "SELECT strategy_sha FROM cards WHERE run_id = ? ORDER BY seq DESC LIMIT 2",
        (active.run_id,),
    ).fetchall()
    if len(rows) < 2:
        return ""
    return diffs.unified(store.get_blob(str(rows[1][0])), store.get_blob(str(rows[0][0])))
