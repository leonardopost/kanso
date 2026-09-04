"""One monitoring pass: judge every deployed version, and enforce what only a pass can see.

Every `[monitor] interval` this runs once. For each deployed version it evaluates the paper or
live gates of its **sleeve hypothesis's** plan — the plan that decided what proof this idea
needs, applied now to a stage rather than to a backtest — with the bands taken from the
version's own expectation. What follows from the verdicts is fixed, because a rule an operator
has to be woken up to apply is not a rule:

* **paper, all passing → `promotable`**, and an escalation, because moving to real capital is
  the one decision the framework will not take by itself;
* **live, any failing → demoted**, and an escalation saying so after the fact;
* **live, `daily_loss_kill` failing → the stage's kill switch**, and an escalation, but *not* a
  demotion. Halting is the stronger action: it cancels and flattens everything on the stage
  rather than moving one version off it, and demoting a version into a halted stage would
  change nothing about the money while making the two mechanisms race each other.

A version is promoted only when at least one paper gate actually judged. A gate that could not
reach its context passes and says so, which is right for a certificate — the verdict is the
conjunction of the tests that ran — but a version whose every gate skipped has been tested by
nothing, and "no evidence against" is not the same as "ready for real money".

**Stage exposure is enforced here because here is the only place it can be.** Gross and net
are properties of every deployed version at once, and no version sees the others. The engine's
own risk configuration is a per-order, per-instrument backstop that cannot express a
per-strategy limit, let alone a net one, so each pass sums the books its stages last recorded
and halts a stage on a breach. The percentage limits are turned into money once and compared
in money, so a stage funded with nothing has no room for a position rather than an undefined
percentage.

**Actions are taken once, on the transition.** A version already `promotable` is not
re-escalated and a stage already halted is not halted again: the pass runs every few minutes,
and an escalation repeated every few minutes is an inbox nobody reads.

Demotion is the portfolio's act, not this module's, because it moves a version *and* redeploys
the stages whose switch is off — so it is called through, and a caller that wants to observe a
pass without one hands in its own.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from kanso.certify.plan import read_plan
from kanso.criteria import GateContext, gates
from kanso.criteria.run import day_of
from kanso.errors import KansoError
from kanso.inbox import escalate
from kanso.monitor.realised import Tenure, tenure
from kanso.monitor.stage import UNKNOWN, StageRecord
from kanso.portfolio import clients, files, records
from kanso.portfolio.deploy import clock_of, restated
from kanso.portfolio.promote import demote as portfolio_demote
from kanso.schemas import (
    CertificationPlan,
    GateResult,
    Hypothesis,
    Portfolio,
    StrategyFile,
    StrategyVersion,
    parse_duration,
    parse_yaml,
)
from kanso.schemas.portfolio import STAGES, Deployment
from kanso.strategy import files as strategy_files

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "DAILY_LOSS",
    "DEMOTED",
    "DEPLOY_BLOCKED",
    "HALTED",
    "PROMOTABLE",
    "Exposure",
    "Outcome",
    "halt",
    "mark",
    "paper_window_s",
    "run_once",
]

PAPER: Final = "paper"
LIVE: Final = "live"

DAILY_LOSS: Final = "daily_loss_kill"
"""The one live gate whose failure halts the stage instead of demoting the version."""

PROMOTABLE: Final = "promotable"
DEMOTED: Final = "demoted"
HALTED: Final = "halted"
DEPLOY_BLOCKED: Final = "deploy_blocked"
ON_PAPER: Final = "paper"
"""The version state a paper version holds until its gates have passed."""

PERCENT: Final = 100.0

NO_VERSION: Final = "the workspace holds no such composed version"
NO_BOOK: Final = "the stage has closed no window for it yet, so there is nothing to judge"
UNIMPLEMENTED: Final = "this version of kanso has no implementation for it"

Demote = Callable[["Workspace", "StateStore", str, int], object]
"""How a failing live version is moved off its stage: the portfolio's `demote`."""


@dataclass(frozen=True)
class Exposure:
    """One stage's book against the two limits only a whole-stage view can enforce."""

    stage: str
    gross: float
    net: float
    capital: float
    max_gross: float
    max_net: float

    @property
    def breached(self) -> bool:
        """Whether either limit is exceeded; net is judged on its size, not its sign."""
        return self.gross > self.max_gross or abs(self.net) > self.max_net

    def payload(self) -> dict[str, object]:
        """The exposure as one JSON object."""
        return {
            "stage": self.stage,
            "gross": self.gross,
            "net": self.net,
            "capital": self.capital,
            "max_gross": self.max_gross,
            "max_net": self.max_net,
            "breached": self.breached,
        }


@dataclass(frozen=True)
class Outcome:
    """One judgement of one pass: a version's gates, or a stage's exposure."""

    stage: str
    strategy: str | None = None
    version: int | None = None
    gates: tuple[GateResult, ...] = ()
    exposure: Exposure | None = None
    actions: tuple[str, ...] = ()
    escalations: tuple[str, ...] = ()
    skipped: str | None = None

    @property
    def subject(self) -> str:
        """What this judgement is about, in the words an escalation names it by."""
        if self.strategy is None:
            return self.stage
        return records.subject_of(self.strategy, int(self.version or 0))

    @property
    def judged(self) -> tuple[GateResult, ...]:
        """The gates that reached their context; a skipped gate passes but proves nothing."""
        return tuple(result for result in self.gates if result.skipped is None)

    @property
    def passed(self) -> bool:
        """Whether every gate passed, skipped ones included."""
        return all(result.passed for result in self.gates)

    def payload(self) -> dict[str, object]:
        """The judgement as one JSON object."""
        return {
            "stage": self.stage,
            "strategy": self.strategy,
            "version": self.version,
            "gates": [result.model_dump(mode="json", by_alias=True) for result in self.gates],
            "exposure": None if self.exposure is None else self.exposure.payload(),
            "actions": list(self.actions),
            "escalations": list(self.escalations),
            "skipped": self.skipped,
        }


def run_once(ws: Workspace, store: StateStore, *, demote: Demote | None = None) -> list[Outcome]:
    """One pass: stage exposure first, then every deployed version's own stage gates.

    Exposure comes first because a breach halts the stage, and a version judged on a stage
    that is about to be halted should be judged knowing it.
    """
    move = demote or portfolio_demote
    outcomes: list[Outcome] = []
    portfolio = files.read(ws)
    for stage in STAGES:
        portfolio, outcome = _exposure_pass(ws, store, portfolio, stage)
        outcomes.append(outcome)
    work = [
        (stage, held) for stage in STAGES for held in files.stage_of(portfolio, stage).strategies
    ]
    for stage, deployment in work:
        outcomes.append(_version_pass(ws, store, stage, deployment, move))
    return outcomes


def halt(ws: Workspace, portfolio: Portfolio, stage: str) -> Portfolio:
    """Set a stage's kill switch and write it. Only the operator clears it again."""
    held = files.stage_of(portfolio, stage).model_copy(update={"kill_switch": True})
    updated = files.with_stage(portfolio, stage, held)
    files.write(ws, updated)
    return updated


def mark(ws: Workspace, store: StateStore, strategy_id: str, version: int, state: str) -> None:
    """Move one version to a state, in the file an operator reads and the row a query does.

    The stage is untouched: a version becoming `promotable` is still the version on paper,
    and only a promotion moves it anywhere.
    """
    held = strategy_files.require(ws, strategy_id)
    strategy_files.write(ws, restated(held, {version: state}))
    store.connection.execute(
        "UPDATE strategy_versions SET state = ? WHERE strategy_id = ? AND version = ?",
        (state, strategy_id, version),
    )
    store.event(
        state,
        records.subject_of(strategy_id, version),
        {"strategy": strategy_id, "version": version},
    )


def paper_window_s(plan: CertificationPlan, hyp: Hypothesis) -> float | None:
    """How long the plan required the paper window that confirmed the expectation to be.

    It is the paper gates' own floor — the longer of their absolute minimum and their
    multiple of the hypothesis's horizon — because that is the window the expectation was
    held to, and so the only live window comparable with it. A plan whose paper gates state
    no duration leaves a live drift nothing to roll over, and it says so instead.
    """
    horizon = parse_duration(hyp.horizon, "horizon").total_seconds()
    floors: list[float] = []
    for gate in plan.stage_gates(PAPER):
        floor = 0.0
        duration = gate.params.get("min_duration")
        if isinstance(duration, str):
            floor = max(floor, parse_duration(duration, "min_duration").total_seconds())
        multiple = gate.params.get("horizon_mult")
        if isinstance(multiple, int | float) and not isinstance(multiple, bool):
            floor = max(floor, float(multiple) * horizon)
        if floor > 0:
            floors.append(floor)
    return max(floors) if floors else None


# --- exposure ----------------------------------------------------------------


def _exposure_pass(
    ws: Workspace, store: StateStore, portfolio: Portfolio, stage: str
) -> tuple[Portfolio, Outcome]:
    """Sum the books the stage last recorded, and halt it the first pass that finds it over."""
    held = files.stage_of(portfolio, stage)
    books = _tenures(store, stage, held.strategies)
    exposure = Exposure(
        stage=stage,
        gross=sum(book.gross for book in books),
        net=sum(book.net for book in books),
        capital=held.capital,
        max_gross=portfolio.limits.max_gross_pct / PERCENT * held.capital,
        max_net=portfolio.limits.max_net_pct / PERCENT * held.capital,
    )
    if not exposure.breached or held.kill_switch:
        return portfolio, Outcome(stage=stage, exposure=exposure)
    updated = halt(ws, portfolio, stage)
    store.event(HALTED, stage, exposure.payload())
    entry = escalate(
        ws,
        store,
        DEPLOY_BLOCKED,
        stage,
        f"{stage} stage exposure breach: gross {exposure.gross:,.0f} of "
        f"{exposure.max_gross:,.0f} and net {exposure.net:,.0f} of {exposure.max_net:,.0f}; "
        "the kill switch is on and the stage is halted",
    )
    return updated, Outcome(
        stage=stage,
        exposure=exposure,
        actions=(HALTED,),
        escalations=(entry.escalation_id,),
    )


def _tenures(store: StateStore, stage: str, deployed: Sequence[Deployment]) -> list[Tenure]:
    """What every version on a stage has done there, for the versions that have done any."""
    clock = clock_of(store, stage)
    found = [_tenure(store, stage, entry, clock) for entry in deployed]
    return [one for one in found if one is not None]


def _tenure(
    store: StateStore, stage: str, deployment: Deployment, clock: int | None
) -> Tenure | None:
    return tenure(
        stage,
        records.stage_results(
            store, strategy_id=deployment.id, version=deployment.version, stage=stage
        ),
        clock,
    )


# --- one version -------------------------------------------------------------


def _version_pass(
    ws: Workspace, store: StateStore, stage: str, deployment: Deployment, move: Demote
) -> Outcome:
    """Judge one deployed version, never letting one bad version end the pass."""
    try:
        return _judge(ws, store, stage, deployment, move)
    except KansoError as error:
        return Outcome(
            stage=stage,
            strategy=deployment.id,
            version=deployment.version,
            skipped=error.message,
        )


def _judge(
    ws: Workspace, store: StateStore, stage: str, deployment: Deployment, move: Demote
) -> Outcome:
    """Evaluate this version's stage gates and take whatever the verdicts require."""
    strategy = strategy_files.read(ws, deployment.id)
    version = None if strategy is None else _version_of(strategy, deployment.version)
    if version is None:
        return _nothing(stage, deployment, NO_VERSION)
    held = _tenure(store, stage, deployment, clock_of(store, stage))
    if held is None:
        return _nothing(stage, deployment, NO_BOOK)
    hyp = _sleeve(store, version.sleeve.hyp_id)
    if hyp is None:
        return _nothing(stage, deployment, f"the sleeve {version.sleeve.hyp_id} has no pinned copy")
    plan = read_plan(ws, hyp.id)
    planned = [] if plan is None else [gate for gate in plan.gates if gate.stage == stage]
    if plan is None or not planned:
        return _nothing(stage, deployment, f"{hyp.id} has no plan naming a {stage} gate")
    portfolio = files.read(ws)
    record = _record(ws, store, portfolio, stage, held, plan, hyp)
    registry = gates()
    results: list[GateResult] = []
    for gate in planned:
        found = registry.get(gate.id)
        if found is None:
            results.append(
                GateResult.model_validate({"id": gate.id, "pass": True, "skipped": UNIMPLEMENTED})
            )
            continue
        results.append(
            found.evaluate(
                GateContext(
                    hyp=hyp,
                    construct="" if hyp.construct is None else hyp.construct.id,
                    stage=stage,
                    params=dict(gate.params),
                    window=held.run.window,
                    run=held.run,
                    research_folds=ws.config.research.folds,
                    snapshot_id=version.pins.snapshot_id,
                    strategy_sha=version.sleeve.strategy_sha,
                    expectation=version.expectation.model_dump(mode="json"),
                    session=record,
                )
            )
        )
    outcome = Outcome(
        stage=stage,
        strategy=deployment.id,
        version=deployment.version,
        gates=tuple(results),
    )
    if stage == PAPER:
        return _paper(ws, store, outcome, version)
    return _live(ws, store, outcome, move)


def _paper(ws: Workspace, store: StateStore, outcome: Outcome, version: StrategyVersion) -> Outcome:
    """All paper gates passing, with at least one of them having judged, is promotable."""
    if not outcome.judged or not outcome.passed or version.state != ON_PAPER:
        return outcome
    mark(ws, store, str(outcome.strategy), int(outcome.version or 0), PROMOTABLE)
    entry = escalate(
        ws,
        store,
        PROMOTABLE,
        outcome.subject,
        f"{outcome.subject} passed every paper gate "
        f"({', '.join(result.id for result in outcome.judged)}) and is ready for real capital",
    )
    return replace(outcome, actions=(PROMOTABLE,), escalations=(entry.escalation_id,))


def _live(ws: Workspace, store: StateStore, outcome: Outcome, move: Demote) -> Outcome:
    """A failing live gate demotes, except the daily loss, which halts the stage instead."""
    failed = [result for result in outcome.gates if not result.passed]
    if not failed:
        return outcome
    actions: list[str] = []
    escalations: list[str] = []
    halted = any(result.id == DAILY_LOSS for result in failed)
    if halted:
        portfolio = files.read(ws)
        if not files.stage_of(portfolio, LIVE).kill_switch:
            halt(ws, portfolio, LIVE)
            store.event(HALTED, LIVE, {"gate": DAILY_LOSS, "subject": outcome.subject})
            actions.append(HALTED)
            escalations.append(
                escalate(
                    ws,
                    store,
                    DEPLOY_BLOCKED,
                    LIVE,
                    f"{outcome.subject} lost more today than limits.daily_loss_pct allows; "
                    "the live stage kill switch is on and the stage is halted",
                ).escalation_id
            )
    others = [result for result in failed if result.id != DAILY_LOSS]
    if others:
        move(ws, store, str(outcome.strategy), int(outcome.version or 0))
        actions.append(DEMOTED)
        escalations.append(
            escalate(
                ws,
                store,
                DEMOTED,
                outcome.subject,
                f"{outcome.subject} failed the live gates "
                f"{', '.join(result.id for result in others)} and was demoted"
                + (
                    "; the live stage is halted, so clear stages.live.kill_switch to resume it"
                    if halted
                    else ""
                ),
            ).escalation_id
        )
    return replace(outcome, actions=tuple(actions), escalations=tuple(escalations))


# --- what the gates are told -------------------------------------------------


def _record(
    ws: Workspace,
    store: StateStore,
    portfolio: Portfolio,
    stage: str,
    held: Tenure,
    plan: CertificationPlan,
    hyp: Hypothesis,
) -> StageRecord:
    """The stage facts this version's gates read, assembled from the whole stage's books."""
    model = files.stage_of(portfolio, stage)
    on_stage = _tenures(store, stage, model.strategies)
    day = day_of(max((book.clock_ns for book in on_stage), default=held.clock_ns))
    spec = clients.registry(ws).get(model.exec)
    return StageRecord(
        stage=stage,
        joined_ns=held.joined_ns,
        clock_ns=held.clock_ns,
        paper_window_s=paper_window_s(plan, hyp),
        day_pnl=sum(book.day_pnl(day) for book in on_stage),
        capital=model.capital,
        daily_loss_pct=portfolio.limits.daily_loss_pct,
        funding=UNKNOWN if spec is None else spec.capital,
        n_fills=len(held.run.fills),
    )


# --- small helpers -----------------------------------------------------------


def _version_of(strategy: StrategyFile, version: int) -> StrategyVersion | None:
    return next((found for found in strategy.versions if found.version == version), None)


def _sleeve(store: StateStore, hyp_id: str) -> Hypothesis | None:
    """The sleeve hypothesis as its pin holds it, which is what was certified."""
    row = store.connection.execute(
        "SELECT hypothesis_sha FROM hypotheses WHERE hyp_id = ?", (hyp_id,)
    ).fetchone()
    sha = None if row is None else row["hypothesis_sha"]
    if sha is None or not store.has_blob(str(sha)):
        return None
    return parse_yaml(Hypothesis, store.get_blob(str(sha)).decode("utf-8"), hyp_id)


def _nothing(stage: str, deployment: Deployment, reason: str) -> Outcome:
    """A version this pass could not judge, and what it lacked."""
    return Outcome(stage=stage, strategy=deployment.id, version=deployment.version, skipped=reason)
