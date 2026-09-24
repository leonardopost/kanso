"""Alignment, deterministic half: does this `strategy.py` still test the stated idea?

A research loop optimises a number, and the cheapest way to move a number is often to
stop testing the hypothesis — to trade an instrument that is not in the universe, to
subscribe to a finer bar than the one the idea is about, or to read a data type the
hypothesis never asked for. None of that is cheating in the `strategy_integrity` sense:
the code is well-behaved, it simply answers a different question. So it is checked
separately, and checked here first, because three of the ways to drift are visible in the
source's own syntax tree and need no model at all:

* **universe** — an instrument literal naming something outside `universe`;
* **resolution** — a bar specification whose step or aggregation differs from
  `resolution`, whether it is spelled as a `BarSpecification` call or as a bar-type
  string;
* **data types** — a subscription or a handler for a type outside `data_requirements`.

Each is a syntactic fact, so a violation is stated rather than judged: this half never
returns "probably". Only a source that passes it is worth a model's opinion, which is the
other half of the check and costs a call. A literal the checks cannot read — a symbol
built at runtime, a step read from the config — is not evidence of drift and is passed
over: this is a detector of stated intent, not a proof of its absence.

`check` is the whole check: the syntax tree first, the model only if it passed and the lane
holds something to judge, and the recovery if either says no. Recovery is the point. A
drifted run is not stopped — research is indefinite and stopping it on a judgement call
would hand the model a veto — it is *rewound*: the lane copy and `best` go back to the last
keep this run made while it was still aligned, or to the bytes the run began with when it
made none, the cards since the last check are marked not aligned so no later reader trusts
them, and the operator is told. The run then carries on from ground that was checked.

A check judges what the loop proposed, so the model is never asked about the run's base: the
bytes a run was handed — the workspace `strategy.py`, or a keep an earlier run made — are
not this run's proposal, and no rewind can go behind them, so a verdict against them moves
nothing. Asked anyway, once every proposal since the last check had failed to keep and the
lane had been restored to its base, a model judged a deliberately simple reference seed
against a thesis that describes what the search is for, and the recovery "rewound" the run
to the bytes it was already on: it marked the cards since the last check, told the proposer
the run had been rewound, and escalated, and not one byte moved. The same is true of the
bytes the last check left the lane on, the file it passed or the file it rewound to. The
loop's lane is back on them only when nothing has kept since — a keep moves the lane to new
bytes, and every card after it that does not keep restores the lane to that keep — so their
answer is already on record, and a drift verdict could only rewind the lane to where it
stands.

A lane on either is not put to the model and raises nothing. The check is recorded as an
`aligned` event marked `judged: false`, so `checkpoint` advances, the next check
differences from here, and a reader can tell it from a verdict; and every card keeps the
mark it had, since nothing judged it. It advances up to, and never past, a keep whose
bytes no check has answered for. The loop never takes such a keep out of the lane, but an
operator who cards a file and then writes the base back by hand does, and a checkpoint past
that keep would leave it under no verdict at all, standing as the ground a later drift
rewinds to. So the checkpoint stops short of it, and the next check that asks the model
marks it.

A keep an earlier run made was that run's to judge, and a run can end before the check that
would have judged it: by hand, or on a spell of misses, which count toward `stall_k` and not
toward `align_every`. Such a keep is carried as the next run's base unjudged; row 90 of
`docs/backlog.md` says what closing that would take.

The syntax tree still reads the base and the checkpoint's bytes. What it finds is a fact
about the bytes rather than an opinion of them: it costs nothing, it says the same thing
every time it is asked, and a check that answered `aligned` for a file whose tree names an
instrument outside the universe would assert what the tree disproves, whoever wrote the
file. So a base that fails it is reported as drift whenever a check finds the lane on it, as
any other file that fails it is.

A drift never rewinds onto the bytes it found drifted. The verdict marks the cards since the
last check, and every other card of the run that carried those bytes, bar the baseline: a
lane rewritten by hand can hand a pass to a keep it never held, and the newest keep no check
had marked drifted could otherwise be the very file just judged.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from kanso.hyp import HYPOTHESIS_FILE, STRATEGY_FILE, hypothesis_dir
from kanso.inbox import escalate
from kanso.models import CallInputs, route
from kanso.research import diff as diffs
from kanso.research import lanes, records
from kanso.schemas import Hypothesis, RunRecord, is_duration, parse_yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "ALIGNED",
    "BAR_TYPE_PATTERN",
    "DRIFTED",
    "HANDLERS",
    "MISALIGNED",
    "REASON_LIMIT",
    "TASK",
    "Checkpoint",
    "align_static",
    "check",
    "checkpoint",
    "lane_strategy",
    "problems",
    "since",
]

REASON_LIMIT: Final = 200
"""What `align_check` allows a reason to be, so both halves report the same shape."""

AGGREGATION: Final[dict[str, str]] = {
    "s": "SECOND",
    "m": "MINUTE",
    "h": "HOUR",
    "d": "DAY",
    "w": "WEEK",
}
"""The engine's bar aggregation for each duration unit, as the strategy API spells it."""

BAR_TYPE_PATTERN: Final = (
    r"^(?P<instrument>[A-Za-z0-9._/:]+)-(?P<step>\d+)-(?P<aggregation>[A-Z_]+)"
    r"-(?P<price>[A-Z_]+)-(?P<source>INTERNAL|EXTERNAL)$"
)
"""A bar type written as a string, which is how the engine parses one."""

_BAR_TYPE: Final = re.compile(BAR_TYPE_PATTERN)

_INSTRUMENT: Final = re.compile(r"^[A-Z0-9][A-Z0-9._]*\.[A-Z][A-Z0-9_]*$")
"""An instrument literal: a qualified `SYMBOL.VENUE`, upper case as the engine writes it."""

_SPECIFICATION: Final = "BarSpecification"
_AGGREGATION_ENUM: Final = "BarAggregation"

HANDLERS: Final[dict[str, str]] = {
    "subscribe_bars": "bar",
    "request_bars": "bar",
    "on_bar": "bar",
    "handle_bar": "bar",
    "subscribe_quote_ticks": "quote",
    "request_quote_ticks": "quote",
    "on_quote_tick": "quote",
    "handle_quote_tick": "quote",
    "subscribe_trade_ticks": "trade",
    "request_trade_ticks": "trade",
    "on_trade_tick": "trade",
    "handle_trade_tick": "trade",
}
"""Every subscription and handler name that names one market data type outright."""


@dataclass(frozen=True)
class _Stated:
    """What the hypothesis states, in the shapes the syntax tree is compared against."""

    universe: tuple[str, ...]
    known: frozenset[str]
    resolution: str
    bar: tuple[int, str] | None
    required: frozenset[str]

    @classmethod
    def of(cls, hyp: Hypothesis) -> _Stated:
        names = set(hyp.universe)
        names.update(name.split(".")[0] for name in hyp.universe)
        bar = (
            (int(hyp.resolution[:-1]), AGGREGATION[hyp.resolution[-1]])
            if is_duration(hyp.resolution)
            else None
        )
        return cls(
            universe=tuple(sorted(hyp.universe)),
            known=frozenset(names),
            resolution=hyp.resolution,
            bar=bar,
            required=frozenset(hyp.data_requirements),
        )

    def instrument(self, name: str, line: int) -> str | None:
        """A literal instrument the universe does not hold."""
        if name in self.known:
            return None
        return f"line {line}: {name!r} is not in the universe ({', '.join(self.universe)})"

    def specification(self, step: int, aggregation: str, line: int) -> str | None:
        """A bar whose step or aggregation is not the one the resolution names."""
        if self.bar is None:
            return (
                f"line {line}: a {step}-{aggregation} bar, but the resolution is "
                f"{self.resolution!r}, which is not a bar size"
            )
        if (step, aggregation) == self.bar:
            return None
        return (
            f"line {line}: a {step}-{aggregation} bar, but the resolution is "
            f"{self.resolution!r} ({self.bar[0]}-{self.bar[1]})"
        )

    def data_type(self, name: str, line: int) -> str | None:
        """A subscription or handler for a type the hypothesis does not require."""
        wanted = HANDLERS.get(name)
        if wanted is None or wanted in self.required:
            return None
        return (
            f"line {line}: {name} reads {wanted} data, which is not required "
            f"({', '.join(sorted(self.required))})"
        )


def _constant(stated: _Stated, node: ast.Constant) -> list[str]:
    """What one string literal says about the universe and about the resolution."""
    if not isinstance(node.value, str):
        return []
    matched = _BAR_TYPE.match(node.value)
    if matched is not None:
        found = (
            stated.instrument(matched["instrument"], node.lineno),
            stated.specification(int(matched["step"]), matched["aggregation"], node.lineno),
        )
        return [problem for problem in found if problem is not None]
    if _INSTRUMENT.match(node.value):
        problem = stated.instrument(node.value, node.lineno)
        return [] if problem is None else [problem]
    return []


def _call(stated: _Stated, node: ast.Call) -> list[str]:
    """What a `BarSpecification(step, BarAggregation.UNIT, ...)` call says."""
    called = node.func
    name = (
        called.attr
        if isinstance(called, ast.Attribute)
        else called.id
        if isinstance(called, ast.Name)
        else None
    )
    if name != _SPECIFICATION or len(node.args) < 2:
        return []
    step, aggregation = node.args[0], node.args[1]
    if not isinstance(step, ast.Constant) or not isinstance(step.value, int):
        return []
    if not isinstance(aggregation, ast.Attribute):
        return []
    if not isinstance(aggregation.value, ast.Name) or aggregation.value.id != _AGGREGATION_ENUM:
        return []
    problem = stated.specification(step.value, aggregation.attr, node.lineno)
    return [] if problem is None else [problem]


def problems(hyp: Hypothesis, source: bytes) -> list[str]:
    """Every way this source departs from the hypothesis it claims to test, in file order."""
    try:
        tree = ast.parse(source.decode("utf-8", errors="replace"), filename="strategy.py")
    except SyntaxError as exc:
        return [f"strategy.py does not parse: {exc.msg} at line {exc.lineno}"]
    stated = _Stated.of(hyp)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            found.extend(_constant(stated, node))
        elif isinstance(node, ast.Call):
            found.extend(_call(stated, node))
        elif isinstance(node, ast.Attribute):
            problem = stated.data_type(node.attr, node.lineno)
            if problem is not None:
                found.append(problem)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            problem = stated.data_type(node.name, node.lineno)
            if problem is not None:
                found.append(problem)
    return sorted(set(found), key=found.index)


def align_static(hyp: Hypothesis, source: bytes) -> tuple[bool, str | None]:
    """Whether this source still tests `hyp`, and the reasons it does not.

    The reason is capped at the length the model half is allowed, so a caller records one
    shape whichever half produced it.
    """
    found = problems(hyp, source)
    if not found:
        return True, None
    reason = "; ".join(found)
    if len(reason) > REASON_LIMIT:
        reason = reason[: REASON_LIMIT - 1].rstrip() + "…"
    return False, reason


# --- the model half, and what a drift costs ----------------------------------

TASK: Final = "align_check"
"""The task class the model half is routed under."""

BASELINE_SEQ: Final = 1
"""The run's baseline card, which no check judges: no model proposed those bytes."""

ALIGNED: Final = "aligned"
DRIFTED: Final = "drifted"
"""The two events one check can append, under the hypothesis id as subject."""

MISALIGNED: Final = "misaligned"
"""The escalation kind a drift raises."""

_NO_REASON: Final = "the model reported drift without giving a reason"


@dataclass(frozen=True)
class Checkpoint:
    """Where the last check of this run left it: how many cards it covered, on what bytes,
    and whether it judged them or found nothing there to judge.

    The run's own beginning is a checkpoint that judged nothing, on the bytes it was handed.
    """

    cards: int
    sha: str
    judged: bool = False


def lane_strategy(store: StateStore, run: RunRecord, directory: Path) -> bytes:
    """The lane's `strategy.py`, restored from its blob when the directory lost it.

    A run whose lane copy vanished has not lost anything — every version it ever held is
    a blob — so the file is written back and the caller carries on rather than failing.
    """
    path = directory / STRATEGY_FILE
    if not path.is_file():
        lanes.restore(store, directory, {STRATEGY_FILE: run.best_sha or run.base_sha})
    return path.read_bytes()


def checkpoint(store: StateStore, run: RunRecord) -> Checkpoint:
    """The last check of this run, or the run's own beginning when it has had none."""
    found = Checkpoint(cards=0, sha=run.base_sha)
    for event in store.events(subject=run.hyp_id):
        if event.kind not in (ALIGNED, DRIFTED):
            continue
        if str(event.detail.get("run_id", "")) != run.run_id:
            continue
        found = Checkpoint(
            cards=int(str(event.detail["cards"])),
            sha=str(event.detail["sha"]),
            judged=bool(event.detail.get("judged", True)),
        )
    return found


def since(store: StateStore, run: RunRecord) -> int:
    """How many cards this run has recorded since its last alignment check."""
    return _card_count(store, run) - checkpoint(store, run).cards


def check(
    ws: Workspace, store: StateStore, hyp_id: str, lane: str = lanes.DEFAULT_LANE
) -> tuple[bool, str | None]:
    """Check the active run's `strategy.py` against its thesis, and rewind it on drift.

    The deterministic checks run first, whatever the lane holds, so a drift the syntax
    tree can prove costs nothing. The model is asked only when they pass and the lane holds
    bytes nothing has answered for — neither the run's base nor the bytes the last check
    left it on. A lane on either is recorded as a check that judged nothing, whose
    checkpoint stops short of any keep no check has answered for. A verdict marks the cards
    since the last check and is an event, and a drift also marks every card that carried
    the drifted bytes, reverts the lane copy, re-points `best`, rewrites the workspace copy
    and escalates. The lane's bytes are stored either way, since the next check differences
    from them.
    """
    run = records.require_active(store, hyp_id, lanes.check_lane(lane))
    hyp = parse_yaml(
        Hypothesis, store.get_blob(run.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    directory = ws.root / run.dir
    source = lane_strategy(store, run, directory)
    sha = store.put_blob(source)
    mark = checkpoint(store, run)
    counted = _card_count(store, run)
    ok, reason = align_static(hyp, source)
    if ok and sha in (run.base_sha, mark.sha):
        pending = _unanswered_keep(store, run, mark)
        store.event(
            ALIGNED,
            hyp_id,
            {
                "run_id": run.run_id,
                "cards": counted if pending is None else pending - 1,
                "sha": sha,
                "judged": False,
            },
        )
        return True, None
    if ok:
        ok, reason = _ask(ws, store, hyp, source, mark, lane)
    _mark(store, run, mark.cards, aligned=ok)
    if ok:
        store.event(
            ALIGNED, hyp_id, {"run_id": run.run_id, "cards": counted, "sha": sha, "judged": True}
        )
        return True, None
    _mark_bytes(store, run, sha)
    restored = _revert(ws, store, run, directory)
    store.event(
        DRIFTED,
        hyp_id,
        {"run_id": run.run_id, "cards": counted, "sha": restored, "reason": reason},
    )
    escalate(
        ws,
        store,
        MISALIGNED,
        hyp_id,
        f"{hyp_id} drifted from its thesis and was rewound to {restored[:7]}: {reason}",
        actions=f"kanso research show {hyp_id} --sha {restored[:7]} · kanso align check {hyp_id}",
    )
    return False, reason


def _ask(
    ws: Workspace,
    store: StateStore,
    hyp: Hypothesis,
    source: bytes,
    mark: Checkpoint,
    lane: str,
) -> tuple[bool, str | None]:
    """The model half: the thesis, the file, and what has changed since the last check."""
    inputs = CallInputs(
        subject=hyp.id,
        stable={
            "thesis": hyp.thesis,
            "mechanism": hyp.mechanism,
            "universe": sorted(hyp.universe),
            "horizon": hyp.horizon,
        },
        dynamic={
            STRATEGY_FILE: source.decode("utf-8", errors="replace"),
            "diff_since_last_check": diffs.unified(store.get_blob(mark.sha), source),
        },
    )
    answer = route(ws, store, TASK, inputs, lane=lane)
    if bool(answer.data["aligned"]):
        return True, None
    return False, str(answer.data["reason"]).strip() or _NO_REASON


def _revert(ws: Workspace, store: StateStore, run: RunRecord, directory: Path) -> str:
    """Rewind to the last aligned keep of this run, or to the bytes it began with.

    The run's best is the run's to clear; the hypothesis's is cleared only when this run
    earned it, so a rewind in a run whose baseline discarded leaves a best another run
    earned standing. The workspace `strategy.py` follows the hypothesis's best, not the
    run's: it is rewritten when the best is now the bytes rewound to, or when there is no
    best left, and a best another run earned keeps its file.
    """
    keep = _last_aligned_keep(store, run)
    if keep is None:
        sha = run.base_sha
        store.connection.execute(
            "UPDATE runs SET best_sha = NULL, best_metric = NULL WHERE run_id = ?", (run.run_id,)
        )
        records.unset_best(store, run.hyp_id, run_id=run.run_id)
    else:
        sha, metric = keep
        records.set_best(store, run, sha, metric)
    source = store.get_blob(sha)
    lanes.write_atomic(directory / STRATEGY_FILE, source)
    if records.best_of(store, run.hyp_id)[0] in (sha, None):
        lanes.write_atomic(hypothesis_dir(ws, run.hyp_id) / STRATEGY_FILE, source)
    return sha


def _last_aligned_keep(store: StateStore, run: RunRecord) -> tuple[str, float] | None:
    """The newest keep of this run that a check has not marked drifted."""
    row = store.connection.execute(
        "SELECT strategy_sha, metric FROM cards WHERE run_id = ? AND status = 'keep'"
        " AND aligned = 1 ORDER BY seq DESC LIMIT 1",
        (run.run_id,),
    ).fetchone()
    return None if row is None else (str(row["strategy_sha"]), float(row["metric"]))


def _mark(store: StateStore, run: RunRecord, after: int, *, aligned: bool) -> None:
    """Mark every card *proposed* since the last check with this check's verdict.

    Never the run's baseline card. A check judges what the loop proposed, and no model
    proposed the bytes a run was handed: they are its base, and the run exists to climb
    from them. Marking that card drifted is what left a rewound run with nothing to fall
    back on — `_last_aligned_keep` looks for a keep with `aligned = 1`, the baseline is
    the only keep a young run has, and without it `_revert` clears the best and the next
    card that passes its constraints keeps at any metric at all. Reachable only when a
    run's *first* check drifts, which is why runs that had already been checked survived
    the same verdict untouched.
    """
    store.connection.execute(
        "UPDATE cards SET aligned = ? WHERE run_id = ? AND seq > ?",
        (int(aligned), run.run_id, max(after, BASELINE_SEQ)),
    )


def _mark_bytes(store: StateStore, run: RunRecord, sha: str) -> None:
    """Mark drifted every card of this run, bar the baseline, that carried these bytes.

    `_mark` reaches the cards since the last check. A keep before it was covered by a
    verdict on the file the lane held then, which in the loop is that keep or one built on
    it; a lane rewritten by hand can hand that pass to a keep it never held. Once the keep's
    own bytes are found drifted, `_last_aligned_keep` must not return it, or the rewind lands
    on the file it was judging. The baseline is left for the reason `_mark` gives.
    """
    store.connection.execute(
        "UPDATE cards SET aligned = 0 WHERE run_id = ? AND seq > ? AND strategy_sha = ?",
        (run.run_id, BASELINE_SEQ, sha),
    )


def _unanswered_keep(store: StateStore, run: RunRecord, mark: Checkpoint) -> int | None:
    """The first keep since the last check whose bytes no check has answered for, if any.

    A check that judges nothing must not count such a keep as checked. The base and the
    last check's bytes are answered for; any other keep past the checkpoint is a proposal
    no model has seen, which the loop never takes out of the lane but an operator who
    writes older bytes back by hand does.
    """
    row = store.connection.execute(
        "SELECT MIN(seq) FROM cards WHERE run_id = ? AND seq > ? AND status = 'keep'"
        " AND strategy_sha NOT IN (?, ?)",
        (run.run_id, max(mark.cards, BASELINE_SEQ), run.base_sha, mark.sha),
    ).fetchone()
    return None if row[0] is None else int(row[0])


def _card_count(store: StateStore, run: RunRecord) -> int:
    row = store.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    return int(row[0])
