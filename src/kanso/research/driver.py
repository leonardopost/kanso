"""The driver: propose, apply, evaluate, repeat, for as long as the operator lets it.

This is autoresearch. It begins a run if the hypothesis has none, then asks a model for
the next single change to `strategy.py`, applies it, and evaluates the result as one card
— the same card an operator gets by editing the file by hand, through the same function.
There is no second evaluation path, so nothing the driver produces is judged more kindly
than something a person produced.

Four rules make the loop finite in effort while remaining infinite in time.

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
answers that do not apply, or do not parse — is still a failure of the step.

**Context is bounded, not summarised.** The stable half of the prompt — the program, the
hypothesis, the objective's definition — is byte-identical on every call of a run, so a
provider cache hits; the moving half is the current file, the last `context_cards` cards
of the hypothesis under the run's pins — across runs, so a run that begins after a stall
is not shown a blank slate and made to re-walk the last run's discards — the previous
diff, and the tail of a crash if the last card crashed. Nothing else, however much of it
exists.

**Drift is checked on a clock, not on suspicion.** Every `align_every` cards the run is
asked whether it still tests the idea, and a drift rewinds it and carries on.

**A run ends on a stall, and a stall is not an ending.** `stall_k` consecutive non-keeps
close the run and hand the hypothesis to the scheduler, which certifies what is worth
certifying and puts the hypothesis back in the queue either way. Between the two the lane
holds the hypothesis in its own name, so a certification that cannot run, or a stop that
lands during one, leaves the hypothesis owed to the queue rather than lost.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from kanso.criteria import catalogue
from kanso.errors import PreconditionError, ValidationError
from kanso.hyp import HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE
from kanso.models import CallInputs, route
from kanso.research import align, lanes, records, scheduler
from kanso.research import diff as diffs
from kanso.research import loop as research_loop
from kanso.schemas import Hypothesis, RunRecord, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "CRASH_TAIL_LINES",
    "GATE_LINES",
    "REPEATED",
    "TASK",
    "NothingNewError",
    "Outcome",
    "run",
]

TASK: Final = "propose"
"""The task class every card of a run is proposed by."""

CRASH_TAIL_LINES: Final = 50
"""How much of a crash a proposer is shown: the end, where the exception is."""

GATE_LINES: Final = 10
"""How many failing certification gates are fed back into the next proposal."""

CARDS: Final = "cards"
STALLED: Final = "stalled"
"""Why a driver stopped: it reached the count it was given, or the run stalled."""

REPEATED: Final = "repeated"
"""The event a miss appends, under the hypothesis id: the ladder ran out having judged an
answer that reproduced bytes already carded."""


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
            "checks": self.checks,
            "drifts": self.drifts,
            "reason": self.reason,
            "ended": self.ended,
            "best_sha": self.best_sha,
            "best_metric": self.best_metric,
        }


def run(
    ws: Workspace,
    store: StateStore,
    hyp_id: str,
    *,
    cards: int | None = None,
    lane: str = lanes.DEFAULT_LANE,
) -> Outcome:
    """Research a hypothesis: begin a run if it has none, then propose cards.

    `cards` counts the proposals this call asks for — cards and misses alike — so an
    exhausted proposer cannot keep a bounded call running; the baseline and everything a
    previous call left behind do not count. `None` researches until the run stalls, which
    is what a daemon lane asks for.
    """
    lane = lanes.check_lane(lane)
    if records.active(store, hyp_id) is None:
        research_loop.begin(ws, store, hyp_id, lane=lane)
    active = records.require_active(store, hyp_id, lane)
    settings = ws.config.research
    directory = ws.root / active.dir
    tally = {"keep": 0, "discard": 0, "crash": 0}
    proposed = missed = checks = drifts = 0
    misses = _trailing_non_keeps(store, active)
    waiting = align.since(store, active)
    previous = _last_diff(store, active)
    reason = CARDS

    while cards is None or proposed < cards:
        source = align.lane_strategy(store, active, directory)
        try:
            desc, patch, candidate = _propose(ws, store, active, source, previous, lane)
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
        lanes.write_atomic(directory / STRATEGY_FILE, candidate)
        card = research_loop.card(ws, store, hyp_id, desc, lane=lane)
        proposed += 1
        waiting += 1
        previous = patch
        tally[card.status] += 1
        misses = 0 if card.status == "keep" else misses + 1
        if waiting >= settings.align_every:
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
) -> tuple[str, str, bytes]:
    """Ask for the next change and return its description, its diff and the new bytes.

    Applying the diff is the caller's check, so a diff that will not fit is corrected on
    the router's ladder rather than turned into a card that could not have run.
    """
    applied: dict[str, bytes] = {}
    repeat: dict[str, str] = {}

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
        dynamic=_dynamic(ws, store, active, source, previous),
        check=judge,
    )
    try:
        answer = route(ws, store, TASK, inputs, lane=lane)
    except PreconditionError as exc:
        said = repeat.get("complaint")
        if said is not None:
            raise NothingNewError(exc.message, remedy=said) from exc
        raise
    return str(answer.data["desc"]), str(answer.data["diff"]), applied["source"]


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
) -> dict[str, object]:
    """What has changed since the last call: the file, the recent cards and the last diff."""
    recent = _recent(store, active, ws.config.research.context_cards)
    facts: dict[str, object] = {
        STRATEGY_FILE: source.decode("utf-8", errors="replace"),
        "recent_cards": [_summary(row) for row in recent],
        "last_diff": previous,
    }
    if recent and recent[-1]["crash_tail"]:
        facts["crash_tail"] = _tail(str(recent[-1]["crash_tail"]), CRASH_TAIL_LINES)
    failing = _failing_gates(store, active.hyp_id)
    if failing:
        facts["failing_certification_gates"] = failing
    return facts


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
    continues the count rather than starting it over."""
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
