"""Promotion and demotion: the two moves between the paper stage and the live one.

A promotion is the only act in kanso that can put money at risk, so it is the only one that
requires a person. `promote --live --as NAME` refuses without a name — there is no
environment fallback and no default, because a name read from the environment is a name
nobody chose — and it records the approval **before** it moves anything. The order matters:
`deploy --stage live` refuses a real-capital client holding a version with no approval on
record, so an approval written after the move would leave a window in which the file says
one thing and the record says another.

A demotion is the mirror, and it must never deadlock. The monitor demotes a version when a
live gate fails, and one of those gates — the daily loss — halts the stage instead. A
demotion that insisted on restarting every stage would then be trying to start a node the
kill switch exists to keep stopped. So the move is made first and only the stages whose
switch is off are redeployed; a halted stage is named in the escalation together with the
command that resumes it, and it stays halted until the operator says otherwise.

A demoted version returns to the paper stage — unless a newer version of the same strategy
is already there, in which case it retires. A stage holds one version of a strategy, and the
newer one is the one paper has already been judging.

A retirement is the last move: the version leaves whatever stages hold it and never returns.
It obeys the same rule about halted stages, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kanso import strategy as strategies
from kanso.errors import ApprovalError, PreconditionError
from kanso.inbox import escalate
from kanso.portfolio import files, records
from kanso.portfolio.capital import assign
from kanso.portfolio.deploy import BLOCKED, LIVE, PAPER, RETIRED, Deployment, deploy, restated
from kanso.schemas import StrategyFile, StrategyVersion
from kanso.schemas.portfolio import STAGES
from kanso.schemas.strategy import PAPER_STATES
from kanso.strategy import files as strategy_files

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from kanso.portfolio.records import Approval
    from kanso.state import StateStore
    from kanso.workspace import Workspace

__all__ = [
    "DEMOTED",
    "PROMOTABLE",
    "Demotion",
    "Promotion",
    "Retirement",
    "demote",
    "promote",
    "retire",
    "set_state",
]

PROMOTABLE = "promotable"
"""The one state a version may be promoted from: paper, with every paper gate passed."""

DEMOTED = "demoted"
"""The escalation a demotion leaves when it could not restart a stage."""


@dataclass(frozen=True)
class Promotion:
    """What a promotion did: the approval it recorded, and the stages it restarted."""

    strategy_id: str
    version: int
    operator: str
    approval: Approval
    retired: int | None
    deployments: tuple[Deployment, ...]

    @property
    def label(self) -> str:
        """How the promoted version is named to an operator."""
        return records.subject_of(self.strategy_id, self.version)


@dataclass(frozen=True)
class Demotion:
    """What a demotion did: where the version went, and which stages could be restarted."""

    strategy_id: str
    version: int
    state: str
    deployments: tuple[Deployment, ...]
    halted: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """How the demoted version is named to an operator."""
        return records.subject_of(self.strategy_id, self.version)


@dataclass(frozen=True)
class Retirement:
    """What a retirement did: which version left, from where, and what was restarted."""

    strategy_id: str
    version: int
    was: str
    stages: tuple[str, ...]
    deployments: tuple[Deployment, ...]
    halted: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """How the retired version is named to an operator."""
        return records.subject_of(self.strategy_id, self.version)


def promote(
    ws: Workspace,
    store: StateStore,
    strategy_id: str,
    version: int | None = None,
    operator: str | None = None,
) -> Promotion:
    """Move a promotable version onto the live stage under a named operator's approval."""
    if not (operator or "").strip():
        raise ApprovalError(
            f"promote: moving {strategy_id} onto the live stage is a named operator act",
            remedy=f"kanso promote {strategy_id} --live --as NAME",
        )
    named = str(operator).strip()
    file, chosen = _version(ws, strategy_id, version, PROMOTABLE)
    portfolio = files.read(ws)
    live = files.stage_of(portfolio, LIVE)
    if live.kill_switch:
        raise PreconditionError(
            f"stages.live.kill_switch is on, so {records.subject_of(strategy_id, chosen.version)} "
            "cannot be promoted into it",
            remedy="clear stages.live.kill_switch in portfolio.yaml, then promote again",
        )
    subject = records.subject_of(strategy_id, chosen.version)
    if assign(live, portfolio.limits, strategy_id) <= 0:
        escalate(ws, store, BLOCKED, subject, f"stages.live has no capital left to fund {subject}")
        raise PreconditionError(
            f"stages.live has no capital left to fund {subject}",
            remedy="raise stages.live.capital, or demote what holds it",
        )

    approval = records.approve(store, strategy_id, chosen.version, named)
    replaced = _move_to_live(ws, store, file, chosen)
    store.event(
        records.PROMOTED,
        subject,
        {"strategy": strategy_id, "version": chosen.version, "operator": named},
    )
    return Promotion(
        strategy_id=strategy_id,
        version=chosen.version,
        operator=named,
        approval=approval,
        retired=replaced,
        deployments=_redeploy(ws, store, (LIVE, PAPER)),
    )


def demote(
    ws: Workspace, store: StateStore, strategy_id: str, version: int | None = None
) -> Demotion:
    """Take a live version off the live stage, then restart the stages that are not halted."""
    file, chosen = _version(ws, strategy_id, version, LIVE)
    state = _demoted_state(file, chosen)
    _move_off_live(ws, store, file, chosen, state)
    subject = records.subject_of(strategy_id, chosen.version)
    store.event(
        records.DEMOTED, subject, {"strategy": strategy_id, "version": chosen.version, "to": state}
    )
    portfolio = files.read(ws)
    halted = tuple(name for name in STAGES if files.stage_of(portfolio, name).kill_switch)
    if halted:
        escalate(
            ws,
            store,
            DEMOTED,
            subject,
            f"{subject} left the live stage; {', '.join(halted)} is halted and was not restarted",
            actions=" · ".join(
                f"clear stages.{name}.kill_switch in portfolio.yaml, then "
                f"kanso portfolio deploy --stage {name}"
                for name in halted
            ),
        )
    return Demotion(
        strategy_id=strategy_id,
        version=chosen.version,
        state=state,
        deployments=_redeploy(ws, store, tuple(n for n in STAGES if n not in halted)),
        halted=halted,
    )


def set_state(
    ws: Workspace, store: StateStore, strategy_id: str, version: int, state: str
) -> StrategyFile:
    """Move one version to a state in the file and in the store, without moving a stage.

    What a version *is* lives in two places that must agree — the strategy file an operator
    reads and the row the portfolio queries — so the pair is written together. The monitor
    marks a version `promotable` this way when its paper gates pass; a change of stage is
    `promote` or `demote`, which move money as well as state.
    """
    file = strategies.require(ws, strategy_id)
    written = restated(file, {version: state})
    strategy_files.write(ws, written)
    store.connection.execute(
        "UPDATE strategy_versions SET state = ? WHERE strategy_id = ? AND version = ?",
        (state, strategy_id, version),
    )
    return written


def retire(
    ws: Workspace, store: StateStore, strategy_id: str, version: int | None = None
) -> Retirement:
    """End one version's life: take it off every stage it is on and mark it retired.

    The default is the strategy's latest version, which is the one an operator naming no
    number means. A version already retired is refused rather than retired again, because
    the second retirement would restart two stages to change nothing.

    Only the stages whose kill switch is off are restarted, for the reason a demotion has:
    a halted stage is halted on purpose, and a retirement that started its node would be
    fighting the switch rather than obeying it.
    """
    file = strategies.require(ws, strategy_id)
    chosen = _retiring(file, strategy_id, version)
    was = chosen.state
    on_stages = tuple(name for name in STAGES if _holds(ws, name, strategy_id, chosen.version))
    strategy_files.write(ws, restated(file, {chosen.version: RETIRED}))
    for stage in on_stages:
        records.clear_stage(store, strategy_id, stage)
        _off_stage(ws, stage, strategy_id)
    records.set_stage(store, strategy_id, chosen.version, state=RETIRED, stage=None)
    subject = records.subject_of(strategy_id, chosen.version)
    store.event(
        RETIRED,
        subject,
        {"strategy": strategy_id, "version": chosen.version, "was": was, "stages": list(on_stages)},
    )
    portfolio = files.read(ws)
    halted = tuple(name for name in STAGES if files.stage_of(portfolio, name).kill_switch)
    return Retirement(
        strategy_id=strategy_id,
        version=chosen.version,
        was=was,
        stages=on_stages,
        deployments=_redeploy(ws, store, tuple(n for n in STAGES if n not in halted)),
        halted=halted,
    )


# --- the moves ----------------------------------------------------------------


def _version(
    ws: Workspace, strategy_id: str, version: int | None, required: str
) -> tuple[StrategyFile, StrategyVersion]:
    """The version this move is about, refusing one that is not in the state it needs."""
    file = strategies.require(ws, strategy_id)
    if version is None:
        found = file.deployed(required)  # type: ignore[arg-type]
        if found is None:
            raise PreconditionError(
                f"{strategy_id} has no {required} version",
                remedy=f"run `kanso strat show {strategy_id}` to see what state its versions "
                "are in",
            )
        return file, found
    if version < 1 or version > len(file.versions):
        raise PreconditionError(
            f"{strategy_id} has no version {version}; it has 1..{len(file.versions)}",
        )
    chosen = file.versions[version - 1]
    if chosen.state != required:
        raise PreconditionError(
            f"{records.subject_of(strategy_id, version)} is {chosen.state}, not {required}",
            remedy=f"only a {required} version makes this move",
        )
    return file, chosen


def _move_to_live(
    ws: Workspace, store: StateStore, file: StrategyFile, chosen: StrategyVersion
) -> int | None:
    """Retire whatever was live, put this version there, and take it off the paper stage."""
    previous = file.deployed(LIVE)
    updates: dict[int, str] = {chosen.version: LIVE}
    if previous is not None:
        updates[previous.version] = RETIRED
        records.set_stage(store, file.id, previous.version, state=RETIRED, stage=None)
    strategy_files.write(ws, restated(file, updates))
    records.clear_stage(store, file.id, LIVE)
    records.set_stage(store, file.id, chosen.version, state=LIVE, stage=None)
    _off_stage(ws, PAPER, file.id)
    return None if previous is None else previous.version


def _move_off_live(
    ws: Workspace, store: StateStore, file: StrategyFile, chosen: StrategyVersion, state: str
) -> None:
    """Take this version off the live stage and give it the state it falls back to."""
    strategy_files.write(ws, restated(file, {chosen.version: state}))
    records.clear_stage(store, file.id, LIVE)
    records.set_stage(store, file.id, chosen.version, state=state, stage=None)
    _off_stage(ws, LIVE, file.id)


def _demoted_state(file: StrategyFile, chosen: StrategyVersion) -> str:
    """Where a demoted version lands: back on paper, or retired if paper is already taken."""
    occupied = any(
        version.state in PAPER_STATES and version.version != chosen.version
        for version in file.versions
    )
    return RETIRED if occupied else PAPER


def _off_stage(ws: Workspace, stage: str, strategy_id: str) -> None:
    """Remove one strategy's entry from a stage of the portfolio file."""
    portfolio = files.read(ws)
    files.write(
        ws,
        files.with_stage(
            portfolio, stage, files.remove(files.stage_of(portfolio, stage), strategy_id)
        ),
    )


def _redeploy(ws: Workspace, store: StateStore, stages: tuple[str, ...]) -> tuple[Deployment, ...]:
    """Restart these stages in order, so the move is reflected in running nodes."""
    return tuple(deploy(ws, store, stage) for stage in stages)


def _retiring(file: StrategyFile, strategy_id: str, version: int | None) -> StrategyVersion:
    """The version a retirement is about: the one named, or the strategy's latest."""
    if version is None:
        chosen = file.latest()
    elif version < 1 or version > len(file.versions):
        raise PreconditionError(
            f"{strategy_id} has no version {version}; it has 1..{len(file.versions)}",
        )
    else:
        chosen = file.versions[version - 1]
    if chosen.state == RETIRED:
        raise PreconditionError(
            f"{records.subject_of(strategy_id, chosen.version)} is already retired",
            remedy=f"run `kanso strat show {strategy_id}` to see what state its versions are in",
        )
    return chosen


def _holds(ws: Workspace, stage: str, strategy_id: str, version: int) -> bool:
    """Whether this stage's file entry is this exact version."""
    held = files.held(files.stage_of(files.read(ws), stage), strategy_id)
    return held is not None and held.version == version
