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
as a malformed JSON object would. It never becomes a card, so a wasted answer costs a call
rather than a trial.

**Context is bounded, not summarised.** The stable half of the prompt — the program, the
hypothesis, the objective's definition — is byte-identical on every call of a run, so a
provider cache hits; the moving half is the current file, the last `context_cards` cards,
the previous diff, and the tail of a crash if the last card crashed. Nothing else, however
much of it exists.

**Drift is checked on a clock, not on suspicion.** Every `align_every` cards the run is
asked whether it still tests the idea, and a drift rewinds it and carries on.

**A run ends on a stall, and a stall is not an ending.** `stall_k` consecutive non-keeps
close the run and hand the hypothesis to the scheduler, which certifies what is worth
certifying and puts the hypothesis back in the queue either way.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from kanso.criteria import catalogue
from kanso.errors import ValidationError
from kanso.hyp import HYPOTHESIS_FILE, PROGRAM_FILE, STRATEGY_FILE
from kanso.models import CallInputs, route
from kanso.research import align, lanes, records, scheduler
from kanso.research import diff as diffs
from kanso.research import loop as research_loop
from kanso.schemas import Hypothesis, RunRecord, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = ["CRASH_TAIL_LINES", "GATE_LINES", "TASK", "Outcome", "run"]

TASK: Final = "propose"
"""The task class every card of a run is proposed by."""

CRASH_TAIL_LINES: Final = 50
"""How much of a crash a proposer is shown: the end, where the exception is."""

GATE_LINES: Final = 10
"""How many failing certification gates are fed back into the next proposal."""

CARDS: Final = "cards"
STALLED: Final = "stalled"
"""Why a driver stopped: it reached the count it was given, or the run stalled."""

_UNCHANGED: Final = (
    "diff: applies but leaves strategy.py exactly as it was, so it is not an experiment"
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

    `cards` counts the cards this call proposes; the baseline and everything a previous
    call left behind do not count. `None` researches until the run stalls, which is what
    a daemon lane asks for.
    """
    lane = lanes.check_lane(lane)
    if records.active(store, hyp_id) is None:
        research_loop.begin(ws, store, hyp_id, lane=lane)
    active = records.require_active(store, hyp_id, lane)
    settings = ws.config.research
    directory = ws.root / active.dir
    tally = {"keep": 0, "discard": 0, "crash": 0}
    proposed = checks = drifts = 0
    misses = _trailing_non_keeps(store, active)
    waiting = align.since(store, active)
    previous = _last_diff(store, active)
    reason = CARDS

    while cards is None or proposed < cards:
        source = align.lane_strategy(store, active, directory)
        desc, patch, candidate = _propose(ws, store, active, source, previous, lane)
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
        applied["source"] = candidate
        return complaints

    inputs = CallInputs(
        subject=active.hyp_id,
        stable=_stable(store, active),
        dynamic=_dynamic(ws, store, active, source, previous),
        check=judge,
    )
    answer = route(ws, store, TASK, inputs, lane=lane)
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
    """One card as the proposer sees it: what was tried, and what it scored."""
    return {
        "sha7": str(row["strategy_sha"])[:7],
        "status": str(row["status"]),
        "metric": float(row["metric"]),
        "metric_se": float(row["metric_se"] or 0.0),
        "desc": str(row["description"]),
    }


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
    """The gates the newest failed certificate of this hypothesis reported as failing."""
    row = store.connection.execute(
        "SELECT gates FROM certificates WHERE hyp_id = ? AND verdict = 'fail'"
        " ORDER BY created_at DESC LIMIT 1",
        (hyp_id,),
    ).fetchone()
    if row is None:
        return []
    gates: Any = json.loads(str(row["gates"]))
    failed = [gate for gate in gates if not gate.get("pass", True)]
    return [f"{gate['id']}: {gate.get('evidence', {})}" for gate in failed[:GATE_LINES]]


def _recent(store: StateStore, active: RunRecord, limit: int) -> list[sqlite3.Row]:
    """The last `limit` cards of this run, oldest first.

    Read as rows rather than as `Card`s, and bounded in SQL rather than in Python, because
    a run is unbounded and a proposer is shown a fixed window of it either way.
    """
    rows = store.connection.execute(
        "SELECT strategy_sha, status, metric, metric_se, description, crash_tail FROM cards"
        " WHERE run_id = ? ORDER BY seq DESC LIMIT ?",
        (active.run_id, limit),
    ).fetchall()
    return list(reversed(rows))


def _trailing_non_keeps(store: StateStore, active: RunRecord) -> int:
    """How many cards this run has recorded since its last keep, so a resume continues."""
    row = store.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE run_id = ? AND seq > COALESCE("
        " (SELECT MAX(seq) FROM cards WHERE run_id = ? AND status = 'keep'), 0)",
        (active.run_id, active.run_id),
    ).fetchone()
    return int(row[0])


def _last_diff(store: StateStore, active: RunRecord) -> str:
    """The change the previous card of this run tried, or nothing when it has had one."""
    rows = store.connection.execute(
        "SELECT strategy_sha FROM cards WHERE run_id = ? ORDER BY seq DESC LIMIT 2",
        (active.run_id,),
    ).fetchall()
    if len(rows) < 2:
        return ""
    return diffs.unified(store.get_blob(str(rows[1][0])), store.get_blob(str(rows[0][0])))
