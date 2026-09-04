"""`portfolio.yaml`: reading it, writing it, and moving a version on or off a stage.

The file is the deployment surface of a workspace and the only place a stage's money, its
clients and its kill switch are written down. It is edited by hand — a stage's capital, its
execution client, the switch itself — and it is rewritten by `deploy`, `promote` and
`demote`, which is why every mutation here returns a new object rather than changing one in
place: a refusal partway through leaves the file exactly as it was.

Editing the file by hand can never move real money. The entries under a stage say what is
deployed, but what makes a deployment legitimate is the approval record and the engine pin
that `deploy` checks before it starts a node, and neither of those is in this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from kanso.errors import PreconditionError, ValidationError
from kanso.schemas import Deployment, Portfolio, Stage, Stages, load_yaml, write_yaml
from kanso.schemas.portfolio import STAGES

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from datetime import datetime
    from pathlib import Path

    from kanso.workspace import Workspace

__all__ = [
    "PORTFOLIO_FILE",
    "STAGES",
    "allocated",
    "halt",
    "held",
    "place",
    "portfolio_file",
    "read",
    "remove",
    "stage_of",
    "unallocated",
    "with_stage",
    "write",
]

PORTFOLIO_FILE: Final = "portfolio.yaml"
"""The workspace file every stage is configured in; `init` writes it."""


def portfolio_file(ws: Workspace) -> Path:
    """Where this workspace's stages are configured."""
    return ws.path(PORTFOLIO_FILE)


def read(ws: Workspace) -> Portfolio:
    """The workspace's portfolio, refusing a workspace that has none."""
    path = portfolio_file(ws)
    if not path.is_file():
        raise PreconditionError(
            f"{path} is missing, so this workspace has no stages to deploy to",
            remedy="run `kanso init` in this directory, or restore portfolio.yaml",
        )
    return load_yaml(Portfolio, path)


def write(ws: Workspace, portfolio: Portfolio) -> Path:
    """Write the portfolio back, validated as a whole before anything is replaced."""
    return write_yaml(portfolio, portfolio_file(ws))


def halt(ws: Workspace, stage: str, *, on: bool = True) -> Portfolio:
    """Set or clear one stage's kill switch and write the file.

    The switch is written by the operator or by the monitor, never by a gate, which is a
    pure evaluator in another process. A running node reads it from the state store on its
    own timer rather than being told, so setting it here halts the stage within one monitor
    interval rather than instantly.
    """
    portfolio = read(ws)
    halted = with_stage(
        portfolio, stage, stage_of(portfolio, stage).model_copy(update={"kill_switch": on})
    )
    write(ws, halted)
    return halted


def stage_of(portfolio: Portfolio, stage: str) -> Stage:
    """One stage of the portfolio, refusing a name that is not a stage."""
    found = getattr(portfolio.stages, stage, None)
    if not isinstance(found, Stage):
        raise ValidationError(
            f"stage: {stage!r} is not a deployment stage",
            remedy=f"one of {', '.join(STAGES)}",
        )
    return found


def with_stage(portfolio: Portfolio, stage: str, replacement: Stage) -> Portfolio:
    """The portfolio with one stage replaced, revalidated in full."""
    stages = {name: stage_of(portfolio, name) for name in STAGES}
    stages[stage] = replacement
    return portfolio.model_copy(update={"stages": Stages(**stages)})


def held(stage: Stage, strategy_id: str) -> Deployment | None:
    """The version of one strategy this stage holds, if it holds one."""
    return next((entry for entry in stage.strategies if entry.id == strategy_id), None)


def allocated(stage: Stage, *, without: str | None = None) -> float:
    """The stage capital already committed, optionally ignoring one strategy's share."""
    return sum(entry.capital for entry in stage.strategies if entry.id != without)


def unallocated(stage: Stage, *, without: str | None = None) -> float:
    """The stage capital nothing has claimed, never below zero."""
    return max(0.0, stage.capital - allocated(stage, without=without))


def place(
    stage: Stage,
    strategy_id: str,
    version: int,
    capital: float,
    joined_at: datetime,
) -> Stage:
    """This stage with one strategy's entry replaced or added, in id order.

    A stage holds at most one version of a strategy, so placing a version replaces the
    entry rather than joining it: the predecessor leaves the stage in the same act.
    """
    entries = [entry for entry in stage.strategies if entry.id != strategy_id]
    entries.append(
        Deployment(id=strategy_id, version=version, capital=capital, joined_at=joined_at)
    )
    entries.sort(key=lambda entry: entry.id)
    return stage.model_copy(update={"strategies": entries})


def remove(stage: Stage, strategy_id: str) -> Stage:
    """This stage with one strategy's entry gone, whichever version it held."""
    return stage.model_copy(
        update={"strategies": [e for e in stage.strategies if e.id != strategy_id]}
    )
