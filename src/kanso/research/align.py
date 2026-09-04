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

`check` is the whole check: the syntax tree first, the model only if it passed, and the
recovery if either says no. Recovery is the point. A drifted run is not stopped — research
is indefinite and stopping it on a judgement call would hand the model a veto — it is
*rewound*: the lane copy and `best` go back to the last keep this run made while it was
still aligned, or to the bytes the run began with when it made none, the cards since the
last check are marked not aligned so no later reader trusts them, and the operator is told.
The run then carries on from ground that was checked.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from hashlib import sha256
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

ALIGNED: Final = "aligned"
DRIFTED: Final = "drifted"
"""The two events one check can append, under the hypothesis id as subject."""

MISALIGNED: Final = "misaligned"
"""The escalation kind a drift raises."""

_NO_REASON: Final = "the model reported drift without giving a reason"


@dataclass(frozen=True)
class Checkpoint:
    """Where the last check of this run left it: how many cards it had, and on what bytes."""

    cards: int
    sha: str


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
        found = Checkpoint(cards=int(str(event.detail["cards"])), sha=str(event.detail["sha"]))
    return found


def since(store: StateStore, run: RunRecord) -> int:
    """How many cards this run has recorded since its last alignment check."""
    return _card_count(store, run) - checkpoint(store, run).cards


def check(
    ws: Workspace, store: StateStore, hyp_id: str, lane: str = lanes.DEFAULT_LANE
) -> tuple[bool, str | None]:
    """Check the active run's `strategy.py` against its thesis, and rewind it on drift.

    The deterministic checks run first and the model is asked only when they pass, so a
    drift the syntax tree can prove costs nothing. Either way the cards since the last
    check are marked with the verdict, the verdict is an event, and a drift also reverts
    the lane copy, re-points `best`, rewrites the workspace copy and escalates.
    """
    run = records.require_active(store, hyp_id, lanes.check_lane(lane))
    hyp = parse_yaml(
        Hypothesis, store.get_blob(run.hypothesis_sha).decode("utf-8"), HYPOTHESIS_FILE
    )
    directory = ws.root / run.dir
    source = lane_strategy(store, run, directory)
    mark = checkpoint(store, run)
    counted = _card_count(store, run)
    ok, reason = align_static(hyp, source)
    if ok:
        ok, reason = _ask(ws, store, hyp, source, mark, lane)
    _mark(store, run, mark.cards, aligned=ok)
    if ok:
        store.event(
            ALIGNED,
            hyp_id,
            {"run_id": run.run_id, "cards": counted, "sha": sha256(source).hexdigest()},
        )
        return True, None
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
    """Rewind to the last aligned keep of this run, or to the bytes it began with."""
    keep = _last_aligned_keep(store, run)
    if keep is None:
        sha = run.base_sha
        store.connection.execute(
            "UPDATE runs SET best_sha = NULL, best_metric = NULL WHERE run_id = ?", (run.run_id,)
        )
        records.unset_best(store, run.hyp_id)
    else:
        sha, metric = keep
        records.set_best(store, run, sha, metric)
    source = store.get_blob(sha)
    lanes.write_atomic(directory / STRATEGY_FILE, source)
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
    """Mark every card recorded since the last check with this check's verdict."""
    store.connection.execute(
        "UPDATE cards SET aligned = ? WHERE run_id = ? AND seq > ?",
        (int(aligned), run.run_id, after),
    )


def _card_count(store: StateStore, run: RunRecord) -> int:
    row = store.connection.execute(
        "SELECT COUNT(*) FROM cards WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    return int(row[0])
