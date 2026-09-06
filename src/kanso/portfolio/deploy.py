"""Deployment: admitting composed versions to a stage, funding them, and restarting the node.

`deploy --stage S` is the only way a version reaches a stage. It admits what composition
produced, applies the capital rule, validates what the stage's execution client declares,
renders the node configuration and (re)starts the node. Each step is a refusal waiting to
happen, and each refusal exists because the alternative is a book nobody intended.

* **A halted stage stays halted.** The kill switch is the operator's, and a deployment that
  cleared it by starting a node would make the switch advisory.
* **An engine pin must match.** A version was certified under one engine; running it under
  another is running something that was never measured. The way out is a plain `cert run` on
  the same commit under the new engine, not a deployment that looks away.
* **A stage needs data at or after the forward window's start.** The forward window is what a
  deployed book lives in and the only window nothing may backtest, so a stage with no data in
  it has nothing to trade and nothing to be judged on.
* **A wall-clock execution client cannot be fed a replay, or run at any speed but one.** A
  broker matches against current prices; feeding it history would fill orders at prices
  unrelated to the data that triggered them.
* **Real capital needs a named approval on record, per version.** `deploy --stage live`
  refuses a version that has none, so editing `portfolio.yaml` by hand can never move real
  money — the file says what is deployed, and the approval says what was allowed.
* **A stage node executes against the simulated venue, so a wall-clock client is refused.**
  The node this version builds is a bounded run: it releases what the catalog holds that the
  stage has not replayed, flattens and returns — which is what a `clock: replay` client
  declares it is fed by. A `clock: wall` client fills against current prices and needs a node
  that outlives the command; running one through this node would fill every order in
  simulation while the stage record — and the gates that read it — called the money the
  broker's. The refusal comes after the approval check, so real capital still fails first for
  the approval it is missing rather than for the node it would have run in.

**Capital is inherited, then rationed.** A new version of a strategy the stage already holds
takes its predecessor's share; anything else takes the largest slice the limits leave. A
strategy that would be funded at nothing is not deployed at all: it is escalated as
`deploy_blocked`, because a version trading no size produces no evidence and would sit on a
stage forever without ever becoming promotable.

**A node flattens before every stop, so a stage always restarts flat.** Simulated execution
keeps no position across a restart, and the alternative — a record claiming a book the new
node does not hold — loses positions silently. Each redeploy therefore closes a window and
realises its result into the record the paper and live gates read, which is also why a
redeploy shortens the window those gates have to work with.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final

from kanso import strategy as strategies
from kanso.criteria.run import day_of
from kanso.env.envelope import engine_version
from kanso.errors import ApprovalError, PreconditionError
from kanso.inbox import escalate
from kanso.nautilus import node
from kanso.nautilus.node import Placement, StageNode, StageRun
from kanso.portfolio import files, records
from kanso.portfolio.capital import assign
from kanso.portfolio.clients import check_runnable
from kanso.portfolio.clients import get as exec_client
from kanso.replay import record
from kanso.replay.record import Intent, Point, Session
from kanso.replay.run import last_day
from kanso.replay.target import Target
from kanso.replay.target import resolve as resolve_target
from kanso.schemas import Limits, Stage, StrategyFile, StrategyVersion, check_execution_client
from kanso.schemas.strategy import PAPER_STATES
from kanso.strategy import files as strategy_files

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.portfolio.records import StageResult
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "BLOCKED",
    "LIVE",
    "PAPER",
    "RETIRED",
    "Admitted",
    "Deployment",
    "clock_of",
    "deploy",
    "served_to",
]

BLOCKED: Final = "deploy_blocked"
"""The escalation a stage with no capital left to give produces."""

PAPER: Final = "paper"
LIVE: Final = "live"
RETIRED: Final = "retired"


@dataclass(frozen=True)
class Admitted:
    """One version this deployment put on the stage, and the money it was given."""

    strategy_id: str
    version: int
    capital: float
    joined_at: datetime
    replaced: int | None = None

    @property
    def label(self) -> str:
        """How this version is named to an operator."""
        return records.subject_of(self.strategy_id, self.version)


@dataclass(frozen=True)
class Deployment:
    """What one `deploy` did: what is on the stage, what left it, and what the node ran."""

    stage: str
    admitted: tuple[Admitted, ...] = ()
    retired: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    session: Session | None = None
    results: tuple[StageResult, ...] = ()
    halted: str | None = None

    @property
    def capital(self) -> float:
        """The stage capital this deployment committed."""
        return sum(one.capital for one in self.admitted)


def deploy(ws: Workspace, store: StateStore, stage: str) -> Deployment:
    """Admit, fund and run this stage, refusing everything that would trade the wrong thing."""
    portfolio = files.read(ws)
    configured = files.stage_of(portfolio, stage)
    if configured.kill_switch:
        raise PreconditionError(
            f"stages.{stage}.kill_switch is on, so the stage is halted and nothing deploys to it",
            remedy=f"clear stages.{stage}.kill_switch in portfolio.yaml, then deploy again",
        )
    spec = exec_client(configured.exec, ws)
    check_execution_client(
        stage, spec, data_client=configured.data, speed=configured.speed, approved=True
    )
    chosen = _candidates(ws, stage)
    _check_pins(chosen)
    chosen = tuple(replace(one, target=_target(ws, store, one)) for one in chosen)
    _check_data(ws, chosen)
    if spec.capital == "real":
        _check_approvals(store, stage, chosen)
    check_runnable(stage, spec)

    admitted, blocked = _fund(ws, store, stage, portfolio.limits, chosen)
    if not admitted:
        return Deployment(stage=stage, blocked=tuple(blocked))
    ran = _restart(
        ws, store, stage, files.stage_of(files.read(ws), stage), portfolio.limits, chosen, admitted
    )
    store.event(
        records.DEPLOYED,
        stage,
        {
            "stage": stage,
            "exec": spec.id,
            "data": configured.data,
            "speed": configured.speed,
            "strategies": [one.label for one in admitted],
            "capital": sum(one.capital for one in admitted),
            "session": None if ran.session is None else ran.session.session_id,
        },
    )
    return Deployment(
        stage=stage,
        admitted=tuple(admitted),
        retired=tuple(
            records.subject_of(one.strategy_id, one.replaced)
            for one in admitted
            if one.replaced is not None
        ),
        blocked=tuple(blocked),
        session=ran.session,
        results=ran.results,
        halted=ran.halted,
    )


def clock_of(store: StateStore, stage: str) -> int | None:
    """The stage's session clock: the availability instant its last node reached.

    Held as digits rather than as a timestamp, because a nanosecond does not survive a
    `datetime` and losing the last one means resuming a nanosecond early or late.
    """
    row = store.connection.execute(
        "SELECT clock_ts FROM sessions WHERE mode = ? AND clock_ts IS NOT NULL"
        " ORDER BY started_at DESC, session_id DESC LIMIT 1",
        (stage,),
    ).fetchone()
    return None if row is None else int(row[0])


# --- what the stage admits ----------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One strategy's answer to "what runs on this stage?", and what it displaces."""

    file: StrategyFile
    version: StrategyVersion
    replaced: StrategyVersion | None
    state: str
    target: Target | None = None

    @property
    def subject(self) -> str:
        """How this version is named in a refusal and an escalation."""
        return records.subject_of(self.file.id, self.version.version)

    @property
    def resolved(self) -> Target:
        """The pinned target, once it has been resolved."""
        if self.target is None:  # pragma: no cover - resolved before every use of it
            raise PreconditionError(f"{self.subject} was not resolved before it was deployed")
        return self.target


def _candidates(ws: Workspace, stage: str) -> tuple[_Candidate, ...]:
    """Every version this stage will hold after the deployment, and what each replaces.

    The paper stage admits composition's output: the newest composed version of each
    strategy, replacing whatever that strategy already had on paper. The live stage admits
    nothing on its own — a version reaches it through `promote` and no other way — so
    deploying it renders and restarts what promotion already moved there.
    """
    found: list[_Candidate] = []
    for file in strategies.strategies(ws):
        if stage == LIVE:
            live = file.deployed(LIVE)
            if live is not None:
                found.append(_Candidate(file, live, None, LIVE))
            continue
        current = next((v for v in file.versions if v.state in PAPER_STATES), None)
        composed = next(
            (v for v in reversed(file.versions) if v.state == strategies.COMPOSED), None
        )
        if composed is not None and (current is None or composed.version > current.version):
            found.append(_Candidate(file, composed, current, PAPER))
        elif current is not None:
            found.append(_Candidate(file, current, None, current.state))
    return tuple(found)


def _target(ws: Workspace, store: StateStore, candidate: _Candidate) -> Target:
    """What this version runs as: its implementation, its pinned hypothesis and its model."""
    return resolve_target(ws, store, strategy=candidate.file.id, version=candidate.version.version)


def _check_pins(chosen: tuple[_Candidate, ...]) -> None:
    """Refuse a version certified under an engine other than the installed one."""
    installed = engine_version()
    for candidate in chosen:
        pinned = candidate.version.pins.nautilus_version
        if pinned != installed:
            raise PreconditionError(
                f"{candidate.subject} was certified under nautilus_trader {pinned} and this "
                f"workspace runs {installed}; a version is deployed only on the engine it was "
                "measured on",
                remedy=f"run `kanso cert run {candidate.version.sleeve.hyp_id}` under this "
                "engine, compose it, and deploy again",
            )


def _check_data(ws: Workspace, chosen: tuple[_Candidate, ...]) -> None:
    """Refuse a stage whose catalog holds nothing at or after the forward window's start."""
    for candidate in chosen:
        universe = candidate.resolved.universe
        opens = candidate.resolved.hyp.windows.forward.start
        served = served_to(ws, universe)
        if served is None or served < opens:
            held = "nothing" if served is None else f"data only to {served}"
            raise PreconditionError(
                f"data: {candidate.subject} trades a forward window opening {opens} and the "
                f"catalog holds {held} for {', '.join(universe)}",
                remedy="run `kanso data sync` for the universe, then deploy again",
            )


def served_to(ws: Workspace, universe: tuple[str, ...]) -> date | None:
    """The last day the catalog serves for a universe, or nothing when it serves none.

    Read from the manifests, which record what a source actually served rather than what
    was asked of it, so a stage ends where its data ends. A universe the catalog holds
    nothing for is not an error here — it is one of the things `deploy` refuses, and it is
    what `show` reports as a stage with no data behind it.
    """
    if not universe:
        return None
    try:
        return last_day(ws, universe)
    except PreconditionError:
        return None


def _check_approvals(store: StateStore, stage: str, chosen: tuple[_Candidate, ...]) -> None:
    """Refuse real capital for any version with no named approval on record."""
    for candidate in chosen:
        if records.approved(store, candidate.file.id, candidate.version.version):
            continue
        raise ApprovalError(
            f"stages.{stage}.exec trades real capital and {candidate.subject} has no recorded "
            "operator approval; an entry in portfolio.yaml is not an approval",
            remedy=f"kanso promote {candidate.file.id} --live --as NAME",
        )


# --- funding and writing ------------------------------------------------------


def _fund(
    ws: Workspace,
    store: StateStore,
    stage: str,
    limits: Limits,
    chosen: tuple[_Candidate, ...],
) -> tuple[list[Admitted], list[str]]:
    """Give every admitted version its share, write the file and the rows, and report both.

    The stage is rebuilt strategy by strategy, so each allocation sees the money the ones
    before it took; the file is written once at the end, and only when the rebuilt stage
    differs from the one on disk, so a refusal partway through leaves it exactly as it was
    and a redeploy that changes nothing does not rewrite an operator's file.
    """
    joined = datetime.now(tz=UTC)
    portfolio = files.read(ws)
    working = files.stage_of(portfolio, stage)
    admitted: list[Admitted] = []
    blocked: list[str] = []
    for candidate in chosen:
        strategy_id = candidate.file.id
        version = candidate.version.version
        share = assign(working, limits, strategy_id)
        held = files.held(working, strategy_id)
        if share <= 0:
            blocked.append(candidate.subject)
            escalate(
                ws,
                store,
                BLOCKED,
                candidate.subject,
                f"stages.{stage} has no capital left to fund {candidate.subject}",
            )
            continue
        joined_at = held.joined_at if held is not None and held.version == version else joined
        working = files.place(working, strategy_id, version, share, joined_at)
        admitted.append(
            Admitted(
                strategy_id=strategy_id,
                version=version,
                capital=share,
                joined_at=joined_at,
                replaced=None if candidate.replaced is None else candidate.replaced.version,
            )
        )
    if working != files.stage_of(portfolio, stage):
        files.write(ws, files.with_stage(portfolio, stage, working))
    _restate(ws, store, stage, chosen, admitted)
    return admitted, blocked


def _restate(
    ws: Workspace,
    store: StateStore,
    stage: str,
    chosen: tuple[_Candidate, ...],
    admitted: list[Admitted],
) -> None:
    """Write the version states into the strategy files and into the state rows together."""
    by_strategy = {one.strategy_id: one for one in admitted}
    for candidate in chosen:
        placed = by_strategy.get(candidate.file.id)
        if placed is None:
            continue
        updates: dict[int, str] = {}
        if candidate.replaced is not None:
            updates[candidate.replaced.version] = RETIRED
            records.clear_stage(store, candidate.file.id, stage)
            records.set_stage(
                store, candidate.file.id, candidate.replaced.version, state=RETIRED, stage=None
            )
        updates[placed.version] = candidate.state
        records.set_stage(
            store,
            candidate.file.id,
            placed.version,
            state=candidate.state,
            stage=stage,
            capital=placed.capital,
            joined_at=placed.joined_at,
        )
        strategy_files.write(ws, restated(candidate.file, updates))


def restated(file: StrategyFile, updates: dict[int, str]) -> StrategyFile:
    """The strategy file with these versions moved to these states."""
    return file.model_copy(
        update={
            "versions": [
                version
                if version.version not in updates
                else version.model_copy(update={"state": updates[version.version]})
                for version in file.versions
            ]
        }
    )


# --- restarting the node ------------------------------------------------------


@dataclass(frozen=True)
class _Ran:
    """What the stage node left behind."""

    session: Session | None = None
    results: tuple[StageResult, ...] = ()
    halted: str | None = None


def _restart(
    ws: Workspace,
    store: StateStore,
    stage: str,
    configured: Stage,
    limits: Limits,
    chosen: tuple[_Candidate, ...],
    admitted: list[Admitted],
) -> _Ran:
    """Build this stage's node, run it over what it has not replayed yet, and record it."""
    targets = {candidate.file.id: candidate.resolved for candidate in chosen}
    placements = tuple(_placement(ws, targets[one.strategy_id], one) for one in admitted)
    clock = clock_of(store, stage)
    opens = _opens(placements, clock)
    closes = max(served_to(ws, tuple(placed.hyp.universe)) or opens for placed in placements)
    if closes < opens:
        return _Ran()
    ran = node.run(
        StageNode(
            stage=stage,
            capital=configured.capital,
            limits=limits,
            placements=placements,
            window=(opens, closes),
            catalog=targets[admitted[0].strategy_id].catalog,
            after=clock,
        )
    )
    session = _session(ws, store, stage, configured, placements, ran)
    return _Ran(
        session=session,
        results=tuple(records.record_stage_run(store, stage, session.session_id, ran.realised)),
        halted=ran.halted,
    )


def _opens(placements: tuple[Placement, ...], clock: int | None) -> date:
    """The day this stage resumes on: its clock's day, or the earliest forward start."""
    if clock is not None:
        return day_of(clock)
    return min(placed.hyp.windows.forward.start for placed in placements)


def _placement(ws: Workspace, target: Target, admitted: Admitted) -> Placement:
    """One deployed version as the node runs it, funded with what deployment gave it."""
    return Placement(
        strategy_id=admitted.strategy_id,
        version=admitted.version,
        capital=admitted.capital,
        hyp=target.hyp,
        venue_model=target.venue_model,
        snapshot_id=target.snapshot_id,
        period=target.period,
        source=target.strategy_source,
        loaded=strategies.load(ws, admitted.strategy_id, admitted.version),
    )


def _session(
    ws: Workspace,
    store: StateStore,
    stage: str,
    configured: Stage,
    placements: tuple[Placement, ...],
    ran: StageRun,
) -> Session:
    """Write the stage's session, which is what carries its clock to the next restart."""
    started = record.now()
    universe = sorted({name for placed in placements for name in placed.hyp.universe})
    made = Session.model_validate(
        {
            "session_id": record.session_id(stage, stage, ran.window, started),
            "mode": stage,
            "target": stage,
            "instruments": universe,
            "from": ran.window[0],
            "to": ran.window[1],
            "speed": configured.speed,
            "exec": configured.exec,
            "released": ran.released,
            "intents": len(ran.intents),
            "clock_ns": ran.clock_ns,
            "started_at": started,
            "ended_at": record.now(),
        }
    )
    written = record.write(
        ws,
        made,
        (Point.of(point) for point in ran.points[: ran.released]),
        (Intent.of(row) for row in ran.intents),
    )
    record.insert(store, written)
    return written
