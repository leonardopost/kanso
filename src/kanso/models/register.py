"""The workspace's model register: reading it, and answering which model serves a call.

`models.yaml` is the operator's list of models and the routing table that says which tier,
which thinking effort and which output cap each task class gets. The schema parses it; this
module answers the two questions the router asks of it — which model serves a tier, and
which tier a failed attempt escalates to.

Two rules here are the reason a run cannot half-work.

**Every tier must have a model, checked where a call is about to be made.** The ladder
escalates by one adjacent tier and never skips, so a register with a hole in the middle
would silently turn escalation into no escalation. The check is not done at load, so a
half-configured workspace can still be inspected by `doctor`; it is done at the call,
where the failure is actionable and names the tier.

**A tier's first-listed model is the one that serves it.** The file's order is the
operator's preference and nothing here reorders it, so which model answers a class is
readable from the file rather than derived from cost, context or availability.
"""

from __future__ import annotations

from pathlib import Path

from kanso.errors import PreconditionError, ValidationError
from kanso.schemas.models import TIERS, ModelsFile, ModelSpec, Route, TaskClass, Tier
from kanso.schemas.models import check_tier_coverage as _coverage
from kanso.schemas.yamlio import load_yaml
from kanso.workspace import Workspace

__all__ = [
    "REGISTER_NAME",
    "covered",
    "escalation",
    "first_for_tier",
    "read_register",
    "route_for",
    "script_path",
    "tiers_covered",
]

REGISTER_NAME = "models.yaml"
"""The register's file name inside a workspace."""


def read_register(ws: Workspace) -> ModelsFile:
    """The workspace's register.

    A workspace with no register refuses rather than defaults: no task class has a
    non-model fallback, so an absent file means every step that needs one cannot run, and
    saying so at the file is clearer than saying it at each empty tier.
    """
    path = ws.path(REGISTER_NAME)
    if not path.is_file():
        raise PreconditionError(
            f"no {REGISTER_NAME} at {path}",
            remedy=f"write {REGISTER_NAME}, or run `kanso init` in a fresh directory",
        )
    return load_yaml(ModelsFile, path)


def tiers_covered(ws: Workspace) -> None:
    """Refuse a workspace whose register leaves a tier with no model."""
    covered(read_register(ws))


def covered(register: ModelsFile) -> None:
    """`tiers_covered` for a register already read."""
    try:
        _coverage(register)
    except ValidationError as exc:
        # The register is well-formed; what is missing is a model to call. That is the
        # state of the workspace, not a mistake in the file, so it fails as a precondition.
        raise PreconditionError(
            exc.message,
            remedy=f"add a model for that tier to {REGISTER_NAME}, or list an existing one on it",
        ) from None


def first_for_tier(register: ModelsFile, tier: Tier) -> ModelSpec:
    """The model that serves a tier: the first the file lists on it."""
    serving = register.for_tier(tier)
    if not serving:
        raise PreconditionError(
            f"models: no model serves the {tier} tier",
            remedy=f"add a model for that tier to {REGISTER_NAME}",
        )
    return serving[0]


def escalation(tier: Tier) -> Tier | None:
    """The one tier a failed attempt may escalate to, or `None` at the top."""
    index = TIERS.index(tier)
    return TIERS[index + 1] if index + 1 < len(TIERS) else None


def route_for(register: ModelsFile, task: TaskClass) -> Route:
    """The resolved route for one task class, with every absent field defaulted."""
    return register.routing.route(task)


def script_path(root: Path, spec: ModelSpec) -> Path:
    """Where a `mock` model's script lives: a path relative to the workspace root.

    Required, because a mock with no script has no answers and would fail every call
    with a schema complaint rather than with the configuration mistake it is.
    """
    if spec.script is None:
        raise PreconditionError(
            f"models: {spec.id} speaks the mock protocol but names no script",
            remedy=f"add `script: <path>` to {spec.id} in {REGISTER_NAME}",
        )
    return root / spec.script
